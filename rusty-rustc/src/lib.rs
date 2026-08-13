#![deny(unsafe_code)]

//! Rust-only facades for APIs supplied by the rusty-cpp C++ runtime.
//!
//! The `rrr` crate uses this package for direct rustc checking and tests. The
//! rusty-cpp crate emitter recognizes this exact local package identity and
//! omits it from generated C++ because the production definitions already
//! live in the rusty runtime headers.

use ::std::cell::Cell;
use ::std::marker::PhantomData;
use ::std::ops::{Deref, DerefMut, Index};
use ::std::rc::Rc;
use ::std::sync::{Condvar, Mutex};
use ::std::time::Duration;

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

/// Rust-only spelling for exact `std::vector<T>` ABI mappings.
pub type StdVector<T> = Vec<T>;

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
}

/// Rust-only declarations for C++ modules imported by canonical rrr sources.
/// The exact local `rusty` facade dependency is omitted from generated C++.
#[allow(clippy::missing_safety_doc)]
pub mod rrr {
    pub mod basetypes {
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
        use crate::ReactorBoxEvent;
        use ::std::sync::Arc;

        /// # Safety
        ///
        /// This facade has no caller-side precondition. `unsafe` records the
        /// foreign named-module boundary at canonical Rust call sites.
        #[allow(unsafe_code)]
        pub unsafe fn create_sp_box_event<T>() -> Arc<ReactorBoxEvent<T>> {
            Arc::new(ReactorBoxEvent::new())
        }
    }

    pub mod serializable {
        use crate::{SerializableBase, SerializableProxy, SerializableSharedPtrHolder};

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

pub struct BinaryWriteArchive;

impl BinaryWriteArchive {
    /// # Safety
    ///
    /// `data` must denote `len` readable bytes.
    #[allow(unsafe_code)]
    pub unsafe fn write_bytes(&mut self, _data: *const u8, _len: usize) {}
}

pub struct BinaryReadArchive;

impl BinaryReadArchive {
    /// # Safety
    ///
    /// `data` must denote `len` writable bytes.
    #[allow(unsafe_code)]
    pub unsafe fn read_exact(&mut self, _data: *mut u8, _len: usize) -> bool {
        false
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
