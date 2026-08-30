use srpc::utils::{find_open_port, get_host_name, AddrInfo};
use std::mem::{align_of, size_of};
use std::sync::atomic::{AtomicI32, Ordering};

static NEXT_PORT: AtomicI32 = AtomicI32::new(0);

#[allow(unsafe_code)]
#[unsafe(no_mangle)]
pub extern "C" fn srpc_find_open_port() -> i32 {
    NEXT_PORT.load(Ordering::Relaxed)
}

// `srpc::logging::log_line` now runs for real here, so its two plain-C seam
// helpers must be supplied by this test binary: there is no build.rs, so
// nothing links `base/srpc_base.c` or `misc/srpc_timing.c` into the Rust lane.
#[allow(unsafe_code)]
#[allow(clippy::missing_safety_doc)]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn srpc_path_basename(path: *const i8) -> *const i8 {
    if path.is_null() {
        return core::ptr::null();
    }
    let mut last = path;
    let mut cursor = path;
    // SAFETY: the caller passes a NUL-terminated path, as the C kernel requires.
    while unsafe { *cursor } != 0 {
        // SAFETY: `cursor` still points inside that same NUL-terminated string.
        if unsafe { *cursor } == b'/' as i8 {
            // SAFETY: one past a non-NUL byte is still inside the string.
            last = unsafe { cursor.add(1) };
        }
        // SAFETY: same bound as the loop condition.
        cursor = unsafe { cursor.add(1) };
    }
    last
}

#[allow(unsafe_code)]
#[allow(clippy::missing_safety_doc)]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn srpc_time_now_str(now: *mut i8) {
    // `log_time_now` resizes to 24 bytes and keeps the first 23, so this oracle
    // writes exactly the historical 23-character stamp plus its NUL.
    let stamp = b"2026-01-01 00:00:00.000\0";
    // SAFETY: the canonical caller sized the destination to 24 bytes.
    unsafe { core::ptr::copy_nonoverlapping(stamp.as_ptr().cast::<i8>(), now, stamp.len()) };
}

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

#[test]
#[allow(unsafe_code)]
fn addrinfo_layout_empty_state_and_rust_traits_are_pinned() {
    assert_eq!(size_of::<AddrInfo>(), 16);
    assert_eq!(align_of::<AddrInfo>(), 8);
    let empty = AddrInfo::new();
    assert!(!empty.valid());
    assert!(empty.get().is_null());

    // SAFETY: null is explicitly permitted by `adopt`; Drop performs no free.
    let adopted_null = unsafe { AddrInfo::adopt(core::ptr::null_mut()) };
    assert!(!adopted_null.valid());

    assert_not_auto_trait!(AddrInfo, Send);
    assert_not_auto_trait!(AddrInfo, Sync);
}

#[test]
fn port_contract_and_hostname_facade_are_rustc_exercised() {
    NEXT_PORT.store(43_210, Ordering::Relaxed);
    assert_eq!(find_open_port(), 43_210);

    NEXT_PORT.store(0, Ordering::Relaxed);
    assert_eq!(find_open_port(), -1);
    NEXT_PORT.store(-7, Ordering::Relaxed);
    assert_eq!(find_open_port(), -1);

    // The direct-rustc facade may return an empty name when the environment
    // lacks HOSTNAME. The generated C++ runtime lane separately pins the real
    // gethostname success and failure behavior.
    let _ = get_host_name();
}
