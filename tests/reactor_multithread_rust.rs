// The multi-thread unlock the LocalKey migration bought: two threads in ONE
// process each construct their own reactor, spawn their own suspending
// stackless task, pump their own run_loop, and read their own result --
// impossible before the migration, when the nine "thread-local" statics were
// process-global `static mut` under rustc and the second thread either raced
// or tripped the reactor's owner-thread check (measured at 7 failures per
// 600 runs from parallel tests, and deterministically in the first draft of
// the wake-pump tests, which had to become separate test binaries; this
// binary is the counter-proof that they no longer need to be).

use std::future::Future;
use std::pin::Pin;
use std::sync::mpsc;
use std::task::{Context, Poll};

use srpc::reactor::{reactor_spawn_stackless_task_with_result, Reactor};

// House pattern: no build.rs, so this binary supplies every C symbol the
// linked modules reference. Same tiers as tests/stackless_wake_pump_rust.rs.
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

/// Ready on the second poll; wakes itself during the first.
struct PendingOnce {
    polls: u32,
    value: i64,
}

impl Future for PendingOnce {
    type Output = i64;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<i64> {
        self.polls += 1;
        if self.polls == 1 {
            cx.waker().wake_by_ref();
            Poll::Pending
        } else {
            Poll::Ready(self.value)
        }
    }
}

fn drive_one_reactor(seed: i64) -> (i64, usize) {
    let reactor = Reactor::get_reactor();
    let task = rusty::Task::from_future(async move {
        let v = PendingOnce {
            polls: 0,
            value: seed,
        }
        .await;
        srpc::misc::async_double(v).await
    });
    let (tx, rx) = mpsc::channel::<i64>();
    reactor_spawn_stackless_task_with_result(&reactor, task, move |value| {
        tx.send(value).expect("receiver alive");
    });
    assert!(rx.try_recv().is_err(), "suspended, not completed");
    reactor.run_loop(false, true);
    let got = rx.try_recv().expect("one pump completes the woken task");
    // Return the reactor's identity too, so the test can prove the two
    // threads did not share an instance.
    (got, std::rc::Rc::as_ptr(&reactor) as usize)
}

#[test]
fn two_threads_run_independent_reactors_in_one_process() {
    let here = drive_one_reactor(21);

    let there = std::thread::spawn(|| drive_one_reactor(100))
        .join()
        .expect("second reactor thread");

    assert_eq!(here.0, 42, "this thread's task: 21 -> 42");
    assert_eq!(there.0, 200, "the other thread's task: 100 -> 200");
    assert_ne!(
        here.1, there.1,
        "each thread must have constructed its own reactor instance"
    );

    // And the original thread's reactor still works after the other thread
    // has come and gone -- its state was never shared or torn down remotely.
    let again = drive_one_reactor(3);
    assert_eq!(again.0, 6);
    assert_eq!(again.1, here.1, "same thread, same reactor");
}
