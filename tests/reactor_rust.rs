use core::mem::{align_of, size_of};
use srpc::misc::Job;
use srpc::pollable_proxy::PollableProxy;
use srpc::reactor::*;
use std::sync::Arc;

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

#[test]
fn worker_transfer_types_prove_auto_traits() {
    fn assert_send<T: Send>() {}
    fn assert_send_sync<T: Send + Sync>() {}

    assert_send::<PollCommand>();
    assert_send_sync::<PollThread>();
    assert_send::<PollableProxy>();
    assert_send_sync::<Arc<dyn Job>>();
    assert_send_sync::<rusty::Waker>();
}

#[test]
fn historical_export_surface_is_rust_visible() {
    type SpawnWithResultFn = fn(&Reactor, rusty::Task<()>, fn(()));
    let _spawn_with_result: SpawnWithResultFn =
        reactor_spawn_stackless_task_with_result::<(), fn(())>;

    // The seven per-thread statics are LocalKeys now; visibility is proven
    // by reading each through its closure-only accessor.
    sp_reactor_th_.with(|slot| assert!(slot.borrow().is_none()));
    sp_disk_reactor_th_.with(|slot| assert!(slot.borrow().is_none()));
    sp_running_fiber_th_.with(|slot| assert!(slot.borrow().is_none()));
    g_fiber_global_id.with(|id| assert_eq!(id.get(), 0));
    reactor_clients_th_.with(|clients| assert!(clients.borrow().is_empty()));
    reactor_prune_hwm_th_.with(|hwm| assert_eq!(hwm.get(), 64));
    g_current_poll_worker.with(|worker| assert!(worker.get().is_null()));

    let dangling: QuorumDanglingVec = vec![rusty::StdPair::new(7u16, 11i64)];
    assert_eq!(dangling[0].first, 7u16);
    assert_eq!(dangling[0].second, 11i64);

}

#[test]
fn stackless_wakers_use_owner_ingress_and_stable_bindings() {
    let source = include_str!("../reactor/reactor.rs");

    for required in [
        "struct StacklessWakeIngress",
        "struct StacklessWakeBinding",
        "static OWNERS: Cell<*mut Vec<StacklessWakeOwner>>",
        "stackless_wake_release_empty_storage::<WakeDomain>",
        "stackless_wake_take_pending::<()>",
        "stackless_wake_shutdown_begin::<()>",
        "if !ingress.accepting.load(rusty::sync::atomic::Ordering::Acquire)",
        "core::mem::take(&mut *tasks_guard)",
        "drop(retired_tasks);",
        "drop(poll_fn);",
        "thread_id_: Cell::new(rusty::thread::current_id())",
    ] {
        assert!(source.contains(required), "missing stackless safety contract: {required}");
    }

    assert!(!source.contains("reactor: *const Reactor"));
    assert!(!source.contains("(*rp).enqueue_stackless_task"));
    assert!(!source.contains("static mut OWNERS: Vec<StacklessWakeOwner>"));
    assert!(!source.contains("self.stackless_tasks_.borrow_mut().clear()"));

    let task_drop = source.find("drop(poll_fn);").unwrap();
    let binding_drop = source.find("stackless_wake_detach::<()>(self, idx);").unwrap();
    let slot_reuse = source.find("free_guard.push(idx as usize);").unwrap();
    assert!(task_drop < binding_drop && binding_drop < slot_reuse);
}

/// W1 shape pin: the H1 namespace-placement contract.
///
/// The markers are inert `cfg_attr(any(), ...)`, so rustc cannot check them and
/// a silent deletion would only surface at the G3 nm oracle, long after the
/// fact.  This census is what makes dropping one a red test here instead.
///
/// The item list is fixed on purpose: exactly the entities that introduce a C++
/// namespace-scope name.  Members follow their enclosing type, so marking an
/// `impl` too would be an overlapping placement contract, which compiler
/// contract 1 requires to reject atomically.
#[test]
fn janus_placement_markers_cover_the_quorum_surface() {
    let source = include_str!("../reactor/reactor.rs");
    const MARKER: &str = "#[cfg_attr(any(), cpp_namespace(::janus))]";

    // The three types plus the five free functions behind all 46 janus strong
    // entries and the 3 QuorumEvent RTTI/vtable entries.
    for item in [
        "pub enum QuorumPolicy {",
        "pub struct QuorumEvent {",
        "pub struct QuorumEventWrapper {",
        "pub fn quorum_event_make(",
        "pub fn create_sp_quorum_event(",
        "fn quorum_collect_dangling(",
        "fn quorum_event_finalize(",
        "fn quorum_event_is_slow(",
    ] {
        let at = source
            .find(item)
            .unwrap_or_else(|| panic!("quorum item vanished from the carrier: {item}"));
        let before = &source[..at];
        assert!(
            before.trim_end().ends_with(MARKER)
                || before
                    .trim_end()
                    .lines()
                    .rev()
                    .take(4)
                    .any(|l| l.trim() == MARKER),
            "missing janus placement marker on: {item}"
        );
    }

    // Exactly eight markers, plus the single mention inside the explanatory
    // comment.  A ninth marker means something was marked that should follow
    // its type instead.
    assert_eq!(
        9,
        source.matches(MARKER).count(),
        "unexpected number of janus placement markers"
    );

    // The target must stay absolute: `srpc::janus` and a relative `janus` are
    // both invalid substitutes with different mangling.
    assert!(!source.contains("cpp_namespace(janus)"));
    assert!(!source.contains("cpp_namespace(srpc::janus)"));
}

/// W2 shape pin: teardown owes waiters an error.
///
/// Cargo cannot run the teardown races -- that is battery item 9's job on the
/// generated C++ -- but it can pin that every silent-cancel path the audit
/// found still routes through the cancellation accounting, and that the two
/// spawn paths still refuse to treat a rejected registration as success.
#[test]
fn teardown_paths_report_cancelled_waiters_instead_of_silence() {
    let source = include_str!("../reactor/reactor.rs");

    for required in [
        // the four accounted teardown paths
        "g_stackless_cancel.pending_wakes.fetch_add(",
        "g_stackless_cancel.rejected_spawns.fetch_add(",
        "g_stackless_cancel.teardown_tasks.fetch_add(",
        "g_stackless_cancel.admitted_completions.fetch_add(",
        // shutdown drains the ingress rather than stranding tickets
        "ticket.enqueued.store(false, rusty::sync::atomic::Ordering::Release);",
        // a rejected registration is not a successful spawn
        "if idx == STACKLESS_UNREGISTERED_SLOT {",
    ] {
        assert!(source.contains(required), "missing teardown contract: {required}");
    }

    // Both spawn entry points must honour the sentinel.  Scope the check to
    // each function body: `register_stackless_poller` also compares against the
    // sentinel while picking a free slot, so a bare occurrence count would pass
    // even if a spawn path silently dropped its guard.
    fn body_of<'a>(source: &'a str, header: &str) -> &'a str {
        let start = source
            .find(header)
            .unwrap_or_else(|| panic!("spawn entry point vanished: {header}"));
        let rest = &source[start..];
        // These are top-level fns, so the body ends at the next column-0 brace.
        let end = rest.find("\n}\n").map(|e| e + 3).unwrap_or(rest.len());
        &rest[..end]
    }

    for spawn in [
        "pub fn reactor_spawn_stackless_task_with_result<",
        "fn reactor_spawn_stackless_task_impl(",
    ] {
        let body = body_of(source, spawn);
        assert!(
            body.contains("let idx = self_.register_stackless_poller(poller);"),
            "{spawn} no longer registers through the audited path"
        );
        assert!(
            body.contains("if idx == STACKLESS_UNREGISTERED_SLOT {"),
            "{spawn} stopped checking for a rejected registration, so a \
             refused spawn looks like a successful one again"
        );
    }

    // The cancellation must be reported at ERROR, not swallowed or logged as
    // routine debug noise.
    let cancel_reports = source
        .lines()
        .filter(|l| l.contains("log_line(Log::ERROR") && l.contains("cancel"))
        .count();
    assert!(
        cancel_reports >= 3,
        "teardown cancellation is not reported at ERROR on every path"
    );

    // Accounting must be counted BEFORE the queue is cleared, or the
    // already-admitted completions are lost before they can be reported.
    let admitted = source.find("let admitted: u64 = self.ready_stackless_tasks_").unwrap();
    let cleared = source.find("self.ready_stackless_tasks_.borrow_mut().clear();").unwrap();
    assert!(
        admitted < cleared,
        "admitted completions are counted after the ready queue is cleared"
    );
}

/// W2 shape pin: the cancellation accessor must stay generic.
///
/// It is generic solely so it lowers to a C++ template and cannot add an
/// ordinary strong symbol to the exact 300-entry owned manifest (compiler
/// contract 7 / gate G3).  Losing the type parameter would be invisible in
/// Rust and would fail at G3 instead.
#[test]
fn stackless_cancel_report_stays_a_template() {
    let _report: fn() -> StacklessCancelReport = stackless_cancel_report::<()>;

    let source = include_str!("../reactor/reactor.rs");
    assert!(source.contains("pub fn stackless_cancel_report<WakeDomain>()"));
    assert!(source.contains("static g_stackless_cancel: StacklessCancelCounters"));
    // Same shape as g_stackless_profile, which the incumbent object proves
    // carries no owned strong symbol.
    assert!(source.contains("static g_stackless_profile: StacklessProfileCounters"));
}

#[test]
#[ignore = "generated C++ ABI gate: rustc facade containers intentionally have different storage"]
fn incumbent_concrete_layouts_are_pinned() {
    let mut mismatches: Vec<String> = Vec::new();
    macro_rules! check {
        ($ty:ty, $size:expr, $align:expr) => {{
            let actual = (size_of::<$ty>(), align_of::<$ty>());
            if actual != ($size, $align) {
                mismatches.push(format!(
                    "{}: expected {}/{}, got {}/{}",
                    stringify!($ty), $size, $align, actual.0, actual.1
                ));
            }
        }};
    }

    check!(EventStatus, 4, 4);
    check!(EventState, 160, 16);
    check!(BoxEvent<i32>, 224, 16);
    check!(IntEvent, 224, 16);
    check!(SharedIntEvent, 56, 8);
    check!(NeverEvent, 208, 16);
    check!(TimeoutEvent, 224, 16);
    check!(WaitAny, 256, 16);
    check!(WaitAll, 272, 16);
    check!(fiber_yield_t, 8, 8);
    check!(fiber_task_t, 240, 16);
    check!(FiberStatus, 4, 4);
    check!(Fiber, 144, 16);
    check!(StacklessTaskEntry, 64, 16);
    check!(Reactor, 504, 8);
    check!(PollCommand, 16, 8);
    check!(PollThreadWorker, 200, 8);
    check!(PollThread, 104, 8);
    check!(QuorumPolicy, 4, 4);
    check!(QuorumEvent, 352, 16);
    check!(QuorumEventWrapper, 8, 8);

    assert!(mismatches.is_empty(), "{}", mismatches.join("\n"));
}

// The LocalKey migration made the running-fiber slot's drop chain reachable
// from this binary (reading the statics through `.with` instantiates it), so
// the linker now demands the fiber-destroy kernel. House pattern: supply a
// loud stub -- nothing in this text-pin binary ever runs a fiber.
#[allow(unsafe_code)]
#[unsafe(no_mangle)]
extern "C" fn srpc_fiber_destroy(_f: *mut core::ffi::c_void) {
    unreachable!("srpc_fiber_destroy must not run: reactor_rust never creates fibers")
}
