# Plan: SRPC's TCP transport on Lion's TcpStream

Status legend: `[ ]` not started · `[~]` deferred with reason · `[x]` done.

**Status: IN PROGRESS (started 2026-10-04).** Requested by the owner after
the Lion runtime migration (`docs/dev/lion-runtime-plan.md`) was accepted and
published. Everything in this plan stays local until the owner asks for a push:
`srpc-lion` will diverge locally from the published Lion branch, and a cold
clone cannot fetch new pins until they are pushed.

## 0. The short version

Today SRPC's TCP transport (`rpc/tcp_channel.rs`) runs a reader task, a writer
task and an accept task per connection or listener. Each waits on readiness
through `lion_reactor::AsyncFd`, which is trusted glue. Socket syscalls are
SRPC's reviewed C kernels (`rpc/srpc_{net,connect,server}.c`).

Lion's own `lion_utility::net::tcp::{TcpStream, TcpListener}` put each poll's
suspend-or-complete decision through a **verified** `IoKernel::poll_step`. It
logs every I/O wait in Lion's ghost event log, with a proved wake-up
guarantee (`net/tcp/{kernel,proof}.rs`). Moving SRPC onto them means SRPC's
TCP waits join the log Lion's liveness argument is stated over.

**What this buys, precisely (corrected by L0).** The verified part is the
decision `Arm <=> (!was_ready || would_block)` and its log.

- The log is a per-utility `IoKernel` log. No utility kernel has a
  machine-checked link to Lion's composed liveness theorem (TCB §4), and the
  kernel's waker is a ghost `arbitrary()`.
- Fixing audit item 4 (store `cx.waker()`) moves TCP wakes to the TaskWake
  queue. That is outside the composed model's io path, where `AsyncFd` and
  `Sleep` already are (TCB §2).

So SRPC gains a machine-checked suspend/complete decision, enforced by
construction: a `Pending` without an armed waker can no longer be written.
It gains nothing in the composed theorem's io obligation compared with
today. Readiness flag clearing, waker registration and the syscalls remain
trusted glue. Restoring the composed-model match is the optional **L1b**
below.

**What it costs.** Lion's stream cannot be used as it is:

- `lion-utility` depends unconditionally on `mio`, `socket2` and `tokio`.
  SRPC's D5 allowlist forbids all three, and none of them can be transpiled
  to C++.
- Its trusted glue is less careful than `AsyncFd`. The pre-plan audit
  (2026-10-04) of `lion-utility/src/net/tcp/stream.rs` at `3496113` found
  five problems:
  1. `try_io(_interest, f)` ignores `interest` and always clears the
     *readable* flag on `WouldBlock`. Used for a write it clears the wrong
     flag. That is the lost-wakeup class of `HANG_FIXING_STORY.md` story 1.
     **A Lion bug.**
  2. `poll_read_ready` busy-spins when the stream is not readable. It calls
     `cx.waker().wake_by_ref()` and returns `Pending` without registering
     with the reactor.
  3. `poll_read` and `poll_write` clear readiness after a short transfer
     (`0 < n < len`). Tokio does the same for TCP, so this is defensible for
     stream sockets. It still contradicts `AsyncFd`'s invariant 2 ("only an
     observed `WouldBlock` clears a flag").
  4. Every arm allocates a fresh task waker (`create_reactor_waker_for_current`)
     instead of storing `cx.waker()`. That is an allocation per wait, and the
     wrong sub-future is woken under combinators.
  5. `OwnedReadHalf` and `OwnedWriteHalf` are `unsafe impl Send` over an
     `UnsafeCell`.

So the work is mostly in Lion. Rebuild `TcpStream` and `TcpListener` on
`AsyncFd`'s readiness protocol plus the verified `IoKernel`, over a pluggable
socket-ops seam, with mio, socket2 and tokio optional. This also leaves Lion
with one readiness implementation instead of two. SRPC then adopts the stream
in its reader, writer and accept tasks.

## 1. Fixed constraints

- **The cork stays a raw-fd path.** S5's write-through (`kTcpWriteThroughIdleUs`)
  is a `send(2)` from a *foreign* thread under the outbound mutex. Lion's
  stream is `!Send`, and its readiness flags are owner-thread state. The
  stream is used only by the owner's reader, writer and accept tasks. A
  foreign send that hits `EAGAIN` wakes the writer and never touches a flag.
  The cork is where S5's gains came from (817 to 9184 qps at window 1), so a
  design that loses it fails this plan.
- **SRPC's connect, listen and accept policy stays SRPC's.** That covers
  nonblocking connect in C, reconnect, and the job hand-off to the
  PollThread. Lion's `connect`/`ToSocketAddrs` are not used. Instead, the
  stream wraps an already-connected fd, and the listener wraps a bound,
  listening fd. Socket syscalls stay in SRPC's reviewed C kernels, reached
  through the new seam.
- **Descriptor lease order** (`tcp_channel.rs:1589-1606`): the fd outlives
  the stream, as it outlives the `AsyncFd` today.
- **D5 is unchanged in spirit.** mio, socket2, tokio, flume, futures-task and
  pin-project-lite must still appear nowhere in `cargo tree -e normal`. New
  Lion crates join `LION_CRATES` explicitly.
- **Lion's `ci.sh` stays green.** `IoKernel` and `proof.rs` must stay
  byte-identical where possible. Any proof change gets a re-verify and a note.

## 2. Phases

- [x] **L0. Spike (go/no-go): GO** (2026-10-04, Lion `lion/tcp-spike`
  `b746cac`, `56e0cb9`; see *Result* below). In the Lion worktree (`scratchpad/lion-u`),
  off `srpc-lion` (`3496113`):
  - Make `lion-utility`'s mio, socket2 and tokio `dep:`-optional behind its
    existing `mio` feature, with socket syscalls behind a small trait (the U6
    `OsBackend` precedent).
  - Decide the packaging by transpile surface. Either a new crate (for
    example `lion-net`: `kernel.rs`, `method.rs`, `proof.rs`, and a generic
    stream and listener) or `lion-utility` with `default-features = false`,
    which brings fs, sync, time, task and udp into SRPC's crate graph.
  - Transpile the result with `--verus-exec --crate-graph` and list the
    lowering gaps.
  - **Go** if the gap list is modest. **No-go, or ask the owner**, if the
    transpiler work exceeds the SRPC work, for example a std::net-shaped
    surface.
  - **Result.**
    - **Packaging:** a new crate, `lion-net`.
      - It holds `kernel.rs`, `method.rs` and `proof.rs`, which are
        byte-identical renames of `3496113`'s.
      - It also holds a generic `TcpStream`/`TcpListener` over a
        `SocketOps` trait: `recv`, `send` and `accept` on a raw fd. SRPC
        supplies its C kernels through a zero-sized implementation.
      - The stream wraps a fd it does not own, so the lease order is
        kept.
      - Its API is `from_raw_fd`, `poll_{read,write}_with(cx, f)`,
        `poll_{read,write}(cx, &ops, buf)`, `poll_accept[_with]`,
        `poll_*_ready` and `try_*_io`.
      - Each poll observes the `AsyncFd` flag, runs the operation through
        the ready guard (which clears only that direction, only on
        `WouldBlock`), feeds `poll_step` the truthful facts, and on `Arm`
        registers `cx.waker()`.
    - **lion-utility:** mio, socket2 and tokio are `dep:`-optional behind
      its existing `mio` feature. Its tokio-shaped `TcpStream`,
      `TcpListener` and `UdpSocket` are thin shells over `lion-net`.
    - **Audit items:** 1, 2, 4 and 5 are fixed, and no `unsafe impl` is
      left in the TCP and UDP glue. Item 3 is decided for invariant 2 (only
      `WouldBlock` clears). Each fix has a test that went red under a
      mutation.
    - **lion-reactor changes:** `AsyncFd` is `Send + Sync`; its guard is
      `!Send`. The reactor epoch is process-wide, so use on the wrong
      thread fails closed. New: `AsyncFd::{resource_id, try_read_ready,
      try_write_ready}`.
    - **Results:**
      - Lion tests: lion-utility 54/0, lion-net 6/0, reactor 4, executor 5.
      - `./ci.sh`: "All checks passed". lion-net verified 10, and the
        negative control made it fail.
      - An SRPC-shaped `cargo tree -e normal` has 23 packages and no
        forbidden crate.
    - **Transpile (rusty-cpp `7e0c201f`):** 0 hand slots and 0 compile
      errors for `lion-net` and an SRPC-shaped probe. Packaging (b),
      `lion-utility` without default features, gave 86 compile errors.
      The 10 lowering gaps met (G1-G10) are all avoided on SRPC's path;
      none is large. **L2 needs no transpiler change.**
    - **Estimate:** L1 about 1 day (mostly done in the spike), L2 0, L3 2-4
      days.
- [~] **L1b. (Optional; owner's call; not in scope by default.)** Restore the
  composed-model match:
  - register a reactor-source waker when `cx.waker()` is the task's own,
    and `cx.waker()` otherwise;
  - this needs lion-executor to cache one waker per task. `poll_task_contained`
    makes a fresh one on every poll, so `will_wake` never matches.
  - Verified executor code; about 1-3 days.
- [x] **L1. Lion.** Done (2026-10-04) as Lion `lion/net`, `3496113..d697b77`:
  - `40a5c7a` reactor: `AsyncFd` Send + Sync over a process-wide epoch;
  - `0c8d128` net: add lion-net, with `kernel.rs`, `method.rs` and `proof.rs`
    as 100% renames;
  - `a4b956b` lion: `lion::io::Interest`, which pingora's recvmsg uses;
  - `850e780` utility: sockets on lion-net, with mio, socket2 and tokio
    optional;
  - `d697b77` docs: TCB §2 and §4 now say TCP and UDP waits are TaskWake
    and the IoKernel log is per-utility.

  Every commit builds with default features and with
  `--no-default-features`, across 7 downstream crates.

  Measured:
  - `./ci.sh`: "All checks passed" in 9m39s, with 0 errors (net 10,
    utility 80, reactor 208, executor 99, liveness 556). The `poll_step`
    negative control fails with "postcondition not satisfied".
  - Tests: reactor 5, executor 5, net 14 (6 real-socket, 8 mock), utility
    56 (24 without default features; at `3496113` that configuration did
    not compile), facade 2. The net and socket suites were clean in 20 of
    20 repeated runs.
  - Each of the five audit fixes, and the drop and deregister path, has a
    mutation that turns its tests red.
  - Default dependency graphs gain only `lion-net`. tokio's `Interest` impl
    was dropped rather than gated, because it needed tokio's `net` feature.
    `try_io` takes Lion's `Interest`.
  - The SRPC-shaped graph has 23 packages and no forbidden crate.
  - Transpile at rusty-cpp `7e0c201f`: 0 hand slots and 0 compile errors.
    `poll_{read,write,accept}` lower to `template<typename O>` over
    `const O&`.

  For L3:
  - Polls take `&mut self`, with one registration per fd, so the reader
    and writer share one stream through `Rc<RefCell<_>>`, borrowed only
    inside a poll.
  - `from_raw_fd` needs the thread's reactor.
  - A `Ready` from `poll_*_ready` obliges the caller to `try_*_io` until
    `WouldBlock`.
  - Off-thread use fails with `ErrorKind::Other`.
  - Land the refactor on `srpc-lion` locally.
  - The stream and listener sit on `AsyncFd`'s protocol plus `IoKernel`, and
    store `cx.waker()`.
  - Fix audit items 1, 2 and 4, decide 3, and remove or justify 5.
  - Add tests: port `lion-utility/tests/async_fd.rs`-style cases to the
    stream, and add regression tests for items 1 and 2.
  - Update `TCB_and_limitations.md`.
  - `./ci.sh` passes with 0 errors.
- [~] **L2. rusty-cpp.** Not required (L0: no gap on SRPC's path). G1-G3 (a
  return-type-only generic, `&dyn Trait` from another crate, generic dispatch
  to an escaped method name) are worth general fixes later.
  - Fix the lowering gaps from L0.
  - Regenerate T6's Lion snapshot (`transpiler/tests/fixtures/lion/regen.sh`),
    whose per-crate counts and crate list are pinned.
  - Bump SRPC's pin (four edits).
- [ ] **L3. SRPC.**
  - `Cargo.toml` dependency, and `LION_CRATES` in
    `scripts/check_rust_independence.py`.
  - `rpc/tcp_channel.rs`: reader, writer and accept over the Lion stream and
    listener, with the cork unchanged.
  - Tests: `tests/tcp_transport_rust.rs` and `tests/helpers/tcp_cork.rs`.
  - Gate tables in `scripts/check_srpc_crate_mode.py`: `DEPENDENCY_*`,
    `EXPECTED_DEPENDENCY_REEXPORTS`, importer coverage and the ABI re-pin.
  - `SRPC_LION_PROVIDERS` in CMake, and `test_goal0_contracts.py` totals.
  - Books and CLAUDE.md, which describe `AsyncFd` tasks and are gate-checked
    (books).
- [ ] **L4. Acceptance**, one quiet run on the final SHA:
  - the full gate and `ctest -L srpc`;
  - ASan, UBSan and TSan;
  - `verify_lion.sh`;
  - the D5 allowlist;
  - a Mako source-compat refresh.
- [ ] **L5. Performance, before (`c544c0c`) against after.**
  - rpcbench: one interleaved sitting, all five modes, 6 trials, build order
    rotated (`s8/rpcbench_interleave.sh`).
  - `run_rpc_echo_bench.sh --compare c544c0c <after>`.
  - Per-request counters (sends, write-throughs, reader and writer polls,
    allocations per wait), so any delta has a mechanism.
  - Read the spread, not the best number.
