#![deny(unsafe_code)]

//! Rust-only facades for APIs supplied by the rusty-cpp C++ runtime.
//!
//! The `srpc` crate uses this package for direct rustc checking and tests. The
//! rusty-cpp crate emitter recognizes this exact local package identity and
//! omits it from generated C++ because the production definitions already
//! live in the rusty runtime headers.

pub use ::std::boxed::Box;
pub use ::std::cell::{Cell, RefCell, RefMut};
pub use ::std::collections::{VecDeque};
use ::std::ops::{Deref, Index};
pub use ::std::option::Option;
pub use ::std::option::Option::{None, Some};
pub use ::std::rc::Rc;
pub use ::std::vec::Vec;

pub use rusty_cpp_markers::cpp_inherit;


/// Rustc-only storage model for the reactor's `std::set<Arc<Job>>` slot.
///
/// The production type map lowers `ReactorJobSet<T>` to `std::set<T>`; this
/// sorted storage preserves the set's pointee-address order and uniqueness for
/// direct Rust checking without requiring `dyn Job: Ord`.
pub struct ReactorJobSet<T> {
    entries: Vec<T>,
}

pub trait ReactorJobSetKey {
    fn identity_address(&self) -> usize;
}

impl<T: ?Sized> ReactorJobSetKey for ::std::sync::Arc<T> {
    fn identity_address(&self) -> usize {
        ::std::sync::Arc::as_ptr(self) as *const () as usize
    }
}

impl<T> Default for ReactorJobSet<T> {
    fn default() -> Self {
        Self { entries: Vec::new() }
    }
}

impl<T: ReactorJobSetKey> ReactorJobSet<T> {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&mut self, value: T) {
        if let Err(index) = self.entries.binary_search_by_key(
            &value.identity_address(), |existing| existing.identity_address(),
        ) {
            self.entries.insert(index, value);
        }
    }

    pub fn erase(&mut self, value: T) {
        if let Ok(index) = self.entries.binary_search_by_key(
            &value.identity_address(), |existing| existing.identity_address(),
        ) {
            self.entries.remove(index);
        }
    }

    pub fn iter(&self) -> ::std::slice::Iter<'_, T> {
        self.entries.iter()
    }
}


/// Rust-only representation of `std::pair<A, B>` used by canonical sources.
pub struct StdPair<A, B> {
    pub first: A,
    pub second: B,
}

impl<A, B> StdPair<A, B> {
    pub fn new(first: A, second: B) -> StdPair<A, B> {
        StdPair { first, second }
    }
}


/// Rust-only spelling for exact `std::vector<T>` ABI mappings.
pub type StdVector<T> = Vec<T>;




/// The production emitter recognizes this call and emits
/// `rusty::make_box<Adapter>(value)`.  The divergent Rust facade lets the call
/// coerce to the local trait-object return type without pretending to model
/// C++'s generated adapter hierarchy. It is an emitter contract (the
/// transpiler's `make_box` coercion path), not a wrapper over `Box::new`, so
/// it is not a candidate for the std spelling.
pub fn make_box<Adapter>(value: Adapter) -> Box<Adapter> {
    Box::new(value)
}

/// Remaining compatibility names for standard runtime modules.
pub mod rusty {


    pub mod io {
        pub use ::std::io::Error;
    }

    pub mod net {
        use ::std::{io::Error, net::SocketAddrV4};

        pub fn socket_addr_v4_from_str(value: &str) -> Result<SocketAddrV4, Error> {
            value.parse::<SocketAddrV4>().map_err(|error| {
                Error::new(::std::io::ErrorKind::InvalidInput, error.to_string())
            })
        }

        pub fn socket_addr_v4_to_string(value: SocketAddrV4) -> String {
            value.to_string()
        }


    }



}

pub mod panic {
    /// Opaque model of the C++ `std::exception_ptr` payload carried out of a
    /// caught unwind. Production C++ resolves the pair below to
    /// `rusty::panic::catch_unwind` / `rusty::panic::payload_message`.
    ///
    /// Canonical code that only needs to swallow an unwind uses
    /// `std::panic::catch_unwind` directly (rpc/callbacks.rs,
    /// rpc/request_queue.rs). This model exists for the one site that inspects
    /// the payload (the shutdown-hook invoker in rpc/server.rs): std's
    /// `Err(Box<dyn Any + Send>)` has no C++ spelling, while the runtime's
    /// payload is a `std::exception_ptr` whose `what()` `payload_message`
    /// recovers.
    pub struct PanicPayload(Option<String>);

    /// Production C++ takes a `std::string_view`; `&str` lowers to exactly that.
    pub fn do_panic(message: &str) -> ! {
        ::std::panic::panic_any(message.to_string())
    }

    /// Run `body`, converting an unwind into `Err(PanicPayload)`.
    pub fn catch_unwind<F: FnMut()>(body: F) -> Result<(), PanicPayload> {
        let result = ::std::panic::catch_unwind(::std::panic::AssertUnwindSafe(body));
        match result {
            Ok(()) => Ok(()),
            Err(payload) => {
                let message = payload
                    .downcast_ref::<&str>()
                    .map(|text| (*text).to_string())
                    .or_else(|| payload.downcast_ref::<String>().cloned());
                Err(PanicPayload(message))
            }
        }
    }

    /// Recover a typed `std::exception::what()` message; an opaque payload
    /// yields `None`.
    pub fn payload_message(payload: PanicPayload) -> Option<String> {
        payload.0
    }
}

/// Handle-validity and emptiness predicates the runtime types carry but Rust's
/// own owning handles cannot be without. `rusty::Box` / `rusty::Arc` expose
/// `is_valid()` and `rusty::Function` exposes `is_empty()`; a Rust `Box` or
/// `Arc` is never null and a Rust boxed closure is never empty, so the rustc
/// models are constants. Canonical sources still spell the predicate so the
/// generated C++ keeps checking handles that reach it from C++ callers.
pub trait RustyHandleIsValid {
    fn is_valid(&self) -> bool;
}

impl<T: ?Sized> RustyHandleIsValid for Box<T> {
    fn is_valid(&self) -> bool {
        true
    }
}

impl<T: ?Sized> RustyHandleIsValid for ::std::sync::Arc<T> {
    fn is_valid(&self) -> bool {
        true
    }
}

pub trait RustyFunctionIsEmpty {
    fn is_empty(&self) -> bool;
}

impl<T: ?Sized> RustyFunctionIsEmpty for Box<T> {
    fn is_empty(&self) -> bool {
        false
    }
}

/// Rust-only declarations behind `use cpp::std` in canonical code.
pub mod std {
    use crate::StdPair;
    use ::std::io::Write as _;

    pub struct Cout;

    #[allow(non_upper_case_globals)]
    pub static cout: Cout = Cout;

    impl Cout {
        /// # Safety
        ///
        /// `data` must denote `size` readable bytes.
        #[allow(unsafe_code)]
        pub unsafe fn write(&self, data: *const i8, size: usize) {
            let bytes = unsafe { core::slice::from_raw_parts(data.cast::<u8>(), size) };
            let _ = ::std::io::stdout().write_all(bytes);
        }

        /// # Safety
        ///
        /// The byte is written synchronously and has no additional precondition.
        #[allow(unsafe_code)]
        pub unsafe fn put(&self, value: i8) {
            let _ = ::std::io::stdout().write_all(&[value as u8]);
        }

        /// # Safety
        ///
        /// The flush is synchronous and has no additional precondition.
        #[allow(unsafe_code)]
        pub unsafe fn flush(&self) {
            let _ = ::std::io::stdout().flush();
        }
    }

    /// # Safety
    ///
    /// This facade has no caller-side precondition. `unsafe` records the
    /// foreign named-module boundary at canonical Rust call sites.
    #[allow(unsafe_code)]
    pub unsafe fn make_pair<A, B>(first: A, second: B) -> StdPair<A, B> {
        StdPair::new(first, second)
    }
}

/// Rust-only contract for metric views used by the canonical load-balancer module.
pub trait LoadBalancerMetrics {
    fn in_flight_requests(&self) -> u64;
    fn avg_latency_us(&self) -> u64;
    fn requests_completed(&self) -> u64;
}

/// Rust-only contract for a client exposing a load-balancer metric view.
pub trait LoadBalancerClient {
    type Metrics: LoadBalancerMetrics;

    fn metrics(&self) -> &Self::Metrics;
}

/// Rust-only contract for pointer-like client handles.
pub trait LoadBalancerClientHandle: Deref
where
    Self::Target: LoadBalancerClient,
{
}

impl<T> LoadBalancerClientHandle for T
where
    T: Deref,
    T::Target: LoadBalancerClient,
{
}

/// Rust-only contract for indexable client pools.
#[allow(clippy::len_without_is_empty)]
pub trait LoadBalancerClientVec: Index<usize>
where
    Self::Output: LoadBalancerClientHandle,
    <Self::Output as Deref>::Target: LoadBalancerClient,
{
    fn len(&self) -> usize;
}

impl<T> LoadBalancerClientVec for Vec<T>
where
    T: LoadBalancerClientHandle,
    <T as Deref>::Target: LoadBalancerClient,
{
    fn len(&self) -> usize {
        Vec::len(self)
    }
}
