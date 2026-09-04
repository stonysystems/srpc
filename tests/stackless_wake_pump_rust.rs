// The suspension story under rustc, proven at its two layers.
//
// A canonical async task that returns Pending is parked by
// `reactor_spawn_stackless_task_with_result` with a wake binding; the waker
// deposits a ticket the reactor's ingress holds until `Reactor::run_loop`
// drains it and re-polls the task.  In the C++ lane pollworker's loop calls
// run_loop every tick, so this "just works"; under rustc the facade poll
// loop knows nothing of srpc, and the pump arrives via
// `PollThread::add_tick_hook` -- registered by the consumer, which owns both
// crates.  This binary drives run_loop by hand on the test thread, pinning
// the canonical wake machinery itself; the sibling
// tests/stackless_wake_pollthread_rust.rs registers the hook on a real facade
// poll thread.  They are separate test BINARIES on purpose: the reactor is
// process-global under rustc and binds to the thread that first constructs
// it, so the manual-pump test (reactor on the test thread) and the
// poll-thread test (reactor on the poll thread) cannot share a process.
//
// The suspending future wakes DURING its first poll (wake_by_ref before
// returning Pending), which exercises the reactor's pending-wake re-arm path
// rather than the easy wake-after-park ordering.

use std::future::Future;
use std::pin::Pin;
use std::sync::mpsc;
use std::task::{Context, Poll};

use srpc::reactor::{reactor_spawn_stackless_task_with_result, Reactor};

// House pattern: no build.rs, so this binary supplies every C symbol the
// linked modules reference.  Same tiers as tests/rpc_roundtrip_inmemory_rust.rs.
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

#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_reactor_gettid() -> i64 {
    1
}

macro_rules! never_runs {
    ($($name:ident($($arg:ident: $ty:ty),*) $(-> $ret:ty)?;)+) => {$(
        #[allow(unsafe_code)]
        #[unsafe(no_mangle)]
        extern "C" fn $name($(_: $ty),*) $(-> $ret)? {
            unreachable!(concat!(
                stringify!($name),
                " must not run: stackless tasks spawn no fibers and open no sockets"
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

/// Ready on the second poll; wakes itself during the first, exercising the
/// pending-wake re-arm rather than a leisurely wake-after-park.
struct PendingOnce {
    polls: u32,
}

impl Future for PendingOnce {
    type Output = i64;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<i64> {
        self.polls += 1;
        if self.polls == 1 {
            cx.waker().wake_by_ref();
            Poll::Pending
        } else {
            Poll::Ready(7)
        }
    }
}

/// The composed workload both tests spawn: suspend once, then run the
/// canonical `async fn` chain (7 -> 14).
fn suspending_workload() -> rusty::Task<i64> {
    rusty::Task::from_future(async {
        let seven = PendingOnce { polls: 0 }.await;
        srpc::misc::async_double(seven).await
    })
}

#[test]
fn layer1_manual_run_loop_completes_a_suspended_task() {
    let reactor = Reactor::get_reactor();

    let (tx, rx) = mpsc::channel::<i64>();
    reactor_spawn_stackless_task_with_result(&reactor, suspending_workload(), move |value| {
        tx.send(value).expect("receiver alive");
    });

    // Parked, not completed: the spawn's early poll saw Pending.
    assert!(
        rx.try_recv().is_err(),
        "a suspended task must not complete inside the spawn call"
    );

    // One pump is the whole protocol: drain the wake ingress, re-poll.
    reactor.run_loop(false, true);
    assert_eq!(
        rx.try_recv().expect("run_loop must re-poll the woken task"),
        14,
        "PendingOnce yields 7, async_double doubles it"
    );
}
