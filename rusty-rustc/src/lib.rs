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

/// Rustc-only model of `rusty::thread`.
///
/// The production facade (`third-party/rusty-cpp/include/rusty/thread.hpp`)
/// declares `auto spawn(F&& func, Args&&... args)` -- variadic -- and its
/// `JoinHandle<T>` carries BOTH `join()` and `detach()`.  Rust has no variadic
/// functions, so this model is the single-callable form, and every canonical
/// caller spells that form.
///
/// It stays a model rather than a re-export of `std::thread`, for two measured
/// reasons. First, `spawn` must abort on a panicking body: the runtime's
/// `run_into_state` (thread.hpp) runs the body with no try/catch, so an
/// exception escaping a spawned thread reaches `std::thread` and terminates
/// the process, and the `catch_unwind(..).unwrap_or_else(abort)` below gives
/// rustc the same semantics. `std::thread::spawn` would capture the panic into
/// the handle instead; a detached client thread's panic would then die
/// silently while a test awaiting it hangs. Second, `ThreadId` needs a zero
/// niche: reactor/reactor.rs round-trips thread ids through `u64` with
/// `transmute` (`u64_to_thread_id` / `thread_id_to_u64`) and an unset id is
/// the bit pattern `0`; std's `ThreadId` wraps a `NonZero<u64>`, so
/// transmuting `0` into it is undefined behaviour, whereas the
/// `Option<std::thread::ThreadId>` below makes `0` a sound `None`.
///
/// `spawn` runs the body on a standard Rust thread and requires `Send` captures.
/// Canonical Future and ClientConnection synchronize shared state with mutexes
/// and atomics, and callback aliases require the appropriate thread bounds.
/// Their thread safety is checked through Rust auto traits.
pub mod thread {
    #[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
    #[repr(transparent)]
    pub struct ThreadId(Option<::std::thread::ThreadId>);

    /// The runtime's spawned-thread handle: a real `std::thread::JoinHandle`.
    ///
    /// Generic in the body's result so `JoinHandle<()>` -- the payload of the
    /// reactor's `PollJoinSlot` -- names the same type production `spawn`
    /// deduces for a void body.  Dropping the handle detaches, exactly like
    /// the C++ runtime's. An uncaught panic aborts at the thread boundary,
    /// matching an uncaught exception in the native C++ thread.
    pub struct JoinHandle<T>(Option<::std::thread::JoinHandle<T>>);

    impl<T> JoinHandle<T> {
        pub fn join(mut self) {
            if let Some(handle) = self.0.take() {
                handle.join().unwrap_or_else(|_| ::std::process::abort());
            }
        }
        pub fn detach(mut self) {
            self.0.take();
        }
    }

    pub fn current_id() -> ThreadId {
        ThreadId(Some(::std::thread::current().id()))
    }

    /// Production C++ resolves this to the runtime's thread spawn.
    pub fn spawn<F, R>(body: F) -> JoinHandle<R>
    where
        F: FnOnce() -> R + Send + 'static,
        R: Send + 'static,
    {
        JoinHandle(Some(::std::thread::spawn(move || {
            ::std::panic::catch_unwind(::std::panic::AssertUnwindSafe(body))
                .unwrap_or_else(|_| ::std::process::abort())
        })))
    }
}

mod task;
pub use task::{Context, Poll, Task, Waker};

/// Native x86-64 fiber register layout, shared with the C/assembly engine.
#[cfg(target_arch = "x86_64")]
#[repr(C)]
pub struct ReactorFiberContext {
    pub rsp: *mut core::ffi::c_void,
    pub rip: *mut core::ffi::c_void,
    pub rbx: usize,
    pub rbp: usize,
    pub r12: usize,
    pub r13: usize,
    pub r14: usize,
    pub r15: usize,
}

/// Native AArch64 register layout from reactor/srpc_fiber.h.
#[cfg(target_arch = "aarch64")]
#[repr(C)]
pub struct ReactorFiberContext {
    pub sp: *mut core::ffi::c_void,
    pub pc: *mut core::ffi::c_void,
    pub x19: usize,
    pub x20: usize,
    pub x21: usize,
    pub x22: usize,
    pub x23: usize,
    pub x24: usize,
    pub x25: usize,
    pub x26: usize,
    pub x27: usize,
    pub x28: usize,
    pub fp: usize,
}

/// Rustc-side layout model for `::srpc_fiber` from `reactor/srpc_fiber.h`.
#[repr(C)]
pub struct ReactorFiberState {
    pub caller_ctx: ReactorFiberContext,
    pub fiber_ctx: ReactorFiberContext,
    pub stack_mapping: *mut core::ffi::c_void,
    pub stack_mapping_bytes: usize,
    pub state: i32,
    pub entry_fn: Option<unsafe extern "C" fn(*mut core::ffi::c_void)>,
    pub entry_arg: *mut core::ffi::c_void,
}

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


pub type RustcSocketAddrV4 = ::std::net::SocketAddrV4;
pub type RustcIoErrorKind = ::std::io::ErrorKind;

#[derive(Debug)]
pub struct RustcIoError {
    inner: ::std::io::Error,
}

impl RustcIoError {
    pub fn kind(&self) -> RustcIoErrorKind {
        self.inner.kind()
    }

    pub fn what(&self) -> String {
        self.inner.to_string()
    }
}

impl From<::std::io::Error> for RustcIoError {
    fn from(inner: ::std::io::Error) -> Self {
        Self { inner }
    }
}

#[derive(Debug)]
pub struct RustcTcpStream {
    inner: ::std::net::TcpStream,
}

impl RustcTcpStream {
    pub fn set_nonblocking(&self, value: bool) -> Result<(), RustcIoError> {
        self.inner.set_nonblocking(value).map_err(Into::into)
    }

    pub fn into_owned_fd(self) -> ::std::os::fd::OwnedFd {
        self.inner.into()
    }
}

#[derive(Debug, Default)]
pub struct RustcTcpListener {
    inner: Option<::std::net::TcpListener>,
}

/// Rustc-only borrowed descriptor view for a possibly-unbound TCP listener.
///
/// Production `rusty::net::TcpListener` stores an invalid/default
/// `rusty::os::fd::OwnedFd`, whose borrowed view reports `-1`. Rust's standard
/// `BorrowedFd` cannot represent that state, so the facade uses this tiny view
/// to preserve the production API's pre-bind behavior.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RustcBorrowedFd {
    raw: i32,
}

impl RustcBorrowedFd {
    pub fn as_raw_fd(&self) -> i32 {
        self.raw
    }
}

impl RustcTcpListener {
    pub fn bind(address: RustcSocketAddrV4) -> Result<Self, RustcIoError> {
        ::std::net::TcpListener::bind(address)
            .map(|inner| Self { inner: Some(inner) })
            .map_err(Into::into)
    }

    pub fn set_nonblocking(&self, value: bool) -> Result<(), RustcIoError> {
        self.inner
            .as_ref()
            .ok_or_else(|| RustcIoError::from(::std::io::Error::new(
                ::std::io::ErrorKind::InvalidInput, "listener not bound",
            )))?
            .set_nonblocking(value)
            .map_err(Into::into)
    }

    pub fn local_addr(&self) -> Result<RustcSocketAddrV4, RustcIoError> {
        let address = self
            .inner
            .as_ref()
            .ok_or_else(|| RustcIoError::from(::std::io::Error::new(
                ::std::io::ErrorKind::InvalidInput, "listener not bound",
            )))?
            .local_addr()
            .map_err(RustcIoError::from)?;
        match address {
            ::std::net::SocketAddr::V4(address) => Ok(address),
            _ => Err(RustcIoError::from(::std::io::Error::new(
                ::std::io::ErrorKind::InvalidInput,
                "not IPv4",
            ))),
        }
    }

    pub fn accept(&self) -> Result<(RustcTcpStream, RustcSocketAddrV4), RustcIoError> {
        let (stream, address) = self
            .inner
            .as_ref()
            .ok_or_else(|| RustcIoError::from(::std::io::Error::new(
                ::std::io::ErrorKind::InvalidInput, "listener not bound",
            )))?
            .accept()
            .map_err(RustcIoError::from)?;
        match address {
            ::std::net::SocketAddr::V4(address) => Ok((RustcTcpStream { inner: stream }, address)),
            _ => Err(RustcIoError::from(::std::io::Error::new(
                ::std::io::ErrorKind::InvalidInput,
                "not IPv4",
            ))),
        }
    }

    pub fn is_bound(&self) -> bool {
        self.inner.is_some()
    }

    pub fn as_raw_fd(&self) -> i32 {
        use ::std::os::fd::AsRawFd;
        self.inner.as_ref().map_or(-1, AsRawFd::as_raw_fd)
    }

    pub fn as_owned_fd(&self) -> RustcBorrowedFd {
        RustcBorrowedFd {
            raw: self.as_raw_fd(),
        }
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

/// Rust-only facade spelling mapped to the public `std::string` ABI. It is the
/// impl target that keeps the generated `Serialize_`/`Deserialize_` overloads
/// for C++ consumers' own `std::string` fields; see `std::string` below.
pub type SerializableStdString = std::string;

/// Opaque rustc-only model mapped to libc's `FILE` in generated C++.
#[repr(C)]
pub struct CFile {
    _opaque: [u8; 0],
}

/// Rust-only spelling for exact `std::vector<T>` ABI mappings.
pub type StdVector<T> = Vec<T>;

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

/// Distinct adapters preserve STL container identity during Rust checking.
pub struct SerializableStdVector<T> {
    values: Vec<T>,
}
impl<T> Default for SerializableStdVector<T> {
    fn default() -> Self { Self { values: Vec::new() } }
}
impl<T> SerializableStdVector<T> {
    pub fn size(&self) -> usize { self.values.len() }
    pub fn clear(&mut self) { self.values.clear(); }
    pub fn reserve(&mut self, additional: usize) { self.values.reserve(additional); }
    pub fn push_back(&mut self, value: T) { self.values.push(value); }
}
impl<T> ::std::ops::Index<usize> for SerializableStdVector<T> {
    type Output = T;
    fn index(&self, index: usize) -> &T { &self.values[index] }
}
impl<'a, T> IntoIterator for &'a SerializableStdVector<T> {
    type Item = &'a T;
    type IntoIter = ::std::slice::Iter<'a, T>;
    fn into_iter(self) -> Self::IntoIter { self.values.iter() }
}

pub struct SerializableStdList<T> {
    values: ::std::collections::LinkedList<T>,
}
impl<T> Default for SerializableStdList<T> {
    fn default() -> Self { Self { values: Default::default() } }
}
impl<T> SerializableStdList<T> {
    pub fn size(&self) -> usize { self.values.len() }
    pub fn clear(&mut self) { self.values.clear(); }
    pub fn push_back(&mut self, value: T) { self.values.push_back(value); }
}
impl<'a, T> IntoIterator for &'a SerializableStdList<T> {
    type Item = &'a T;
    type IntoIter = ::std::collections::linked_list::Iter<'a, T>;
    fn into_iter(self) -> Self::IntoIter { self.values.iter() }
}

pub struct SerializableStdSet<T> {
    values: ::std::collections::BTreeSet<T>,
}
impl<T> Default for SerializableStdSet<T> {
    fn default() -> Self { Self { values: Default::default() } }
}
impl<T> SerializableStdSet<T> {
    pub fn size(&self) -> usize { self.values.len() }
    pub fn clear(&mut self) { self.values.clear(); }
}
impl<T: Ord> SerializableStdSet<T> {
    pub fn insert(&mut self, value: T) { self.values.insert(value); }
}
impl<'a, T> IntoIterator for &'a SerializableStdSet<T> {
    type Item = &'a T;
    type IntoIter = ::std::collections::btree_set::Iter<'a, T>;
    fn into_iter(self) -> Self::IntoIter { self.values.iter() }
}

pub struct SerializableStdUnorderedSet<T> {
    values: ::std::collections::HashSet<T>,
}
impl<T> Default for SerializableStdUnorderedSet<T> {
    fn default() -> Self { Self { values: Default::default() } }
}
impl<T> SerializableStdUnorderedSet<T> {
    pub fn size(&self) -> usize { self.values.len() }
    pub fn clear(&mut self) { self.values.clear(); }
}
impl<T: Eq + ::std::hash::Hash> SerializableStdUnorderedSet<T> {
    pub fn insert(&mut self, value: T) { self.values.insert(value); }
}
impl<'a, T> IntoIterator for &'a SerializableStdUnorderedSet<T> {
    type Item = &'a T;
    type IntoIter = ::std::collections::hash_set::Iter<'a, T>;
    fn into_iter(self) -> Self::IntoIter { self.values.iter() }
}

fn borrowed_std_pair<'a, K, V>((key, value): (&'a K, &'a V)) -> StdPair<&'a K, &'a V> {
    StdPair::new(key, value)
}

pub struct SerializableStdMap<K, V> {
    values: ::std::collections::BTreeMap<K, V>,
}
impl<K, V> Default for SerializableStdMap<K, V> {
    fn default() -> Self { Self { values: Default::default() } }
}
impl<K, V> SerializableStdMap<K, V> {
    pub fn size(&self) -> usize { self.values.len() }
    pub fn clear(&mut self) { self.values.clear(); }
}
impl<K: Ord, V> SerializableStdMap<K, V> {
    pub fn emplace(&mut self, key: K, value: V) { self.values.entry(key).or_insert(value); }
}
impl<'a, K, V> IntoIterator for &'a SerializableStdMap<K, V> {
    type Item = StdPair<&'a K, &'a V>;
    type IntoIter = ::std::iter::Map<
        ::std::collections::btree_map::Iter<'a, K, V>,
        fn((&'a K, &'a V)) -> StdPair<&'a K, &'a V>,
    >;
    fn into_iter(self) -> Self::IntoIter { self.values.iter().map(borrowed_std_pair::<K, V>) }
}

pub struct SerializableStdUnorderedMap<K, V> {
    values: ::std::collections::HashMap<K, V>,
}
impl<K, V> Default for SerializableStdUnorderedMap<K, V> {
    fn default() -> Self { Self { values: Default::default() } }
}
impl<K, V> SerializableStdUnorderedMap<K, V> {
    pub fn size(&self) -> usize { self.values.len() }
    pub fn clear(&mut self) { self.values.clear(); }
}
impl<K: Eq + ::std::hash::Hash, V> SerializableStdUnorderedMap<K, V> {
    pub fn emplace(&mut self, key: K, value: V) { self.values.entry(key).or_insert(value); }
}
impl<'a, K, V> IntoIterator for &'a SerializableStdUnorderedMap<K, V> {
    type Item = StdPair<&'a K, &'a V>;
    type IntoIter = ::std::iter::Map<
        ::std::collections::hash_map::Iter<'a, K, V>,
        fn((&'a K, &'a V)) -> StdPair<&'a K, &'a V>,
    >;
    fn into_iter(self) -> Self::IntoIter { self.values.iter().map(borrowed_std_pair::<K, V>) }
}

/// Rustc stand-ins for the compiler-generated trait adapters.
///
/// `rust-type-map.toml` pins these NAMES to the C++ spellings
/// `SinkBaseAdapterRefMut` / `SourceBaseAdapterRefMut`, so the emitted turbofish
/// is fixed no matter what the Rust side means by them.  That frees the Rust
/// meaning to be the honest one: a C++ `SinkBaseAdapterRefMut<T>` holds exactly
/// one `T&`, so the Rust model is the raw pointer itself.  Being an alias rather
/// than a struct is what lets `misc/serializable.rs` implement `SinkBase` for
/// `*mut BufferSink` -- an impl the emitter lowers to nothing, because its self
/// type is a pointer rather than a nominal type.
pub type RustcSinkBaseAdapterRefMut<T> = *mut T;
pub type RustcSourceBaseAdapterRefMut<T> = *mut T;

/// Callable surface for the erased sink and source the archive layer writes
/// through.
///
/// `srpc_sink_write` / `srpc_source_read` below stand in for C++ helpers whose
/// parameter is unconstrained, so their Rust signatures take `?Sized` -- and an
/// unbounded type parameter has no callable surface, which is why both were
/// empty stubs that silently dropped every byte.  These traits are the bound
/// that gives them one.  They are declared here because only `srpc` can
/// implement them: coherence puts the impl next to the trait it forwards to.
pub trait RustcSinkDyn {
    /// # Safety
    ///
    /// `pointer` must address `length` readable bytes for the call.
    #[allow(unsafe_code)]
    unsafe fn rustc_sink_write(&mut self, pointer: *const u8, length: usize);
}

/// Rust bound for the poison-scoped ADL bridges `srpc_adl_serialize` /
/// `srpc_adl_deserialize` below.  In C++ those are open-set ADL calls, so
/// their Rust signatures historically left both parameters unconstrained --
/// and an unbounded type parameter has no callable surface, which is why both
/// were empty stubs that silently discarded every value.  The archive is
/// `Self` here rather than the value: an `impl<T: Serialize> ... for T`
/// blanket in `srpc` would leave `T` uncovered and violate the orphan rule,
/// while the archive is a type `srpc` owns.
pub trait RustcAdlSerialize<T: ?Sized> {
    /// # Safety
    ///
    /// Both borrows are held only for the duration of the call.
    #[allow(unsafe_code)]
    unsafe fn rustc_adl_serialize(&mut self, value: &T);
}

pub trait RustcAdlDeserialize<T: ?Sized> {
    /// # Safety
    ///
    /// Both borrows are held only for the duration of the call.
    #[allow(unsafe_code)]
    unsafe fn rustc_adl_deserialize(&mut self, value: &mut T);
}

pub trait RustcSourceDyn {
    /// # Safety
    ///
    /// `pointer` must address `length` writable bytes for the call.
    #[allow(unsafe_code)]
    unsafe fn rustc_source_read(&mut self, pointer: *mut u8, length: usize) -> usize;
}

/// Opaque rustc-only stand-in mapped to C++ `void` at the Serializable C ABI.
pub enum LegacyCVoid {}

/// The production emitter recognizes this call and emits
/// `rusty::make_box<Adapter>(value)`.  The divergent Rust facade lets the call
/// coerce to the local trait-object return type without pretending to model
/// C++'s generated adapter hierarchy. It is an emitter contract (the
/// transpiler's `make_box` coercion path), not a wrapper over `Box::new`, so
/// it is not a candidate for the std spelling.
pub fn make_box<Adapter>(value: Adapter) -> Box<Adapter> {
    Box::new(value)
}

/// Declarations for module-local C++ templates supplied by
/// `misc/serializable_support.hpp`.  They preserve structural C++ dispatch
/// while giving direct rustc a fully typed foreign boundary.
pub mod rusty {


    pub mod io {
        pub use ::std::io::Error;
    }

    pub mod net {
        pub use crate::{
            RustcIoError as Error, RustcSocketAddrV4 as SocketAddrV4,
            RustcTcpListener as TcpListener, RustcTcpStream as TcpStream,
        };

        pub fn socket_addr_v4_from_str(value: &str) -> Result<SocketAddrV4, Error> {
            value.parse::<SocketAddrV4>().map_err(|error| {
                Error::from(::std::io::Error::new(
                    ::std::io::ErrorKind::InvalidInput,
                    error.to_string(),
                ))
            })
        }

        pub fn socket_addr_v4_to_string(value: SocketAddrV4) -> String {
            value.to_string()
        }

        #[repr(C)]
        pub struct InAddr {
            pub s_addr: u32,
        }

        #[repr(C)]
        pub struct SockAddrIn {
            pub sin_addr: InAddr,
            pub sin_port: u16,
        }

        pub fn sockaddr_in_from_socket_addr_v4(value: SocketAddrV4) -> SockAddrIn {
            let octets = value.ip().octets();
            SockAddrIn {
                sin_addr: InAddr {
                    // Mirror the C++ runtime (rusty/net/tcp.hpp): assemble the
                    // HOST-order word from the octets, then htonl.  The old
                    // `from_ne_bytes(octets).to_be()` double-swapped on
                    // little-endian -- the octets are already network order in
                    // memory -- so every connect went to the byte-reversed
                    // address (127.0.0.1 became 1.0.0.127) and timed out.
                    s_addr: u32::from_be_bytes(octets).to_be(),
                },
                sin_port: value.port().to_be(),
            }
        }
    }

    /// # Safety
    ///
    /// The C++ associated namespace for `T` must provide a compatible
    /// `serialize(const T&, Archive&)` overload which does not retain either
    /// borrowed argument.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_adl_serialize<T: ?Sized, Archive: crate::RustcAdlSerialize<T> + ?Sized>(
        value: &T,
        archive: &mut Archive,
    ) {
        unsafe { archive.rustc_adl_serialize(value) }
    }

    /// # Safety
    ///
    /// The C++ associated namespace for `T` must provide a compatible
    /// `deserialize(T&, Archive&)` overload which does not retain either
    /// borrowed argument.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_adl_deserialize<T: ?Sized, Archive: crate::RustcAdlDeserialize<T> + ?Sized>(
        value: &mut T,
        archive: &mut Archive,
    ) {
        unsafe { archive.rustc_adl_deserialize(value) }
    }

    /// # Safety
    ///
    /// If `_length` is nonzero, `_pointer` must address that many initialized
    /// readable bytes for the duration of the call.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_sink_write<Sink: crate::RustcSinkDyn + ?Sized>(
        sink: &mut Sink,
        pointer: *const u8,
        length: usize,
    ) {
        // SAFETY: the caller's contract is forwarded unchanged to the impl.
        unsafe { sink.rustc_sink_write(pointer, length) }
    }

    /// # Safety
    ///
    /// If `_length` is nonzero, `_pointer` must address that many writable
    /// bytes for the duration of the call.
    #[allow(unsafe_code)]
    pub unsafe fn srpc_source_read<Source: crate::RustcSourceDyn + ?Sized>(
        source: &mut Source,
        pointer: *mut u8,
        length: usize,
    ) -> usize {
        // SAFETY: the caller's contract is forwarded unchanged to the impl.
        unsafe { source.rustc_source_read(pointer, length) }
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
    use ::std::cell::UnsafeCell;
    use ::std::io::Write as _;

    /// Rustc-only model of C++ `std::string`, kept for exactly one purpose: it
    /// is the type the canonical `Serialize`/`Deserialize` impls target, so the
    /// generated C++ keeps its `std::string` wire overloads for consumers' own
    /// fields (rpcgen-generated services declare `std::string`). Canonical Rust
    /// never builds or reads one: it spells strings as `String`, which the
    /// transpiler lowers to `rusty::String`. The API is therefore exactly what
    /// the two wire impls call -- `size`, `resize`, `data`.
    ///
    /// `UnsafeCell` makes this type `!Sync`, matching the shared-access contract
    /// of `data()`; shared access from multiple threads requires an external
    /// lock, moving ownership between threads is allowed.
    ///
    /// ```compile_fail
    /// fn require_sync<T: Sync>() {}
    /// require_sync::<rusty::std::string>();
    /// ```
    #[allow(non_camel_case_types)]
    pub struct string(UnsafeCell<Vec<u8>>);

    impl Default for string {
        fn default() -> Self {
            Self(UnsafeCell::new(Vec::new()))
        }
    }

    impl string {
        pub fn resize(&mut self, size: usize) {
            self.0.get_mut().resize(size, 0);
        }

        /// # Safety
        ///
        /// The returned pointer addresses `size()` initialized bytes. Its use
        /// must not overlap another access to the byte storage, and must end
        /// before any safe method mutates the string or the value is dropped.
        /// Writes must stay within the existing length; they cannot change
        /// the allocation or the vector's length and capacity.
        #[allow(unsafe_code)]
        pub unsafe fn data(&self) -> *mut i8 {
            unsafe { (&mut *self.0.get()).as_mut_ptr().cast() }
        }

        #[allow(unsafe_code)]
        pub fn size(&self) -> usize {
            // SAFETY: safe byte mutations require &mut self, !Sync prevents
            // concurrent shared access, and data() forbids overlapping access.
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
