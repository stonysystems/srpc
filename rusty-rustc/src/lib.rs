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
use ::std::sync::Condvar;
use ::std::time::Duration;

pub use rusty_cpp_markers::cpp_inherit;

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

/// Rust-only spelling for exact `std::vector<T>` ABI mappings.
pub type StdVector<T> = Vec<T>;

/// Rustc-only spelling for the sparse 32-bit wrapper exported directly by the
/// `rrr.basetypes` C++ module.  The production type map restores the public
/// `rrr::v32` spelling; this local model only supplies the checked Rust API.
#[derive(Clone, Copy, Default, Eq, PartialEq)]
pub struct SerializableV32(i32);

impl SerializableV32 {
    pub const fn new(value: i32) -> Self {
        Self(value)
    }

    pub fn set(&mut self, value: i32) {
        self.0 = value;
    }

    pub const fn get(&self) -> i32 {
        self.0
    }
}

/// Rustc-only spelling for the corresponding sparse 64-bit wrapper.
#[derive(Clone, Copy, Default, Eq, PartialEq)]
pub struct SerializableV64(i64);

impl SerializableV64 {
    pub const fn new(value: i64) -> Self {
        Self(value)
    }

    pub fn set(&mut self, value: i64) {
        self.0 = value;
    }

    pub const fn get(&self) -> i64 {
        self.0
    }
}

/// Rustc-only dispatch facade for the generated C++ `Serialize_` overload
/// namespace. The production type map restores that exact namespace spelling;
/// the Rust body is intentionally inert because canonical unit tests exercise
/// concrete leaf implementations directly.
pub struct SerializableSerializeDispatch;

impl SerializableSerializeDispatch {
    pub fn serialize<T: ?Sized, Archive>(_value: &T, _archive: &mut Archive) {}
}

/// Read-side counterpart of `SerializableSerializeDispatch`.
pub struct SerializableDeserializeDispatch;

impl SerializableDeserializeDispatch {
    pub fn deserialize<T, Archive>(_value: &mut T, _archive: &mut Archive) {}
}

/// Rustc-only model of the move-only zero-argument registry factory. The
/// production type map restores `rusty::Function<SerializableProxy()>`.
pub struct SerializableRegistryFactory {
    callback: Box<dyn Fn() -> SerializableProxy + Send>,
}

impl SerializableRegistryFactory {
    pub fn from_callable<C>(callback: C) -> Self
    where
        C: Fn() -> SerializableProxy + Send + 'static,
    {
        Self {
            callback: Box::new(callback),
        }
    }
}

impl Deref for SerializableRegistryFactory {
    type Target = dyn Fn() -> SerializableProxy + Send;

    fn deref(&self) -> &Self::Target {
        &*self.callback
    }
}

/// Minimal rustc-only model of the runtime mutex.  Production C++ keeps using
/// `rusty::Mutex`; this wrapper exists only so canonical sources can be checked
/// by rustc, including const initialization of process-wide registries.
pub struct Mutex<T>(::std::sync::Mutex<T>);

impl<T> Mutex<T> {
    pub const fn new(value: T) -> Self {
        Self(::std::sync::Mutex::new(value))
    }

    pub fn lock(&self) -> ::std::sync::LockResult<::std::sync::MutexGuard<'_, T>> {
        self.0.lock()
    }
}

/// Facade-only sequence used for the Rusty B-tree set.  The production type
/// map restores the concrete C++ runtime container spelling.
pub struct BTreeSet<T> {
    values: Vec<T>,
}

impl<T> Default for BTreeSet<T> {
    fn default() -> Self {
        Self { values: Vec::new() }
    }
}

impl<T> BTreeSet<T> {
    pub fn len(&self) -> usize {
        self.values.len()
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    pub fn clear(&mut self) {
        self.values.clear();
    }

    pub fn insert(&mut self, value: T) {
        self.values.push(value);
    }

    pub fn iter(&self) -> ::std::slice::Iter<'_, T> {
        self.values.iter()
    }
}

/// Facade-only associative sequence.  It deliberately requires no ordering or
/// hashing bounds, matching the unconstrained C++ templates emitted from the
/// canonical source.
pub struct BTreeMap<K, V> {
    values: Vec<(K, V)>,
}

impl<K, V> Default for BTreeMap<K, V> {
    fn default() -> Self {
        Self { values: Vec::new() }
    }
}

impl<K, V> BTreeMap<K, V> {
    pub const fn new() -> Self {
        Self { values: Vec::new() }
    }

    pub fn len(&self) -> usize {
        self.values.len()
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    pub fn clear(&mut self) {
        self.values.clear();
    }

    pub fn insert(&mut self, key: K, value: V) {
        self.values.push((key, value));
    }

    pub fn get(&self, key: &K) -> Option<&V>
    where
        K: PartialEq,
    {
        self.values
            .iter()
            .find_map(|(candidate, value)| (candidate == key).then_some(value))
    }

    pub fn iter(&self) -> ::std::slice::Iter<'_, (K, V)> {
        self.values.iter()
    }
}

/// Hash-map facade kept distinct from `BTreeMap` so canonical source can carry
/// both otherwise-overlapping Rust trait implementations.
pub struct HashMap<K, V> {
    values: Vec<(K, V)>,
}

impl<K, V> Default for HashMap<K, V> {
    fn default() -> Self {
        Self { values: Vec::new() }
    }
}

impl<K, V> HashMap<K, V> {
    pub const fn new() -> Self {
        Self { values: Vec::new() }
    }

    pub fn len(&self) -> usize {
        self.values.len()
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    pub fn clear(&mut self) {
        self.values.clear();
    }

    pub fn insert(&mut self, key: K, value: V) {
        self.values.push((key, value));
    }

    pub fn insert_callable<C, R>(&mut self, key: K, callback: C)
    where
        C: Fn() -> R + 'static,
        R: 'static,
        V: FromCallable0<C, R>,
    {
        self.values
            .push((key, <V as FromCallable0<C, R>>::from_callable(callback)));
    }

    pub fn get(&self, key: &K) -> Option<&V>
    where
        K: PartialEq,
    {
        self.values
            .iter()
            .find_map(|(candidate, value)| (candidate == key).then_some(value))
    }

    pub fn iter(&self) -> ::std::slice::Iter<'_, (K, V)> {
        self.values.iter()
    }
}

/// Hash-set facade exposes the `map` field used by the historical encoder.
pub struct HashSet<T> {
    pub map: HashMap<T, ()>,
}

impl<T> Default for HashSet<T> {
    fn default() -> Self {
        Self {
            map: HashMap::new(),
        }
    }
}

impl<T> HashSet<T> {
    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }

    pub fn clear(&mut self) {
        self.map.clear();
    }

    pub fn insert(&mut self, value: T) {
        self.map.insert(value, ());
    }
}

/// Distinct rustc-only model for `std::string_view`.
#[derive(Default)]
pub struct SerializableStdStringView {
    bytes: Vec<u8>,
}

impl SerializableStdStringView {
    pub fn size(&self) -> usize {
        self.bytes.len()
    }

    pub fn data(&self) -> *const u8 {
        self.bytes.as_ptr()
    }
}

/// Distinct rustc-only models keep otherwise-aliasing C++ container impls
/// coherent in Rust.  Each is mapped back to its historical STL spelling.
macro_rules! serializable_std_sequence {
    ($name:ident) => {
        pub struct $name<T> {
            values: Vec<T>,
        }

        impl<T> Default for $name<T> {
            fn default() -> Self {
                Self { values: Vec::new() }
            }
        }

        impl<T> $name<T> {
            pub fn size(&self) -> usize {
                self.values.len()
            }

            pub fn clear(&mut self) {
                self.values.clear();
            }

            pub fn reserve(&mut self, additional: usize) {
                self.values.reserve(additional);
            }

            pub fn push_back(&mut self, value: T) {
                self.values.push(value);
            }

            pub fn insert(&mut self, value: T) {
                self.values.push(value);
            }
        }

        impl<T> ::std::ops::Index<usize> for $name<T> {
            type Output = T;

            fn index(&self, index: usize) -> &Self::Output {
                &self.values[index]
            }
        }

        impl<'a, T> IntoIterator for &'a $name<T> {
            type Item = &'a T;
            type IntoIter = ::std::slice::Iter<'a, T>;

            fn into_iter(self) -> Self::IntoIter {
                self.values.iter()
            }
        }
    };
}

serializable_std_sequence!(SerializableStdList);
serializable_std_sequence!(SerializableStdVector);
serializable_std_sequence!(SerializableStdSet);
serializable_std_sequence!(SerializableStdUnorderedSet);

macro_rules! serializable_std_map {
    ($name:ident) => {
        pub struct $name<K, V> {
            values: Vec<StdPair<K, V>>,
        }

        impl<K, V> Default for $name<K, V> {
            fn default() -> Self {
                Self { values: Vec::new() }
            }
        }

        impl<K, V> $name<K, V> {
            pub fn size(&self) -> usize {
                self.values.len()
            }

            pub fn clear(&mut self) {
                self.values.clear();
            }

            pub fn emplace(&mut self, key: K, value: V) {
                self.values.push(StdPair::new(key, value));
            }
        }

        impl<'a, K, V> IntoIterator for &'a $name<K, V> {
            type Item = &'a StdPair<K, V>;
            type IntoIter = ::std::slice::Iter<'a, StdPair<K, V>>;

            fn into_iter(self) -> Self::IntoIter {
                self.values.iter()
            }
        }
    };
}

serializable_std_map!(SerializableStdMap);
serializable_std_map!(SerializableStdUnorderedMap);

/// Type-only rustc stand-ins for compiler-generated trait adapters.
pub struct RustcSinkBaseAdapterRefMut<T>(PhantomData<T>);
pub struct RustcSourceBaseAdapterRefMut<T>(PhantomData<T>);

/// Opaque rustc-only stand-in mapped to C++ `void` at the Serializable C ABI.
pub enum LegacyCVoid {}

/// The production emitter recognizes this call and emits
/// `rusty::make_box<Adapter>(value)`.  The divergent Rust facade lets the call
/// coerce to the local trait-object return type without pretending to model
/// C++'s generated adapter hierarchy.
pub fn make_box<Adapter>(_value: impl Sized) -> ! {
    let _ = core::marker::PhantomData::<Adapter>;
    panic!("rustc-only make_box facade is not executable")
}

/// Declarations for module-local C++ templates supplied by
/// `misc/serializable_support.hpp`.  They preserve structural C++ dispatch
/// while giving direct rustc a fully typed foreign boundary.
pub mod rusty {
    use crate::{Arc, SerializableProxy};

    /// # Safety
    ///
    /// The C++ associated namespace for `T` must provide a compatible
    /// `serialize(const T&, Archive&)` overload which does not retain either
    /// borrowed argument.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_adl_serialize<T, Archive>(_value: &T, _archive: &mut Archive) {}

    /// # Safety
    ///
    /// The C++ associated namespace for `T` must provide a compatible
    /// `deserialize(T&, Archive&)` overload which does not retain either
    /// borrowed argument.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_adl_deserialize<T, Archive>(_value: &mut T, _archive: &mut Archive) {}

    /// # Safety
    ///
    /// If `_length` is nonzero, `_pointer` must address that many initialized
    /// readable bytes for the duration of the call.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_sink_write<Sink: ?Sized>(
        _sink: &mut Sink,
        _pointer: *const u8,
        _length: usize,
    ) {
    }

    /// # Safety
    ///
    /// If `_length` is nonzero, `_pointer` must address that many writable
    /// bytes for the duration of the call.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_source_read<Source: ?Sized>(
        _source: &mut Source,
        _pointer: *mut u8,
        _length: usize,
    ) -> usize {
        0
    }

    /// Rustc-only coercion into the move-only registry callback facade. The
    /// production runtime helper constructs `rusty::Function<R()>` from the
    /// supplied closure.
    ///
    /// # Safety
    ///
    /// The callable must remain valid after it is moved into the returned C++
    /// function wrapper and must return a well-formed owning proxy.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_factory_from_callable<C>(callback: C) -> crate::SerializableRegistryFactory
    where
        C: Fn() -> SerializableProxy + Send + 'static,
    {
        crate::SerializableRegistryFactory::from_callable(callback)
    }

    /// # Safety
    ///
    /// `T` must implement the structural C++ payload save contract for the
    /// supplied archive and may not retain the archive reference.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_payload_save<T, Archive>(_value: &T, _archive: &mut Archive) {}

    /// # Safety
    ///
    /// `T` must implement the structural C++ payload load contract for the
    /// supplied archive and may not retain the archive reference.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_payload_load<T, Archive>(_value: &mut T, _archive: &mut Archive) {}

    /// # Safety
    ///
    /// `T` must provide the structural C++ `kind() const` payload method.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_payload_kind<T>(_value: &T) -> i32 {
        0
    }

    /// # Safety
    ///
    /// The production C++ `T` must be default constructible and safe to own in
    /// `rusty::Arc<T>`.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_arc_default<T>() -> Arc<T> {
        panic!("rustc-only default Arc construction facade is not executable")
    }

    /// # Safety
    ///
    /// The production C++ `T` must be copy constructible and safe to own in
    /// `rusty::Arc<T>`.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_arc_copy<T>(_value: &T) -> Arc<T> {
        panic!("rustc-only copying Arc construction facade is not executable")
    }

    /// # Safety
    ///
    /// `Holder` must be the module-owned `SerializableBase` holder for `T`,
    /// with a constructor that takes ownership of the supplied Arc.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_holder_proxy<Holder, T>(_value: Arc<T>) -> SerializableProxy {
        let _ = core::marker::PhantomData::<Holder>;
        panic!("rustc-only holder proxy construction facade is not executable")
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
            pub unsafe fn dump64(value: i64, buffer: *mut u8) -> usize {
                unsafe { crate::sparse_dump64(value, buffer) }
            }

            #[allow(unsafe_code)]
            pub unsafe fn load32(buffer: *const u8) -> i32 {
                unsafe { crate::sparse_load32(buffer) }
            }

            #[allow(unsafe_code)]
            pub unsafe fn load64(buffer: *const u8) -> i64 {
                unsafe { crate::sparse_load64(buffer) }
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
        use crate::{REACTOR_CURRENT_FIBER, REACTOR_SLEEP_CALLS, ReactorBoxEvent, ReactorFiber};
        use ::std::cell::Cell;
        use ::std::rc::Rc;
        use ::std::sync::Arc;

        pub type Fiber = ReactorFiber;

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

#[allow(unsafe_code)]
unsafe fn sparse_dump32(value: i32, buffer: *mut u8) -> usize {
    unsafe { sparse_dump64(value as i64, buffer) }
}

#[allow(unsafe_code)]
unsafe fn sparse_load32(buffer: *const u8) -> i32 {
    unsafe { sparse_load_signed(buffer, 4) as i32 }
}

fn sparse_value_size(value: i64) -> usize {
    if (-64..=63).contains(&value) {
        1
    } else if (-8_192..=8_191).contains(&value) {
        2
    } else if (-1_048_576..=1_048_575).contains(&value) {
        3
    } else if (-134_217_728..=134_217_727).contains(&value) {
        4
    } else if (-17_179_869_184..=17_179_869_183).contains(&value) {
        5
    } else if (-2_199_023_255_552..=2_199_023_255_551).contains(&value) {
        6
    } else if (-281_474_976_710_656..=281_474_976_710_655).contains(&value) {
        7
    } else if (-36_028_797_018_963_968..=36_028_797_018_963_967).contains(&value) {
        8
    } else {
        9
    }
}

#[allow(unsafe_code)]
unsafe fn sparse_dump64(value: i64, buffer: *mut u8) -> usize {
    let encoded = value as u64;
    let size = sparse_value_size(value);
    unsafe {
        if size <= 7 {
            for index in 0..size {
                *buffer.add(index) = ((encoded >> (8 * (size - 1 - index))) & 0xff) as u8;
            }
            let prefix = match size {
                1 => 0x00,
                2 => 0x80,
                3 => 0xc0,
                4 => 0xe0,
                5 => 0xf0,
                6 => 0xf8,
                7 => 0xfc,
                _ => unreachable!(),
            };
            *buffer &= 0xff >> size;
            *buffer |= prefix;
            return size;
        }
        for index in 0..8 {
            *buffer.add(index + 1) = ((encoded >> (8 * (7 - index))) & 0xff) as u8;
        }
        *buffer = if size == 8 { 0xfe } else { 0xff };
    }
    size
}

#[allow(unsafe_code)]
unsafe fn sparse_load_signed(buffer: *const u8, width: usize) -> i64 {
    let size = unsafe { sparse_buf_size(*buffer) };
    // The 64-bit marker 0xfe reports eight while still carrying eight payload
    // bytes after the marker. The historical decoder therefore enters this
    // full-width branch for `size == width`, not only `width + 1`.
    let full_width_marker = if width == 8 { width } else { width + 1 };
    if size >= full_width_marker {
        let mut value = 0u64;
        for index in 0..width {
            value |= unsafe { *buffer.add(width - index) as u64 } << (8 * index);
        }
        return value as i64;
    }
    let mut value = 0u64;
    for index in 0..size.saturating_sub(1) {
        value |= unsafe { *buffer.add(size - 1 - index) as u64 } << (8 * index);
    }
    let mut top = unsafe { *buffer } & (0xff >> size);
    let negative = ((top >> (7 - size)) & 1) == 1;
    if negative {
        top |= (0xff << (7 - size)) as u8;
        for index in size..width {
            value |= 0xffu64 << (8 * index);
        }
    }
    value |= (top as u64) << (8 * (size - 1));
    value as i64
}

#[allow(unsafe_code)]
unsafe fn sparse_load64(buffer: *const u8) -> i64 {
    unsafe { sparse_load_signed(buffer, 8) }
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

impl<T: ?Sized> Deref for Arc<T> {
    type Target = T;

    fn deref(&self) -> &Self::Target {
        &self.inner
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
    pub fn from_bytes(bytes: Vec<u8>) -> Self {
        Self { bytes, offset: 0 }
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

    pub fn payload_type_id(&self) -> ::std::any::TypeId {
        ::std::any::TypeId::of::<Self>()
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

/// Conversion used by the serializable registry's rustc-only HashMap facade.
pub trait FromCallable0<C, R> {
    fn from_callable(callback: C) -> Self;
}

impl<C, R> FromCallable0<C, R> for Function<dyn Fn() -> R>
where
    C: Fn() -> R + 'static,
    R: 'static,
{
    fn from_callable(callback: C) -> Self {
        Self {
            inner: Some(Box::new(callback)),
            runtime_layout_padding: [0; 32],
        }
    }
}

impl<R: 'static> Function<dyn Fn() -> R + Send> {
    pub fn from_callable<C>(callback: C) -> Self
    where
        C: Fn() -> R + Send + 'static,
    {
        Self {
            inner: Some(Box::new(callback)),
            runtime_layout_padding: [0; 32],
        }
    }
}

impl<R: 'static> Function<dyn Fn() -> R> {
    /// Erases a const-callable zero-argument callback with a return value.
    pub fn from_callable<C>(callback: C) -> Self
    where
        C: Fn() -> R + 'static,
    {
        <Self as FromCallable0<C, R>>::from_callable(callback)
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
