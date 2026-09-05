// Tier 1.1 of docs/testing-plan.md: property-based round-trip over the wire
// codecs.  `decode(encode(x)) == x` across the whole input space, with
// shrinking, which is exactly the test class that would have caught the
// SparseInt length-8 defect the moment it shipped instead of leaving it as a
// hand-pinned curiosity in basetypes_rust.rs.
//
// Two codecs are exercised:
//   * SparseInt v32/v64 -- the signed varint under `v32`/`v64` wire fields;
//   * the 4-byte frame header -- native-endian size word with the bit-31
//     extended-header flag.
//
// The SparseInt length-8 defect is KNOWN (base/basetypes.rs SparseInt::dump64
// reports 8 bytes at length 8 but the value needs 9 to round-trip): magnitudes
// in +/-[2^48-ish, 2^55-ish] lose their low byte.  The v64 property below
// excludes that band and documents it, and a separate test pins the defect
// explicitly so the property suite stays green while the bug stays visible.
// The fix is item 4.1 of the plan (a wire-format change, its own commit).

use proptest::prelude::*;

use srpc::basetypes::SparseInt;
use srpc::frame_codec::{
    frame_codec_peek_header, frame_codec_write_header, kFrameHeaderSize,
    kMaxFramePayloadSize, FrameDecodeStatus, FrameHeader,
};

// No build.rs, so this binary supplies the C symbols basetypes references.
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
pub extern "C" fn srpc_clock_monotonic_us() -> u64 {
    1_000_000
}
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
pub extern "C" fn srpc_clock_realtime_coarse_us() -> u64 {
    2_000_000
}
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
pub extern "C" fn srpc_gettimeofday_us() -> u64 {
    3_000_000
}
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
pub extern "C" fn srpc_sleep_us(_microseconds: u64) {}

// The upper bound of the SparseInt 7-byte range (inclusive): values with a
// magnitude at or below this round-trip through the <=7-byte path unharmed.
const SPARSE_7BYTE_MAX: i64 = 281_474_976_710_655;
// The upper bound of the (defective) 8-byte range.  Magnitudes strictly above
// SPARSE_7BYTE_MAX and at or below this select the 8-byte encoding, which does
// not round-trip; magnitudes above this use the 9-byte path and are fine.
const SPARSE_8BYTE_MAX: i64 = 36_028_797_018_963_967;

// Archive-realistic round trip: BinaryWriteArchive writes exactly the REPORTED
// byte count (`Serialize for v32/v64` in misc/serializable.rs calls
// `write_bytes(buf, bsize)`), so a faithful round trip persists only those
// `reported` bytes into a fresh zero-filled buffer before decoding. This is
// the path that reaches the wire -- and the one where the length-8 defect
// (dump64 writes 9 bytes but reports 8) actually manifests.
#[allow(unsafe_code)]
fn sparse_roundtrip_32(val: i32) -> (i32, usize) {
    let mut written = [0u8; 9];
    // SAFETY: written has 9 bytes; dump32 needs at most 5.
    let n = unsafe { SparseInt::dump32(val, written.as_mut_ptr()) };
    let mut persisted = [0u8; 9];
    persisted[..n].copy_from_slice(&written[..n]);
    // SAFETY: load reads a valid encoding prefix in a 9-byte buffer.
    let back = unsafe { SparseInt::load32(persisted.as_ptr()) };
    (back, n)
}

#[allow(unsafe_code)]
fn sparse_roundtrip_64(val: i64) -> (i64, usize) {
    let mut written = [0u8; 9];
    // SAFETY: written has 9 bytes; dump64 needs at most 9.
    let n = unsafe { SparseInt::dump64(val, written.as_mut_ptr()) };
    let mut persisted = [0u8; 9];
    persisted[..n].copy_from_slice(&written[..n]);
    // SAFETY: load reads a valid encoding prefix in a 9-byte buffer.
    let back = unsafe { SparseInt::load64(persisted.as_ptr()) };
    (back, n)
}

proptest! {
    // v32 is unaffected by the length-8 defect at every magnitude.
    #[test]
    fn v32_sparse_round_trips_for_every_i32(val in any::<i32>()) {
        let (back, n) = sparse_roundtrip_32(val);
        prop_assert_eq!(back, val, "v32 must round-trip");
        prop_assert!((1..=5).contains(&n), "v32 encodes in 1..=5 bytes, got {}", n);
        prop_assert_eq!(n, SparseInt::val_size(val as i64), "reported length matches val_size");
    }

    // v64 round-trips everywhere EXCEPT the documented 8-byte defect band.
    #[test]
    fn v64_sparse_round_trips_outside_the_length8_defect(val in any::<i64>()) {
        let mag = val.unsigned_abs();
        let in_defect_band = mag > (SPARSE_7BYTE_MAX as u64)
            && mag <= (SPARSE_8BYTE_MAX as u64);
        prop_assume!(!in_defect_band); // item 4.1 fixes this band

        let (back, n) = sparse_roundtrip_64(val);
        prop_assert_eq!(back, val, "v64 must round-trip outside the defect band");
        prop_assert!((1..=9).contains(&n), "v64 encodes in 1..=9 bytes, got {}", n);
        prop_assert_eq!(n, SparseInt::val_size(val), "reported length matches val_size");
    }

    // The frame header round-trips every legal payload size and flag.
    #[test]
    fn frame_header_round_trips(
        payload_size in 0i32..=(kMaxFramePayloadSize),
        extended in any::<bool>(),
    ) {
        let mut buf = [0u8; kFrameHeaderSize];
        prop_assert!(
            frame_codec_write_header(&mut buf, payload_size, extended),
            "a legal payload size must encode"
        );
        let mut header = FrameHeader { payload_size: -1, extended_header_flag: false };
        let status = frame_codec_peek_header(&buf, &mut header);
        prop_assert_eq!(status, FrameDecodeStatus::Complete, "a written header must peek Complete");
        prop_assert_eq!(header.payload_size, payload_size, "payload size round-trips");
        prop_assert_eq!(header.extended_header_flag, extended, "extended flag round-trips");
    }

    // Out-of-range payload sizes are refused by the writer, never silently
    // truncated into the size field.
    #[test]
    fn frame_header_rejects_oversized_payloads(
        payload_size in (kMaxFramePayloadSize + 1)..=i32::MAX,
    ) {
        let mut buf = [0u8; kFrameHeaderSize];
        prop_assert!(
            !frame_codec_write_header(&mut buf, payload_size, false),
            "a payload over kMaxFramePayloadSize must be refused"
        );
    }

    // Negative payload sizes are refused.
    #[test]
    fn frame_header_rejects_negative_payloads(payload_size in i32::MIN..0i32) {
        let mut buf = [0u8; kFrameHeaderSize];
        prop_assert!(
            !frame_codec_write_header(&mut buf, payload_size, false),
            "a negative payload size must be refused"
        );
    }
}

// The length-8 defect, pinned explicitly so the property suite above can
// exclude it while it stays visible and regression-guarded. When item 4.1
// fixes SparseInt::dump64, flip this to assert a correct round-trip and drop
// the exclusion in v64_sparse_round_trips_outside_the_length8_defect.
#[test]
fn sparse_length8_defect_is_still_present() {
    // A value squarely inside the 8-byte band (the historical example).
    let broken: i64 = 36_028_797_018_963_967;
    let (back, _n) = sparse_roundtrip_64(broken);
    assert_ne!(
        back, broken,
        "if this now round-trips, SparseInt::dump64 was fixed -- update plan item 4.1 \
         and flip this test plus the property exclusion"
    );
    // The historical decoded value: low byte lost.
    assert_eq!(back, 36_028_797_018_963_712, "defect drops the low byte to zero");
}
