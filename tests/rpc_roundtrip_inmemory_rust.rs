// The first RPC round trip the Rust lane has ever executed: a real
// `srpc::client::Client` sends a request through the in-memory channel to a
// real `srpc::server::Server`, which dispatches it to a registered service and
// replies, and the client decodes the reply out of its `Future`.
//
// Everything here is synchronous by construction, which is what makes it
// possible under rustc at all: `inmemory_factory_connect` accepts on the
// caller's thread, the in-memory channel delivers each frame inline to the
// peer's `on_frame`, and the RPC is registered with `reg_fast_rpc`, so the
// server dispatches it inline on the delivering thread instead of spawning a
// stackful fiber (fibers bottom out in `reactor/srpc_fiber.c`, which nothing
// links in the Rust lane).  By the time `Client::request` returns, the reply
// has already been decoded and the future is resolved.
//
// The wire format exercised end to end is the real one:
// request `v64 xid | i32 rpc_id | args`, reply
// `v64 xid | v32 error | v64 server_instance_id | payload`
// (`rpc/internal_protocol.rs`), written and read by the same canonical Rust
// the transpiler ships as C++.

use std::ffi::CString;
use std::sync::Arc;

use srpc::client::{deserialize_from, Client, FutureAttr};
use srpc::inmemory_channel::{make_inmemory_factory_proxy, InMemoryFactory, InMemorySwitchboard};
use srpc::serializable::{BinaryReadArchive, BinaryWriteArchive, Deserialize, Serialize};
use srpc::server::{Request, Server, ServerReplyFn, Service, WeakServerConnection};

use rusty::srpc::reactor::PollThread;

// There is no build.rs, so nothing links the plain-C kernels; this binary
// pulls in client, server, tcp_channel, reactor, debugging, utils and rand,
// and must supply every C symbol they reference (house pattern, ~12 test
// files).  Three tiers:
//   - timing/identity stubs that DO run (clocks, random, strlen, log stamps):
//     real or nonzero-constant bodies, per tests/basetypes_rust.rs;
//   - diagnostics that may run on a failure path (stderr, backtrace):
//     the inert bodies tests/debugging_rust.rs uses;
//   - fiber and TCP kernels that must NEVER run on this synchronous in-memory
//     fast path: `unreachable!`, so a routing regression aborts the test
//     instead of silently limping.
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
pub extern "C" fn srpc_clock_monotonic_us() -> u64 {
    1_000_000
}

#[allow(unsafe_code)]
#[unsafe(no_mangle)]
pub extern "C" fn srpc_clock_realtime_coarse_us() -> u64 {
    2_000_000
}

#[allow(unsafe_code)]
#[unsafe(no_mangle)]
pub extern "C" fn srpc_random_u64() -> u64 {
    // Fixed golden-ratio constant: distinct-looking instance ids, no OS RNG.
    0x9E37_79B9_7F4A_7C15
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
    // The logger keeps the first 23 characters; write the historical stamp.
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

// Real spawn instantiates the reconnect/retry bodies, which reach the timing
// and jitter kernels; supply them for real (sleep) and deterministically (rand).
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_sleep_us(microseconds: u64) {
    std::thread::sleep(std::time::Duration::from_micros(microseconds));
}

#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_rand_raw() -> i32 {
    4 // deterministic jitter for tests
}

macro_rules! never_runs {
    ($($name:ident($($arg:ident: $ty:ty),*) $(-> $ret:ty)?;)+) => {$(
        #[allow(unsafe_code)]
        #[unsafe(no_mangle)]
        extern "C" fn $name($(_: $ty),*) $(-> $ret)? {
            unreachable!(concat!(
                stringify!($name),
                " must not run: this test is a synchronous in-memory fast-path round trip"
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

const ECHO_DOUBLE_RPC_ID: i32 = 0x00E0_0042;

// A service with one fast RPC: read an i64, reply with twice its value.
struct EchoDoubleService;

impl Service for EchoDoubleService {
    fn __reg_to__(&mut self, server: &mut Server, svc_index: usize) -> i32 {
        // Fast registration is the point: fast RPCs dispatch inline on the
        // delivering thread, so no fiber runtime is needed.
        server.reg_fast_rpc(ECHO_DOUBLE_RPC_ID, svc_index)
    }

    #[allow(unsafe_code)]
    fn __dispatch__(&mut self, rpc_id: i32, mut req: Box<Request>, sconn: WeakServerConnection) {
        assert_eq!(rpc_id, ECHO_DOUBLE_RPC_ID);

        // The header (xid, rpc_id) has already been consumed; the source
        // cursor now sits on the argument bytes.  Same construction as
        // rpc/server.rs's own header read.
        let mut value = 0i64;
        {
            let mut ar = BinaryReadArchive {
                // SAFETY: `req.src` is owned by the live boxed request.
                source_: unsafe {
                    srpc::serializable::make_source_proxy_buffer(&raw mut req.src)
                },
            };
            Deserialize::deserialize(&mut value, &mut ar);
        }

        let sconn = sconn.upgrade().expect("connection alive during inline dispatch");
        let writer: ServerReplyFn = Box::new(move |ar: &mut BinaryWriteArchive| {
            Serialize::serialize(&(value * 2), ar);
        });
        sconn.reply(&req, 0, writer);
    }
}

#[test]
#[allow(unsafe_code)]
fn client_calls_server_over_the_inmemory_channel_and_reads_the_reply() {
    // One switchboard is the "network"; both sides get a factory over it.
    let switchboard = Arc::new(InMemorySwitchboard::new());
    let addr = CString::new("inmemory://roundtrip").expect("static addr");

    // SAFETY (both create calls): the rustc-lane PollThread is inert state;
    // the in-memory channel never schedules onto it.
    let mut server = unsafe { Server::new(Some(PollThread::create())) };
    server.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard.clone(),
    ))));
    server.reg_service(Box::new(EchoDoubleService));
    // SAFETY: `addr` is NUL-terminated and outlives the call.
    let started = unsafe { server.start(addr.as_ptr()) };
    assert_eq!(started, 0, "Server::start over the in-memory factory");

    let client = Client::create(unsafe { PollThread::create() });
    client.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard,
    ))));
    let connected = client.connect(addr.as_ptr(), true);
    assert_eq!(connected, 0, "Client::connect through the switchboard");

    let fu = client
        .request(ECHO_DOUBLE_RPC_ID, &FutureAttr::default(), |ar| {
            Serialize::serialize(&21_i64, ar);
        })
        .expect("request accepted");

    // The whole path ran inline on this thread, so the reply must already be
    // decoded; wait() is then a no-op rather than a hang.
    assert!(fu.ready(), "in-memory round trip resolves synchronously");
    fu.wait();
    assert_eq!(fu.get_error_code(), 0, "server handler replied success");

    let mut doubled = 0i64;
    deserialize_from(fu.get_reply(), &mut doubled);
    assert_eq!(doubled, 42, "the reply payload crossed both archives intact");

    // Explicit teardown order, kept for determinism.  Historically this was
    // load-bearing: the facade `PollThread::add` used to discard the deferred
    // close job, so a client dropped first left a raw-address `on_closed`
    // callback aimed at a freed `ClientConnection`.  The facade poll thread
    // is real now and runs the close job -- whose captured Arc keeps the
    // connection alive until then -- so either order is safe.
    drop(server);
    drop(client);
}

#[test]
#[allow(unsafe_code)]
fn unknown_rpc_id_comes_back_as_an_error_not_a_hang() {
    let switchboard = Arc::new(InMemorySwitchboard::new());
    let addr = CString::new("inmemory://roundtrip-err").expect("static addr");

    // SAFETY: as above — inert rustc-lane poll threads, NUL-terminated addr.
    let mut server = unsafe { Server::new(Some(PollThread::create())) };
    server.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard.clone(),
    ))));
    server.reg_service(Box::new(EchoDoubleService));
    let started = unsafe { server.start(addr.as_ptr()) };
    assert_eq!(started, 0);

    let client = Client::create(unsafe { PollThread::create() });
    client.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard,
    ))));
    assert_eq!(client.connect(addr.as_ptr(), true), 0);

    let fu = client
        .request(ECHO_DOUBLE_RPC_ID + 1, &FutureAttr::default(), |ar| {
            Serialize::serialize(&1_i64, ar);
        })
        .expect("request accepted");

    assert!(fu.ready());
    // Exactly SERVER_ERR_NO_ENTRY, not merely nonzero: a decode-order
    // regression that swapped the error-code and instance-id reads would
    // still be nonzero, but not 2.
    assert_eq!(
        fu.get_error_code(),
        srpc::server::SERVER_ERR_NO_ENTRY,
        "an unregistered rpc_id must surface as SERVER_ERR_NO_ENTRY on the future"
    );

    // Same explicit teardown order as the happy-path test above.
    drop(server);
    drop(client);
}
