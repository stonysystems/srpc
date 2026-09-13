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
pub use ::std::option::Option;
pub use ::std::option::Option::{None, Some};
pub use ::std::rc::Rc;
pub use ::std::vec::Vec;

pub use rusty_cpp_markers::cpp_inherit;





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


/// Rust-only declarations behind `use cpp::std` in canonical code.
pub mod std {
}
