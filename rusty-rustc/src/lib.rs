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

/// Rust-only declarations behind `use cpp::std` in canonical code.
pub mod std {
}
