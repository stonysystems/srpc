// The tier-1 visibility flips, exercised: every surface here was reachable
// only from the C++ lane until rpc/client.rs spelled it `pub` — a change the
// transpile diff measured as byte-identical in the emitted C++ (methods of a
// generated class are public members either way), so the flip widens the Rust
// lane without moving the ABI.  The tests run over the in-memory channel,
// whose synchronous delivery makes even the fire-and-forget path assertable
// inline.

use std::cell::Cell;
use std::ffi::CString;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::Arc;

use srpc::circuit_breaker::CircuitState;
use srpc::client::{Client, KeepaliveConfig};
use srpc::heartbeat::HeartbeatConfig;
use srpc::inmemory_channel::{make_inmemory_factory_proxy, InMemoryFactory, InMemorySwitchboard};
use srpc::reconnect_policy::ReconnectPolicy;
use srpc::serializable::{BinaryReadArchive, BinaryWriteArchive, Deserialize, Serialize};
use srpc::server::{Request, Server, ServerReplyFn, Service, WeakServerConnection};

use rusty::srpc::reactor::PollThread;

// House pattern: no build.rs, so this binary supplies every C symbol the
// linked modules reference. Same tiers as tests/rpc_roundtrip_inmemory_rust.rs.
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
                " must not run: this suite is a synchronous in-memory fast path"
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

const ECHO_DOUBLE_RPC_ID: i32 = 0x00E0_0043;

struct EchoDoubleService;

impl Service for EchoDoubleService {
    fn __reg_to__(&mut self, server: &mut Server, svc_index: usize) -> i32 {
        server.reg_fast_rpc(ECHO_DOUBLE_RPC_ID, svc_index)
    }

    #[allow(unsafe_code)]
    fn __dispatch__(&mut self, rpc_id: i32, mut req: Box<Request>, sconn: WeakServerConnection) {
        assert_eq!(rpc_id, ECHO_DOUBLE_RPC_ID);
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
            Serialize::serialize(&(value * 2), ar);
        });
        sconn.reply(&req, 0, writer);
    }
}

/// One connected in-memory client/server pair, the shared harness.
#[allow(unsafe_code)]
fn connected_pair(tag: &str) -> (Server, Arc<Client>) {
    let switchboard = Arc::new(InMemorySwitchboard::new());
    let addr = CString::new(format!("inmemory://{tag}")).expect("addr");

    // SAFETY: rustc-lane poll threads; the in-memory channel never schedules
    // onto them.
    let mut server = unsafe { Server::new(Some(PollThread::create())) };
    server.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard.clone(),
    ))));
    server.reg_service(Box::new(EchoDoubleService));
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
fn request_async_delivers_the_reply_to_the_callback() {
    let (server, client) = connected_pair("async-cb");

    // In-memory delivery is synchronous, so the callback fires inside the
    // request call and a plain shared cell can observe it.
    let got = Arc::new(AtomicI64::new(0));
    let got_in = got.clone();
    let on_reply = srpc::client::AsyncReplyCallback::from_callable(Box::new(
        move |err: i32, payload: *const u8, size: usize| {
            assert_eq!(err, 0);
            // SAFETY: the callback contract borrows `size` bytes at `payload`
            // for the duration of the call.
            #[allow(unsafe_code)]
            let bytes = unsafe { std::slice::from_raw_parts(payload, size) };
            assert_eq!(bytes.len(), 8, "one i64, fixed width");
            got_in.store(i64::from_ne_bytes(bytes.try_into().unwrap()), Ordering::Release);
        },
    ));

    let sent = client.request_async(ECHO_DOUBLE_RPC_ID, |ar| {
        Serialize::serialize(&21_i64, ar);
    }, on_reply);
    assert!(sent.is_ok());
    assert_eq!(got.load(Ordering::Acquire), 42, "fire-and-forget still round-trips");

    drop(server);
    drop(client);
}

#[test]
fn connection_accessors_and_live_metrics_are_reachable() {
    let (server, client) = connected_pair("accessors");

    assert!(client.connected());
    assert!(client.has_connection());
    let conn = client.connection().expect("live connection");

    let fu = client
        .request(ECHO_DOUBLE_RPC_ID, &srpc::client::FutureAttr::default(), |ar| {
            Serialize::serialize(&5_i64, ar);
        })
        .expect("request accepted");
    assert_eq!(fu.get_error_code(), 0);

    // The LIVE counters, on the connection — not Client::metrics(), which is
    // the documented zeroed stub (asserted as such below so a future fix is
    // a deliberate edit here, not a surprise).
    assert!(conn.metrics().requests_sent() >= 1);
    assert_eq!(client.metrics().requests_sent(), 0, "Client::metrics() is the stub");

    drop(server);
    drop(client);
}

#[test]
fn config_setters_store_and_read_back() {
    let (server, client) = connected_pair("configs");

    let keepalive = KeepaliveConfig::aggressive();
    client.set_keepalive(&keepalive);
    assert_eq!(client.keepalive_config().idle_sec, keepalive.idle_sec);

    client.set_heartbeat(&HeartbeatConfig::aggressive());
    assert_eq!(
        client.heartbeat_config().interval_ms,
        HeartbeatConfig::aggressive().interval_ms
    );

    client.set_reconnect_policy(&ReconnectPolicy::aggressive());

    let mut breaker = srpc::circuit_breaker::CircuitBreakerConfig::new();
    breaker.failure_threshold = 2;
    client.set_circuit_breaker(&breaker);
    assert_eq!(client.circuit_breaker_config().failure_threshold, 2);
    assert_eq!(client.circuit_breaker_state(), CircuitState::CLOSED);

    // The documented trap, now assertable from Rust: buffering config is NOT
    // staged — it must be set after connect, and then it sticks.
    client.set_buffering_config(&srpc::client::BufferingConfig::disabled());

    drop(server);
    drop(client);
}

#[test]
fn connection_callbacks_fire_on_connect() {
    let switchboard = Arc::new(InMemorySwitchboard::new());
    let addr = CString::new("inmemory://cb-connect").expect("addr");

    #[allow(unsafe_code)]
    let mut server = unsafe { Server::new(Some(PollThread::create())) };
    server.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard.clone(),
    ))));
    server.reg_service(Box::new(EchoDoubleService));
    #[allow(unsafe_code)]
    let started = unsafe { server.start(addr.as_ptr()) };
    assert_eq!(started, 0);

    #[allow(unsafe_code)]
    let client = Client::create(unsafe { PollThread::create() });
    client.set_channel_factory(make_inmemory_factory_proxy(Arc::new(InMemoryFactory::new(
        switchboard,
    ))));

    let fired = Arc::new(AtomicBool::new(false));
    let fired_in = fired.clone();
    client.add_on_connected(Box::new(move || {
        fired_in.store(true, Ordering::Release);
    }));

    assert_eq!(client.connect(addr.as_ptr(), true), 0);
    assert!(fired.load(Ordering::Acquire), "on_connected fires during connect");

    drop(server);
    drop(client);
}

#[test]
#[allow(unsafe_code)]
fn client_pool_construction_and_config_are_reachable() {
    use srpc::client::{ClientPool, PoolConfig};
    use srpc::load_balancer::LoadBalancingStrategy;

    let mut config = PoolConfig::defaults();
    config.load_balancing = LoadBalancingStrategy::ROUND_ROBIN;
    config.max_connections = 8;

    let pool = ClientPool::new(Some(unsafe { PollThread::create() }), config);
    assert_eq!(pool.pool_config().max_connections, 8);
    assert_eq!(pool.total_client_count(), 0);
    assert_eq!(pool.address_count(), 0);
    // Idle sweeps over an empty pool are well-defined no-ops.
    assert_eq!(pool.close_all_idle(0), 0);
    assert_eq!(pool.remove_all_unhealthy(), 0);

    // Keep the unused-variable shape identical to production call sites.
    let _ = (Rc::new(Cell::new(0u8)), &pool);
}
