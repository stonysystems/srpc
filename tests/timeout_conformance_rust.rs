// Tier 2.4 of docs/testing-plan.md: the RPC Future's timeout contract.
//
// The bare `Future::wait()` is hard-capped at one second and then latches
// permanently -- "the single most surprising thing about the client" (the
// book, ch.8) and, until now, untested. (future_rust.rs covers the *fiber*
// FiberFuture in reactor/future.rs, a different type.) wait_with_options and
// the retry chain are covered by client_retry_rust.rs; this file pins the
// plain-wait cap and its one-way latch.
//
// The cap test costs one real second by construction (the 1s deadline is a
// private constant on Future), which is negligible next to the battery's
// 600s-timeout suites.

use std::ffi::CString;
use std::sync::Arc;
use std::time::{Duration, Instant};

use srpc::client::{Client, FutureAttr, CLIENT_ERR_TIMED_OUT};
use srpc::inmemory_channel::{make_inmemory_factory_proxy, InMemoryFactory, InMemorySwitchboard};
use srpc::serializable::{BinaryReadArchive, BinaryWriteArchive, Deserialize, Serialize};
use srpc::server::{Request, Server, ServerReplyFn, Service, WeakServerConnection};

use rusty::srpc::reactor::PollThread;

#[allow(unsafe_code)]
#[unsafe(no_mangle)]
pub extern "C" fn srpc_clock_monotonic_us() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_micros() as u64
}
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
pub extern "C" fn srpc_clock_realtime_coarse_us() -> u64 {
    srpc_clock_monotonic_us()
}
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
pub extern "C" fn srpc_random_u64() -> u64 {
    0x9E37_79B9_7F4A_7C15
}
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_sleep_us(microseconds: u64) {
    std::thread::sleep(Duration::from_micros(microseconds));
}
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_rand_raw() -> i32 {
    4
}
#[allow(unsafe_code)]
#[allow(clippy::missing_safety_doc)]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn srpc_cstr_len(text: *const u8) -> usize {
    if text.is_null() {
        return 0;
    }
    let mut n = 0usize;
    // SAFETY: NUL-terminated per the C kernel contract.
    while unsafe { *text.add(n) } != 0 {
        n += 1;
    }
    n
}
#[allow(unsafe_code)]
#[allow(clippy::missing_safety_doc)]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn srpc_path_basename(path: *const i8) -> *const i8 {
    if path.is_null() {
        return core::ptr::null();
    }
    let mut last = path;
    let mut cursor = path;
    // SAFETY: NUL-terminated per the C kernel contract.
    while unsafe { *cursor } != 0 {
        // SAFETY: cursor still inside the string.
        if unsafe { *cursor } == b'/' as i8 {
            // SAFETY: one past a non-NUL byte is still inside.
            last = unsafe { cursor.add(1) };
        }
        // SAFETY: same bound as the loop.
        cursor = unsafe { cursor.add(1) };
    }
    last
}
#[allow(unsafe_code)]
#[allow(clippy::missing_safety_doc)]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn srpc_time_now_str(now: *mut i8) {
    let stamp = b"2026-01-01 00:00:00.000\0";
    // SAFETY: destination sized to 24 bytes by the caller.
    unsafe { core::ptr::copy_nonoverlapping(stamp.as_ptr().cast::<i8>(), now, stamp.len()) };
}
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_stderr() -> *mut rusty::CFile {
    core::ptr::null_mut()
}
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_backtrace_capture(_o: *mut *mut *mut i8) -> i32 {
    -1
}
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_backtrace_free(_s: *mut *mut i8) {}
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_reactor_reusing_fiber() -> i32 {
    0
}

macro_rules! never_runs {
    ($($name:ident($($arg:ident: $ty:ty),*) $(-> $ret:ty)?;)+) => {$(
        #[allow(unsafe_code)]
        #[unsafe(no_mangle)]
        extern "C" fn $name($(_: $ty),*) $(-> $ret)? {
            unreachable!(concat!(stringify!($name), " must not run on the in-memory fast path"))
        }
    )+};
}
never_runs! {
    srpc_fiber_init(f: *mut core::ffi::c_void, s: usize,
        e: unsafe extern "C" fn(*mut core::ffi::c_void), a: *mut core::ffi::c_void);
    srpc_fiber_resume(f: *mut core::ffi::c_void);
    srpc_fiber_yield(f: *mut core::ffi::c_void);
    srpc_fiber_destroy(f: *mut core::ffi::c_void);
    srpc_tcp_connect_socket(a: u32, p: u16, t: i32, e: *mut i32) -> i32;
    srpc_tcp_current_thread_id() -> u32;
    srpc_tcp_last_errno() -> i32;
    srpc_tcp_recv_scratch() -> *mut u8;
    srpc_tcp_recv_bytes(fd: i32, d: *mut u8, n: usize) -> i64;
    srpc_tcp_send_bytes(fd: i32, d: *const u8, n: usize) -> i64;
    srpc_tcp_shutdown(fd: i32) -> i32;
}

const ECHO_RPC_ID: i32 = 0x00E0_0046;
const BLACK_HOLE_RPC_ID: i32 = 0x00E0_0047;

struct TimeoutProbeService;

impl Service for TimeoutProbeService {
    fn __reg_to__(&mut self, server: &mut Server, svc_index: usize) -> i32 {
        let r = server.reg_fast_rpc(ECHO_RPC_ID, svc_index);
        if r != 0 {
            return r;
        }
        server.reg_fast_rpc(BLACK_HOLE_RPC_ID, svc_index)
    }

    #[allow(unsafe_code)]
    fn __dispatch__(&mut self, rpc_id: i32, mut req: Box<Request>, sconn: WeakServerConnection) {
        if rpc_id == BLACK_HOLE_RPC_ID {
            return; // received, never replied -> the caller's Future must time out
        }
        let mut value = 0i64;
        {
            let mut ar = BinaryReadArchive {
                source_: unsafe { srpc::serializable::make_source_proxy_buffer(&raw mut req.src) },
            };
            Deserialize::deserialize(&mut value, &mut ar);
        }
        let sconn = sconn.upgrade().expect("live connection");
        let writer: ServerReplyFn = Box::new(move |ar: &mut BinaryWriteArchive| {
            Serialize::serialize(&(value * 2), ar);
        });
        sconn.reply(&req, 0, writer);
    }
}

#[allow(unsafe_code)]
fn connected_pair(tag: &str) -> (Server, Arc<Client>) {
    let switchboard = Arc::new(InMemorySwitchboard::new());
    let addr = CString::new(format!("inmemory://{tag}")).expect("addr");
    // SAFETY: rustc-lane poll threads are inert; in-memory never schedules on them.
    let mut server = unsafe { Server::new(Some(PollThread::create())) };
    server.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard.clone(),
    ))));
    server.reg_service(Box::new(TimeoutProbeService));
    assert_eq!(unsafe { server.start(addr.as_ptr()) }, 0);
    let client = Client::create(unsafe { PollThread::create() });
    client.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard,
    ))));
    assert_eq!(client.connect(addr.as_ptr(), true), 0);
    (server, client)
}

#[test]
fn plain_wait_caps_at_one_second_and_latches_timed_out() {
    let (server, client) = connected_pair("wait-cap");

    let fu = client
        .request(BLACK_HOLE_RPC_ID, &FutureAttr::default(), |ar| {
            Serialize::serialize(&1_i64, ar);
        })
        .expect("request accepted");

    let start = Instant::now();
    fu.wait();
    let elapsed = start.elapsed();

    // The cap is ~1s: it must not return immediately, and must not hang past
    // a generous ceiling.
    assert!(
        elapsed >= Duration::from_millis(800),
        "wait() must block ~1s on a black-holed request, blocked {elapsed:?}"
    );
    assert!(
        elapsed < Duration::from_secs(5),
        "wait() must not hang past the 1s cap, blocked {elapsed:?}"
    );
    assert_eq!(
        fu.get_error_code(),
        CLIENT_ERR_TIMED_OUT,
        "the cap latches ETIMEDOUT (110)"
    );
    // The one-way latch: a timed-out future never becomes ready, and repeated
    // reads stay ETIMEDOUT (and return immediately, not after another second).
    assert!(!fu.ready(), "a timed-out future stays not-ready");
    let recheck = Instant::now();
    assert_eq!(fu.get_error_code(), CLIENT_ERR_TIMED_OUT, "latch is stable");
    assert!(!fu.ready(), "latch is one-way");
    assert!(
        recheck.elapsed() < Duration::from_millis(200),
        "reads after the latch return immediately"
    );

    drop(server);
    drop(client);
}

#[test]
fn a_replied_request_resolves_well_under_the_cap() {
    let (server, client) = connected_pair("wait-fast");

    let start = Instant::now();
    let fu = client
        .request(ECHO_RPC_ID, &FutureAttr::default(), |ar| {
            Serialize::serialize(&21_i64, ar);
        })
        .expect("request accepted");
    fu.wait();
    let elapsed = start.elapsed();

    assert_eq!(fu.get_error_code(), 0, "a real reply resolves success");
    assert!(fu.ready(), "a resolved future is ready");
    assert!(
        elapsed < Duration::from_millis(500),
        "an in-memory reply resolves far under the 1s cap, took {elapsed:?}"
    );
    let mut doubled = 0i64;
    srpc::client::deserialize_from(fu.get_reply(), &mut doubled);
    assert_eq!(doubled, 42);

    drop(server);
    drop(client);
}
