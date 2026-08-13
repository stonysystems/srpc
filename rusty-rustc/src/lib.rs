#![deny(unsafe_code)]

//! Rust-only facades for APIs supplied by the rusty-cpp C++ runtime.
//!
//! The `rrr` crate uses this package for direct rustc checking and tests. The
//! rusty-cpp crate emitter recognizes this exact local package identity and
//! omits it from generated C++ because the production definitions already
//! live in the rusty runtime headers.

use ::std::cell::{Cell, RefCell};
use ::std::marker::PhantomData;
use ::std::ops::{Deref, DerefMut, Index};
use ::std::rc::Rc;
use ::std::sync::{Condvar, Mutex};
use ::std::time::Duration;

pub use rusty_cpp_markers::cpp_inherit;

/// Rust-only model of the C++ runtime's owning allocation helper.
pub fn make_box<T>(value: T) -> Box<T> {
    Box::new(value)
}

/// Rust-only callback-wrapper spelling for canonical cross-module facades.
pub struct CallbackWrapper<F> {
    inner: Option<::std::sync::Arc<F>>,
}

impl<F> CallbackWrapper<F> {
    pub fn from_callable(callable: F) -> Self {
        Self {
            inner: Some(::std::sync::Arc::new(callable)),
        }
    }

    pub fn has_value(&self) -> bool {
        self.inner.is_some()
    }

    pub fn callable(&self) -> &F {
        self.inner.as_deref().unwrap()
    }
}

impl<F> Clone for CallbackWrapper<F> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
        }
    }
}

impl<F> Default for CallbackWrapper<F> {
    fn default() -> Self {
        Self { inner: None }
    }
}

/// Opaque rustc-only models of the native pthread types used by the
/// canonical threading wrapper. The checked C++ type map restores the native
/// typedef spellings; canonical Rust only passes pointers to these values.
#[repr(C)]
pub struct PthreadSpinlock {
    _opaque: [u8; 0],
}

#[repr(C)]
pub struct PthreadMutex {
    _opaque: [u8; 0],
}

#[repr(C)]
pub struct PthreadMutexAttr {
    _opaque: [u8; 0],
}

#[repr(C)]
pub struct PthreadCond {
    _opaque: [u8; 0],
}

#[repr(C)]
pub struct PthreadCondAttr {
    _opaque: [u8; 0],
}

pub mod sync {
    pub use ::std::sync::{Arc, Weak};

    pub fn downgrade<T>(arc: Arc<T>) -> Weak<T> {
        Arc::downgrade(&arc)
    }

    pub mod atomic {
        pub use ::std::sync::atomic::{AtomicBool, Ordering};
    }
}

thread_local! {
    static REACTOR_CURRENT_FIBER: RefCell<Option<Rc<ReactorFiber>>> = const { RefCell::new(None) };
    static REACTOR_SLEEP_CALLS: RefCell<Vec<u64>> = const { RefCell::new(Vec::new()) };
}

/// Rust-only model of the `rrr.reactor` module's `Fiber` class.
///
/// The checked type map restores the existing `rrr::Fiber` spelling for C++;
/// this state exists only for direct rustc tests of `rrr.fiber`.
pub struct ReactorFiber {
    pub id: Cell<u64>,
    yields: Cell<u64>,
}

pub type ReactorIntEvent = rrr::reactor::IntEvent;

impl ReactorFiber {
    /// # Safety
    ///
    /// Reading the reactor's thread-local handle has no caller precondition.
    #[allow(unsafe_code)]
    pub unsafe fn current_fiber() -> Option<Rc<ReactorFiber>> {
        REACTOR_CURRENT_FIBER.with(|slot| slot.borrow().clone())
    }

    /// # Safety
    ///
    /// The receiver must be a live reactor fiber.
    #[allow(unsafe_code)]
    pub unsafe fn yield_(&self) {
        self.yields.set(self.yields.get().wrapping_add(1));
    }
}

/// Rust-only model of the `rrr.reactor` module's `BoxEvent<T>` template.
///
/// Crate-mode C++ generation maps this type back to the existing
/// `rrr::BoxEvent<T>` definition. The synchronization state exists only so
/// direct rustc tests can exercise one-shot set/wait/get behavior; it is never
/// emitted into production C++.
pub struct ReactorBoxEvent<T> {
    pub is_set_: Cell<bool>,
    value: Mutex<Option<T>>,
    ready: Condvar,
    not_thread_safe: PhantomData<Rc<()>>,
}

/// Rust-only conversion used by [`ReactorBoxEvent::set`] so canonical source
/// can pass either an owned value or the borrowed value accepted by C++.
pub trait ReactorSetValue<T> {
    fn into_owned(self) -> T;
}

impl<T> ReactorSetValue<T> for T {
    fn into_owned(self) -> T {
        self
    }
}

impl<T> ReactorSetValue<T> for &T
where
    T: Clone,
{
    fn into_owned(self) -> T {
        self.clone()
    }
}

impl<T> ReactorBoxEvent<T> {
    pub fn new() -> ReactorBoxEvent<T> {
        ReactorBoxEvent {
            is_set_: Cell::new(false),
            value: Mutex::new(None),
            ready: Condvar::new(),
            not_thread_safe: PhantomData,
        }
    }

    pub fn is_ready(&self) -> bool {
        self.is_set_.get()
    }

    pub fn set<V>(&self, value: V)
    where
        V: ReactorSetValue<T>,
    {
        *self.value.lock().unwrap() = Some(value.into_owned());
        self.is_set_.set(true);
        self.ready.notify_all();
    }

    pub fn wait(&self) {
        let mut value = self.value.lock().unwrap();
        while value.is_none() {
            value = self.ready.wait(value).unwrap();
        }
    }

    pub fn wait_timeout(&self, timeout_us: u64) {
        if timeout_us == 0 {
            self.wait();
            return;
        }
        let value = self.value.lock().unwrap();
        if value.is_none() {
            let _guard = self
                .ready
                .wait_timeout(value, Duration::from_micros(timeout_us))
                .unwrap();
        }
    }

    pub fn get(&self) -> T
    where
        T: Clone,
    {
        self.value
            .lock()
            .unwrap()
            .as_ref()
            .expect("BoxEvent value is not set")
            .clone()
    }
}

impl<T> Default for ReactorBoxEvent<T> {
    fn default() -> Self {
        Self::new()
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

/// Rust-only facade spelling mapped to the public `std::string` ABI.
pub type LoggingString = std::string;

/// Opaque rustc-only model mapped to libc's `FILE` in generated C++.
#[repr(C)]
pub struct CFile {
    _opaque: [u8; 0],
}

/// Rust-side model of `std::source_location` used by Debugging tests.
pub struct SourceLocation {
    file: &'static str,
    line: u32,
}

impl SourceLocation {
    pub fn current() -> SourceLocation {
        SourceLocation {
            file: file!(),
            line: line!(),
        }
    }

    pub fn file_name(&self) -> &'static str {
        self.file
    }

    pub fn line(&self) -> u32 {
        self.line
    }
}

/// Rust-only spelling for exact `std::vector<T>` ABI mappings.
pub type StdVector<T> = Vec<T>;

/// Rust-only spelling for APIs imported from the C++ `rusty` module.
pub mod rusty {
    pub mod os {
        pub mod fd {
            pub type OwnedFd = std::os::fd::OwnedFd;
        }
    }
}

/// Rust-side model of helpers supplied by the C++ rusty runtime.
pub mod sys {
    pub mod env {
        /// Return a host name for direct-rustc tests without adding an unsafe
        /// syscall boundary to this compile-time-only facade. Production C++
        /// resolves this path to `rusty::sys::env::hostname()`.
        pub fn hostname() -> String {
            ::std::env::var("HOSTNAME").unwrap_or_default()
        }
    }

    pub mod time {
        pub fn sleep_us(microseconds: u64) {
            ::std::thread::sleep(::std::time::Duration::from_micros(microseconds));
        }
    }
}

pub mod panic {
    pub fn do_panic(message: crate::std::string) -> ! {
        ::std::panic::panic_any(message.to_rust_string())
    }
}

/// Rust-only declarations for C++ modules imported by canonical rrr sources.
/// The exact local `rusty` facade dependency is omitted from generated C++.
#[allow(clippy::missing_safety_doc)]
pub mod rrr {
    pub mod basetypes {
        pub struct Time;

        impl Time {
            /// # Safety
            ///
            /// Both clock selectors are valid; `unsafe` records the foreign
            /// named-module boundary used by canonical Fiber code.
            #[allow(unsafe_code)]
            pub unsafe fn now(_monotonic: bool) -> u64 {
                use ::std::time::{SystemTime, UNIX_EPOCH};
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_micros() as u64
            }
        }

        #[allow(non_snake_case)]
        pub mod SparseInt {
            #[allow(unsafe_code)]
            pub unsafe fn buf_size(byte0: u8) -> usize {
                crate::sparse_buf_size(byte0)
            }

            #[allow(unsafe_code)]
            pub unsafe fn dump32(value: i32, buffer: *mut u8) -> usize {
                unsafe { crate::sparse_dump32(value, buffer) }
            }

            #[allow(unsafe_code)]
            pub unsafe fn load32(buffer: *const u8) -> i32 {
                unsafe { crate::sparse_load32(buffer) }
            }
        }
    }

    pub mod debugging {
        #[allow(unsafe_code)]
        pub unsafe fn verify(value: bool) {
            assert!(value);
        }
    }

    /// Compile-time-only namespace model used to retain the private
    /// `rrr.errors` named-module import in canonical callback generation.
    pub mod errors {}

    pub mod logging {
        /// Rust-side no-op model of the production logging entry point.
        ///
        /// # Safety
        ///
        /// `file` must be null or point to a valid NUL-terminated path for the
        /// duration of the call. The production logger scans any non-null path.
        #[allow(unsafe_code)]
        pub unsafe fn log_line(_level: i32, _line: i32, _file: *const i8, _message: &String) {}
    }

    pub mod reactor {
        use crate::{ReactorBoxEvent, ReactorFiber, REACTOR_CURRENT_FIBER, REACTOR_SLEEP_CALLS};
        use ::std::cell::Cell;
        use ::std::rc::Rc;
        use ::std::sync::{Arc, Condvar, Mutex};

        pub type Fiber = ReactorFiber;

        /// Rust-only model of the reactor's fiber-aware integer event.
        pub struct IntEvent {
            value: Mutex<i32>,
            target: i32,
            ready: Condvar,
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
                ready: Condvar::new(),
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
        use crate::{SerializableBase, SerializableProxy, SerializableSharedPtrHolder};

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
            #[allow(clippy::ptr_arg)]
            #[allow(unsafe_code)]
            pub unsafe fn serialize(value: &String, archive: &mut BinaryWriteArchive) {
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
                unsafe { archive.write_bytes(encoded.as_ptr(), size) };
                unsafe { archive.write_bytes(value.as_ptr(), value.len()) };
            }
        }

        #[allow(non_camel_case_types)]
        pub struct Deserialize_;

        impl Deserialize_ {
            #[allow(unsafe_code)]
            pub unsafe fn deserialize(value: &mut String, archive: &mut BinaryReadArchive) {
                let mut encoded = [0u8; 9];
                unsafe { archive.read_or_abort(encoded.as_mut_ptr(), 1) };
                let size = sparse_size(encoded[0]);
                if size > 1 {
                    unsafe { archive.read_or_abort(encoded.as_mut_ptr().add(1), size - 1) };
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
                unsafe { archive.read_or_abort(bytes.as_mut_ptr(), bytes.len()) };
                *value = String::from_utf8(bytes).expect("valid UTF-8 AnyMessage type name");
            }
        }

        #[allow(unsafe_code)]
        pub unsafe fn serializable_holder_of<T: 'static>(
            _base: *const SerializableBase,
        ) -> *const SerializableSharedPtrHolder<T> {
            core::ptr::null()
        }

        #[allow(non_snake_case)]
        pub mod SerializableRegistry {
            use super::{SerializableBase, SerializableProxy};

            #[allow(unsafe_code)]
            pub unsafe fn create(_kind: i32) -> SerializableProxy {
                SerializableProxy::make(SerializableBase)
            }
        }
    }
}

/// Rust-only declarations behind `use cpp::std` in canonical code.
pub mod std {
    use crate::StdPair;
    use ::std::cell::UnsafeCell;
    use ::std::io::Write as _;

    /// Values accepted by the rustc-only `std::string::append` model.
    pub trait StringAppend {
        fn append_to(self, output: &mut Vec<u8>);
    }

    impl StringAppend for &str {
        fn append_to(self, output: &mut Vec<u8>) {
            output.extend_from_slice(self.as_bytes());
        }
    }

    impl StringAppend for &::std::string::String {
        fn append_to(self, output: &mut Vec<u8>) {
            output.extend_from_slice(self.as_bytes());
        }
    }

    /// Rustc-only byte model of `std::string`.
    #[allow(non_camel_case_types)]
    pub struct string(UnsafeCell<Vec<u8>>);

    impl Default for string {
        fn default() -> Self {
            Self(UnsafeCell::new(Vec::new()))
        }
    }

    impl StringAppend for string {
        fn append_to(self, output: &mut Vec<u8>) {
            output.extend_from_slice(self.0.into_inner().as_slice());
        }
    }

    impl StringAppend for &string {
        #[allow(unsafe_code)]
        fn append_to(self, output: &mut Vec<u8>) {
            // SAFETY: this facade is used only by single-threaded direct-rustc
            // logging tests; generated C++ maps the type to `std::string`.
            output.extend_from_slice(unsafe { (&*self.0.get()).as_slice() });
        }
    }

    impl string {
        pub fn append<T: StringAppend>(&mut self, value: T) {
            value.append_to(self.0.get_mut());
        }

        pub fn push_back(&mut self, value: i8) {
            self.0.get_mut().push(value as u8);
        }

        pub fn resize(&mut self, size: usize) {
            self.0.get_mut().resize(size, 0);
        }

        /// Rustc-only signature model of `std::string::c_str`.
        pub fn c_str(&self) -> *const i8 {
            core::ptr::null()
        }

        /// # Safety
        ///
        /// The returned pointer must not outlive this value or overlap any
        /// other access to its byte storage.
        #[allow(unsafe_code)]
        pub unsafe fn data(&self) -> *mut i8 {
            unsafe { (&mut *self.0.get()).as_mut_ptr().cast() }
        }

        #[allow(unsafe_code)]
        pub fn size(&self) -> usize {
            // SAFETY: direct-rustc facade callers do not access this model
            // concurrently; the generated C++ uses `std::string` instead.
            unsafe { (&*self.0.get()).len() }
        }

        /// Clone the facade bytes into an ordinary Rust string for tests.
        #[allow(unsafe_code)]
        pub fn to_rust_string(&self) -> ::std::string::String {
            // SAFETY: direct-rustc facade callers do not mutate this model
            // concurrently; production maps the type to `std::string`.
            let bytes = unsafe { (&*self.0.get()).clone() };
            ::std::string::String::from_utf8(bytes).expect("valid UTF-8 in std::string facade")
        }
    }

    pub struct Cout;

    #[allow(non_upper_case_globals)]
    pub static cout: Cout = Cout;

    impl Cout {
        /// # Safety
        ///
        /// `data` must denote `size` readable bytes.
        #[allow(unsafe_code)]
        pub unsafe fn write(&self, data: *mut i8, size: usize) {
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

fn sparse_buf_size(byte0: u8) -> usize {
    if byte0 & 0x80 == 0 {
        1
    } else if byte0 & 0xc0 == 0x80 {
        2
    } else if byte0 & 0xe0 == 0xc0 {
        3
    } else if byte0 & 0xf0 == 0xe0 {
        4
    } else {
        5
    }
}

#[allow(unsafe_code)]
unsafe fn sparse_dump32(value: i32, buffer: *mut u8) -> usize {
    let bytes = value.to_ne_bytes();
    unsafe { core::ptr::copy_nonoverlapping(bytes.as_ptr(), buffer, bytes.len()) };
    bytes.len()
}

#[allow(unsafe_code)]
unsafe fn sparse_load32(buffer: *const u8) -> i32 {
    let mut bytes = [0u8; 4];
    unsafe { core::ptr::copy_nonoverlapping(buffer, bytes.as_mut_ptr(), bytes.len()) };
    i32::from_ne_bytes(bytes)
}

pub struct Arc<T: ?Sized> {
    inner: ::std::sync::Arc<T>,
}

impl<T: ?Sized> Clone for Arc<T> {
    fn clone(&self) -> Self {
        Self {
            inner: ::std::sync::Arc::clone(&self.inner),
        }
    }
}

impl<T: ?Sized> Arc<T> {
    pub fn get(&self) -> *const T {
        ::std::sync::Arc::as_ptr(&self.inner)
    }

    pub fn get_mut(&mut self) -> Option<&mut T> {
        ::std::sync::Arc::get_mut(&mut self.inner)
    }
}

pub trait ArcMake<Argument> {
    type Output;
    fn make(argument: Argument) -> Self::Output;
}

impl<T> ArcMake<T> for Arc<T> {
    type Output = Arc<T>;

    fn make(argument: T) -> Self::Output {
        Arc {
            inner: ::std::sync::Arc::new(argument),
        }
    }
}

impl<T> ArcMake<Arc<T>> for Arc<SerializableSharedPtrHolder<T>> {
    type Output = SerializableProxy;

    fn make(_argument: Arc<T>) -> Self::Output {
        SerializableProxy::make(SerializableBase)
    }
}

impl<T: ?Sized> Arc<T> {
    pub fn make<Argument>(argument: Argument) -> <Self as ArcMake<Argument>>::Output
    where
        Self: ArcMake<Argument>,
    {
        <Self as ArcMake<Argument>>::make(argument)
    }
}

/// Rust-only test model for the binary archive supplied by
/// `rrr.serializable` in production C++.
#[derive(Default)]
pub struct BinaryWriteArchive {
    bytes: Vec<u8>,
}

impl BinaryWriteArchive {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn as_bytes(&self) -> &[u8] {
        &self.bytes
    }

    /// Append a raw byte range to this archive.
    ///
    /// # Safety
    ///
    /// When `length` is nonzero, `pointer` must be non-null and reference
    /// `length` readable, initialized bytes for the duration of this call.
    #[allow(unsafe_code)]
    pub unsafe fn write_bytes(&mut self, pointer: *const u8, length: usize) {
        if length == 0 {
            return;
        }

        // SAFETY: upheld by the caller contract above. The early return keeps
        // the zero-length case independent of raw-pointer validity.
        let bytes = unsafe { ::std::slice::from_raw_parts(pointer, length) };
        self.bytes.extend_from_slice(bytes);
    }

    pub fn into_bytes(self) -> Vec<u8> {
        self.bytes
    }
}

/// Rust-only test model for the binary archive supplied by
/// `rrr.serializable` in production C++.
pub struct BinaryReadArchive {
    bytes: Vec<u8>,
    offset: usize,
}

impl BinaryReadArchive {
    pub fn new(bytes: &[u8]) -> Self {
        Self::from_bytes(bytes.to_vec())
    }

    pub fn from_bytes(bytes: Vec<u8>) -> Self {
        Self { bytes, offset: 0 }
    }

    pub fn remaining(&self) -> usize {
        self.bytes.len().saturating_sub(self.offset)
    }

    /// Copy the requested archive bytes into a raw destination.
    ///
    /// # Safety
    ///
    /// When `length` is nonzero, `pointer` must be non-null and reference
    /// `length` writable bytes which do not overlap this archive's storage.
    #[allow(unsafe_code)]
    pub unsafe fn read_exact(&mut self, pointer: *mut u8, length: usize) -> bool {
        let Some(end) = self.offset.checked_add(length) else {
            return false;
        };
        let Some(source) = self.bytes.get(self.offset..end) else {
            return false;
        };

        if length != 0 {
            // SAFETY: upheld by the caller contract above. The nonzero guard
            // keeps an empty copy independent of raw-pointer validity.
            let destination = unsafe { ::std::slice::from_raw_parts_mut(pointer, length) };
            destination.copy_from_slice(source);
        }
        self.offset = end;
        true
    }

    /// Copy the next archive bytes or abort this Rust-only model.
    ///
    /// # Safety
    ///
    /// The caller must uphold the same destination contract as `read_exact`.
    #[allow(unsafe_code)]
    pub unsafe fn read_or_abort(&mut self, pointer: *mut u8, length: usize) {
        assert!(
            unsafe { self.read_exact(pointer, length) },
            "binary archive source is truncated"
        );
    }
}

pub struct SerializableBase;

impl SerializableBase {
    pub fn save(&self, _archive: &mut BinaryWriteArchive) {}
    pub fn load(&mut self, _archive: &mut BinaryReadArchive) {}
    pub fn kind(&self) -> i32 {
        0
    }
}

pub struct SerializableSharedPtrHolder<T> {
    pub ptr: Arc<T>,
}

pub type SerializableProxy = Arc<SerializableBase>;

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

/// Rust-side model of rusty-cpp's move-only type-erased callable.
///
/// `None` is the exact empty state. The explicit representation padding and
/// alignment keep the Rust facade at 48/16 on both 32- and 64-bit pointer
/// widths, matching the production 64-bit `rusty::Function` layout. The boxed
/// trait object gives rustc the same `Fn`/`FnMut` call semantics.
#[repr(C, align(16))]
pub struct Function<F: ?Sized> {
    inner: Option<Box<F>>,
    runtime_layout_padding: [u8; 32],
}

impl<F: ?Sized> Function<F> {
    /// Returns true when no callback is installed.
    pub fn is_empty(&self) -> bool {
        self.inner.is_none()
    }
}

impl<F: ?Sized> Default for Function<F> {
    fn default() -> Self {
        Self {
            inner: None,
            runtime_layout_padding: [0; 32],
        }
    }
}

impl<F: ?Sized> Deref for Function<F> {
    type Target = F;

    fn deref(&self) -> &F {
        self.inner
            .as_deref()
            .expect("attempted to call an empty rusty::Function")
    }
}

impl<F: ?Sized> DerefMut for Function<F> {
    fn deref_mut(&mut self) -> &mut F {
        self.inner
            .as_deref_mut()
            .expect("attempted to call an empty rusty::Function")
    }
}

impl<A: 'static, B: 'static> Function<dyn Fn(A, B)> {
    /// Erases a const-callable two-argument callback.
    pub fn from_callable<C>(callback: C) -> Self
    where
        C: Fn(A, B) + 'static,
    {
        Self {
            inner: Some(Box::new(callback)),
            runtime_layout_padding: [0; 32],
        }
    }
}

impl Function<dyn FnMut()> {
    /// Erases a mutable zero-argument callback.
    pub fn from_callable<C>(callback: C) -> Self
    where
        C: FnMut() + 'static,
    {
        Self {
            inner: Some(Box::new(callback)),
            runtime_layout_padding: [0; 32],
        }
    }
}

impl<A: 'static> Function<dyn FnMut(A)> {
    /// Erases a mutable one-argument callback.
    pub fn from_callable<C>(callback: C) -> Self
    where
        C: FnMut(A) + 'static,
    {
        Self {
            inner: Some(Box::new(callback)),
            runtime_layout_padding: [0; 32],
        }
    }
}

#[cfg(test)]
mod tests {
    use super::Function;
    use ::std::cell::Cell;
    use ::std::mem::{align_of, size_of};
    use ::std::rc::Rc;

    #[test]
    fn empty_and_layout_match_the_cpp_runtime() {
        let callback: Function<dyn Fn(i32, i32)> = Function::default();
        assert!(callback.is_empty());
        assert_eq!(size_of::<Function<dyn Fn(i32, i32)>>(), 48);
        assert_eq!(align_of::<Function<dyn Fn(i32, i32)>>(), 16);
        assert_eq!(size_of::<Function<dyn FnMut()>>(), 48);
        assert_eq!(align_of::<Function<dyn FnMut()>>(), 16);
        assert_eq!(size_of::<Function<dyn FnMut(i32)>>(), 48);
        assert_eq!(align_of::<Function<dyn FnMut(i32)>>(), 16);

        macro_rules! assert_not_auto_trait {
            ($type:ty, $auto_trait:ident) => {{
                trait AmbiguousIfImplemented<Marker> {
                    fn marker() {}
                }
                impl<T: ?Sized> AmbiguousIfImplemented<()> for T {}
                impl<T: ?Sized + $auto_trait> AmbiguousIfImplemented<u8> for T {}
                let _ = <$type as AmbiguousIfImplemented<_>>::marker;
            }};
        }
        assert_not_auto_trait!(Function<dyn Fn(i32, i32)>, Send);
        assert_not_auto_trait!(Function<dyn Fn(i32, i32)>, Sync);
        assert_not_auto_trait!(Function<dyn FnMut()>, Send);
        assert_not_auto_trait!(Function<dyn FnMut()>, Sync);
        assert_not_auto_trait!(Function<dyn FnMut(i32)>, Send);
        assert_not_auto_trait!(Function<dyn FnMut(i32)>, Sync);
    }

    #[test]
    fn fn_and_fn_mut_dispatch() {
        let observed = Rc::new(Cell::new((0, 0)));
        let sink = Rc::clone(&observed);
        let callback = Function::<dyn Fn(i32, i32)>::from_callable(move |a, b| {
            sink.set((a, b));
        });
        callback(4, 9);
        assert_eq!(observed.get(), (4, 9));

        let calls = Rc::new(Cell::new(0));
        let counter = Rc::clone(&calls);
        let mut callback = Function::<dyn FnMut()>::from_callable(move || {
            counter.set(counter.get() + 1);
        });
        callback();
        callback();
        assert_eq!(calls.get(), 2);

        let sum = Rc::new(Cell::new(0));
        let accumulator = Rc::clone(&sum);
        let mut callback = Function::<dyn FnMut(i32)>::from_callable(move |value| {
            accumulator.set(accumulator.get() + value);
        });
        callback(7);
        callback(5);
        assert_eq!(sum.get(), 12);
    }
}
