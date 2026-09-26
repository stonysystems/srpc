# Plan: running SRPC on Lion

Status legend: `[ ]` not started · `[~]` deferred with reason · `[x]` done.

**Status: PROPOSED, revision 2 (2026-09-26).** Nothing below is implemented.

Revision 2 changes the route at the owner's direction. rusty-cpp learns to
transpile the **executable code inside `verus! { }` blocks**, and to discard
specs and proofs, so Lion's unmodified, verified sources feed the C++ lane
directly. Revision 1 imported a `cargo expand` snapshot as generated canonical
modules. That route is now the first fallback (§5).

Lion is [stonysystems/lion](https://github.com/stonysystems/lion) (SOSP '26), a
Tokio-shaped async runtime whose executor and reactor liveness is verified in
Verus. This plan was investigated against Lion `aa5bebe`, SRPC `99f625d` and
rusty-cpp `1689f438`.

## 0. The short version

This is **not a dependency swap**. SRPC's runtime is not small: `reactor/reactor.rs`
alone is 3,793 lines. It covers:
- stackful fibers;
- the event family: `IntEvent`, `WaitAny`/`WaitAll`, `TimeoutEvent` and the
  `janus::` Quorum surface;
- `PollThread`/`Pollable` I/O dispatch;
- job and command ingress;
- a stackless executor.

Every piece is pinned C++ ABI, and Mako consumes it. Lion supplies a verified
**executor + reactor + timer wheel**. It has no stackful fibers, no event family
and no C++ lane.

The work has four parts:

1. **Teach rusty-cpp to transpile Verus executable code** (§4, track T). The
   transpiler erases `verus! { }` exactly as rustc sees it, by reusing Verus's
   own erasure pass. It then lowers the small ghost residue that is left and
   transpiles the executable code that remains.
2. **Bring Lion in as pinned, unmodified crates** (§6, S1). Its crates become
   path dependencies from a `third-party/lion` submodule. rustc compiles them in
   the Rust lane. Crate-mode rusty-cpp walks them in the C++ lane. Both lanes
   see the same bytes, and those bytes are the ones Lion's CI verified.
3. **Replace SRPC's executor/reactor core with Lion's, and re-host the
   SRPC-specific pieces on top of it** (§1, §6). Fibers, events, the TCP
   transport and job ingress stay SRPC-owned.
4. **Land prerequisite fixes in Lion upstream, and re-prove them there** (§3).
   Some of them block adoption on *any* route, including a Rust-only one.

Why the direct routes fail today (all measured or read, not guessed):

| Route | Why it fails |
| --- | --- |
| `lion = { git = ... }` in `Cargo.toml` | `scripts/check_rust_independence.py:16-27` rejects any production dependency, and `Cargo.toml:8-9` bans vstd from the production build. Lion's normal closure is 58 packages (`cargo tree -e normal`), including tokio, mio, flume, socket2 and vstd from git. The C++ lane would get no runtime at all. |
| Transpile Lion as-is | Almost all executable code is inside `verus! { }`. rusty-cpp does not handle `verus!`, `Ghost`, `Tracked`, `nat` or `Seq`. Its macro fallback emits `// TODO: verus!(...)` (`transpiler/src/codegen/emit_expr.rs:3022-3024`), and SRPC's placeholder check rejects that. Crate mode does not transpile registry dependencies such as mio, tokio and flume (`main.rs:2764-2769`). Nothing lowers the `RawWakerVTable` waker path (`lion-executor/src/types/waker.rs:30-100`). |

Track T removes the `verus!` blocker. Upstream items U6/U7 remove the mio,
flume and raw-waker blockers. S1 changes the dependency policy on purpose (D5).

## 1. The boundary: what Lion replaces, what SRPC re-hosts

| SRPC piece today | After | Notes |
| --- | --- | --- |
| Stackless task table, ready queue, `StacklessWakeTarget` ingress (`reactor.rs:955-1168, 1683-1845, 1933-1991`) | **Lion executor** | Needs a `!Send` spawn upstream (U2). |
| `epoll_wait_impl` loop with a hard-coded 1 ms timeout (`epoll_wrapper.rs:118-144`), per-pass sweeps of every fd (`reactor.rs:3345-3383`) | **Lion reactor** | Wake is by eventfd, so there is no 1 ms polling floor. |
| `TimeoutEvent` / `fiber_sleep`, found by a linear timeout scan (`reactor.rs:1849-1880`) | **Lion timer wheel** | Lion is millisecond-granular (R4). |
| Cross-thread wake: a ticket queue, with 1 ms latency | **Lion waker** (queue + interrupt) | U6 replaces flume. |
| Stackful fibers (`srpc_fiber.c`, `fiber_context_*.S`, `Fiber`, `this_fiber`) | **SRPC, re-hosted** | Lion has none. A grep for fiber/ucontext/stackful is empty. |
| Event family: `IntEvent`, `SharedIntEvent`, `WaitAny`, `WaitAll`, `NeverEvent`, `janus::QuorumEvent*` | **SRPC, re-hosted** | Events are no longer polled on every pass; they wake their waiters when they change. |
| `PollThread`, `Pollable`/`PollableProxy`, and the `TcpConnection` / `TcpListener` shims | **SRPC, rewritten** as async tasks on Lion's reactor | Lion's own `TcpStream` is not reused (S5). |
| `Job`/`OneTimeJob`/`PollCommand` ingress | **SRPC, re-hosted** as a command-drain task woken by Lion | |
| Client `Future`: a Condvar that blocks an OS thread (`client.rs:503-573`) | **Unchanged** in the core plan | Making it awaitable is optional (S6). |
| Disk reactor (`sp_disk_reactor_th_`) | **Deleted** | No callers in `rpc/`, `misc/` or `base/`. |

**Where things live after the swap:**
- SRPC's `Reactor` type survives as the **per-thread fiber and event registry**.
  It tracks the running fiber, the waiting events and fiber recycling, and it is
  still created lazily on any thread, as today.
- A **Lion runtime exists only inside a `PollThread`**, as that thread's loop.
- A thread with no `PollThread` still has a `Reactor`. Examples are Mako's
  `main` and an in-memory-channel sender. Such a thread can create, yield and
  continue fibers synchronously; it just has no I/O or timer driver.
  - Events set on such a thread resume their fibers directly, as `run_loop`
    does today.
  - What a timer wait on a loop-less thread should do is an S4 design item.
    Today, `create_sp_timeout_event(..)->wait()` is serviced by whoever pumps
    `run_loop`.

**Mako's runtime API**, confirmed by grep in `../mako`. Mako vendors SRPC
`683c506`, 141 commits behind HEAD, and has its own rusty-cpp pin.

| Mako use | Where | Side |
| --- | --- | --- |
| `Fiber::create_run`, which runs the body up to its first yield *synchronously* | `src/run.cc:18,27` | SRPC fiber layer |
| `Fiber::current_fiber().unwrap()->yield_()` | `src/run.cc:29,31` | SRPC fiber layer |
| `Reactor::get_reactor()->continue_fiber(f)`, which resumes *synchronously* from a thread with no loop | `src/run.cc:35,37`, which asserts `x` after each call | SRPC fiber layer |
| `IntEvent` plus `->wait()` inside a fiber | `src/deptran/raft/srpc_transport.hpp:84,104,129` | SRPC events, woken through Lion |
| `create_sp_timeout_event(to)->wait()` | `src/deptran/raft/testconf.cc:220` | SRPC event over a Lion timer |
| A `janus::QuorumEventWrapper` subclass | `src/deptran/replication_quorum.h:10-13` | SRPC events (`cpp_namespace(::janus)`) |
| `srpc::PollThread::create()` | `src/rocks_interface/remote_db.hh:506` | SRPC `PollThread`, which now owns a Lion runtime |

Nothing breaks for Mako until its next SRPC bump. That bump must find these
shapes source-compatible. The ABI may change. The source shapes and the
synchronous `create_run`/`continue_fiber` semantics must not.

Mako builds SRPC through its own `src/srpc-cmake/CMakeLists.txt`. That file must
learn to transpile the Lion dependency crates too, or switch to SRPC's own CMake.

## 2. Decisions needed before Phase 1

- **D1. Keep the C++ lane?** *Recommended: yes.*
  - This is the plain reading of CLAUDE.md, and Mako consumes the C++ lane.
  - The alternative is a Rust-only runtime: `lion` becomes a `[dependencies]`
    line, and track T plus U6/U7 disappear. That is roughly an order of
    magnitude cheaper, but it abandons the dual-compile design and Mako.
  - Everything below assumes **yes**.
- **D2. What verification claim does SRPC make?** *Recommended: identity with
  Lion@pin.*
  - SRPC compiles and transpiles Lion's pinned bytes **unmodified**, and Lion's
    CI (`ci.sh`, Verus) is the proof of record.
  - The Rust lane runs what rustc produces from those bytes. That is Verus's
    `EraseAll` expansion, the same thing any Lion user runs.
  - The C++ lane transpiles the **same** erasure, because T1 reuses Verus's own
    erasure code at the same revision. It then applies T2's residue lowering,
    which is new, small and tested (R7).
  - Nothing is normalized, snapshotted or patched on the SRPC side. Any Lion
    change goes upstream (D4) and arrives through a pin bump.
  - Re-verifying Lion inside SRPC's `verify/` lane stays out of scope. Lion's
    proofs need Rust 1.91 and Verus/vstd `db81a74`. An optional non-gating
    `verify-lion` lane can run Lion's `ci.sh` on the pinned submodule.
- **D3. Keep stackful fibers?** *Recommended: yes.* Mako depends on them, and so
  do the IDL modes default, `defer`, `fiber` and `raw`
  (`pylib/simplerpcgen/lang_cpp.py:367-470`). Dropping fibers would remove
  those modes and break Mako's source.
- **D4. Where do Lion fixes land?** *Recommended: in stonysystems/lion, re-proved
  there.* A patched submodule would break D2's identity claim.
- **D5. Accept the policy changes.**
  - **The Rust lane gains dependencies.** "No production Cargo dependencies"
    (`check_rust_independence.py:16-27`) becomes an exact allowlist:
    - the Lion path crates under the `third-party/lion` gitlink;
    - vstd at the Verus git rev in Lion's lockfile;
    - the build-time proc-macro closure of those two, and nothing else.
    - Lion's runtime crates (mio, flume, tokio, socket2, futures-task,
      pin-project-lite) must be **absent**, via U6's features.
    - `Cargo.toml:8-9`'s "vstd must never enter the production build" is
      reversed for the erased vstd library. vstd's manifest calls it an "erased
      vstd library for linking with non-Verus Rust code".
  - **`libsrpc.a` gains non-SRPC providers.** "Generated SRPC modules are the
    only providers" becomes "the 37 SRPC providers plus the generated Lion
    providers". The Lion providers are a separately inventoried class (S7).
  - **Ownership moves.** "Canonical Rust owns SRPC scheduling" becomes this
    split: verified Lion owns the executor, reactor and timers, and
    SRPC-canonical code owns fibers, events and transport.
  - CLAUDE.md, `docs/canonical-rust-runtime.md`,
    `docs/dev/facade-and-runtime-remaining.md` and `docs/async-runtime.md` must
    be rewritten to match.

## 3. Upstream prerequisites in Lion (go/no-go)

These are properties Lion lacks today that an RPC server needs.
- U1–U5 block **every** route, including Rust-only.
- U6–U7 are needed for the C++ route.
- Each item that touches verified code needs a re-proof in Lion's CI.

- [ ] **U1. Bounded ids.** Resource ids never recycle ("worst-case UNBOUNDED
  memory", `lion-reactor/src/alloc_verified.rs:9-14`, `TCB_and_limitations.md:71-81`).
  - `IO_READINESS` is indexed by raw resource id and never shrinks
    (`readiness.rs:8-21`).
  - `TASK_NOTIFIED` is indexed by a task id that only ever increases
    (`tls.rs:14,30-46`, `handle.rs:24,30`).
  - Every connection, every `Sleep` and every per-request timeout consumes new
    ids, so a server with connection churn leaks memory without bound. The micro
    timer benchmark reaching about 10 GB (`README.md:55`) fits this.
  - *Verified code; re-proof required.*
- [ ] **U2. `spawn_local` for `!Send` futures.**
  - `spawn` requires `Send` (`lion-executor/src/lib.rs:130`), and the facade's
    `spawn_local` just calls `spawn` (`lion/src/lib.rs:33-39`).
  - SRPC's stackless futures are not `Send` (`async-runtime.md`), and C++
    coroutine frames will not be either.
- [ ] **U3. Cancellation and panic isolation.**
  - `JoinHandle::abort` is empty (`join_handle.rs:38`).
  - Nothing catches unwinds at the poll boundary (`executor/ext.rs:111-123`).
  - SRPC has teardown cancellation today (`StacklessCancelReport`,
    `reactor.rs:1059-1096`).
- [ ] **U4. Runtime TLS save/restore.**
  - `Runtime::new` overwrites `CURRENT_HANDLE`, `CROSS_THREAD_CTX` and
    `CURRENT_REACTOR`, and drop sets them to `None` instead of restoring them
    (`lib.rs:69-89,121-128`, `reactor/enter.rs:19-25`).
  - SRPC creates a reactor lazily on *any* thread that calls `get_reactor()`
    (`reactor.rs:3088`).
  - The in-memory channel delivers frames synchronously on the sender's thread
    (`inmemory_channel.rs:255`), so fiber handlers start on that thread
    (`server.rs:1444-1464`).
- [ ] **U5. A driving API SRPC can own.**
  - Today the only entry point is `Runtime::block_on` (`lib.rs:102-118`), and
    the executor module is private.
  - SRPC needs one of two things:
    - a public `turn(timeout)` step; or
    - a clean "`PollThread` = a thread parked in `block_on(worker)`" contract
      with foreign spawn.
- [ ] **U6. (C++ route) An OS seam behind a trait, plus optional runtime
  dependencies.**
  - `types/poll.rs`, `interrupt_handle.rs` and `io_event_queue.rs` are already
    `external_body` glue. Put them behind a small `Poller`/`Interrupt` trait.
    SRPC then implements that trait over `srpc_epoll.c` from a canonical module.
  - Replace flume with `std::sync::mpsc` or a `Mutex<VecDeque>`.
  - Put mio, socket2 and tokio behind a default-on feature (for example `mio`).
    SRPC depends with `default-features = false`.
  - Drop `futures-task` and `pin-project-lite`: no uses were found in
    `lion-executor/src`.
- [ ] **U7. (C++ route) An `Arc`/`std::task::Wake` waker only.**
  - Drop the `RawWakerVTable` path (`waker.rs:30-100`).
  - rusty-cpp lowers `impl Wake` with an `Arc<Self>` receiver
    (`transpiler/src/codegen/standard_future.rs:7-100`). Its C++ `rusty::Waker`
    is a pair of `std::function`s, not a vtable.
  - Doing this upstream is preferred over teaching the transpiler
    `RawWakerVTable`.

Also worth raising upstream, though not blocking:
- `spawn_blocking`'s `Cell` counter sits in a type force-marked `Sync`
  (`blocking.rs:17,20,70-71`).
- Sockets carry hand-written `unsafe impl Send` but only work on their creating
  thread (`stream.rs:200-201`, `listener.rs:30-31`).
- Network futures use the TLS task waker instead of the `Context` waker
  (`stream.rs:135,169`).

SRPC does not plan to use `lion-utility` or the `lion` facade (S5), so these do
not enter its graph.

## 4. Track T: Verus executable code in rusty-cpp

The work lands in `shuaimu/rusty-cpp`, with its own tests. It reaches SRPC
through a transpiler pin bump. That bump is four edits: the gitlink, plus the
literals in `scripts/extract_srpc_rust.py`, `scripts/check_srpc_crate_mode.py`
and `scripts/tests/test_goal0_standalone.py`. It gets its own
`build: bump rusty-cpp <old> -> <new>` commit.

**Goal.** For any item-level `verus! { ... }`, the transpiler emits the C++ for
exactly the executable code rustc compiles from it. It emits nothing for specs,
proofs or ghost state, and **fails closed** on anything it cannot classify.

- [ ] **T1. Erasure front end: reuse Verus's own pass. Do not reimplement it.**
  - Verus's `builtin_macros` (MIT) already has the pass plain rustc uses.
    `cfg_erase()` returns `EraseGhost::EraseAll` without `verus_keep_ghost`
    (`builtin_macros/src/lib.rs:53-81,158-161`), and
    `syntax::rewrite_items(stream, erase, use_spec_traits)` does the work
    (`syntax.rs:4538`, 5,386 lines, built on `verus_syn`).
  - `builtin_macros` is a proc-macro crate, so it cannot be linked as a
    library. Vendor its `syntax.rs` and siblings into rusty-cpp as a
    `verus_erase` module.
    - Swap the 43 `proc_macro::` uses (entry points and `Diagnostic`
      warnings) for `proc_macro2`.
    - Depend on `verus_syn` at the same revision. Verus at `db81a74` uses
      `verus_syn =0.0.0-2025-11-10-1957` from inside its own repository. The
      crate is published on crates.io: the local registry cache holds
      `verus_syn-0.0.0-2026-08-02-0125` and `-2026-09-06-0133`. Use the
      crates.io release matching Lion's Verus revision if one exists, and
      otherwise a git dependency on `verus-lang/verus` at that revision.
  - **Pipeline:** find the `verus!` item macro → erase its tokens with
    `EraseAll` → reparse the result with `syn` → run the existing pipeline.
  - **Version coupling.** The vendored pass must match the vstd revision
    recorded in Lion's lockfile, which is `db81a74` today. The transpiler
    records the revision it vendors. An SRPC gate compares that against
    `Cargo.lock`'s vstd source, so a Lion pin bump that moves Verus fails
    loudly.
  - **Rejected alternative:** a native erasure written from scratch in
    rusty-cpp. It means about 5k lines of re-derived Verus syntax handling,
    which would drift from what rustc actually compiles and weaken D2.
- [ ] **T2. Lowering the ghost residue.** `EraseAll` still leaves ghost
  residue in executable positions. Measured on Lion's crates (see §8 for the
  per-crate counts):
  - `Ghost<T>` fields, tuple elements and locals, for example
    `pub log: Ghost<Log>` and
    `Option<(ResourceId, Ghost<int>, Ghost<InstantView>)>`, initialised with
    `Ghost::assume_new_fallback(|| unreachable!())`;
  - `impl View for X { type V = Map<nat, _>; }` and `V: View` bounds;
  - `use vstd::prelude::*`;
  - empty `{}` statements.

  The rules:
  - `Ghost<T>` and `Tracked<T>` lower to one empty C++ tag type (for example
    `rusty::Ghost`, with `[[no_unique_address]]` on fields). `T` is never
    emitted. `Ghost::assume_new_fallback(..)` lowers to `rusty::Ghost{}`.
    Keeping the tag, rather than deleting the element, preserves tuple arity
    and patterns at every call site.
    - The tag must be trivially copyable, and must support `==`, hashing and
      debug printing. Structs with ghost fields still derive
      `Clone`/`Copy`/`PartialEq`/`Debug` through them: the expanded reactor
      contains `AssertParamIsClone<Ghost<int>>`.
  - `cfg(verus_keep_ghost)` and `cfg(verus_keep_ghost_body)` are always false,
    exactly as `cfg(verus)` is today. Lion uses them **outside** `verus!` too,
    for example `#![cfg_attr(verus_keep_ghost, verus::trusted)]` at
    `lion-executor/src/types/waker.rs:1`, `tls.rs:1` and
    `lion-reactor/src/handle.rs:1`.
  - `View` and `DeepView` impls and bounds are dropped. So are `vstd::prelude`
    imports, the empty statements, and `#[verifier::*]` attributes, which are
    already dropped (`cpp_default_args.rs:608-650`).
  - A reachability pass prunes spec-only datatypes such as `Log`, `IoLog` and
    `InstantView`. Once `Ghost<T>` stops naming `T`, nothing references them.
    `Seq`, `Map`, `Set`, `nat` and `int` must then be unreachable.
  - **Fail closed.** A ghost-typed value flowing into a non-ghost position, or
    any surviving `vstd::` path not covered by T3, is a hard transpile error.
    It is never a TODO.
- [ ] **T3. The vstd executable surface table.**
  - A small, explicit table maps the vstd executable items Lion uses to C++.
    `Vec::set(i, x)` becomes index assignment; it appears at
    `lion-slab/src/slab.rs:103`. The 11 `.set(` call sites across executor, reactor and slab (§8) are not all vstd's; Phase 0 classifies them.
  - Phase 0 produces the complete list. Unknown vstd executable items are
    errors.
- [ ] **T4. Multi-crate crate mode.** Path dependencies are already walked
  recursively (`main.rs:2712-2745`), but these pieces are needed:
  - **Per-crate C++ namespaces and module names.** For example
    `lion_executor::...` and `import lion_executor.<mod>;`. Namespace wrapping
    exists as an opt-in with documented gaps (`--crate-namespace-wrap`,
    `main.rs:176-186`). With it working, Lion's `Reactor`, `Waker`, `Handle`
    and `Runtime` never collide with SRPC's flat `namespace srpc`.
  - **Ghost-only crates and modules emit no provider.** This covers the four
    `*-spec` crates, which are real dependencies of the executor and reactor,
    and the `proof/`, `invariants/` and `spec/` module trees.
  - **`#[cfg(feature = ...)]` evaluated per crate** against Cargo's resolved
    feature set (from `cargo metadata`), so U6's `mio` feature is off in the
    C++ lane exactly as it is in the Rust lane.
  - **Cross-crate trait implementations.** SRPC's canonical `epoll_wrapper.rs`
    implements Lion's U6 `Poller` trait, and SRPC calls generic Lion APIs.
  - **The adapter restriction.** Today "cross-crate adapter calls are
    unsupported" (`main.rs:669`). Confirm SRPC's C++ ABI adapters never need to
    target a Lion item, or lift the restriction.
  - **Audit ordering.** `CrateOpaqueSurfaceAudit` (`cpp_abi.rs:2374`, invoked
    at `:3626` and `:4454`) rejects opaque macros and glob imports in crates
    that have C++ ABI adapters, and SRPC's crate has them.
    - The audit must run **after** T1 and T2, so it sees erased code, not
      `verus!` or `use vstd::prelude::*`.
    - Confirm its scope is per crate and not the whole graph, before Phase 0
      step 4.
- [ ] **T5. Lowering gaps in Lion's executable code.** Whatever Phase 0 and its
  stretch surface: generics over `Slab<V>` and the timer wheel, `thread_local!`
  holding `RefCell`s, statics, `dyn Future + Send` boxes (already supported),
  and closures. Each gets a general fix with a codegen fixture, never a
  Lion-specific special case.
- [ ] **T6. Tests in rusty-cpp.**
  - **Differential erasure.** This proves the vendored pass is the one rustc
    runs. For every `verus!` block in Lion@pin, compare the transpiler's post-T1
    output with the corresponding items in `cargo expand` output.
    - `cargo expand` expands *every* macro, so compare this way:
      1. Parse both sides with `syn` and print them with `prettyplease`.
      2. Compare exactly only the items that contain no other macro
         invocation, such as `format!`, derives or `thread_local!`.
      3. Report coverage as the fraction of `verus!` items compared.
  - **Codegen fixtures** for each T2 rule and each fail-closed error.
  - **`parity-test`** on `lion-slab` and `lion-timer-wheel`, plus added cases
    for insert, remove, advance and fire. This is the existing command. Its
    stages run the Rust `cargo test` baseline, transpile, compile the C++, and
    run it (`docs/rusty-cpp-transpiler.md:3105-3120`).
- **Side benefit, not in scope.** Once T1–T3 exist, SRPC's own canonical
  modules could carry in-body Verus proofs in `verus!{}` form. That lifts the
  "no in-body `proof!`" limit in `docs/verification.md`, because the transpiler
  would erase proofs instead of rejecting them. It is a follow-on decision.

## 5. Phase 0: the go/no-go spike

Goal: before any runtime design work, prove that unmodified Lion sources
transpile and compile with **zero** hand-attention slots. Do it in worktrees of
rusty-cpp and SRPC, and throw both away afterwards.

1. **rusty-cpp prototype.** Implement T1 and just enough of T2/T3 behind a flag
   (for example `--verus-exec`). Add a debug flag that dumps the erased Rust
   for each `verus!` block.
2. **Differential check.** For `lion-slab` and `lion-timer-wheel`, the dumped
   erasure matches `cargo expand --lib` output of the same crate. These are
   the smallest crates: 109 and 608 expanded lines, no mio, only vstd as a
   dependency.
3. **Standalone transpile.** Transpile each crate standalone in crate mode on
   its own `Cargo.toml`. Build the BMIs with SRPC's production flags,
   including `-march=native`. Run a parity check of insert, remove, advance and
   fire against the Rust build.
4. **Inside SRPC.**
   - Add `third-party/lion` at `aa5bebe`, and add both crates as `[dependencies]`
     path entries. Leave the independence gate failing; that is expected.
   - Add one probe use in a canonical module, for example a
     `lion_timer_wheel` type in a test-only function.
   - Run the transpiler directly, with the flags CMake passes
     (`CMakeLists.txt:788-799`):

     ```sh
     build/compiler-final-debug/debug/rusty-cpp-transpiler --verus-exec \
       --crate "$PWD/Cargo.toml" --output-dir "$SCRATCH/cpp" \
       --cxx-namespace srpc --flat-import-namespace srpc \
       --module-preamble module-preambles.toml --type-map rust-type-map.toml \
       --cpp-module-index cpp-module-index.toml
     ```

   - Confirm that the path dependencies are walked and namespaced, that the
     `*-spec` crates emit nothing, and that the SRPC probe module imports the
     Lion module.
   - Skip the CMake inventories, the ABI tables and the importer. Those belong
     to S1 and S7.

**Pass criterion (all must hold):**
- the differential check is exact;
- `rusty_hand_slots.md` reports `0 slot(s)`, and there is no
  `TODO`/`UNSUPPORTED`/`skipped` in any generated `.cppm`;
- no fail-closed error fires, or each one that fires is recorded as a T2/T3 item
  with its fix;
- the BMIs build and load in an importer TU;
- the parity check agrees;
- `RUSTFLAGS=-Dwarnings cargo test --locked --workspace --all-targets` is green.
  - This compiles the Lion path crates **uncapped**: Cargo only applies
    `--cap-lints allow` to non-path dependencies.
  - The only expected red is `check_rust_independence.py`.

**Stretch.** Point the same flow at `lion-executor` and `lion-reactor` as they
are today. They will fail on mio, flume and `RawWakerVTable`. The value is the
*rest* of the failure list. It sizes T5, and it confirms U6/U7 are the only
upstream C++-route items.

**Fallbacks, in order, if the spike fails and the gaps are not general
transpiler fixes:**
1. **A native erasure pass on `verus_syn`**, if vendoring Verus's pass is what
   fails, for example because it is too entangled with proc-macro-only APIs.
2. **The revision 1 route.** Commit a `cargo expand` snapshot of Lion@pin.
   Normalize it with a reviewed script into generated canonical modules under
   `reactor/lion_*.rs`, and gate byte-equality against the committed snapshot.
   - The provenance claim weakens: erasure plus reviewed normalization, not
     identity.
   - Every imported module costs the seven-place edit, with renames for the flat
     `srpc` namespace.
3. **Staticlib.** Link a Cargo-built, unmodified Lion plus a `lion-capi` shim
   into `libsrpc.a`, called through `extern "C"`.
   - It keeps Lion's bytes exactly as verified.
   - It costs a hand-written C-ABI poll/wake bridge between C++ `rusty::Task` /
     `rusty::Waker` and Rust `Future` / `Waker` in the hot path.
   - The native-kernel policy (`check_native_kernels.py:54-57,88-96`) and the
     "only providers" rule both break.
   - There is no precedent in the tree.

## 6. SRPC phases (after Phase 0 passes and the needed U- and T-items land)

Each phase ends with the full pre-commit sequence from CLAUDE.md. Its
`Verified:` numbers are measured on that revision.

- [ ] **S1. Lion as pinned dependency crates, and the gate policy.**
  - **Submodule and dependencies.** Add the `third-party/lion` submodule. Add
    path dependencies on `lion-executor` and `lion-reactor` with
    `default-features = false`; `lion-slab`, `lion-timer-wheel` and the
    `*-spec` crates come in transitively.
  - **Lint exposure.** The source gate's `RUSTFLAGS=-Dwarnings` now compiles
    Lion's path crates with no lint cap. vstd, a git dependency, stays capped.
    - Measured clean today: slab, timer-wheel, executor and reactor at
      `aa5bebe` produce 0 warnings on rustc 1.97.1.
    - A new rustc lint, or a Lion change, can therefore fail SRPC's gate.
      Treat "Lion is warning-free on SRPC's toolchain" as a standing upstream
      requirement, checked at every pin bump.
  - **`check_rust_independence.py`.** Replace the dependency ban with the D5
    allowlist:
    - the path deps must resolve under the gitlink;
    - vstd must be the expected git rev;
    - no mio, flume, tokio or socket2 may appear in `cargo tree -e normal`;
    - the isolated copy must include `third-party/lion`, and run offline
      against a warm Cargo git cache. This is the same prerequisite
      `rust_source_audit` already has; document it next to that one in
      CLAUDE.md.
  - **Transpiler invocation.** Add `--verus-exec` to the CMake invocation and to
    `check_srpc_crate_mode.py`'s. Add the T1 version-coupling check: the
    vendored Verus revision must equal the vstd source in `Cargo.lock`.
  - **Verification lane.** Add the optional `verify-lion` lane, not wired to
    CMake, the same way `verify_srpc.sh` is not.
  - **Unchanged.** SRPC's own module inventory, the seven-place edit and the
    `37` hard-codes do not change. Lion modules are not canonical SRPC modules.
    The census rules for `base/`, `misc/`, `rpc/` and `reactor/` stay as they
    are.
- [ ] **S2. OS backend.**
  - Implement Lion's U6 seam in canonical `reactor/epoll_wrapper.rs` over
    `srpc_epoll.c`.
  - SRPC's kernel has no eventfd today (a grep for `eventfd|EFD_|pipe2` is
    empty). Add `srpc_epoll_eventfd_{create,signal,drain}`.
  - New native leaves need review against `scripts/native-kernels.json` and
    `native-abi-bindings.json`. There is no auto-approval.
  - Keep edge-triggered semantics: EPOLLET today, and mio's behaviour in Lion.
- [ ] **S3. Core swap.**
  - `PollThread` becomes one OS thread running one Lion runtime.
  - Stackless tasks go to Lion `spawn_local`.
  - Foreign wakes go through the Lion waker.
  - `Job`/`PollCommand` become an mpsc queue drained by a task that is woken on
    send, with no per-pass `try_recv`.
  - `reactor_spawn_stackless_task_with_result` keeps its C++ signature.
- [ ] **S4. Fibers re-hosted on Lion.**
  - Keep `srpc_fiber.c` and the `.S` switches.
  - Fibers resume from two sources:
    - **synchronous**: `create_run` runs to the first yield, and
      `continue_fiber` resumes immediately. This matches Mako's `run.cc`
      assertions.
    - **event-driven**: `event.wait()` registers a Lion waker for the owner's
      fiber driver and yields. `set()` wakes it, and the driver task resumes the
      fiber on the owner thread.
  - Keep SRPC's per-thread `Reactor` as the fiber/event registry (§1).
    - A thread with no `PollThread` must still support
      `create_run`/`yield_`/`continue_fiber`, and event `set()` → resume, with
      no Lion runtime present. Mako's `run.cc` does this.
    - This is SRPC's job, not Lion's.
  - Convert events one type at a time from per-pass polling to waking on
    change. Today `run_loop` calls `test()` on every waiting event
    (`reactor.rs:1503-1545`).
    - Predicate-style waits whose readiness changes without a `set()` need a
      compatibility task that re-tests them. List every such site before
      converting.
  - `TimeoutEvent` and `sleep_*` move onto Lion timers.
  - `QuorumEvent` keeps `cpp_namespace(::janus)`.
- [ ] **S5. Transport.**
  - The `TcpConnection`/`TcpListener` pollable shims become per-connection
    async read and write tasks, registered with Lion's reactor.
  - `send_frame` from a foreign thread wakes the writer task. That replaces the
    `pending_write_update_` latch and the sweep over every fd
    (`tcp_channel.rs:865-915`, `reactor.rs:3345-3357`).
  - Dispatch:
    - fast RPCs still run inline in the read task;
    - fiber RPCs go through S4;
    - async RPCs go to `spawn_local`.
  - `lion-utility`'s `TcpStream` is not used. It needs tokio traits and
    socket2, and it takes the TLS-waker shortcut.
  - The TCP send path open-codes the frame header (CLAUDE.md). Keep that
    unchanged.
- [ ] **S6. (Optional scope.) Cooperative client waits.**
  - The client `Future` blocks an OS thread on a Condvar, so a nested RPC made
    from a fiber blocks the poll thread.
  - The docs contradict each other on this. `srpc-book.md:342,497` promise a
    cooperative nested RPC; `srpc-book.md:1745` and `srpc-cpp-book.md:319` say
    it blocks.
  - With Lion underneath, `Future` can implement `std::future::Future`, and
    `wait()` can yield when it is called inside a fiber.
  - Fix the doc contradiction regardless of whether this scope is taken.
- [ ] **S7. Retire and re-pin.**
  - **Delete** the 1 ms loop, the linear timeout scan, the disk reactor, the
    `epoll_wrapper` `Pollable` remnants and the ticket ingress.
  - **Re-pin SRPC's ABI** with fresh objects and measured symbol and layout
    evidence. Layout pins such as `sizeof(PollThread)` and the `fiber_channel_`
    offset will move. Record why each interface changed.
  - **Add a separate "dependency providers" inventory** to
    `check_srpc_crate_mode.py`: the generated Lion module list and its measured
    exports. It changes only on a Lion pin or transpiler bump.
    - The importer must import and use the Lion modules SRPC depends on.
  - **Update the other tables:** `EXPECTED_IMPORTS`, `IMPORTER_USE_MARKERS`,
    `module-preambles.toml`, `cpp-module-index.toml` and
    `tests/test-inventory.json`.
- [ ] **S8. Acceptance.**
  - The full gate, and all `-L srpc` CTests.
  - `srpc_runtime_parity`: review its `EXPECTED` keys for fiber sleep order
    (R4) rather than editing them until they pass.
  - The sanitizer battery: address, undefined and thread. TSan matters most for
    Lion's trusted glue.
  - `scripts/run_rpcbench.sh` before and after, in all modes including
    `fast_vec`, reading the spread as CLAUDE.md describes.
  - `scripts/run_microbench.sh --compare`. The codec leaves should not move.
  - A Mako build against the new tree.

## 7. Risks

- **R1. Lion is a research artifact.**
  - It is version 0.1.0: 40 commits by one author, tagged `sosp26-ae`.
  - Its CI runs only Verus. The only runtime tests are
    `lion-utility/tests/{cancel,decisions}.rs`, and they are in a crate SRPC
    will not use.
  - Lion documents two hangs, both outside the verified region
    (`HANG_FIXING_STORY.md`). One was a lost wakeup in the trusted
    readiness-flag protocol, which S2 and S5 must reimplement.
  - SRPC's runtime battery, parity test and TSan are what would cover the
    trusted glue that Lion's CI doesn't.
- **R2. The fast path may regress.**
  - Fast RPCs run inline today, with no task. Lion allocates an `Arc` waker per
    poll (`ext.rs:121`), and S5 adds a task per connection.
  - Removing the 1 ms polling floor should help latency. Only rpcbench can say
    what happens to throughput.
- **R3. The liveness theorem covers less than it sounds like.**
  - It covers *spawned stackless tasks*, under these assumptions
    (`lion-liveness/doc/main.tex:296-316`):
    - every registered source eventually fires;
    - the run queue stays bounded;
    - every task is Ready within a bounded number of polls;
    - the clock strictly increases. Lion's own millisecond-truncated `Instant`
      does not strictly increase between two readings in the same millisecond
      (R4).
  - It excludes the `block_on` root, and SRPC's fibers, events and transport.
    In particular, it excludes:
    - Condvar client waits on a poll thread;
    - fast handlers that block;
    - `continue_fiber` from a loop-less thread.
  - The docs must not claim "SRPC is verified live".
- **R4. Time granularity.**
  - Lion's `Instant` is milliseconds and truncates `Duration` with `as_millis`
    (`lion-reactor/src/types/time.rs:69-113`). SRPC exposes `sleep_us` and
    `TimeoutEvent(us)`.
  - Effective granularity is already about 1 ms because of the epoll timeout,
    but sub-ms sleeps become 0 and their ordering can change.
  - Check the parity sleep-order keys. This is a check, not a blocker.
- **R5. ABI churn.** The reactor, fiber, epoll_wrapper, pollable_proxy and
  fiber_channel rows all move. Mako absorbs this at its next bump, which is
  already 141 commits of drift. Its separate CMake also needs the Lion crates
  (§1).
- **R6. Verus version coupling.**
  - Three things must agree:
    - Lion's vstd git rev;
    - the Verus pass vendored into rusty-cpp;
    - the Cargo git cache the offline gate relies on.
  - Lion's proofs also need Rust 1.91, while SRPC builds on 1.97.1. D2 keeps the
    proofs in Lion's own lane, so SRPC only needs the erased build, which
    already works on 1.97.1 (§8).
  - A Lion bump that moves Verus means re-vendoring T1 in rusty-cpp first.
- **R7. The transpiler's erasure joins the trust base.**
  - T1 is Verus's own code, so the new trusted part is T2 (the tag type,
    reachability pruning) and T3 (the vstd table).
  - Mitigations:
    - fail-closed rules;
    - T6's differential and parity tests;
    - the rule that residue lowering never changes executable data flow. It only
      removes ZST ghost values and spec-only items.

## 8. Evidence gathered for this plan

- **Plain cargo build.** Lion builds with plain stable cargo (`cargo build
  --release` in `lion/`, rustc 1.97.1, 26 s). Its normal dependency closure is
  58 packages.
- **Warnings.** With `RUSTFLAGS=-Dwarnings`, `lion-slab`, `lion-timer-wheel`,
  `lion-executor` and `lion-reactor` each build with 0 warning or error lines.
- **`cargo expand --lib` of Lion@`aa5bebe`:**

  | Crate | Expanded lines | `vstd::prelude` | `View` | `Ghost` | `.set(` |
  | --- | --- | --- | --- | --- | --- |
  | executor | 2,056 | 25 | 9 | 2 | 7 |
  | reactor | 2,106 | 31 | 16 | 12 | 3 |
  | timer-wheel | 608 | 3 | 7 | 0 | 0 |
  | slab | 109 | 1 | 4 | 0 | 1 |
  | utility | 3,883 | 17 | 0 | 42 | 0 |

  - `lion-utility` is not planned for use.
  - None of the crates uses nested `tokenized_state_machine!`,
    `struct_with_invariants!`, `atomic_with_ghost!` or `calc!` macros.
- **Verus erasure** (`~/.cargo/git/checkouts/verus-*/db81a74/source/builtin_macros/src/`):
  - `EraseGhost::{Keep, Erase, EraseAll}` are defined at `lib.rs:53-81`.
  - Plain rustc gets `EraseAll` (`lib.rs:158-161`).
  - `syntax.rs` is 5,386 lines and uses 43 `proc_macro::` items.
  - The crate is MIT-licensed.
  - `verus_syn` is Verus's fork of syn, about 61k lines.
- **Trusted base.** 126 `external_body` items, plus 27 whole trusted files
  (`TCB_and_limitations.md:396-405`).
- **rusty-cpp today** (`third-party/rusty-cpp/transpiler/src/`):
  - Verus handling is limited to `#[cfg(verus)]` and dropping
    `verus_spec`/`verus_verify` (`cpp_default_args.rs:549-650`).
  - `verus!` hits the TODO fallback (`codegen/emit_expr.rs:3022-3024`).
  - Path dependencies are walked (`main.rs:2712-2745`); registry dependencies
    are not (`main.rs:2764-2769`).
- **SRPC runtime facts:**
  - 1 ms epoll timeout: `epoll_wrapper.rs:124`.
  - Per-thread lazy reactor: `reactor.rs:3088`.
  - Condvar client wait: `client.rs:503-573`.
  - Mako pin distance: `git rev-list --count 683c506..HEAD` = 141.
