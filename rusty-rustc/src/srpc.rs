// The facade's `srpc` foreign-module surface.
//
// This is the block canonical Rust reaches as `cpp::srpc::*` (`use rusty as
// cpp`), modelling the C++ named modules across the foreign boundary. It lived
// inline in lib.rs until lib.rs passed 2,900 lines and the surface stopped
// being findable inside it.
//
// Moving it here was gated on scripts/check_facade_shadow.py learning to follow
// file-based modules: it previously parsed only the inline `pub mod srpc { .. }`
// form and would have passed VACUOUSLY over this file, silently policing
// nothing. See resolve_module() there.

pub mod debugging {
    #[allow(unsafe_code)]
    pub unsafe fn verify(value: bool) {
        assert!(value);
    }
}

pub mod rand {
    pub struct RandomGenerator;

    // A REAL rustc-lane draw. This used to `return min`, which was not a
    // harmless stub: `client_rand` feeds three ClientPool selection sites in
    // rpc/client.rs, so the Rust lane always selected index 0 and any
    // distribution test over a pool proved nothing.
    //
    // It cannot simply forward to canonical `crate::rand::RandomGenerator`
    // -- misc/rand.rs carries `cpp_abi` markers, so a sibling reference
    // aborts the whole-crate transpile (see ALLOWED_SHADOWS). So the facade
    // owns a small self-contained generator instead: no FFI, hence no
    // `srpc_rand_raw` stub needed in tests. Semantics mirror canonical
    // `misc/rand.rs::rand` -- an inclusive draw over [min, max].
    ::std::thread_local! {
        static RAND_STATE: ::std::cell::Cell<u64> =
            const { ::std::cell::Cell::new(0x9E37_79B9_7F4A_7C15) };
    }

    fn next_u32() -> u32 {
        RAND_STATE.with(|slot| {
            // xorshift64
            let mut x = slot.get();
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            slot.set(x);
            (x >> 32) as u32
        })
    }

    impl RandomGenerator {
        /// # Safety
        ///
        /// Records the foreign named-module boundary; the production
        /// generator is a pure integer draw with no preconditions.
        #[allow(unsafe_code)]
        pub unsafe fn rand(min: i32, max: i32) -> i32 {
            assert!(max >= min);
            let width = (max as i64) - (min as i64) + 1;
            (((next_u32() as i64) % width) + (min as i64)) as i32
        }
    }
}

/// Compile-time-only namespace model used to retain the private
/// `srpc.errors` named-module import in canonical callback generation.
pub mod errors {}

/// Compile-time-only namespace model used to retain the exact
/// `srpc.internal_protocol` named-module import in canonical server
/// generation. The wire constant itself is read through the crate path;
/// only the provider edge is carried here.
pub mod internal_protocol {}
/// `srpc.callback_wrapper` named-module import in canonical client
/// generation; the wrapper template itself is reached through the
/// `rusty::CallbackWrapper` facade type.
pub mod callback_wrapper {}

pub mod reactor {
    use crate::{ReactorBoxEvent, ReactorFiber, REACTOR_CURRENT_FIBER, REACTOR_SLEEP_CALLS};
    use ::std::cell::Cell;
    use ::std::rc::Rc;
    use ::std::sync::{Arc, Mutex};

    pub type Fiber = ReactorFiber;

    /// Rustc-only opaque model of the cross-thread poll command sender.
    /// A REAL rustc-lane poll thread: one epoll instance driven by one
    /// spawned thread, mirroring `reactor/reactor.rs`'s
    /// `pollworker_poll_loop` and the platform flags in
    /// `reactor/epoll_platform_linux.cc` (edge-triggered:
    /// `EPOLLET|EPOLLIN|EPOLLRDHUP`, plus `EPOLLOUT` when write mode is
    /// requested; `EEXIST` retried as del+re-add; `EBADF` drops the
    /// pollable; `ENOENT`/`EBADF` tolerated on MOD; 100-event, 1 ms wait
    /// passes).  Registered pollables are reached through the
    /// [`crate::RustcPollable`] bound, which srpc implements for
    /// `dyn PollableBase`.
    ///
    /// Deliberate departure from the C++ worker, rustc-lane-only: the
    /// loop does not couple to a `Reactor` (`run_loop` drives timers,
    /// fibers and stackless tasks, none of which exist under rustc), and
    /// queued jobs run inline on the poll thread instead of on a fresh
    /// fiber.
    pub struct PollThread {
        inner: ::std::sync::Arc<PollInner>,
    }

    enum PollCmd {
        Add(Box<dyn crate::RustcPollable>),
        UpdateMode(i32, i32),
        Job(Box<dyn QueuedJob>),
        TickHook(Box<dyn FnMut() + Send>),
        Shutdown,
    }

    /// Facade-internal erasure of a queued [`crate::RustcJobRun`] handle.
    trait QueuedJob: Send {
        fn ready(&self) -> bool;
        fn work(&self);
    }

    struct JobHolder<J: crate::RustcJobRun + ?Sized>(Arc<J>);

    impl<J: crate::RustcJobRun + ?Sized> QueuedJob for JobHolder<J> {
        fn ready(&self) -> bool {
            // SAFETY: only the poll thread calls this; the RustcJobRun
            // contract gives it exclusive mutable dispatch.
            #[allow(unsafe_code)]
            unsafe {
                self.0.rustc_job_ready()
            }
        }
        fn work(&self) {
            // SAFETY: as above.
            #[allow(unsafe_code)]
            unsafe {
                self.0.rustc_job_work()
            }
        }
    }

    struct PollInner {
        commands: Mutex<Vec<PollCmd>>,
        join: Mutex<Option<::std::thread::JoinHandle<()>>>,
    }

    mod poll_ffi {
        // Direct glibc entry points, exactly as
        // reactor/epoll_wrapper.rs declares `epoll_wait` for itself; no
        // crate dependency is added.  The event struct is the same
        // 12-byte packed x86-64 shape that file documents.
        #[repr(C, packed)]
        #[derive(Clone, Copy, Default)]
        pub(super) struct EpollEvent {
            pub events: u32,
            pub fd: i32,
            pub padding: u32,
        }

        #[allow(unsafe_code)]
        unsafe extern "C" {
            pub(super) fn epoll_create1(flags: i32) -> i32;
            pub(super) fn epoll_ctl(
                epfd: i32,
                op: i32,
                fd: i32,
                event: *mut EpollEvent,
            ) -> i32;
            pub(super) fn epoll_wait(
                epfd: i32,
                events: *mut EpollEvent,
                max_events: i32,
                timeout_ms: i32,
            ) -> i32;
            pub(super) fn close(fd: i32) -> i32;
        }

        pub(super) const CTL_ADD: i32 = 1;
        pub(super) const CTL_DEL: i32 = 2;
        pub(super) const CTL_MOD: i32 = 3;
        pub(super) const IN: u32 = 0x001;
        pub(super) const OUT: u32 = 0x004;
        pub(super) const ERR: u32 = 0x008;
        pub(super) const HUP: u32 = 0x010;
        pub(super) const RDHUP: u32 = 0x2000;
        pub(super) const ET: u32 = 1 << 31;

        pub(super) const MODE_READ: i32 = 0x1;
        pub(super) const MODE_WRITE: i32 = 0x2;
        pub(super) const MODE_NO_CHANGE: i32 = -1;
        pub(super) const READY_READABLE: i32 = 0x1;
        pub(super) const READY_WRITABLE: i32 = 0x2;
        pub(super) const READY_ERROR: i32 = 0x4;

        pub(super) fn event_bits(mode: i32) -> u32 {
            let mut bits = ET | RDHUP;
            if (mode & MODE_READ) != 0 {
                bits |= IN;
            }
            if (mode & MODE_WRITE) != 0 {
                bits |= OUT;
            }
            bits
        }
    }

    struct PollWorker {
        epoll_fd: i32,
        pollables: ::std::collections::HashMap<i32, Box<dyn crate::RustcPollable>>,
        modes: ::std::collections::HashMap<i32, i32>,
        jobs: Vec<Box<dyn QueuedJob>>,
        tick_hooks: Vec<Box<dyn FnMut() + Send>>,
    }

    impl PollWorker {
        #[allow(unsafe_code)]
        fn ctl(&self, op: i32, fd: i32, mode: i32) -> i32 {
            let mut ev = poll_ffi::EpollEvent {
                events: poll_ffi::event_bits(mode),
                fd,
                padding: 0,
            };
            // SAFETY: `ev` is a live, correctly-shaped epoll_event and
            // `epoll_fd` is the descriptor this worker owns.
            unsafe { poll_ffi::epoll_ctl(self.epoll_fd, op, fd, &mut ev) }
        }

        fn do_add(&mut self, poll: Box<dyn crate::RustcPollable>) {
            let fd = poll.rustc_fd();
            let mode = poll.rustc_poll_mode();
            // Teardown race tolerance, per pollworker_do_add_pollable: a
            // pollable that closed before registration reports fd -1 and
            // can never produce events.
            if fd < 0 || self.pollables.contains_key(&fd) {
                return;
            }
            let mut rc = self.ctl(poll_ffi::CTL_ADD, fd, mode);
            if rc != 0 {
                // The platform impl retries EEXIST as del+re-add; errno is
                // not readable without libc, so retry unconditionally --
                // a second failure drops the pollable either way, which is
                // also the EBADF outcome the platform impl encodes.
                self.ctl(poll_ffi::CTL_DEL, fd, mode);
                rc = self.ctl(poll_ffi::CTL_ADD, fd, mode);
            }
            if rc != 0 {
                return;
            }
            self.pollables.insert(fd, poll);
            self.modes.insert(fd, mode);
        }

        fn do_update_mode(&mut self, fd: i32, new_mode: i32) {
            if !self.pollables.contains_key(&fd) {
                return;
            }
            let old = match self.modes.get(&fd) {
                Some(m) => *m,
                None => return,
            };
            self.modes.insert(fd, new_mode);
            if new_mode != old {
                // ENOENT/EBADF are tolerated by the platform impl; the
                // return value is deliberately ignored to match.
                self.ctl(poll_ffi::CTL_MOD, fd, new_mode);
            }
        }

        fn remove(&mut self, fd: i32) {
            if self.modes.remove(&fd).is_some() {
                self.ctl(poll_ffi::CTL_DEL, fd, 0);
            }
            if let Some(mut p) = self.pollables.remove(&fd) {
                p.rustc_close();
            }
        }

        #[allow(unsafe_code)]
        fn wait_and_dispatch(&mut self) {
            let mut events = [poll_ffi::EpollEvent::default(); 100];
            // SAFETY: the buffer is live for the call and correctly sized.
            let n = unsafe {
                poll_ffi::epoll_wait(self.epoll_fd, events.as_mut_ptr(), 100, 1)
            };
            let mut index = 0i32;
            // Signed loop preserves the reference behavior: a failed wait
            // performs zero callbacks.
            while index < n {
                let ev = events[index as usize];
                let fd = ev.fd;
                let kernel = ev.events;
                let mut ready = 0i32;
                if (kernel & poll_ffi::IN) != 0 {
                    ready |= poll_ffi::READY_READABLE;
                }
                if (kernel & poll_ffi::OUT) != 0 {
                    ready |= poll_ffi::READY_WRITABLE;
                }
                if (kernel & (poll_ffi::ERR | poll_ffi::HUP | poll_ffi::RDHUP)) != 0 {
                    ready |= poll_ffi::READY_ERROR;
                }
                let mut write_mode: Option<i32> = None;
                if let Some(p) = self.pollables.get_mut(&fd) {
                    if (ready & poll_ffi::READY_READABLE) != 0 {
                        p.rustc_handle_read();
                    }
                    if (ready & poll_ffi::READY_WRITABLE) != 0 {
                        let new_mode = p.rustc_handle_write();
                        if new_mode != poll_ffi::MODE_NO_CHANGE {
                            write_mode = Some(new_mode);
                        }
                    }
                }
                if let Some(m) = write_mode {
                    self.do_update_mode(fd, m);
                }
                if (ready & poll_ffi::READY_ERROR) != 0 {
                    if let Some(p) = self.pollables.get_mut(&fd) {
                        p.rustc_handle_error();
                    }
                }
                index += 1;
            }
        }

        fn sweep(&mut self) {
            let fds: Vec<i32> = self.pollables.keys().copied().collect();
            // end_reply() on a fiberless fast path sets the pending-write
            // flag from the dispatch; pick it up exactly as the reference
            // loop does.
            for fd in &fds {
                let wants = match self.pollables.get(fd) {
                    Some(p) => p.rustc_check_pending_write_update(),
                    None => false,
                };
                if wants {
                    self.do_update_mode(*fd, poll_ffi::MODE_READ | poll_ffi::MODE_WRITE);
                }
            }
            // Remove pollables closed by handle_error/handle_read so a
            // reused fd number cannot alias a dead connection.
            for fd in &fds {
                let dead = match self.pollables.get(fd) {
                    Some(p) => p.rustc_is_closed(),
                    None => false,
                };
                if dead {
                    self.remove(*fd);
                }
            }
        }
    }

    fn poll_loop(inner: ::std::sync::Arc<PollInner>, epoll_fd: i32) {
        let mut w = PollWorker {
            epoll_fd,
            pollables: ::std::collections::HashMap::new(),
            modes: ::std::collections::HashMap::new(),
            jobs: Vec::new(),
            tick_hooks: Vec::new(),
        };
        // Worker-owned command batch, swapped with the shared queue each
        // pass: `mem::take` here would hand producers a fresh capacity-0
        // Vec every tick, making every add_proxy/update_mode push
        // re-allocate -- measured as a per-request malloc caller under
        // TCP load. Swapping ping-pongs two buffers whose capacities
        // stabilize at the high-water mark.
        let mut drained: Vec<PollCmd> = Vec::new();
        loop {
            drained.clear();
            {
                let mut queue = inner.commands.lock().unwrap();
                ::std::mem::swap(&mut *queue, &mut drained);
            }
            let mut stop = false;
            for cmd in drained.drain(..) {
                match cmd {
                    PollCmd::Add(p) => w.do_add(p),
                    PollCmd::UpdateMode(fd, mode) => w.do_update_mode(fd, mode),
                    PollCmd::Job(j) => w.jobs.push(j),
                    PollCmd::TickHook(h) => w.tick_hooks.push(h),
                    PollCmd::Shutdown => stop = true,
                }
            }
            // Ready jobs run before shutdown is honored, so a close job
            // queued ahead of Shutdown in the same batch still executes --
            // trigger-then-check, exactly as pollworker_trigger_job does.
            let queued = ::std::mem::take(&mut w.jobs);
            for job in queued {
                if job.ready() {
                    job.work();
                } else {
                    w.jobs.push(job);
                }
            }
            // Tick hooks run every pass, mirroring what pollworker's C++
            // loop does by calling Reactor::run_loop each tick.  The
            // facade cannot name srpc, so the consumer that owns both
            // crates registers the reactor pump here (see
            // PollThread::add_tick_hook); worst-case wake latency is the
            // same 1 ms epoll tick the C++ lane has.
            for hook in w.tick_hooks.iter_mut() {
                hook();
            }
            if stop {
                break;
            }
            w.wait_and_dispatch();
            w.sweep();
        }
        let fds: Vec<i32> = w.pollables.keys().copied().collect();
        for fd in fds {
            w.remove(fd);
        }
        // SAFETY: the worker owns this descriptor; nothing uses it after
        // the loop exits.
        #[allow(unsafe_code)]
        unsafe {
            poll_ffi::close(epoll_fd)
        };
    }

    impl PollThread {
        /// # Safety
        ///
        /// `poll` must be a well-formed owning pollable proxy. It is moved
        /// into the worker command queue, exactly as the production
        /// method's contract states.
        #[allow(unsafe_code)]
        pub unsafe fn add_proxy<P: crate::RustcPollable + 'static>(&self, poll: P) {
            self.inner
                .commands
                .lock()
                .unwrap()
                .push(PollCmd::Add(Box::new(poll)));
        }

        /// # Safety
        ///
        /// `fd` must identify a pollable registered with this thread.
        #[allow(unsafe_code)]
        pub unsafe fn update_mode(&self, fd: i32, new_mode: i32) {
            self.inner
                .commands
                .lock()
                .unwrap()
                .push(PollCmd::UpdateMode(fd, new_mode));
        }

        /// # Safety
        ///
        /// No caller-side precondition; `unsafe` records the foreign
        /// named-module boundary.
        #[allow(unsafe_code)]
        pub unsafe fn create() -> Arc<PollThread> {
            // SAFETY: epoll_create1(0) either yields an owned descriptor
            // or -1; the panic below is the rustc-lane spelling of the
            // platform impl's `verify(fd != -1)`.
            let epoll_fd = unsafe { poll_ffi::epoll_create1(0) };
            assert!(epoll_fd != -1, "epoll_create1 failed");
            let inner = ::std::sync::Arc::new(PollInner {
                commands: Mutex::new(Vec::new()),
                join: Mutex::new(None),
            });
            let loop_inner = inner.clone();
            let handle = ::std::thread::Builder::new()
                .name("srpc-pollthread".to_string())
                .spawn(move || poll_loop(loop_inner, epoll_fd))
                .expect("spawn poll thread");
            *inner.join.lock().unwrap() = Some(handle);
            Arc::new(PollThread { inner })
        }

        /// # Safety
        ///
        /// `job` must be a well-formed owning job handle; it is moved into
        /// the worker command queue and dispatched under the
        /// [`crate::RustcJobRun`] exclusivity contract.  The client's
        /// deferred connection close rides this: the job's captured Arc
        /// keeps the `ClientConnection` alive until the poll thread runs
        /// the close, which is what serializes teardown against event
        /// dispatch -- the same protocol the C++ worker uses.
        #[allow(unsafe_code)]
        pub unsafe fn add<J: crate::RustcJobRun + ?Sized + 'static>(&self, job: Arc<J>) {
            self.inner
                .commands
                .lock()
                .unwrap()
                .push(PollCmd::Job(Box::new(JobHolder(job))));
        }

        /// Register a closure the worker runs once per loop pass, on the
        /// poll thread.  Rustc-lane only (the C++ PollThread has no such
        /// method because pollworker's own loop already pumps the
        /// reactor): this is how a consumer that owns both crates gives
        /// the facade loop the `Reactor::run_loop` pump the C++ lane gets
        /// for free, closing the "async tasks must be ready on first
        /// poll" boundary.  Hooks are never removed; register once per
        /// poll thread.
        pub fn add_tick_hook<F: FnMut() + Send + 'static>(&self, hook: F) {
            self.inner
                .commands
                .lock()
                .unwrap()
                .push(PollCmd::TickHook(Box::new(hook)));
        }

        /// # Safety
        ///
        /// No caller-side precondition; `unsafe` records the foreign
        /// named-module boundary. Idempotent; joins the worker unless
        /// called from it.
        #[allow(unsafe_code)]
        pub unsafe fn shutdown(&self) {
            self.inner.commands.lock().unwrap().push(PollCmd::Shutdown);
            let handle = self.inner.join.lock().unwrap().take();
            if let Some(h) = handle {
                if h.thread().id() != ::std::thread::current().id() {
                    let _ = h.join();
                }
            }
        }
    }

    impl Drop for PollThread {
        fn drop(&mut self) {
            // SAFETY: same contract as shutdown(); Drop runs at the last
            // strong reference, so no registration can race it.
            #[allow(unsafe_code)]
            unsafe {
                self.shutdown()
            };
        }
    }


    /// Rust-only model of the reactor's fiber-aware integer event.
    pub struct IntEvent {
        value: Mutex<i32>,
        target: i32,
        ready: ::std::sync::Condvar,
    }

    impl IntEvent {
        pub fn set(&self, next: i32) -> i32 {
            let mut value = self.value.lock().unwrap();
            let previous = *value;
            *value = next;
            self.ready.notify_all();
            previous
        }

        pub fn wait(&self) {
            let mut value = self.value.lock().unwrap();
            while *value < self.target {
                value = self.ready.wait(value).unwrap();
            }
        }
    }

    /// # Safety
    ///
    /// Every integer target is valid; `unsafe` records the foreign module
    /// boundary used by canonical FiberChannel code.
    #[allow(unsafe_code)]
    pub unsafe fn create_sp_int_event(target: i32) -> Arc<IntEvent> {
        Arc::new(IntEvent {
            value: Mutex::new(0),
            target,
            ready: ::std::sync::Condvar::new(),
        })
    }

    /// # Safety
    ///
    /// This facade has no caller-side precondition. `unsafe` records the
    /// foreign named-module boundary at canonical Rust call sites.
    #[allow(unsafe_code)]
    pub unsafe fn create_sp_box_event<T>() -> Arc<ReactorBoxEvent<T>> {
        Arc::new(ReactorBoxEvent::new())
    }

    /// # Safety
    ///
    /// Every microsecond duration is accepted by the production reactor.
    #[allow(unsafe_code)]
    pub unsafe fn fiber_sleep(microseconds: u64) {
        REACTOR_SLEEP_CALLS.with(|calls| calls.borrow_mut().push(microseconds));
    }

    /// Install a test fiber for the duration of `body`.
    pub fn with_test_fiber<R>(id: u64, body: impl FnOnce() -> R) -> (R, u64) {
        let fiber = Rc::new(ReactorFiber {
            id: Cell::new(id),
            yields: Cell::new(0),
        });
        let previous = REACTOR_CURRENT_FIBER.with(|slot| slot.replace(Some(Rc::clone(&fiber))));
        let result = body();
        REACTOR_CURRENT_FIBER.with(|slot| {
            slot.replace(previous);
        });
        (result, fiber.yields.get())
    }

    /// Return and clear durations recorded by the rustc-only sleep model.
    pub fn take_test_sleep_calls() -> Vec<u64> {
        REACTOR_SLEEP_CALLS.with(|calls| core::mem::take(&mut *calls.borrow_mut()))
    }
}

pub mod serializable {
    pub use crate::{BinaryReadArchive, BinaryWriteArchive};

    fn encoded_length_size(value: usize) -> usize {
        if value <= 63 {
            1
        } else if value <= 8_191 {
            2
        } else if value <= 1_048_575 {
            3
        } else if value <= 134_217_727 {
            4
        } else if value <= 17_179_869_183 {
            5
        } else if value <= 2_199_023_255_551 {
            6
        } else if value <= 281_474_976_710_655 {
            7
        } else if value <= 36_028_797_018_963_967 {
            8
        } else {
            9
        }
    }

    fn sparse_size(byte0: u8) -> usize {
        if byte0 & 0x80 == 0 {
            1
        } else if byte0 & 0xc0 == 0x80 {
            2
        } else if byte0 & 0xe0 == 0xc0 {
            3
        } else if byte0 & 0xf0 == 0xe0 {
            4
        } else if byte0 & 0xf8 == 0xf0 {
            5
        } else if byte0 & 0xfc == 0xf8 {
            6
        } else if byte0 & 0xfe == 0xfc {
            7
        } else if byte0 == 0xfe {
            8
        } else {
            9
        }
    }

    #[allow(non_camel_case_types)]
    pub struct Serialize_;

    impl Serialize_ {
        /// The rustc-executable spelling of the C++ `Serialize_::serialize`
        /// overload set.  The cpp-module-index row makes calls here emit
        /// the QUALIFIED `::srpc::Serialize_::serialize(value, archive)` --
        /// resolving the generated module's concrete leaf overloads before
        /// its generic template, exactly as the retired wire sites did --
        /// while under rustc the `RustcAdlSerialize` bound dispatches for
        /// real: srpc's blanket impl covers its canonical archive over
        /// `T: Serialize`, and the impls below cover this facade's own
        /// archive for the `String` payloads AnyMessage writes.
        /// # Safety
        ///
        /// Foreign named-module boundary; both borrows are held only for
        /// the duration of the call.
        #[allow(unsafe_code)]
        pub unsafe fn serialize<T: ?Sized, Archive: crate::RustcAdlSerialize<T> + ?Sized>(
            value: &T,
            archive: &mut Archive,
        ) {
            // SAFETY: forwarded unchanged to the archive's bound impl.
            unsafe { archive.rustc_adl_serialize(value) }
        }
    }

    #[allow(clippy::ptr_arg)]
    #[allow(unsafe_code)]
    impl crate::RustcAdlSerialize<String> for BinaryWriteArchive {
        unsafe fn rustc_adl_serialize(&mut self, value: &String) {
            let size = encoded_length_size(value.len());
            let bits = value.len() as u64;
            let mut encoded = [0u8; 9];
            if size <= 7 {
                for (index, byte) in encoded[..size].iter_mut().enumerate() {
                    *byte = (bits >> (8 * (size - 1 - index))) as u8;
                }
                encoded[0] &= 0xff >> size;
                if size > 1 {
                    encoded[0] |= 0xff << (9 - size);
                }
            } else {
                for index in 0..8 {
                    encoded[1 + index] = (bits >> (8 * (7 - index))) as u8;
                }
                encoded[0] = if size == 8 { 0xfe } else { 0xff };
            }
            unsafe { self.write_bytes(encoded.as_ptr(), size) };
            unsafe { self.write_bytes(value.as_ptr(), value.len()) };
        }
    }

    #[allow(non_camel_case_types)]
    pub struct Deserialize_;

    impl Deserialize_ {
        /// Read-side twin of [`Serialize_::serialize`] above.
        /// # Safety
        ///
        /// Foreign named-module boundary; both borrows are held only for
        /// the duration of the call.
        #[allow(unsafe_code)]
        pub unsafe fn deserialize<T: ?Sized, Archive: crate::RustcAdlDeserialize<T> + ?Sized>(
            value: &mut T,
            archive: &mut Archive,
        ) {
            // SAFETY: forwarded unchanged to the archive's bound impl.
            unsafe { archive.rustc_adl_deserialize(value) }
        }
    }

    #[allow(unsafe_code)]
    impl crate::RustcAdlDeserialize<String> for BinaryReadArchive {
        unsafe fn rustc_adl_deserialize(&mut self, value: &mut String) {
            let mut encoded = [0u8; 9];
            unsafe { self.read_or_abort(encoded.as_mut_ptr(), 1) };
            let size = sparse_size(encoded[0]);
            if size > 1 {
                unsafe { self.read_or_abort(encoded.as_mut_ptr().add(1), size - 1) };
            }
            let length = if size < 8 {
                let mut bits = 0u64;
                for index in 0..size - 1 {
                    bits |= (encoded[size - 1 - index] as u64) << (8 * index);
                }
                bits | (((encoded[0] & (0xff >> size)) as u64) << (8 * (size - 1)))
            } else {
                let mut bits = 0u64;
                for index in 0..8 {
                    bits |= (encoded[8 - index] as u64) << (8 * index);
                }
                bits
            } as usize;
            let mut bytes = vec![0u8; length];
            unsafe { self.read_or_abort(bytes.as_mut_ptr(), bytes.len()) };
            *value = String::from_utf8(bytes).expect("valid UTF-8 AnyMessage type name");
        }
    }
}
