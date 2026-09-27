//! Canonical epoll registration, error policy, and readiness dispatch.
//! Rust and generated C++ use the same syscall adapters in srpc_epoll.c.
//!
//! Two surfaces live here. `Epoll` and the `epoll_*_impl` functions serve the
//! current `PollThread`. `SrpcEpollBackend` is the OS backend for the Lion
//! runtime (docs/dev/lion-runtime-plan.md, S2): it meets the contract of
//! `lion_reactor::os::OsBackend` method for method, so the trait impl that S1
//! adds only forwards.

use std::os::fd::{AsRawFd, FromRawFd};
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::Arc;
use std::time::Duration;

/// C++ consumers use this module as the `PollMode` namespace.
pub mod PollMode {
    pub const READ: i32 = 0x1_i32;
    pub const WRITE: i32 = 0x2_i32;
    pub const NO_CHANGE: i32 = -1_i32;
}

/// C++ consumers use this module as the `PollReady` namespace.
pub mod PollReady {
    pub const READABLE: i32 = 0x1_i32;
    pub const WRITABLE: i32 = 0x2_i32;
    pub const ERROR: i32 = 0x4_i32;
}

/// Abstract interface consumed by the reactor and transport modules.
pub trait Pollable {
    fn fd(&self) -> i32;
    fn poll_mode(&self) -> i32;
    fn content_size(&mut self) -> usize;
    fn handle_read(&mut self) -> bool;
    fn handle_write(&mut self) -> i32;
    fn handle_error(&mut self);
    fn close(&mut self);
    fn check_pending_write_update(&self) -> bool;
    fn is_closed(&self) -> bool;
}

/// Test instrumentation retained from the historical provider.
pub static epoll_remove_count: AtomicI32 = AtomicI32::new(0_i32);

pub fn epoll_bump_remove_count() {
    epoll_remove_count.fetch_add(1_i32, Ordering::SeqCst);
}

// Native epoll_event layout differs between supported architectures. The C
// boundary normalizes the kernel record; scheduling and error handling stay
// in these canonical Rust functions.
#[repr(C)]
#[derive(Default)]
struct EpollWaitEvent {
    events: u32,
    fd: i32,
}

#[allow(unsafe_code)]
unsafe extern "C" {
    fn srpc_epoll_open() -> i32;
    fn srpc_epoll_ctl(poll_fd: i32, operation: i32, fd: i32, flags: u32) -> i32;
    fn srpc_epoll_wait(poll_fd: i32, events: *mut core::ffi::c_void, capacity: i32, timeout_ms: i32) -> i32;
    fn srpc_epoll_create() -> i32;
    fn srpc_epoll_ctl_token(poll_fd: i32, operation: i32, fd: i32, flags: u32, token: u64) -> i32;
    fn srpc_epoll_wait_tokens(poll_fd: i32, tokens: *mut u64, flags: *mut u32, capacity: i32, timeout_ms: i32) -> i32;
    fn srpc_epoll_eventfd_create() -> i32;
    fn srpc_epoll_eventfd_signal(event_fd: i32) -> i32;
    fn srpc_epoll_eventfd_drain(event_fd: i32) -> i32;
}

#[allow(unsafe_code)]
pub fn epoll_open() -> i32 {
    let fd = unsafe { srpc_epoll_open() };
    assert!(fd >= 0);
    fd
}

#[allow(unsafe_code)]
pub fn epoll_add_impl(poll_fd: i32, fd: i32, poll_mode: i32) -> i32 {
    let mut flags = 0x8000_0000_u32 | LINUX_EPOLLIN | LINUX_EPOLLRDHUP;
    if (poll_mode & PollMode::WRITE) != 0 {
        flags |= LINUX_EPOLLOUT;
    }
    let mut result = unsafe { srpc_epoll_ctl(poll_fd, 1_i32, fd, flags) };
    if result == -17_i32 {
        unsafe { srpc_epoll_ctl(poll_fd, 2_i32, fd, 0_u32); }
        result = unsafe { srpc_epoll_ctl(poll_fd, 1_i32, fd, flags) };
    }
    if result == -9_i32 {
        return -1_i32;
    }
    assert!(result == 0);
    0_i32
}

#[allow(unsafe_code)]
pub fn epoll_remove_impl(poll_fd: i32, fd: i32) -> i32 {
    epoll_bump_remove_count();
    unsafe { srpc_epoll_ctl(poll_fd, 2_i32, fd, 0_u32); }
    0_i32
}

#[allow(unsafe_code)]
pub fn epoll_update_impl(poll_fd: i32, fd: i32, new_mode: i32, _old_mode: i32) -> i32 {
    let mut flags = 0x8000_0000_u32 | LINUX_EPOLLRDHUP;
    if (new_mode & PollMode::READ) != 0 {
        flags |= LINUX_EPOLLIN;
    }
    if (new_mode & PollMode::WRITE) != 0 {
        flags |= LINUX_EPOLLOUT;
    }
    let result = unsafe { srpc_epoll_ctl(poll_fd, 3_i32, fd, flags) };
    if result == -2_i32 || result == -9_i32 {
        return 0_i32;
    }
    assert!(result == 0);
    0_i32
}

const LINUX_EPOLLIN: u32 = 0x001_u32;
const LINUX_EPOLLPRI: u32 = 0x002_u32;
const LINUX_EPOLLOUT: u32 = 0x004_u32;
const LINUX_EPOLLERR: u32 = 0x008_u32;
const LINUX_EPOLLHUP: u32 = 0x010_u32;
const LINUX_EPOLLRDHUP: u32 = 0x2000_u32;
const LINUX_EPOLLET: u32 = 0x8000_0000_u32;

/// Run one one-millisecond poll pass and dispatch every meaningful event.
///
/// Keeping the loop counter signed preserves the legacy behavior on a failed
/// `epoll_wait`: a negative result performs zero callbacks.
#[allow(unsafe_code)]
pub fn epoll_wait_impl<F>(poll_fd: i32, mut on_ready: F)
where
    F: FnMut(i32, i32),
{
    let mut events: [EpollWaitEvent; 100] = std::array::from_fn(|_| EpollWaitEvent::default());
    let ready_count =
        unsafe { srpc_epoll_wait(poll_fd, events.as_mut_ptr() as *mut core::ffi::c_void, 100_i32, 1_i32) };
    let mut index: i32 = 0_i32;
    while index < ready_count {
        let event_index = index as usize;
        let kernel_events = events[event_index].events;
        let mut ready_events: i32 = 0_i32;
        if (kernel_events & LINUX_EPOLLIN) != 0_u32 {
            ready_events |= PollReady::READABLE;
        }
        if (kernel_events & LINUX_EPOLLOUT) != 0_u32 {
            ready_events |= PollReady::WRITABLE;
        }
        if (kernel_events & (LINUX_EPOLLERR | LINUX_EPOLLHUP | LINUX_EPOLLRDHUP)) != 0_u32 {
            ready_events |= PollReady::ERROR;
        }
        if ready_events != 0_i32 {
            on_ready(events[event_index].fd, ready_events);
        }
        index += 1_i32;
    }
}

// The checked type map restores the established C++ spelling
// `rusty::os::fd::OwnedFd`; direct Rust uses std's equivalent RAII owner.
type LegacyOwnedFd = std::os::fd::OwnedFd;

/// Move-only RAII owner of the platform poll descriptor.
#[repr(C)]
#[cfg_attr(any(), cpp_no_fieldwise_ctor)]
pub struct Epoll {
    pub poll_fd_: LegacyOwnedFd,
}

impl Epoll {
    /// Allocate the poll descriptor eagerly, as the historical default
    /// constructor did.
    #[allow(clippy::new_without_default, unsafe_code)]
    pub fn new() -> Epoll {
        Epoll {
            // SAFETY: epoll_open returns a fresh owned descriptor or aborts in
            // the platform implementation before returning an invalid value.
            poll_fd_: unsafe { LegacyOwnedFd::from_raw_fd(epoll_open()) },
        }
    }

    pub fn fd(&self) -> i32 {
        self.poll_fd_.as_raw_fd()
    }

    #[allow(unsafe_code)]
    pub fn Add(&mut self, fd: i32, poll_mode: i32) -> i32 {
        epoll_add_impl(self.poll_fd_.as_raw_fd(), fd, poll_mode)
    }

    #[allow(unsafe_code)]
    pub fn Remove(&mut self, fd: i32) -> i32 {
        epoll_remove_impl(self.poll_fd_.as_raw_fd(), fd)
    }

    #[allow(unsafe_code)]
    pub fn Update(&mut self, fd: i32, new_mode: i32, old_mode: i32) -> i32 {
        epoll_update_impl(self.poll_fd_.as_raw_fd(), fd, new_mode, old_mode)
    }

    pub fn Wait<F>(&mut self, on_ready: F)
    where
        F: FnMut(i32, i32),
    {
        epoll_wait_impl(self.poll_fd_.as_raw_fd(), on_ready);
    }
}

// ---------------------------------------------------------------------------
// The OS backend for the Lion runtime (plan item S2).
//
// Lion's reactor keeps a readiness flag per resource and direction and clears
// it only after an operation returned WouldBlock, so it needs edge-triggered
// reports: every registration carries EPOLLET. It registers each fd under a
// usize token and expects that token back, so the kernel stores the token in
// epoll_event.data (srpc_epoll_ctl_token) rather than the fd. The cross-thread
// interrupt is an eventfd registered under the reserved token 0, which the
// reactor never uses; wait consumes it and never reports it.
//
// The native leaves in srpc_epoll.c make one system call each and return
// -errno. Everything that is policy is here: the registration flags, the
// reserved token, the flag mapping, the timeout rounding, and what EINTR and
// EAGAIN mean.

// Errno values as raw numerics, so the generated module stays valid next to
// errno.h (see TCP_ERR_* in rpc/tcp_channel.rs).
const EPOLL_ERR_INTR: i32 = 4_i32;
const EPOLL_ERR_AGAIN: i32 = 11_i32;
const EPOLL_ERR_INVAL: i32 = 22_i32;

// The token the interrupt eventfd is registered under. Lion's contract: "The
// reactor never registers token 0 (a backend may reserve it for its
// interrupt)". Lion's own mio backend reserves the same token.
const EPOLL_INTERRUPT_TOKEN: u64 = 0_u64;

// The native batch holds at most 100 events per wait (srpc_epoll_wait_tokens).
const EPOLL_BATCH_CAPACITY: usize = 100_usize;

/// The directions a registration reports. Mirrors
/// `lion_reactor::types::Interest` field for field.
// Plain data: rustc derives these, the emitter must not (a C++ aggregate
// copies without them, and `derive(Copy)` has no lowering).
#[cfg_attr(not(any()), derive(Clone, Copy, Debug, Default, PartialEq, Eq))]
pub struct SrpcInterest {
    pub readable: bool,
    pub writable: bool,
}

/// One readiness report for a registered fd. Mirrors
/// `lion_reactor::os::OsEvent` field for field, with mio's meaning of each
/// flag (see `epoll_os_event`).
#[cfg_attr(not(any()), derive(Clone, Copy, Debug, Default, PartialEq, Eq))]
pub struct SrpcOsEvent {
    /// The token the fd was registered under.
    pub token: usize,
    pub readable: bool,
    pub writable: bool,
    pub error: bool,
    pub read_closed: bool,
    pub write_closed: bool,
}

/// The epoll flags of a registration: always EPOLLET and EPOLLRDHUP, EPOLLIN
/// when the interest is readable, EPOLLOUT when it is writable. An interest
/// with neither direction is treated as readable, as Lion's contract says.
pub fn epoll_interest_flags(interest: SrpcInterest) -> u32 {
    let mut flags = LINUX_EPOLLET | LINUX_EPOLLRDHUP;
    if interest.readable || !interest.writable {
        flags |= LINUX_EPOLLIN;
    }
    if interest.writable {
        flags |= LINUX_EPOLLOUT;
    }
    flags
}

/// Maps one kernel event word to readiness exactly as mio does on Linux
/// (mio 1.x, sys/unix/selector/epoll.rs): readable is IN or PRI; writable is
/// OUT; error is ERR; read_closed is HUP, or IN together with RDHUP;
/// write_closed is HUP, or OUT together with ERR, or ERR alone (the word is
/// exactly EPOLLERR: a pipe whose reader went away while the writer waits for
/// space).
pub fn epoll_os_event(token: usize, kernel_events: u32) -> SrpcOsEvent {
    let input = (kernel_events & LINUX_EPOLLIN) != 0_u32;
    let output = (kernel_events & LINUX_EPOLLOUT) != 0_u32;
    let error = (kernel_events & LINUX_EPOLLERR) != 0_u32;
    let hang_up = (kernel_events & LINUX_EPOLLHUP) != 0_u32;
    let priority = (kernel_events & LINUX_EPOLLPRI) != 0_u32;
    let read_hang_up = (kernel_events & LINUX_EPOLLRDHUP) != 0_u32;
    SrpcOsEvent {
        token,
        readable: input || priority,
        writable: output,
        error,
        read_closed: hang_up || (input && read_hang_up),
        write_closed: hang_up || (output && error) || kernel_events == LINUX_EPOLLERR,
    }
}

/// The epoll_wait timeout for a Lion wait: -1 (block) for `None`, otherwise
/// whole milliseconds rounded up, clamped to `i32::MAX`. Rounding up is mio's
/// rule: truncating would turn a 0.5 ms timeout into a zero one and make a
/// short timer busy-spin; only `Some(Duration::ZERO)` does not block.
pub fn epoll_timeout_ms(timeout: Option<Duration>) -> i32 {
    match timeout {
        None => -1_i32,
        Some(duration) => {
            let seconds = duration.as_secs();
            // Past this many seconds the result exceeds i32::MAX anyway, and
            // the multiplication below could overflow u64.
            if seconds >= 2_147_484_u64 {
                return i32::MAX;
            }
            let nanos = duration.subsec_nanos() as u64;
            let whole_millis = nanos / 1_000_000_u64;
            let mut millis = seconds * 1_000_u64 + whole_millis;
            if whole_millis * 1_000_000_u64 < nanos {
                millis += 1_u64;
            }
            if millis > i32::MAX as u64 {
                i32::MAX
            } else {
                millis as i32
            }
        }
    }
}

fn epoll_result(result: i32) -> std::io::Result<()> {
    if result == 0_i32 {
        Ok(())
    } else {
        Err(std::io::Error::from_raw_os_error(-result))
    }
}

/// The cross-thread half of the backend: makes the owner's current or next
/// wait return promptly. Mirrors `lion_reactor::os::OsInterrupt`.
pub struct SrpcEpollInterrupt {
    event_fd_: LegacyOwnedFd,
}

impl SrpcEpollInterrupt {
    /// Adds one to the eventfd counter. Callable from any thread at any time.
    /// EINTR is retried and EAGAIN is success: the counter only refuses a
    /// write when it is about to saturate, and then it is already nonzero, so
    /// the eventfd is readable and a wait will see it. Lion's waker panics on
    /// an error return, so neither condition may surface.
    #[allow(unsafe_code)]
    pub fn signal(&self) -> std::io::Result<()> {
        loop {
            let result = unsafe { srpc_epoll_eventfd_signal(self.event_fd_.as_raw_fd()) };
            if result == 0_i32 || result == -EPOLL_ERR_AGAIN {
                return Ok(());
            }
            if result != -EPOLL_ERR_INTR {
                return Err(std::io::Error::from_raw_os_error(-result));
            }
        }
    }

    // Owner thread only, inside wait. One read of a non-semaphore eventfd
    // returns the whole counter and resets it to zero, so it drains every
    // signal made so far; EINTR is retried. A signal that lands after the read
    // leaves the counter nonzero, and because the eventfd is registered
    // edge-triggered its write also queued a fresh event, so the next wait
    // returns promptly: no signal is lost. EAGAIN (the counter was already
    // zero) and any other error end the drain without being reported: the
    // wait has already harvested io events, and an error return would make
    // the reactor treat the park as empty and drop those edges.
    #[allow(unsafe_code)]
    fn drain(&self) {
        loop {
            let result = unsafe { srpc_epoll_eventfd_drain(self.event_fd_.as_raw_fd()) };
            if result != -EPOLL_ERR_INTR {
                return;
            }
        }
    }
}

/// An edge-triggered epoll instance plus its eventfd interrupt: the OS backend
/// of the Lion runtime. Every method but `SrpcEpollInterrupt::signal` runs on
/// the thread that owns the reactor.
pub struct SrpcEpollBackend {
    poll_fd_: LegacyOwnedFd,
    interrupt_: Arc<SrpcEpollInterrupt>,
}

impl SrpcEpollBackend {
    /// Creates the epoll instance and the interrupt eventfd (both close-on-
    /// exec) and registers the eventfd, edge-triggered, under token 0.
    // Spelled `Result<_, std::io::Error>` rather than `std::io::Result<_>`: the
    // same Rust type, but the emitter lowers the alias to rusty::io::Result,
    // whose error constructor default-constructs the value, and this struct
    // holds an Arc, which has no default. The plain Result lowers to the
    // tagged rusty::Result, which does not need one.
    #[allow(unsafe_code)]
    pub fn new() -> Result<SrpcEpollBackend, std::io::Error> {
        let poll_fd = unsafe { srpc_epoll_create() };
        if poll_fd < 0_i32 {
            return Err(std::io::Error::from_raw_os_error(-poll_fd));
        }
        // SAFETY: srpc_epoll_create returned a fresh descriptor this owner
        // closes exactly once.
        let poll_owner = unsafe { LegacyOwnedFd::from_raw_fd(poll_fd) };
        let event_fd = unsafe { srpc_epoll_eventfd_create() };
        if event_fd < 0_i32 {
            return Err(std::io::Error::from_raw_os_error(-event_fd));
        }
        // SAFETY: as above, for the eventfd.
        let event_owner = unsafe { LegacyOwnedFd::from_raw_fd(event_fd) };
        let registered = unsafe {
            srpc_epoll_ctl_token(poll_fd, 1_i32, event_fd, LINUX_EPOLLET | LINUX_EPOLLIN, EPOLL_INTERRUPT_TOKEN)
        };
        if registered != 0_i32 {
            return Err(std::io::Error::from_raw_os_error(-registered));
        }
        Ok(SrpcEpollBackend {
            poll_fd_: poll_owner,
            interrupt_: Arc::new(SrpcEpollInterrupt { event_fd_: event_owner }),
        })
    }

    /// The epoll descriptor, for diagnostics.
    pub fn fd(&self) -> i32 {
        self.poll_fd_.as_raw_fd()
    }

    /// Starts reporting `fd` under `token` (EPOLL_CTL_ADD). Token 0 is the
    /// interrupt's and is refused with EINVAL; an fd registered twice fails
    /// with EEXIST.
    pub fn register(&mut self, fd: i32, token: usize, interest: SrpcInterest) -> std::io::Result<()> {
        self.control(1_i32, fd, token, interest)
    }

    /// Replaces the token and interest of a registered fd (EPOLL_CTL_MOD).
    pub fn reregister(&mut self, fd: i32, token: usize, interest: SrpcInterest) -> std::io::Result<()> {
        self.control(3_i32, fd, token, interest)
    }

    #[allow(unsafe_code)]
    fn control(&mut self, operation: i32, fd: i32, token: usize, interest: SrpcInterest) -> std::io::Result<()> {
        if token as u64 == EPOLL_INTERRUPT_TOKEN {
            return Err(std::io::Error::from_raw_os_error(EPOLL_ERR_INVAL));
        }
        let result = unsafe {
            srpc_epoll_ctl_token(self.poll_fd_.as_raw_fd(), operation, fd, epoll_interest_flags(interest), token as u64)
        };
        epoll_result(result)
    }

    /// Stops all reports for `fd` (EPOLL_CTL_DEL), including events the kernel
    /// has queued but no wait has returned yet.
    #[allow(unsafe_code)]
    pub fn deregister(&mut self, fd: i32) -> std::io::Result<()> {
        let result = unsafe { srpc_epoll_ctl_token(self.poll_fd_.as_raw_fd(), 2_i32, fd, 0_u32, 0_u64) };
        epoll_result(result)
    }

    /// Blocks for at most `timeout` (`None`: no bound) until an event is ready
    /// or the interrupt is signalled, and appends the ready events to
    /// `events`. See `wait_timeout_ms`.
    pub fn wait(&mut self, events: &mut Vec<SrpcOsEvent>, timeout: Option<Duration>) -> std::io::Result<()> {
        self.wait_timeout_ms(events, epoll_timeout_ms(timeout))
    }

    /// `wait` with an epoll_wait timeout: -1 blocks, 0 does not.
    ///
    /// At most `events.capacity() - events.len()` events are appended, and at
    /// most 100 (or 100 when the vector has no spare capacity); the kernel
    /// keeps the rest queued for the next wait. The interrupt is consumed
    /// here and never reported. An event that carries none of the five flags
    /// is dropped: registrations always ask for EPOLLRDHUP, so a write-only
    /// registration can see a bare EPOLLRDHUP that mio, which asks for it only
    /// with readable interest, would never have reported. A wait cut short by
    /// a signal (EINTR) is an empty wait, not an error.
    #[allow(unsafe_code)]
    pub fn wait_timeout_ms(&mut self, events: &mut Vec<SrpcOsEvent>, timeout_ms: i32) -> std::io::Result<()> {
        let mut tokens: [u64; 100] = [0_u64; 100];
        let mut kernel_events: [u32; 100] = [0_u32; 100];
        let room = events.capacity() - events.len();
        let capacity: i32 = if room == 0_usize || room > EPOLL_BATCH_CAPACITY {
            EPOLL_BATCH_CAPACITY as i32
        } else {
            room as i32
        };
        let ready_count = unsafe {
            srpc_epoll_wait_tokens(
                self.poll_fd_.as_raw_fd(),
                tokens.as_mut_ptr(),
                kernel_events.as_mut_ptr(),
                capacity,
                timeout_ms,
            )
        };
        if ready_count == -EPOLL_ERR_INTR {
            return Ok(());
        }
        if ready_count < 0_i32 {
            return Err(std::io::Error::from_raw_os_error(-ready_count));
        }
        let mut interrupted = false;
        let mut index: usize = 0_usize;
        while index < ready_count as usize {
            if tokens[index] == EPOLL_INTERRUPT_TOKEN {
                interrupted = true;
            } else {
                let event = epoll_os_event(tokens[index] as usize, kernel_events[index]);
                if event.readable || event.writable || event.error || event.read_closed || event.write_closed {
                    events.push(event);
                }
            }
            index += 1_usize;
        }
        if interrupted {
            self.interrupt_.drain();
        }
        Ok(())
    }

    /// The backend's interrupt, shared with every thread that may need to
    /// wake the owner's wait.
    pub fn interrupt(&self) -> Arc<SrpcEpollInterrupt> {
        self.interrupt_.clone()
    }
}
