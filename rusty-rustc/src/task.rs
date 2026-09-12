//! srpc's async runtime -- the rustc-lane half.
//!
//! `Task<T>`, `Waker`, `Context` and `Poll<T>` are shaped to mirror the C++20
//! coroutine types the transpiler lowers `async fn` / `.await` onto, so the same
//! canonical source works in both lanes. The interesting piece is
//! `Task::from_future`, which bridges srpc's own waker to a real
//! `std::task::Waker` so a native rustc `Future` can be polled by srpc's
//! executor.
//!
//! The other half -- the task table, ready queue and poll pass -- lives in
//! canonical `reactor/reactor.rs`, and `PollThread` drives it. See
//! docs/async-runtime.md.

use super::*;

pub struct Waker {
    // The production `rusty::Waker` stores a copyable `std::function` and its
    // `wake()` member is const.  Model that contract directly so a retained
    // waker may be invoked concurrently without an `FnMut` aliasing hole.
    pub wake_fn: ::std::sync::Arc<dyn Fn() + Send + Sync>,
}

impl Waker {
    pub fn from_callable<F>(callback: F) -> Self
    where
        F: Fn() + Send + Sync + 'static,
    {
        Self {
            wake_fn: ::std::sync::Arc::new(callback),
        }
    }

    pub fn wake(&self) {
        (self.wake_fn)();
    }
}

pub struct Context {
    pub waker: *mut Waker,
}


pub struct Poll<T> {
    pub ready: bool,
    pub value: T,
}

impl<T> Poll<T> {
    pub fn ready_with(value: T) -> Self {
        Self { ready: true, value }
    }

    pub fn is_ready(&self) -> bool {
        self.ready
    }

    pub fn is_pending(&self) -> bool {
        !self.ready
    }
}

impl<T: Default> Poll<T> {
    pub fn pending() -> Self {
        Self {
            ready: false,
            value: T::default(),
        }
    }
}

/// The boxed poll closure a `Task<T>` drives.  Named so the signature reads
/// once here rather than at every use.
pub type TaskPoller<T> = Box<dyn FnMut(&mut Context) -> Poll<T>>;

pub struct Task<T> {
    poller: TaskPoller<T>,
}


impl<T> Task<T> {
    pub fn from_poller<F>(poller: F) -> Self
    where
        F: FnMut(&mut Context) -> Poll<T> + 'static,
    {
        Self {
            poller: Box::new(poller),
        }
    }

    /// Wrap a real Rust future as a facade `Task<T>`, so an `async fn` --
    /// which the transpiler lowers to a C++ coroutine returning
    /// `rusty::Task<T>` natively -- can feed the same canonical spawn path
    /// (`reactor_spawn_stackless_task_with_result`) under rustc.  C++ callers
    /// never need this: calling the coroutine already yields a Task, which is
    /// why the bridge lives here in the facade and not in a canonical module.
    ///
    /// Each Rust waker owns the callback copied from the current polling
    /// context. The callback can therefore outlive both Task destruction and
    /// reactor teardown, just as a copied C++ `std::function` can. Canonical
    /// wake admission checks decide whether that retained callback has work.
    pub fn from_future<F>(future: F) -> Self
    where
        F: ::std::future::Future<Output = T> + 'static,
        T: Default,
    {
        struct NativeWake {
            callback: ::std::sync::Arc<dyn Fn() + Send + Sync>,
        }
        impl ::std::task::Wake for NativeWake {
            fn wake(self: ::std::sync::Arc<Self>) {
                self.wake_by_ref();
            }
            fn wake_by_ref(self: &::std::sync::Arc<Self>) {
                (self.callback)();
            }
        }

        let mut pinned = Box::pin(future);
        let mut finished = false;
        Self::from_poller(move |cx: &mut Context| {
            if finished {
                panic!("facade Task polled after completion");
            }
            assert!(!cx.waker.is_null(), "Task polling requires a live Context waker");
            // SAFETY: the executor keeps this polling context's Waker alive
            // for the synchronous poll. Only its owned callback escapes.
            #[allow(unsafe_code)]
            let callback = unsafe { (*cx.waker).wake_fn.clone() };
            let bridge = ::std::sync::Arc::new(NativeWake { callback });
            let std_waker = ::std::task::Waker::from(bridge);
            let mut std_cx = ::std::task::Context::from_waker(&std_waker);
            match pinned.as_mut().poll(&mut std_cx) {
                ::std::task::Poll::Ready(value) => {
                    finished = true;
                    Poll::ready_with(value)
                }
                ::std::task::Poll::Pending => Poll::pending(),
            }
        })
    }

    /// Poll once using an executor-owned wake context.
    ///
    /// # Safety
    ///
    /// `context.waker` must point to a live Waker for this entire call. The
    /// executor must not mutate or destroy that Waker concurrently. Retained
    /// Rust wakers own a callback clone and impose no lifetime requirement
    /// after the call returns.
    #[allow(unsafe_code)]
    pub unsafe fn poll(&mut self, context: &mut Context) -> Poll<T> {
        (self.poller)(context)
    }
}
