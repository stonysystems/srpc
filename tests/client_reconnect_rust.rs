// The revival half of the tier-2 reconnect story, in its OWN test binary:
// under one binary's parallel harness the sibling tests' teardown close-jobs
// interleave with this test's server restart on the shared facade poll
// threads, and the flake pointed at real ordering hazards worth isolating
// rather than papering over in-process.  (The switchboard identity-check fix
// in rpc/inmemory_channel.rs came out of exactly this test; see its comment.)

// (Preamble shared by copy with tests/client_retry_rust.rs.)
// The tier-2 payoff: request_with_options RETRIES under rustc, on a real
// spawned coordinator thread.  Until this increment the facade's
// thread::spawn dropped its body, so the whole options/retry family was
// deliberately left un-pub -- flipping it earlier would have shipped an API
// that accepts options and silently never retries.  The enabling changes:
// Future carries the ClientConnection-style notify-before-read Send/Sync
// contract, the callback aliases carry Send bounds, and the facade spawn is
// a real std::thread.
//
// The black-hole service receives requests and never replies, so each
// attempt times out on its per-attempt budget and the coordinator walks the
// whole chain -- observable, bounded (small budgets), and deterministic in
// outcome if not in timing.

use std::ffi::CString;
use std::sync::mpsc;
use std::sync::Arc;

use srpc::client::Client;
use srpc::inmemory_channel::{make_inmemory_factory_proxy, InMemoryFactory, InMemorySwitchboard};
use srpc::serializable::{BinaryReadArchive, BinaryWriteArchive, Deserialize, Serialize};
use srpc::server::{Request, Server, ServerReplyFn, Service, WeakServerConnection};

use rusty::srpc::reactor::PollThread;

// House pattern: no build.rs; this binary supplies every C symbol the linked
// modules reference, with the timing/jitter kernels REAL because the retry
// coordinator genuinely sleeps between attempts.
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
    std::thread::sleep(std::time::Duration::from_micros(microseconds));
}

#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_rand_raw() -> i32 {
    4 // deterministic jitter
}

#[allow(unsafe_code)]
#[allow(clippy::missing_safety_doc)]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn srpc_cstr_len(text: *const u8) -> usize {
    if text.is_null() {
        return 0;
    }
    let mut n = 0usize;
    // SAFETY: the caller passes a NUL-terminated string, as the C kernel requires.
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
    // SAFETY: the caller passes a NUL-terminated path, as the C kernel requires.
    while unsafe { *cursor } != 0 {
        // SAFETY: `cursor` still points inside that same NUL-terminated string.
        if unsafe { *cursor } == b'/' as i8 {
            // SAFETY: one past a non-NUL byte is still inside the string.
            last = unsafe { cursor.add(1) };
        }
        // SAFETY: same bound as the loop condition.
        cursor = unsafe { cursor.add(1) };
    }
    last
}

#[allow(unsafe_code)]
#[allow(clippy::missing_safety_doc)]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn srpc_time_now_str(now: *mut i8) {
    let stamp = b"2026-01-01 00:00:00.000\0";
    // SAFETY: the canonical caller sized the destination to 24 bytes.
    unsafe { core::ptr::copy_nonoverlapping(stamp.as_ptr().cast::<i8>(), now, stamp.len()) };
}

#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_stderr() -> *mut rusty::CFile {
    core::ptr::null_mut()
}

#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_backtrace_capture(_out_symbols: *mut *mut *mut i8) -> i32 {
    -1_i32
}

#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_backtrace_free(_symbols: *mut *mut i8) {}

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
            unreachable!(concat!(
                stringify!($name),
                " must not run: in-memory fast path, no fibers, no sockets"
            ))
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

const ECHO_RPC_ID: i32 = 0x00E0_0044;
const BLACK_HOLE_RPC_ID: i32 = 0x00E0_0045;

struct RetryProbeService;

impl Service for RetryProbeService {
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
            // Receive and never reply: the attempt's future must time out.
            return;
        }
        assert_eq!(rpc_id, ECHO_RPC_ID);
        let mut value = 0i64;
        {
            let mut ar = BinaryReadArchive {
                source_: unsafe {
                    srpc::serializable::make_source_proxy_buffer(&raw mut req.src)
                },
            };
            Deserialize::deserialize(&mut value, &mut ar);
        }
        let sconn = sconn.upgrade().expect("connection alive during inline dispatch");
        let writer: ServerReplyFn = Box::new(move |ar: &mut BinaryWriteArchive| {
            Serialize::serialize(&(value + 1), ar);
        });
        sconn.reply(&req, 0, writer);
    }
}

#[allow(unsafe_code)]
#[allow(dead_code)] // shared-by-copy preamble; the revival test builds its pair inline
fn connected_pair(tag: &str) -> (Server, Arc<Client>) {
    let switchboard = Arc::new(InMemorySwitchboard::new());
    let addr = CString::new(format!("inmemory://{tag}")).expect("addr");

    // SAFETY: rustc-lane poll threads; the in-memory channel never schedules
    // onto them.
    let mut server = unsafe { Server::new(Some(PollThread::create())) };
    server.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard.clone(),
    ))));
    server.reg_service(Box::new(RetryProbeService));
    // SAFETY: `addr` is NUL-terminated and outlives the call.
    assert_eq!(unsafe { server.start(addr.as_ptr()) }, 0);

    let client = Client::create(unsafe { PollThread::create() });
    client.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard,
    ))));
    assert_eq!(client.connect(addr.as_ptr(), true), 0);
    (server, client)
}

#[test]
#[allow(unsafe_code)]
fn explicit_reconnect_recovers_after_the_server_returns() {
    let switchboard = Arc::new(InMemorySwitchboard::new());
    let probe_switchboard = switchboard.clone();
    let addr = CString::new("inmemory://reconnect").expect("addr");

    // SAFETY: rustc-lane poll threads; the in-memory channel never schedules
    // onto them (three create sites in this test).
    let mut server = unsafe { Server::new(Some(PollThread::create())) };
    server.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard.clone(),
    ))));
    server.reg_service(Box::new(RetryProbeService));
    assert_eq!(unsafe { server.start(addr.as_ptr()) }, 0);

    let client = Client::create(unsafe { PollThread::create() });
    client.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard.clone(),
    ))));
    assert_eq!(client.connect(addr.as_ptr(), true), 0);
    // Only the EXPLICIT path under test: without this, the default policy's
    // auto-reconnect thread can race the revived server below.
    client.set_reconnect_policy(&srpc::reconnect_policy::ReconnectPolicy::no_retry());

    // Reconnecting while CONNECTED is refused by contract, and the completion
    // callback still fires with `false`.
    let (tx0, rx0) = mpsc::channel::<bool>();
    let refused_cb = srpc::client::OnReconnectCompleteCallbackFn::from_callable(Box::new(
        move |ok: bool| {
            let _ = tx0.send(ok);
        },
    ));
    assert_eq!(client.reconnect(refused_cb), 22, "EINVAL from CONNECTED");
    assert!(!rx0.recv_timeout(std::time::Duration::from_secs(1)).unwrap());

    // Kill the server. The listener close rides a poll-thread job (the
    // deferred close-job protocol), so under a loaded parallel test run the
    // disconnect lands a tick later -- wait for it, bounded.
    drop(server);
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    while client.connected() {
        assert!(std::time::Instant::now() < deadline, "disconnect must land");
        std::thread::sleep(std::time::Duration::from_millis(5));
    }

    // Bring a server back on the same switchboard address, then reconnect --
    // the completion arrives from the coordinator's real spawned thread.
    let mut revived = unsafe { Server::new(Some(PollThread::create())) };
    revived.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard,
    ))));
    revived.reg_service(Box::new(RetryProbeService));
    let revived_start = unsafe { revived.start(addr.as_ptr()) };
    let addr_string = "inmemory://reconnect".to_string();
    eprintln!(
        "PROBE revived.start={} entry_upgrades={}",
        revived_start,
        probe_switchboard.find_listener(&addr_string).is_some()
    );
    assert_eq!(revived_start, 0);

    // The OLD listener's deferred close-job can still be unregistering the
    // switchboard address when the revived server registers it, so the first
    // dial may land in the gap. Retry the explicit reconnect, bounded.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    let ok = loop {
        let (tx, rx) = mpsc::channel::<bool>();
        let on_complete = srpc::client::OnReconnectCompleteCallbackFn::from_callable(Box::new(
            move |completed: bool| {
                let _ = tx.send(completed);
            },
        ));
        let ret = client.reconnect(on_complete);
        let completed = rx
            .recv_timeout(std::time::Duration::from_secs(5))
            .expect("reconnect completion must arrive");
        if ret == 0 && completed {
            break true;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "reconnect must eventually reach the revived server (last ret={ret}, entry_upgrades={})",
            probe_switchboard.find_listener(&addr_string).is_some()
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    };
    assert!(ok, "reconnect to the revived server succeeds");
    assert!(client.connected());

    drop(revived);
    drop(client);
}
