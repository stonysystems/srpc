use srpc::fiber::this_fiber;

// `this_fiber::sleep_until_us` now reads the canonical `srpc.basetypes` clock,
// whose bodies are the plain-C kernels. There is no `build.rs`, so nothing
// links them into the Rust lane -- this test must supply them itself.
//
// The constants are deliberately nonzero, matching tests/basetypes_rust.rs.
// The deadline case exercised is `abs_time_us == 0`, and it has to be strictly
// in the past for the assertion to bite: a zero-returning monotonic stub makes
// `sleep_until_us(0)` compare `0 > 0`, so the branch degenerates and a mutation
// of that `>` to `<` would no longer be caught. Zero is also the additive
// identity, which would silently flatten any later `now + delta` arithmetic.
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

#[test]
fn context_identity_current_and_yield_preserve_behavior() {
    assert!(!this_fiber::in_fiber_context());
    assert_eq!(this_fiber::get_id(), 0);
    assert!(this_fiber::current().is_none());
    this_fiber::r#yield();

    let ((id, present), yields) = rusty::srpc::reactor::with_test_fiber(73, || {
        assert!(this_fiber::in_fiber_context());
        let current = this_fiber::current().expect("test fiber must be installed");
        this_fiber::r#yield();
        this_fiber::r#yield();
        (this_fiber::get_id(), current.id.get() == 73)
    });
    assert_eq!(id, 73);
    assert!(present);
    assert_eq!(yields, 2);
    assert!(!this_fiber::in_fiber_context());
}

#[test]
fn sleep_helpers_preserve_unsigned_wrapping_and_past_deadlines() {
    let _ = rusty::srpc::reactor::take_test_sleep_calls();

    this_fiber::sleep_us(17);
    this_fiber::sleep_ms(u64::MAX);
    this_fiber::sleep_s(u64::MAX);
    this_fiber::sleep_until_us(0);

    assert_eq!(
        rusty::srpc::reactor::take_test_sleep_calls(),
        vec![
            17,
            u64::MAX.wrapping_mul(1_000),
            u64::MAX.wrapping_mul(srpc::basetypes::SRPC_USEC_PER_SEC),
        ]
    );
}
