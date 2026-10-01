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
  - On such a thread, a fiber waiting on an event resumes only when someone
    drains: `create_run`'s built-in `run_loop(false, true)` (`reactor.rs:3197`)
    or an explicit `run_loop`. That is true today, and must stay true.
    `set()` never resumes a waiter by itself (`reactor.rs:2515-2545`).
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
- U1–U5 and U8 block **every** route, including Rust-only.
- U6, U7 and U9 are needed for the C++ route.
- Each item that touches verified code needs a re-proof in Lion's CI.

- [x] **U1. Bounded ids.** Done as Lion `srpc/prereqs` `7d872a9`; see the result notes at the end of this item. Resource ids never recycle ("worst-case UNBOUNDED
  memory", `lion-reactor/src/alloc_verified.rs:9-14`, `TCB_and_limitations.md:71-81`).
  - `IO_READINESS` is indexed by raw resource id and never shrinks
    (`readiness.rs:8-21`).
  - `TASK_NOTIFIED` is indexed by a task id that only ever increases
    (`tls.rs:14,30-46`, `handle.rs:24,30`).
  - Every connection, every `Sleep` and every per-request timeout consumes new
    ids, so a server with connection churn leaks memory without bound. The micro
    timer benchmark reaching about 10 GB (`README.md:55`) fits this.
  - *Verified code; re-proof required.*
  - **Design (chosen 2026-09-26).** Keep ids logically unique: a monotonic u64
    never wraps in practice. Every proof that relies on id freshness then stays
    as it is. Change only the **representation** of the id-indexed containers,
    so memory is proportional to live entries rather than to the id space:
    - `lion_slab::Slab` backs both `ResourceSlab` and `TaskSlab`. It becomes a
      std `HashMap<u64, V>` with the same `Map<nat, V::V>` view and the same
      `new`/`insert`/`get`/`remove` contracts. vstd already specifies std
      `HashMap` (`vstd/std_specs/hash.rs:826-1022`), and `get_mut` stays
      `external_body`.
    - `lion_timer_wheel::VecMap` (the wheel's `deadlines` and `positions`)
      changes the same way.
    - The erased code then uses std only, with no new vstd executable types for
      T3.
    - `IO_READINESS` (trusted, `readiness.rs`) becomes a `HashMap<u64, u8>`, with
      an entry removed on deregister. `TASK_NOTIFIED` (trusted, `tls.rs`) becomes
      a `HashSet<u64>`.
    - `resource_slab.rs:819` iterates the slab's `Vec` directly and needs an
      iteration method.
    - Hashing cost on the hot path is measured in S8. A fast id hasher is a
      later optimisation.
  - **Result (2026-09-26, `7d872a9`):**
    - **Proofs:** no client proof changed. Every crate verifies; timer-wheel
      goes from 123 to 117 verified items because the occupancy lemmas are
      gone. A cold `./ci.sh` passes with 0 errors in 8m34s.
    - **Trusted base:** `external_body` items go from 126 to 118. vstd's
      assumed `HashMap` specs now carry what the removed items used to. One
      `#[verifier::external]` iterator is added.
    - **Memory:** the new `lion-utility/tests/bounded_memory.rs` churns about
      200k task ids and 600k resource ids. RSS grows +52.6 MiB on `aa5bebe`
      (the test fails there) and +0.4 MiB after the change.
    - **Speed:** `micro-timer --load 10000` drops from 1.85–2.04 M to
      0.65–0.85 M ops/s, while peak RSS falls from 2.8–3.1 GiB to 11 MiB. TCP
      echo is about −5%, at the edge of the trial-to-trial spread.
    - **Where the speed goes:** about half the loss is SipHash. An unverified
      multiplicative id hasher reached 1.19–1.49 M ops/s. The rest is the cost
      of a hash map against direct vector indexing.
  - [ ] **U1b. Recover container speed (decide at S8, using SRPC's rpcbench).**
    - **Option 1: generational ids** `(index, gen)`. This is the design Lion's
      own comments name (`alloc_verified.rs`). It gives vector indexing with
      memory proportional to the peak number of live entries. It needs
      re-proofs of the allocator's freshness invariants.
    - **Option 2: a fast id hasher.** It needs a new assumed fact about the
      hasher, plus an assumed spec for `HashMap::with_hasher`, because vstd
      only covers the default hasher. That is a small addition to the trusted
      base, and it recovers roughly half the loss.
    - The regression matters only if SRPC's workloads exercise timer churn.
      Today's client timeouts do not use reactor timers.
- [x] **U2. `spawn_local` for `!Send` futures.** Done as `c91784a`: local tasks are stored in `OwnerThreadOnly`, spawning asserts it runs on the owner thread, and `Runtime` is now `!Send`, which a `compile_fail` doctest pins.
  - `spawn` requires `Send` (`lion-executor/src/lib.rs:130`), and the facade's
    `spawn_local` just calls `spawn` (`lion/src/lib.rs:33-39`).
  - SRPC's stackless futures are not `Send` (`async-runtime.md`), and C++
    coroutine frames will not be either.
- [x] **U3. Cancellation and panic isolation.** Done as `a2c8fc8`. A `TaskCell` catches the unwind and checks an abort flag. An aborted or panicked task finishes its poll with Ready, so the verified `poll_task` needs no model change. `JoinError` gains tokio-style `is_cancelled`/`is_panic`/`into_panic`.
  - `JoinHandle::abort` is empty (`join_handle.rs:38`).
  - Nothing catches unwinds at the poll boundary (`executor/ext.rs:111-123`).
  - SRPC has teardown cancellation today (`StacklessCancelReport`,
    `reactor.rs:1059-1096`).
- [x] **U4. Runtime TLS save/restore.** Done as `cf0106f`: `Runtime::new` fails with `AlreadyExists` on a thread that already has a runtime, and drop clears only its own thread-locals. `ReactorGuard` records the identity of its reactor.
  - `Runtime::new` overwrites `CURRENT_HANDLE`, `CROSS_THREAD_CTX` and
    `CURRENT_REACTOR`, and drop sets them to `None` instead of restoring them
    (`lib.rs:69-89,121-128`, `reactor/enter.rs:19-25`).
  - SRPC creates a reactor lazily on *any* thread that calls `get_reactor()`
    (`reactor.rs:3088`).
  - The in-memory channel delivers frames synchronously on the sender's thread
    (`inmemory_channel.rs:255`), so fiber handlers start on that thread
    (`server.rs:1444-1464`).
  - **Design (2026-09-26):** SRPC never needs two Lion runtimes on one thread.
    A `PollThread` owns the only one, and SRPC's lazy per-thread `Reactor` does
    not create a Lion runtime (§1). The minimal fix is therefore:
    - `Runtime::new` on a thread that already has a runtime returns an error
      instead of clobbering its thread-locals;
    - drop clears only what that runtime set.

    Full nesting (saving and restoring the per-thread queues) is out of scope.
- [x] **U5. A driving API SRPC can own.** Done as `343f041`: `Runtime::tick()` and `tick_with_timeout(max_park)`, where `Duration::ZERO` does not block. The bound lives in trusted glue, and no `ensures` clause changed.
  - Today the only entry point is `Runtime::block_on` (`lib.rs:102-118`), and
    the executor module is private.
  - SRPC needs one of two things:
    - a public `turn(timeout)` step; or
    - a clean "`PollThread` = a thread parked in `block_on(worker)`" contract
      with foreign spawn.
  - **Design (2026-09-26):** `block_on` already loops over a private
    `exec.tick()`. Add a public `Runtime::tick()` that runs one iteration of
    that loop (trusted glue, no proof change). A `PollThread` can then run
    either a `block_on(shutdown_signal)` loop or its own `tick()` loop.
    `ExecutorHandle::spawn` already accepts foreign spawns.
- [x] **U6. (C++ route) An OS seam behind a trait, plus optional runtime
  dependencies.** Done as `f11d6f1` (flume removal was `6a40bd8`); see the
  S2 contract recorded under S2.
  - `types/poll.rs`, `interrupt_handle.rs` and `io_event_queue.rs` are already
    `external_body` glue. Put them behind a small `Poller`/`Interrupt` trait.
    SRPC then implements that trait over `srpc_epoll.c` from a canonical module.
  - Replace flume with `std::sync::mpsc` or a `Mutex<VecDeque>`.
  - Put mio, socket2 and tokio behind a default-on feature (for example `mio`).
    SRPC depends with `default-features = false`.
  - Drop `futures-task` and `pin-project-lite`: no uses were found in
    `lion-executor/src`.
  - **Design (2026-09-26):** a trait object owned by the reactor.
    - `lion-reactor` defines `OsBackend` (create, register, reregister,
      deregister a raw fd with an interest; wait for events with a timeout;
      signal and drain the cross-thread interrupt) and holds
      `Box<dyn OsBackend>`.
    - `Source` stops wrapping `&mut dyn mio::event::Source`
      (`types/source.rs`) and carries a raw fd.
    - The mio implementation stays in Lion behind the default `mio` feature.
    - SRPC implements the trait in canonical `reactor/epoll_wrapper.rs` over
      `srpc_epoll.c` (S2).
    - The trait keeps Lion free of a C ABI contract, and it lets Lion test the
      reactor against a mock backend. The price is one dynamic call per park.
- [x] **U7. (C++ route) An `Arc`/`std::task::Wake` waker only.** Done as `d101dd0`: there is no `RawWaker` left in lion-executor. It also fixes a bug where an off-thread reactor wake was lost. The flume removal from U6 landed as `6a40bd8`.
  - Drop the `RawWakerVTable` path (`waker.rs:30-100`).
  - rusty-cpp lowers `impl Wake` with an `Arc<Self>` receiver
    (`transpiler/src/codegen/standard_future.rs:7-100`). Its C++ `rusty::Waker`
    is a pair of `std::function`s, not a vtable.
  - Doing this upstream is preferred over teaching the transpiler
    `RawWakerVTable`.
  - **Measured (2026-09-26):** most of this path is dead.
    - The task raw waker (`create_raw_task_waker`, `TASK_WAKER_VTABLE`,
      `GLOBAL_CTX`) has no caller; `ext.rs:3` imports it and never uses it.
    - The reactor raw waker (`create_reactor_waker_for_current`) is used only
      by `lion-utility`'s TCP listener and UDP code (`listener.rs:105`,
      `udp.rs:107,147`), which SRPC does not use.
    - So U7 is: delete the dead task path, and re-implement the reactor waker
      over `Arc` + `Wake`, or gate it behind the `mio` feature together with
      the utility networking.
- **Results of the U2–U5/U7 batch (2026-09-26):**
  - A cold `./ci.sh` passes with 0 errors, and every crate's verified count is
    unchanged. `external_body` stays at 118. `unsafe` sites in lion-executor
    drop from 15 to 4.
  - Every new behaviour has a test with a negative control:
    `lion-utility/tests/{reactor_waker,foreign_wake,panic_abort,spawn_local,runtime_nesting,tick}.rs`.
  - **Performance:** micro-timer and TCP echo are unchanged. Spawn+await costs
    about 80 ns more (the join channel), and each tick about 30 ns more (the
    mutex queue). `catch_unwind` costs nothing measurable. S8 weighs these.
  - **Constraints for S3:**
    - `Runtime` is `!Send`, so a `PollThread` must construct its runtime on
      its own thread.
    - A loop that interleaves other blocking work must call
      `tick_with_timeout(Duration::ZERO)`. Plain `tick` may park for 100 ms.
  - **Liveness scope:** `abort`, a task panic, or dropping a runtime with live
    tasks can now drop a future that is mid-await on I/O. The theorem does not
    cover those runs (R3), and SRPC's teardown cancellation is one of them.
  - The `spawn_blocking` `Cell` race listed under "worth raising upstream" is
    fixed: it is now an `AtomicUsize`.
- [x] **U9. (C++ route) No `verus!` blocks produced by `macro_rules!`.** Done
  as `b7342a2`: all 19 are written out, and verification is unchanged.
  - `executor/ext.rs` and `reactor/ext.rs` define `macro_rules!` that expand
    to `verus! { impl ... }`: 7 invocations in the executor and 12 in the
    reactor. For example, `reactor_log_action!` is at `reactor/ext.rs:140-160`.
  - T1's pre-pass rejects this form on purpose. It erases literal `verus!`
    items and does not expand macros.
  - Write the 19 invocations out in the source. The change is mechanical, and
    `./ci.sh` must still pass. Doing this upstream is preferred over building a
    `macro_rules` expander into the transpiler.
- [x] **U8. A raw-fd readiness API that honours the `Context` waker.** Done
  as `3496113` (`lion-reactor/src/async_fd.rs`).
  - SRPC's transport (S5) must wait on its own sockets from a task.
  - Lion's building blocks are public (`ReactorHandle::{register_io_resource,
    set_waker, deregister_io_resource}`, `readiness`). But every existing user
    re-implements the edge-triggered readiness-flag protocol, and uses the TLS
    task waker instead of the `Context` waker (`stream.rs:135,169`). The lost
    wakeup in `HANG_FIXING_STORY.md` story 1 was a bug in exactly that protocol.
  - Add one `AsyncFd`-style type to `lion-reactor`:
    `poll_read_ready(cx)` / `poll_write_ready(cx)` / `clear_*_ready()`, over a
    raw fd, owning registration and deregistration. It is the only place the
    readiness protocol is written. It is trusted glue until someone proves it.

- **Results of the U6/U8/U9 batch (2026-09-26):**
  - A cold `./ci.sh` passes with 0 errors. Reactor goes from 206 to 208
    verified items (new `assemble` and `with_poll` constructors). Every other
    crate is unchanged.
  - `external_body` goes from 118 to 119 (`backend_setup`). There are 31
    trusted files (three under `os/`, plus `async_fd.rs`).
  - lion-utility tests pass 51/51, and the backend tests with the mock
    backend pass 4/4 + 4/4. Every negative-control mutation went red.
  - Micro-timer and TCP echo are unchanged.
  - With `--no-default-features`, lion-reactor + lion-executor have no mio,
    socket2 or tokio in `cargo tree -e normal`.
  - **Behaviour fixes:**
    - an error-only or hang-up-only event now wakes both directions (before,
      it woke nobody);
    - EINTR is an empty wait.
  - **The trait as landed** (`lion-reactor/src/os/mod.rs:96-140`):
    - `OsBackend: Send` has `register(fd, token, interest)`, `reregister`,
      `deregister(fd)`, `wait(&mut Vec<OsEvent>, Option<Duration>)` and
      `interrupt() -> Arc<dyn OsInterrupt>`.
    - `OsInterrupt` is `Send + Sync` and has `signal()`.
    - `Reactor::with_backend` and `RuntimeBuilder::os_backend` take the
      backend.
    - Verus rejects `dyn` in verified signatures, so the verified constructor
      takes an opaque `Poll`.
  - **Liveness gap (TCB §2):** `AsyncFd` registers the caller's waker, so its
    wake goes through the task queue, as `Sleep`'s already does. The theorem's
    I/O obligation does not cover `AsyncFd` waits.

Also worth raising upstream, though not blocking:
- ~~`spawn_blocking`'s `Cell` counter sits in a type force-marked `Sync`
  (`blocking.rs:17,20,70-71`).~~ Fixed in `a2c8fc8`: it is now an
  `AtomicUsize`.
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

- [x] **T1. Erasure front end: reuse Verus's own pass. Do not reimplement it.** Done as rusty-cpp `lion/verus-exec` `6663736`; see the result notes at the end of this item.
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
  - **Result (2026-09-26, `6663736`):**
    - **Source:** vendored from the Verus **git** revision `db81a74`, not from
      crates.io. The crates.io `0.0.0-2025-11-10-1957` sources differ from
      `db81a74` in `syntax.rs` and `verus_syn/src/verus.rs`, and Lion's
      lockfile builds from git. The local diff is 39 lines;
      `verus-erase/upstream-diff.sh` prints it.
    - **Where it hooks in:** one chokepoint, `read_crate_source_units` /
      `prepare_crate_source` in `main.rs`. Every pass then sees erased source.
    - **Differential check against `cargo expand`:** slab 4/4 items and
      timer-wheel 13/14 match exactly, and 14/14 once derives are stripped.
      There are 0 mismatches on the executor, the reactor and the spec crates.
    - **Flag off:** output is byte-identical to `1689f438`.
    - **Unit tests:** 2482 passed and 0 failed (2472 baseline + 10 new).
    - **Fail-closed rules:** any other Verus macro, a `verus!` outside item
      position, a macro expanding to `verus!`, or a cfg the pass cannot
      evaluate is a hard error.
  - [x] **T1b. Run the erasure out of process.** Done as `1a63d8f4`. The
    helper binary is `rusty-cpp-verus-erase`, and the transpiler no longer
    links `verus_syn`. With the flag off, peak RSS is 85 MB (1689f438: 84 MB;
    T1: 175 MB). Version and git rev are checked on every response and printed
    by `--verus-build-info`; SRPC's `--build-info` keys stay as they were.
    SRPC's CMake must build `-p verus-erase` in a *separate* cargo invocation,
    or `span-locations` comes back. Linking `verus_syn` turns on
    proc-macro2 `span-locations` for the whole transpiler. On SRPC's crate, even
    with the flag off, peak RSS goes from 84 to 173 MB and CPU time from 56 to
    72 s. Run `verus-erase` as a helper binary that is invoked only under
    `--verus-exec`.
    - Also expose `VERUS_GIT_REV` in `--build-info`, for S1's version-coupling
      check.
- [x] **T2. Lowering the ghost residue.** Done as `2619d788`, in a pre-pass
  (`transpiler/src/verus_lower.rs`) that runs only for crates where stage 1
  erased something. Ghost values become a marker that codegen maps to
  `rusty::Ghost` (`include/rusty/marker.hpp`).
  - A residue scan of the executor and reactor output finds 0 hits for
    vstd/View/Ghost/nat/int/Seq/Map; T1 had more than 1,000.
  - Known limits: pruning is keyed by identifier, and the ghost-flow audit
    does not follow pattern bindings.
  - `gate.sh --with-cache` is still owed for the header change. `EraseAll` still leaves ghost
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
- [x] **T3. The vstd executable surface table.** Done as `2619d788`. Across
  all eight Lion crates, the only vstd executable items used are two
  `Vec::set` calls. The table covers everything `vstd::prelude` brings into
  scope; string methods are errors.
  - A small, explicit table maps the vstd executable items Lion uses to C++.
    `Vec::set(i, x)` becomes index assignment; it appears at
    `lion-slab/src/slab.rs:103`. The 11 `.set(` call sites across executor, reactor and slab (§8) are not all vstd's; Phase 0 classifies them.
  - Phase 0 produces the complete list. Unknown vstd executable items are
    errors.
- **Status after T3 (2026-09-26):**
  - **Slots:** slab has 0, timer-wheel 1 (`derive(Copy)`). The executor has 21
    and the reactor 32, measured on a scratch copy of aa5bebe with U9 written
    out by a script. All of them are T4 (cross-crate `lion_*` paths, `pub use`
    re-exports of spec items, module naming, namespace wrapping) or T5
    (`derive(Copy)`, orphan trait impls).
  - **Compile blockers:**
    - T4: `vec_map::` qualification into a module that exports into the
      global namespace; `--crate-namespace-wrap` emitting an invalid dotted
      namespace; `export using` in crate-root modules; hyphenated module
      names.
    - T5: `Vec::with_capacity` inferring `size_t` instead of `Option<V>`;
      vec_port's `resize_with` iterator lacking `for_each`.
- [x] **T4. Multi-crate crate mode.** Done on rusty-cpp `lion/verus-exec`
  (`59fa35e8` `--crate-graph`, `758a6a86` audit ordering, plus
  `6ea095ec`/`beb47135`/`07b154d7`/`8f64d978`/`47619027`).
  - **Naming:** each dependency crate becomes one named module (for example
    `lion_reactor`) in `namespace lion_reactor`, emitted to
    `<out>/<package>/<module>.cppm`. `crate-graph.json` gives the build order,
    the `ghost_only` crates and the unused crates.
  - **Features:** each crate's features are evaluated separately, and
    ghost-only modules are pruned.
  - **Cross-crate `dyn Trait`:** SRPC implementing Lion's `OsBackend` goes
    through `*DynAdapter`s.
  - **Adapter restriction:** SRPC's `cpp_abi` adapters never target Lion
    items, so the cross-crate adapter restriction stays.
  - **Byte identity:** with the flag off, output is byte-identical to
    `1689f438`.
 Path dependencies are already walked
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
- [~] **T5. Lowering gaps in Lion's executable code.** Partly done
  (2026-09-30, `47619027`):
  - lion-slab and lion-timer-wheel have 0 slots, compile, and give the same
    runtime output as `cargo run`.
  - lion-reactor has 0 slots and compiles.
  - SRPC `lion/s3-core` plus Lion transpiles with 0 slots.
  - **T5e done (2026-09-30, `lion/verus-exec` `dc6e7558`, 19 commits):**
    lion-executor compiles. A C++ program that runs `spawn` and `spawn_local`
    tasks and a reactor timer over a mock `OsBackend` prints exactly what the
    Rust build prints. Unit tests: 2556/0. The parity-matrix failures are
    identical to the `2619d788` baseline.
  - **SRPC `lion/s5-transport` plus Lion:** 0 slots. The build reaches 201
    of 210 steps; 2 root errors remain (T5f).
  - [ ] **T5f.**
    - **E1:** `SrpcEpollBackend::deregister(i32)` and its `OsBackend`
      `deregister(RawFd)` collide as C++ overloads. Dependency manifests
      carry no type-alias targets; put the alias targets in
      `ufcs-traits.json`.
    - **R2:** `AsyncFd`'s `ready.try_io(|_fd| ..)` cannot deduce `R`, and its
      payload is bound through `std::as_const`.
    - **Gate hole (must fail closed):** an `async` block lowers to
      `unreachable_panic` while reporting 0 slots.
    - **G3 (serious; found by S1's C++ half):** every Lion I/O registration
      fails at run time.
      - The lambda emitted for `LocalKey::with` is
        `-> decltype(auto)` and returns `std::move(local)`, a dangling
        reference (Lion `Reactor::enter`, generated `lion_reactor.cppm` ~7028).
      - Clang's `-Wreturn-stack-address` catches it, but SRPC's `-w` hides it.
      - Gate idea: compile with `-Werror=return-stack-address`.
    - **G4:** `std::task::Waker` is missing from the auto-trait table
      (`predicates.rs`). As a result `PollThread`, `Client` and `ClientPool`
      lose `Send`/`Sync` in C++.
    - **G5 (suspected):** `free(): invalid pointer` when a `Waker` stored in
      Lion's `ResourceSlab` (a std `HashMap` since U1) is destroyed. It may be a
      byte-copied `std::function`.
    - **Untriaged:** in C++, `StressTest` sees 110 read callbacks for 100
      writes.
    - **Backlog** (fix if SRPC or Lion hits it):
      - `Pin::new(&mut x)` and `Pin::new_unchecked` spellings;
      - `x.as_ref()` on an `Option` binding `auto&` to a temporary;
      - a lambda return type inferred from a literal arm;
      - designated initializers out of declaration order;
      - an associated-type forward declaration mismatch.
    - The main checkout's `.rusty-modules-cache` is stale against the merged
      headers. `gate.sh --with-cache` has not been run since T7 (see T8).
  - **Original T5e scope (now done):** lion-executor had 0 slots but **24 C++
    compile errors**, in these classes:
    - backward inference of a `let` bound to an `if`/`match` with early
      returns;
    - dependency method signatures missing from the manifests (Duration
      conversions, `?` on `with_backend`);
    - `let (tx, rx) = mpsc_queue()` inference;
    - `pin!`;
    - typing `LocalKey::with`/`try_with` closures;
    - moving move-only values out of match bindings and tuples;
    - a generic variant struct inside a `std::visit` lambda;
    - `Box<dyn FnOnce()+Send>` converted to `rusty::Function`;
    - access through `RefMut<Box<Executor>>`;
    - vec_port iterating Lion's `VecDeque` wrapper.
  - **Probably next, after lion-executor builds, from SRPC's own code:**
    - a `const` move-only `Box` that is later moved;
    - a missing `rusty::io::Error::from(ErrorKind)`;
    - a lifetime-bound `AsyncFd` ready guard bound through `std::as_const`.
 Whatever Phase 0 and its
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
- [x] **T7. A leak in rusty-cpp's btree port (found in S4, 2026-09-27).**
  Fixed as rusty-cpp `lion/t7-btree-leak` `72e66871` (based on `758a6a86`).
  Cherry-pick it onto `lion/verus-exec` once T4/T5 is done.
  - **Root cause:** the runtime headers, not the port.
    `rusty::MaybeUninit::assume_init_read` (`include/rusty/maybe_uninit.hpp`)
    and `NonNull::read` (`include/rusty/ptr.hpp`) copied, where Rust moves.
    - Every btree slot read leaked. That covers remove, inserts that split
      nodes, pop, `into_iter`, `append` and `split_off`, so SRPC's `fibers_`
      leaked on insert too.
    - `alloc.cppm`'s btree and vec `IntoIter` are affected as well.
    - Both functions now relocate, the way `rusty::ptr::read` already does.
  - **Tests:** new drop-balance and relocating-read tests. LSan reported
    1008 B leaked before the fix and nothing after. ctest passes 74/74. The
    negative control fails 12/12.
  - **After the pin bump, in SRPC:**
    - Re-run the sanitizer battery without the five fiber lines in
      `scripts/lsan_suppressions.txt`.
    - `event_deadline_remove_key`'s move-out workaround can go back to a plain
      `remove`.
    - Re-measure `Rc<Fiber>::strong_count` after `fibers_.remove`; expect 2.
  - **Pre-existing, found while testing:**
    - Under ASan, btree `extract_if`/`retain` has use-after-free and
      double-free bugs. SRPC does not call either on a `BTreeMap`; every one of
      its `retain` calls is on a `Vec` or `VecDeque`.
    - Under libstdc++, `std::string` keys break when btree moves slots. SRPC
      builds with libc++ and is not exposed.
- [ ] **T8. rusty-cpp's own gate must be green before the pin bump.** Measured
  at `758a6a86`/`72e66871`, `gate.sh --with-cache` is RED for reasons that
  predate this work:
  - `strpat` is in the parity-matrix crate list (added in `bc41eb7c`), but its
    crate directory was never committed. Under `set -u` that kills the whole
    matrix at `run_parity_matrix.sh:394`.
  - `gate.sh` is committed without its execute bit.
  - The `either` crate fails to compile in the matrix, with and without T7.
    That accounts for 3 transpiler-test failures.
  - `crate_mode_uses_one_cargo_selected_target_dependency_graph_atomically`
    fails for an environmental reason (cargo metadata cannot exec the test's
    fake rustc).
  - With `strpat` dropped locally, the matrix gives 21 total: 7 pass, 12 fail,
    2 known-fail. The 12 are codegen errors in generated code, and they are
    identical with and without T7.
  - Decide with the owner what "green" means for the pin bump. Options: fix
    the matrix, or record the pre-existing failures as the known baseline.

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

- [x] **S0. A fresh build tree must build the battery (a build bug found
  2026-09-26).** Done as `5a2997e`. The `srpc_runtime_imports` probe now does
  `import std; import std.compat;`, so ninja builds the BMIs the modmap
  names. Proven on a fresh tree: without the fix it fails with 14 battery
  errors; with it the build, `ctest` 50/50 and a self-created TSan tree pass.
  - The battery programs and `rpcbench` compile against
    `goal0-battery-modules.modmap`. It names the std module BMIs
    (`@cmake_cxx_std@synth_0.dir/*.bmi`), but nothing in the build graph
    builds them. A fresh `build/` therefore fails in every battery file, and
    so does `scripts/run_sanitizer_battery.sh` on a fresh tree.
  - Existing trees hide the bug with BMIs dated 2026-08-29.
  - Likely fix: extend the scanned `srpc_runtime_imports` probe
    (`tests/runtime_imports.cc`) so it also imports the std modules the modmap
    lists. Verify on a fresh tree.
- [x] **S0b. A client connection accessed from two threads (a pre-existing
  bug found 2026-09-26).** Done as `80b4f9a`. `Client` is now `Send + Sync`.
  Its connection slot is a mutex that is never held across a call into the
  connection. Its scalars are atomics, and its staged configs are
  `ClientCloneCell`s. Short rpcbench client runs failed 4 of 80 before the
  fix and 0 of 80 after it. Full rpcbench trials failed 1 of 67 before and
  0 of 67 after.
  - No `borrow_mut` was involved. `rusty::RefCell` counts borrows in a plain
    `int`. Two threads borrowing at once lost an update, and the count
    reached -1. The next `borrow()` then reported "already mutably borrowed".
  - The wrong claim was in C++. Rust never called `Client` `Sync`, and
    clippy's `arc_with_non_send_sync` fired on `Client::create`. The pin
    that silenced it said "the C++ Arc erases Rust auto traits".
    `rusty::Arc` and `rusty::Function` carry no `Send` bound, so rpcbench's
    reply callback captured the handle with no error.
  - ABI: one row is respelled in place, the fieldwise constructor, and the
    crate still has 2060 symbols. `sizeof(Client)` grows from 224 to 416.
    The importer now pins `is_sync<Client>`.
  - Throughput: no change beyond trial spread in interleaved same-sitting
    runs. Medians moved as follows: fast 1110k -> 1107k, fiber 662k -> 653k,
    defer 638k -> 650k, async 725k -> 711k. Back-to-back blocks on the
    loaded host did show fiber 30% lower, but that gap did not reproduce
    once the trials were interleaved.
  - Original report:
    - In rpcbench, the client thread and the poll thread both call
      `request_async` on the same `Client`. The `RefCell` in
      `Client::connection()` (`rpc/client.rs:1795`) then panics with
      "already mutably borrowed".
    - gdb caught it at the same frame in both the old and the new binaries,
      and it explains every failed rpcbench trial.
    - A `RefCell` reachable from two threads means a `Send`/`Sync` claim is
      wrong somewhere. Find the claim, and fix the ownership rather than the
      symptom.
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
- [ ] **S2. OS backend.** The Lion-independent part is done as `3afd1fe`:
  `SrpcEpollBackend` meets the contract below, and its results are at the
  end of this item. S1 still has to add the `impl lion_reactor::os::OsBackend`
  that forwards to it, once SRPC depends on Lion.
  - Implement Lion's U6 seam in canonical `reactor/epoll_wrapper.rs` over
    `srpc_epoll.c`.
  - SRPC's kernel has no eventfd today (a grep for `eventfd|EFD_|pipe2` is
    empty). Add `srpc_epoll_eventfd_{create,signal,drain}`.
  - New native leaves need review against `scripts/native-kernels.json` and
    `native-abi-bindings.json`. There is no auto-approval.
  - Keep edge-triggered semantics: EPOLLET today, and mio's behaviour in Lion.
  - **The contract SRPC's backend must meet** (recorded from U6, 2026-09-26;
    the full text is the doc comment of `lion-reactor/src/os/mod.rs`):
    - **Tokens, not fds.** The reactor registers each fd under a `usize` token
      and expects that token back. `srpc_epoll_ctl` stores `data.fd`. There are
      two options:
      - a reviewed native-kernel variant that stores a 64-bit token;
      - an fd→token map in canonical Rust. This is safe because the reactor
        never registers an fd twice without deregistering it, and deregisters
        before the fd is closed.
    - **Interrupt.** It needs a new eventfd seam. `signal()` runs from any
      thread and must absorb EAGAIN/EINTR itself, because an error return
      panics in the waker. The backend consumes the interrupt inside `wait`,
      never reports it as an event, and may coalesce signals.
    - **Registration:** always `EPOLLET|EPOLLRDHUP`, plus IN and/or OUT. The
      reactor never uses token 0.
    - **Event flags follow mio:**
      - readable = IN or PRI;
      - writable = OUT;
      - error = ERR;
      - read_closed = HUP, or IN together with RDHUP;
      - write_closed = HUP, or OUT together with ERR, or ERR alone.
    - **Waiting:**
      - a level-triggered backend makes the loop spin;
      - returning at most 100 events per wait is fine;
      - a `None` timeout means block, otherwise whole milliseconds;
      - EINTR comes back as an empty wait. `srpc_epoll_wait` returns -1 with
        errno today, so the seam must return `-errno`, as `srpc_epoll_ctl`
        already does.
    - **Threads:** everything except `signal` runs on the owner thread.
  - **Result (2026-09-27, `3afd1fe`).**
    - **C seam.** Six new leaves. Each makes one system call and returns
      its result or `-errno`:
      - `srpc_epoll_create`, which calls `epoll_create1(EPOLL_CLOEXEC)`;
      - `srpc_epoll_ctl_token`, which stores a `u64` token in
        `epoll_event.data`;
      - `srpc_epoll_wait_tokens`, which returns tokens and flags in two plain
        arrays of capacity 1 to 100, so no record layout is shared;
      - `srpc_epoll_eventfd_{create,signal,drain}`.

      The fd-based leaves stay for `PollThread`. EINTR retries, EAGAIN
      handling, token 0, the flag mapping and timeout rounding are all in
      Rust. The drain is one read, which zeroes the counter, not a
      read-until-EAGAIN loop.
    - **Rust.** `SrpcEpollBackend` has
      `new`/`register`/`reregister`/`deregister`/`wait`/`interrupt`, plus
      `wait_timeout_ms` and `fd`. `SrpcEpollInterrupt::signal`, and the
      `SrpcInterest`/`SrpcOsEvent` mirrors of Lion's types, complete it. The
      pure mappings are `epoll_os_event`, `epoll_interest_flags` and
      `epoll_timeout_ms`.
    - **Parity with Lion's `MioBackend`.** A scratch harness ran both
      backends, Lion `3496113` with mio 1.2.3:
      - mio's own accessors agree with `epoll_os_event` on all 64 flag
        subsets;
      - the timeout rule agrees on 17 of 17 samples;
      - 62 of 63 real-kernel transcript lines are identical. The other line
        is a write-only registration with a full send buffer whose peer
        half-closes. SRPC always asks for RDHUP, so its wait returns early,
        with no events. mio asks for RDHUP only with readable interest, so
        its wait times out. Lion never registers write-only.
    - **ABI.** 2060 -> 2082 symbols, all 22 new rows in `srpc.epoll_wrapper`.
      The module now imports `vec_port.vec`.
    - **Gate.** cargo 322 passed / 0 failed; `ctest -L srpc` 51/51; the TSan
      and ASan batteries 35/35 each. Negative controls went red in both the
      Rust and the C++ lane.
    - **For S1.**
      - The forwarding impl is about 40 lines. The scratch harness compiled
        it against the real `lion_reactor::os` traits.
      - It needs `impl OsInterrupt for SrpcEpollInterrupt`.
      - To avoid an allocation per park, it should reuse one
        `Vec<SrpcOsEvent>`, or `SrpcOsEvent` should become an alias of
        `OsEvent`.
      - SRPC returns at most `events.capacity()` events per wait, capped at
        100. `MioBackend` ignores the capacity.
      - Token 0 is refused with EINVAL.
      - In C++ the method is `register_`.
- [ ] **S3. Core swap.**
  - `PollThread` becomes one OS thread running one Lion runtime.
  - Stackless tasks go to Lion `spawn_local`.
  - Foreign wakes go through the Lion waker.
  - `Job`/`PollCommand` become an mpsc queue drained by a task that is woken on
    send, with no per-pass `try_recv`.
  - `reactor_spawn_stackless_task_with_result` keeps its C++ signature.
- [ ] **S4. Fibers re-hosted on Lion.** The inventory was done on
  2026-09-26 (read-only, from the source, the C++ battery and Mako). Its
  findings drive the order below. Conversion steps 0–5 are done on the
  existing reactor (2026-09-27). No event waited on its owner thread is
  re-tested per pass any more. What remains is the Lion driver, which needs
  S3.
  - Keep `srpc_fiber.c` and the `.S` switches.
  - **Resumption stays deferred.** `set()`, the `vote_*` methods and a direct
    `test()` call only move an event from WAIT to READY. A fiber resumes later,
    when the owner thread drains; `set()` never resumes it.
    - Three things break if the waiter resumes inside `set()`:
      - Mako's quorum code writes state *after* voting (`raft/commo.h:58-68`,
        read at `server.cc:1946,2000`);
      - `vote_*` sets `finalize_event_` after testing;
      - `reactor_stackless_battery.cc:766-821` pins that a `wake()` must not
        complete inline.
    - So the design is an owner-side **ready queue plus a driver**. The
      WAIT→READY edge enqueues the fiber once. The drain repeats until quiet,
      which preserves `test_reactor_extended.cc:113-158`'s three-fiber
      EventChain.
    - On a `PollThread`, a Lion driver task performs the drain. Everywhere
      else, `run_loop` and `create_run`'s built-in drain keep doing it.
    - `create_run` and `continue_fiber` stay synchronous. Mako's
      `paxos/service.cc` captures `[&]` and relies on the fiber starting inside
      `create_run`.
  - **Conversion order:**
    0. [x] **Pre-work, no semantic change.** Done as `897943f` and `3c09fb4`
       on `lion-runtime`. Gate results: cargo 284 passed / 0 failed,
       `ctest -L srpc` 50/50, and the ASan and UBSan batteries 34/34 each.
       There is no ABI change. The negative control makes all three new
       eviction tests fail. Its findings:
       - Timed-out events are now freed once their handles drop. Before, they
         stayed alive for the whole reactor lifetime. Check Mako's quorum and
         `~RaftServer` paths before its next bump.
       - A paused fiber can only be destroyed by tearing down its reactor. The
         `current_fiber().unwrap()->yield_()` idiom leaves an `Rc<Fiber>` on
         its own stack, so nothing destroys a suspended fiber today.

       The original scope of this step was:
       - evict TIMEOUT events from the waiting and composite queues, and keep
         TIMEOUT sticky. Today they are re-tested forever (`reactor.rs:1523-1544`
         keeps them; `test_timeout_race.cc:226-268` pins sticky TIMEOUT);
       - fix the battery tests that hold references into dead stack frames
         (`fiber_test.cc:404-418`, `fiber_runtime.cc:58-141,159-173`).
    1. [x] **The ready queue and driver.** Steps 1 and 2 were done together
       as `f0e2dd2`.
       - **Design:** there is one ready queue per thread, owned by that
         thread's thread-local `Reactor`. It is reached through thread-locals
         that hold plain values, so the pinned `Reactor` layout does not
         change. The WAIT→READY edge enqueues only on the owner thread.
         `run_loop` takes the queue whole on each pass (the length is checked
         first, which keeps an idle pass at O(1)) and repeats until quiet.
       - **Measured:** there is no ABI change (2060 symbols; the reactor
         object has the same 387 strong symbols). cargo 293/0, `ctest` 50/50,
         and ASan/UBSan/TSan 34/34 each. Tests are in
         `tests/reactor_wake_on_change_rust.rs` (9) and
         `ExtendedReactorTest.WakeOnChangeResumesOnlyInTheDrain`.
       - **rpcbench:** fiber mode is about −2.8% on the mean (631–672k against
         651–694k); the ranges overlap and the microbenchmark does not
         reproduce it. The other modes are within the spread.

       The original scope of step 1 was: The hook sits on the WAIT→READY edge in
       `event_test_impl` (`reactor.rs:2530`). It must be reachable through
       `test()` itself, because Mako increments vote counters directly and then
       calls `test()` (`paxos/commo.h:28-35`).
    2. [x] **Leaf events that change only through their own methods** (in
       `f0e2dd2`). `QuorumEvent` is no longer composite. Findings that
       constrain later steps:
       - **Foreign-thread `set()`** on a converted untimed wait no longer
         wakes the waiter. Before, the scan saw it only because of a race on
         the status field.
         - On a timed wait, the waiter still completes at its deadline,
           because the deadline rule re-tests readiness. Mako's `~RaftServer`
           set is on a timed wait, so it degrades to "wakes at the deadline"
           rather than hanging.
         - Events stay owner-thread-only (Rust already enforces this with
           `!Send`). Mako should post a Job instead (see *Cross-thread
           `set()`* below).
       - **Step 4:** a composite's children are never in WAIT; `set()` moves
         them INIT→DONE. The parent hook therefore belongs on the child's
         INIT→DONE path.
       - **Step 5:** predicate events are excluded from the queue. The ticket
         design must make a pinged predicate event eligible for the queue.
       - **S3:** the drain is inline in `run_loop`, so factoring it out for a
         Lion driver adds a strong symbol (re-pinned in S7). The hook must
         wake the driver when the queue goes from empty to non-empty.

       The original scope of step 2 was:
       - `BoxEvent`;
       - `IntEvent` without a predicate;
       - `QuorumEvent`, after which its composite flag with no children
         (`reactor.rs:2388`) is dropped;
       - `SharedIntEvent`, which has no users.
    3. [x] **Timers.** Done as `959a9da`.
       - **Design:** the per-thread wake state from steps 1–2 gains a
         deadline map, `BTreeMap<deadline, Vec<entry>>` in `Time::now(true)`
         microseconds. Equal deadlines keep insertion order.
         - Every timed wait has an entry. Every `TimeoutEvent` also gets
           one at creation, at `wakeup_time_ + 1`, whether or not anything
           waits on it.
         - `check_timeout` keeps its pinned signature. It serves the expired
           prefix in deadline order, and reads the clock only while a
           deadline is pending.
         - The rule "READY if ready, else TIMEOUT" is kept. It now goes
           through `test()`, so a ready event takes the ordinary WAIT→READY
           edge. An event that is already READY is handed over at its
           deadline.
         - Deletion is lazy. Entries hold `Weak` events, and `wakeup_time_`
           names each wait's own deadline (0 when untimed). A threshold
           sweep keeps an early-ending long timeout O(1) amortized.
         - `event_next_deadline_us` (private, generic) is the accessor S3's
           driver will sleep on. It is a lower bound under lazy deletion.
         - The map is used on every thread. On a `PollThread`, S3 can keep
           it and sleep until the next deadline, or move the entries to Lion
           timers.
       - **Measured:** there is no ABI change (2082; all 38 objects keep
         their 2340 strong symbols). cargo 335/0, `ctest` 51/51. Tests are in
         `tests/reactor_deadline_rust.rs` (11),
         `tests/helpers/event_wake_state.rs` (2, crate-internal) and
         `test_timeout_race.cc` Tests 7–11.
       - **Findings:**
         - A foreign-thread `set()` on a timed wait now completes exactly at
           its deadline, READY, and not before. Before, `check_timeout`
           rescued it on the next pass.
         - Expired timers resume in deadline order. Before, a `TimeoutEvent`
           came first and the rest followed in wait order.
         - An unwaited `TimeoutEvent` moves INIT→DONE at its deadline.
         - `run_loop(_, false)` no longer advances `TimeoutEvent` readiness.
           No caller passes `false`.
         - **Pre-existing crash, fixed.** One pass could list an event twice:
           once through the ready queue, and once through `check_timeout`,
           which took READY entries on every pass. A waiter that re-armed the
           event and waited again then tripped
           `reactor_verify(status == TIMEOUT)`. Dispatch now hands over only
           READY or TIMEOUT.
         - **rusty-cpp's btree port leaks on `remove`.** It copies the value
           out and never destroys the original. Its first ASan run showed 3
           leaks, so the map now moves the entries out before `remove`. The
           same bug leaves one `Rc<Fiber>` behind per `fibers_.remove` in
           `recycle()` (measured `strong_count` 3 where 2 is expected). The
           battery's LSan suppressions hide it. It needs an upstream fix and
           a pin bump.
         - **One unexplained LSan report.** The ASan battery on this
           revision once failed `srpc_runtime_parity` with a 24-byte leak.
           The leaked object is the `Arc<Box<ChannelConnectionBase>>` that
           `ClientConnection::bind_channel_direct` allocates, on the client
           path S0b changed, and no reactor frame is involved. It did not
           recur in 3 battery re-runs, 15 runs of that test or 200 runs of
           `test_runtime_parity`. Nothing attributes it yet.

       The original scope of step 3 was: `TimeoutEvent`, `NeverEvent` with a
       timeout, and every `wait_timeout` move to Lion timers on a
       `PollThread`, or to a per-`Reactor` deadline heap drained by
       `run_loop` on other threads. This replaces `check_timeout`'s linear
       scan and `TimeoutEvent`'s clock read. At the deadline, keep the rule
       "READY if ready, else TIMEOUT" (`reactor.rs:1861-1865`).
    4. [x] **Composites.** Done as `cba098e`.
       - **Design:** the parent links live in the wake state as
         `HashMap<child address, Weak parents>`. A child is an
         `Arc<dyn EventPollable>`, and the event layouts are pinned. A new
         trait method would cost 6 UFCS symbols and the vtable rows.
         - The links are made after `reactor_setup_sp_event`, in
           `create_sp_waitany`, `create_sp_waitall_from` and `add_event`.
           Inside `*_make` the parent has no self link yet, and setup's
           `Arc::get_mut` must see no other reference.
         - Any `test()` that finds an event ready tests its WAIT and INIT
           parents, on the owner thread. A waiting parent queues itself. An
           unwaited parent moves INIT→DONE and tells its own parents.
         - This fires on a child's INIT→DONE from `set()`, and on a child
           timer's deadline through step 3's creation entry.
           `MixedEventTypes` needs the second case.
         - Links are pruned lazily, both per list and per map.
       - **Measured:** there is no ABI change (2082 and 2340). cargo 345/0,
         `ctest` 51/51. Tests are in `tests/reactor_composite_rust.rs` (10)
         and 4 new `test_and_event.cc` cases. The "not complete after
         partial sets" cases pass unchanged.
       - **Finding:** `all_events_` keeps every event alive until its own
         pruning threshold. So in a loop that creates and drops composites,
         the number still alive varies from run to run. The link-bound
         tests therefore prune `all_events_` on every round; a first
         version with a loose bound failed once in the independence run.

       The original scope of step 4 was: Add weak parent links, created in
       `add_event`, `waitany_make` and `waitall_make_from`. A child's
       WAIT→READY, its move to DONE, or its timer expiry calls the parent's
       `test()`. A child can be shared, so each child keeps a list of parents.
       `test_and_event.cc:135-157` needs a timer child to notify its parent.
    5. [x] **`FiberChannel`'s predicate.** Done as `63fd412`.
       - **Design:** `EventPing` is a ticket, in the shape of the stackless
         wake ingress. It holds a mutex-bound owner ingress and a `queued`
         flag.
         - `event_ping` runs on any thread, after the caller publishes. It
           queues the ticket once, and returns true on the empty→non-empty
           edge. S3 wakes the driver on that edge.
         - `event_ping_arm` and `event_ping_disarm` run on the owner. They
           map the ticket to the event it re-tests.
         - `run_loop` drains pings first on each pass, with one atomic load
           when none are pending. It clears `queued` before testing only the
           armed events.
         - `FiberChannel`'s frame and closed callbacks ping after they
           publish. `arm_waiter` arms the ticket and `recv_frame` disarms it.
           The arm-then-recheck race handling is kept.
         - `core_self_notifying` is gone: every event waited on its owner
           thread wakes on change.
       - **Measured:** there is no ABI change (2082 and 2340). cargo 353/0,
         `ctest` 51/51, and ASan/UBSan/TSan 35/35 each with 0 reports. Tests
         are in `tests/reactor_ping_rust.rs` (5), in 3 new
         `fiber_channel_rust.rs` tests, and in
         `ExtendedReactorTest.ForeignPingWakesAPredicateWaiterOnTheNextPass`.
       - **Remaining per-pass scan:** none for an event waited on its owner
         thread.
         - `waiting_events_` and `composite_events_` are still scanned, but
           only a wait taken on a thread other than the event's creator
           joins them. Only C++ can do that, since events are `!Send`, and
           such a waiter cannot reach the owner's ready queue.
         - Both queues are otherwise empty. S7 can retire them together
           with the unused `timeout_events_`.
       - **rpcbench:** `1806420` before, this series after, interleaved,
         with the host at load 114–123. Every range overlaps its
         counterpart. Medians: fast 1122k → 1110k, fiber 660k → 664k,
         defer 648k → 652k, async 749k → 731k. No trial failed. No mode
         waits on an event, so this measures only the per-pass checks.
       - **Findings for S3:**
         - Sleep until `event_next_deadline_us`, rounded up, because Lion is
           millisecond-granular.
         - Wake on `event_ping`'s empty→non-empty edge. That needs a driver
           handle in the private `EventPingIngress`, and adding a field there
           is free.
         - The ping, deadline and ready-queue drains are inline in
           `run_loop`. Factoring them out adds strong symbols, to be re-pinned
           in S7.
         - `EventPing` is also a ready-made foreign-safe `set()` for Mako
           (see *Cross-thread `set()`*): publish, then ping.

       The original scope of step 5 was: `FiberChannel`'s predicate, the only
       external predicate in SRPC, the battery or Mako
       (`fiber_channel.rs:141-151`).
       - Give it a ticket with an "already queued" flag, in the shape of the
         stackless wake ingress (`reactor.rs:963-976,1155-1168`).
       - The frame and close callbacks, which run on the poll thread, on the
         in-memory sender's thread, or on any closer's thread, ping the ticket
         after they publish. The ping enqueues on the owner and wakes its Lion
         driver.
       - The owner re-tests only the pinged events.
       - No periodic re-test task is needed: every assignment of `test_` has
         such a publisher.
  - **Update (2026-09-27, after step 5):** `EventPing` (in `63fd412`) is a
    ready-made foreign-safe `set()`: publish the state, then call
    `event_ping`. Mako's `~RaftServer` can use it instead of posting a Job.
  - One unexplained leak report: in the step-3 ASan run, `srpc_runtime_parity`
    once reported a 24-byte leak of the `Arc` that `bind_channel_direct`
    allocates on the S0b client path. It did not recur in 3 battery re-runs,
    15 runs of that test, or 200 runs of `test_runtime_parity`. Watch for it
    in S8.
  - **Cross-thread `set()`.** SRPC never calls `set()` from a foreign thread.
    Events are `!Send`, and foreign publishers go through ingress queues.
    - Mako does, in `~RaftServer` (`raft/server.cc:1826-1830`, and
      `testconf.cc:598` under `RAFT_TEST`). That is a data race today.
    - Either offer a foreign-safe `set` that routes through the ingress queue,
      or require Mako to post a Job. Decide before Mako's next bump.
  - **Timing-sensitive tests** keep passing only if resumption stays
    owner-driven. These pin that:
    - `fiber_rust.rs:180-201`;
    - `fiber_channel_rust.rs:270-287` (not run until the owner drains);
    - the `runtime_parity` keys `timer_order`, `timer_suspended`,
      `deadline_respected`, `pending_before_wake`, `foreign_thread`,
      `completion_on_owner` and `wake_value`;
    - `test_and_event.cc` (not complete after partial sets);
    - `test_timeout_race.cc`.

    Review them against the design rather than editing their expectations.
  - The ABI pins the `EventPollable` generated C++ method layer
    (`check_srpc_crate_mode.py:73-75,215-244`). A new trait method is a ratchet
    edit.
  - `QuorumEvent` keeps `cpp_namespace(::janus)`.
- [~] **S5. Transport.** The Rust lane is done (2026-09-30); the C++ lane
  waits on T5e and the pin bump.
  - **Branches** (SRPC main repo):
    - `lion/s5-transport` (`bbf7fa2`): each connection is a reader task and a
      writer task over `AsyncFd`; `send_frame` wakes the writer directly.
    - `lion/s5-writethrough` (`daf3d92`): a foreign sender writes through an
      empty buffer.
    - `lion/s5-cork` (`95057e3`/`7e51a80`): write-through only when the
      connection's last `send(2)` is at least 20 µs old
      (`kTcpWriteThroughIdleUs`).
  - **Adopted, tentatively, as the Rust-lane tip:** `lion/s5-cork`. S8 must
    re-check the gain with rpcbench.
  - **Rust echo benchmark, release, 12 alternating trials, medians:**

    | Build | w=1 qps | w=64 qps | w=512 qps (range) | CPU/req at w=512 | p99 at w=1 |
    | --- | --- | --- | --- | --- | --- |
    | old 1 ms loop | 817 | 108k | 269k | 4.32 µs | 1436 µs |
    | S5 | 4836 | 182k | 248k | 5.07 µs | 449 µs |
    | write-through | 8842 | 235k | 282k | 6.97 µs | 310 µs |
    | cork 20 µs | 9184 | 223k | 371k (348k–393k) | 4.01 µs | 310 µs |

  - **Tests:** 406 passed / 0 failed on the cork branch.
  - **Notes to carry forward:** the per-branch result notes live in each
    branch's copy of this plan.
    - EOF behind data on the same edge is now seen.
    - A hard error recorded by a foreign sender is reported on the poll
      thread as `ConnectionReset`.
    - New ABI rows for S7: `TcpConnection::{writer_, send_error_,
      last_send_us_}` and `kTcpWriteThroughIdleUs`.
  - **The original S5 design:**

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
  - **`AsyncFd` rules (from U8):**
    - It is `!Send`: create, poll and drop it on the runtime thread
      (`spawn_local`).
    - The fd must be non-blocking and must stay open until the `AsyncFd` drops.
    - Each direction supports one waiter, which gives one reader task and one
      writer task per connection.
    - A short read does not clear readiness, so expect one extra EAGAIN read
      per wake. Measure it in S8.
    - Depend on `lion-reactor` and `lion-executor` with
      `default-features = false`. Never depend on `lion-utility` or `lion`:
      feature unification would bring mio back.
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
  - **Revert S2's `derive` workaround.** `SrpcInterest` and `SrpcOsEvent`
    (`reactor/epoll_wrapper.rs`) hide their derives behind
    `cfg_attr(not(any()), derive(...))`, because the pinned transpiler emits
    `derive(Copy)` as a hand-attention slot. Once the T5 `derive(Copy)`
    lowering is in the pinned transpiler, return them to plain derives. Update
    CLAUDE.md's count of that spelling to match: 19 sites before S2, 21 after.
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
  - It excludes the `block_on` root, SRPC's fibers, events and transport, and runs where a task is aborted, panics, or is dropped with its runtime while it waits on I/O (U3).
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
