// The poll-thread half of the rustc suspension story: a real facade
// PollThread, a `add_tick_hook` reactor pump registered the way a consumer
// wires it, and a task that suspends on the poll thread completing with no
// help from the test thread.  The manual-pump half lives in
// tests/stackless_wake_pump_rust.rs; they are separate test BINARIES because
// the reactor is process-global under rustc and binds to whichever thread
// constructs it first -- here that must be the poll thread, there the test
// thread, and one process cannot have both.

use std::future::Future;
use std::pin::Pin;
use std::sync::mpsc;
use std::sync::Arc;
use std::task::{Context, Poll};

use srpc::misc::{Job, OneTimeJob};
use srpc::reactor::{reactor_spawn_stackless_task_with_result, Reactor};

use rusty::srpc::reactor::PollThread;

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
#[allow(unsafe_code)]
fn poll_thread_tick_hook_pumps_a_suspended_task() {
    // SAFETY: creating the facade poll thread has no caller-side contract.
    let poll_thread = unsafe { PollThread::create() };

    // The consumer-side wiring under test: pump the reactor every pass.  The
    // first hook run constructs the poll thread's reactor, so the spawn job
    // below finds the same instance.
    poll_thread.add_tick_hook(|| {
        let reactor = Reactor::get_reactor();
        reactor.run_loop(false, true);
    });

    let (tx, rx) = mpsc::channel::<i64>();
    let spawn_job: Arc<dyn Job> = Arc::new(OneTimeJob::new(Box::new(move || {
        let reactor = Reactor::get_reactor();
        let tx = tx.clone();
        reactor_spawn_stackless_task_with_result(&reactor, suspending_workload(), move |value| {
            tx.send(value).expect("receiver alive");
        });
    })));
    // SAFETY: the job is a well-formed owning handle, dispatched once on the
    // poll thread under the RustcJobRun exclusivity contract.
    unsafe { poll_thread.add(spawn_job) };

    let value = rx
        .recv_timeout(std::time::Duration::from_secs(5))
        .expect("the tick hook must pump the suspended task to completion");
    assert_eq!(value, 14);

    // SAFETY: idempotent shutdown; joins the worker from this foreign thread.
    unsafe { poll_thread.shutdown() };
}
