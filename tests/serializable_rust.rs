use srpc::serializable::{
    BufferSink, BufferSource, Deserialize, Serializable, SerializableRegistry, Serialize, SinkBase,
    SourceBase, make_serializable_proxy_copy, make_serializable_proxy_default,
};
use srpc::basetypes::SparseInt;
use rusty::{SerializableV32, SerializableV64};

fn expected_sparse(value: i64) -> (usize, [u8; 9]) {
    let size = if (-64..=63).contains(&value) {
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
    };
    let mut out = [0u8; 9];
    if size <= 7 {
        let raw = value as u64;
        for (index, byte) in out[..size].iter_mut().enumerate() {
            *byte = (raw >> (8 * (size - 1 - index))) as u8;
        }
        let prefix = [0, 0, 0x80, 0xc0, 0xe0, 0xf0, 0xf8, 0xfc][size];
        out[0] &= 0xff >> size;
        out[0] |= prefix;
    } else {
        out[0] = if size == 8 { 0xfe } else { 0xff };
        out[1..].copy_from_slice(&(value as u64).to_be_bytes());
    }
    (size, out)
}

#[test]
#[allow(unsafe_code)]
fn sparse_facade_matches_independent_wire_oracle() {
    let values = [
        i64::MIN,
        -36_028_797_018_963_969,
        -36_028_797_018_963_968,
        -281_474_976_710_657,
        -281_474_976_710_656,
        -2_199_023_255_553,
        -2_199_023_255_552,
        -17_179_869_185,
        -17_179_869_184,
        -134_217_729,
        -134_217_728,
        -1_048_577,
        -1_048_576,
        -8_193,
        -8_192,
        -65,
        -64,
        -1,
        0,
        1,
        63,
        64,
        8_191,
        8_192,
        1_048_575,
        1_048_576,
        134_217_727,
        134_217_728,
        17_179_869_183,
        17_179_869_184,
        2_199_023_255_551,
        2_199_023_255_552,
        281_474_976_710_655,
        281_474_976_710_656,
        36_028_797_018_963_967,
        36_028_797_018_963_968,
        i64::MAX,
    ];
    for value in values {
        let (expected_size, expected) = expected_sparse(value);
        let mut actual = [0xa5; 10];
        let actual_size = unsafe { SparseInt::dump64(value, actual.as_mut_ptr()) };
        assert_eq!(actual_size, expected_size, "value {value}");
        let written = if expected_size == 8 { 9 } else { expected_size };
        assert_eq!(&actual[..written], &expected[..written], "value {value}");
        assert_eq!(actual[written], 0xa5, "value {value}");
        assert_eq!(SparseInt::buf_size(actual[0]), expected_size);
        assert_eq!(unsafe { SparseInt::load64(actual.as_ptr()) }, value);

        if expected_size == 8 {
            let mut persisted = [0u8; 9];
            persisted[..expected_size].copy_from_slice(&actual[..expected_size]);
            let expected_truncated = (value as u64 & !0xff) as i64;
            assert_eq!(
                unsafe { SparseInt::load64(persisted.as_ptr()) },
                expected_truncated,
                "length-eight archive persistence must retain the legacy lost low byte"
            );
        }

        if let Ok(value32) = i32::try_from(value) {
            let mut actual32 = [0xa5; 6];
            let size32 = unsafe { SparseInt::dump32(value32, actual32.as_mut_ptr()) };
            let (expected32_size, expected32) = expected_sparse(value32 as i64);
            assert_eq!(size32, expected32_size);
            assert_eq!(&actual32[..size32], &expected32[..size32]);
            assert_eq!(actual32[size32], 0xa5);
            assert_eq!(unsafe { SparseInt::load32(actual32.as_ptr()) }, value32);
        }
    }
}

#[test]
#[allow(unsafe_code)]
fn buffer_source_sink_and_sparse_leaf_impls_match_wire_contract() {
    let payload = [0x10, 0x20, 0x30, 0x40, 0x50];
    let mut sink = BufferSink { bytes: vec![1, 2] };
    unsafe { SinkBase::write_bytes(&mut sink, payload.as_ptr(), payload.len()) };
    assert_eq!(sink.bytes, [1, 2, 0x10, 0x20, 0x30, 0x40, 0x50]);
    unsafe { SinkBase::write_bytes(&mut sink, core::ptr::null(), 0) };
    assert_eq!(sink.bytes.len(), 7);

    let mut source = BufferSource::new(payload.as_ptr(), payload.len());
    let mut first = [0u8; 3];
    assert_eq!(
        unsafe { SourceBase::read_bytes(&mut source, first.as_mut_ptr(), 3) },
        3
    );
    assert_eq!(first, [0x10, 0x20, 0x30]);
    assert_eq!(source.pos(), 3);
    assert_eq!(source.remaining(), 2);
    assert!(!source.eof());
    let mut rest = [0u8; 4];
    assert_eq!(
        unsafe { SourceBase::read_bytes(&mut source, rest.as_mut_ptr(), 4) },
        2
    );
    assert_eq!(&rest[..2], &[0x40, 0x50]);
    assert!(source.eof());

    // Pin the trait implementations themselves.
    let mut v32 = SerializableV32::new(-8_193);
    let mut v64 = SerializableV64::new(36_028_797_018_963_968);
    let _serialize32: fn(&SerializableV32, &mut srpc::serializable::BinaryWriteArchive) =
        <SerializableV32 as Serialize>::serialize;
    let _serialize64: fn(&SerializableV64, &mut srpc::serializable::BinaryWriteArchive) =
        <SerializableV64 as Serialize>::serialize;
    let _deserialize32: fn(&mut SerializableV32, &mut srpc::serializable::BinaryReadArchive) =
        <SerializableV32 as Deserialize>::deserialize;
    let _deserialize64: fn(&mut SerializableV64, &mut srpc::serializable::BinaryReadArchive) =
        <SerializableV64 as Deserialize>::deserialize;
    v32.set(v32.get());
    v64.set(v64.get());
}

#[test]
fn serializable_kind_is_exact_and_nonzero() {
    assert_eq!(Serializable::<7>::static_kind(), 7);
    assert_eq!(Serializable::<-9> {}.kind(), -9);
}

#[test]
fn proxy_factories_keep_the_historical_unconstrained_template_shape() {
    struct NeitherDefaultNorClone;

    // These functions are rustc-only panicking facades, so this lane pins
    // monomorphization and signatures without executing them. Production C++
    // retains the historical unconstrained templates: invalid construction is
    // diagnosed only when the body is instantiated.
    let _default: fn() -> rusty::SerializableProxy =
        make_serializable_proxy_default::<NeitherDefaultNorClone>;
    let _copy: fn(&NeitherDefaultNorClone) -> rusty::SerializableProxy =
        make_serializable_proxy_copy::<NeitherDefaultNorClone>;
    let _register: fn(i32) -> i32 = SerializableRegistry::reg::<NeitherDefaultNorClone>;
}

#[test]
#[should_panic(expected = "Serializable kind 0 is reserved")]
fn serializable_kind_zero_is_rejected() {
    let _ = Serializable::<0>::static_kind();
}

// The proxies used to be unreachable in the Rust lane: `rusty::make_box` was a
// diverging stub, so every `make_*_proxy_*` panicked.  Even once that was fixed,
// `BinaryWriteArchive` dispatched through `rusty::srpc_sink_write`, whose facade
// body was empty -- so writes were silently discarded.  These three tests pin
// both halves, and they assert against the ORIGINAL sink/source rather than the
// proxy, so a proxy that copied its target instead of borrowing it would fail.

#[test]
#[allow(unsafe_code)]
fn sink_proxy_forwards_writes_to_the_pointed_to_buffer_sink() {
    let mut sink = BufferSink { bytes: Vec::new() };
    let first = [0xAAu8, 0xBB];
    let second = [0xCCu8];
    {
        // SAFETY: `sink` outlives the proxy and is not moved while borrowed.
        let mut proxy = unsafe { srpc::serializable::make_sink_proxy_buffer(&raw mut sink) };
        // SAFETY: both slices are live for the duration of each call.
        unsafe {
            proxy.write_bytes(first.as_ptr(), first.len());
            proxy.write_bytes(second.as_ptr(), second.len());
        }
    }
    // Two writes through one proxy, observed on the caller's own sink: this is
    // what fails if the proxy ever holds a copy.
    assert_eq!(sink.bytes, vec![0xAA, 0xBB, 0xCC]);
}

#[test]
#[allow(unsafe_code)]
fn source_proxy_forwards_reads_and_advances_the_pointed_to_cursor() {
    let data = [1u8, 2, 3, 4, 5];
    let mut source = BufferSource::new(data.as_ptr(), data.len());
    let mut out = [0u8; 5];
    let read = {
        // SAFETY: `source` and `data` both outlive the proxy.
        let mut proxy = unsafe { srpc::serializable::make_source_proxy_buffer(&raw mut source) };
        // SAFETY: `out` is live and large enough for the requested length.
        unsafe { proxy.read_bytes(out.as_mut_ptr(), out.len()) }
    };
    assert_eq!(read, 5);
    assert_eq!(out, data);
    // The cursor moved on the caller's source, not on a copy.
    assert_eq!(source.pos(), 5);
    assert!(source.eof());
}

#[test]
#[allow(unsafe_code)]
fn archive_round_trips_a_leaf_through_both_proxies() {
    let mut sink = BufferSink { bytes: Vec::new() };
    {
        // SAFETY: `sink` outlives the archive that borrows it.
        let mut archive = srpc::serializable::BinaryWriteArchive {
            sink_: unsafe { srpc::serializable::make_sink_proxy_buffer(&raw mut sink) },
        };
        <SerializableV64 as Serialize>::serialize(&SerializableV64::new(-9_001), &mut archive);
        <SerializableV32 as Serialize>::serialize(&SerializableV32::new(77), &mut archive);
    }
    // If `srpc_sink_write` were still the empty facade stub, this would be 0.
    assert!(!sink.bytes.is_empty());

    let encoded = sink.bytes.clone();
    let mut source = BufferSource::new(encoded.as_ptr(), encoded.len());
    let (v64, v32) = {
        // SAFETY: `source` and `encoded` both outlive the archive.
        let mut archive = srpc::serializable::BinaryReadArchive {
            source_: unsafe { srpc::serializable::make_source_proxy_buffer(&raw mut source) },
        };
        let mut v64 = SerializableV64::new(0);
        let mut v32 = SerializableV32::new(0);
        <SerializableV64 as Deserialize>::deserialize(&mut v64, &mut archive);
        <SerializableV32 as Deserialize>::deserialize(&mut v32, &mut archive);
        (v64, v32)
    };
    assert_eq!(v64.get(), -9_001);
    assert_eq!(v32.get(), 77);
    assert!(source.eof());
}

// The wire sites in rpc/client.rs and rpc/server.rs serialize their headers
// through the FACADE route `cpp_serializable::Serialize_::serialize` -- which
// emits the same qualified `::srpc::Serialize_::serialize` call the C++ lane
// always made, and under rustc dispatches through the `RustcAdlSerialize`
// bound (srpc's blanket impl over `T: Serialize`).  Until this change the
// facade's `Serialize_` accepted only `String`, so no wire header could
// serialize at all.  This test pins the facade route over the CANONICAL
// archives; the earlier round-trip test calls the trait methods directly and
// would stay green if this path rotted again.
#[test]
#[allow(unsafe_code)]
fn adl_dispatchers_round_trip_leaves_through_the_facade_bridge() {
    let mut sink = BufferSink { bytes: Vec::new() };
    {
        let mut ar = srpc::serializable::BinaryWriteArchive {
            // SAFETY: `sink` outlives the archive that borrows it.
            sink_: unsafe { srpc::serializable::make_sink_proxy_buffer(&raw mut sink) },
        };
        // SAFETY (all six facade calls below): foreign named-module boundary;
        // both borrows are held only for the duration of each call.
        unsafe {
            rusty::srpc::serializable::Serialize_::serialize(&SerializableV64::new(-77_000), &mut ar);
            rusty::srpc::serializable::Serialize_::serialize(&123_456_789_i64, &mut ar);
            rusty::srpc::serializable::Serialize_::serialize(&SerializableV32::new(63), &mut ar);
        }
    }
    assert!(!sink.bytes.is_empty());

    let encoded = sink.bytes.clone();
    let mut source = BufferSource::new(encoded.as_ptr(), encoded.len());
    {
        let mut ar = srpc::serializable::BinaryReadArchive {
            // SAFETY: `source` and `encoded` both outlive the archive.
            source_: unsafe { srpc::serializable::make_source_proxy_buffer(&raw mut source) },
        };
        let mut v64 = SerializableV64::new(0);
        let mut plain = 0i64;
        let mut v32 = SerializableV32::new(0);
        // SAFETY: same facade-boundary contract as the write side.
        unsafe {
            rusty::srpc::serializable::Deserialize_::deserialize(&mut v64, &mut ar);
            rusty::srpc::serializable::Deserialize_::deserialize(&mut plain, &mut ar);
            rusty::srpc::serializable::Deserialize_::deserialize(&mut v32, &mut ar);
        }
        assert_eq!(v64.get(), -77_000);
        assert_eq!(plain, 123_456_789);
        assert_eq!(v32.get(), 63);
    }
    assert!(source.eof());
}

// The canonical GENERIC dispatch chain (`Serialize_::serialize` and its
// Deserialize twin) has no Rust body: it is the C++ open-set ADL path, and a
// `T: Serialize` bound on it would cascade into the generic container impls,
// which the transpiler cannot lower (it degrades a constrained generic impl to
// hand slots).  Everything that reaches it under rustc -- container
// serialization first among them -- panics via `rusty::srpc_adl_serialize`'s
// `unimplemented!`.  These pin that the failure is LOUD -- the alternative, an
// empty stub, silently wrote nothing for years.
#[test]
#[allow(unsafe_code)]
#[should_panic(expected = "generic deserialization dispatch (Deserialize_::deserialize) has no")]
fn container_element_read_dispatch_fails_loudly_under_rustc() {
    let encoded = [2u8, 0, 0]; // v64 length prefix of 2, then would-be elements
    let mut source = BufferSource::new(encoded.as_ptr(), encoded.len());
    let mut ar = srpc::serializable::BinaryReadArchive {
        // SAFETY: `source` and `encoded` outlive the archive.
        source_: unsafe { srpc::serializable::make_source_proxy_buffer(&raw mut source) },
    };
    let mut values: Vec<i64> = Vec::new();
    srpc::serializable::Deserialize::deserialize(&mut values, &mut ar);
}

#[test]
#[allow(unsafe_code)]
#[should_panic(expected = "generic serialization dispatch (Serialize_::serialize) has no")]
fn container_element_dispatch_fails_loudly_under_rustc() {
    let mut sink = BufferSink { bytes: Vec::new() };
    let mut ar = srpc::serializable::BinaryWriteArchive {
        // SAFETY: `sink` outlives the archive that borrows it.
        sink_: unsafe { srpc::serializable::make_sink_proxy_buffer(&raw mut sink) },
    };
    let values: Vec<i64> = vec![1, 2, 3];
    srpc::serializable::Serialize::serialize(&values, &mut ar);
}
