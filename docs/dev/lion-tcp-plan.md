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

**What this buys, precisely.** The verified part is the decision `Arm <=>
(!was_ready || would_block)` and its log. Readiness flag clearing, waker
registration and the syscalls remain trusted glue either way. This is "TCP
waits are in Lion's verified log", not "verified TCP".

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

- [ ] **L0. Spike (go/no-go).** In the Lion worktree (`scratchpad/lion-u`),
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
- [ ] **L1. Lion.**
  - Land the refactor on `srpc-lion` locally.
  - The stream and listener sit on `AsyncFd`'s protocol plus `IoKernel`, and
    store `cx.waker()`.
  - Fix audit items 1, 2 and 4, decide 3, and remove or justify 5.
  - Add tests: port `lion-utility/tests/async_fd.rs`-style cases to the
    stream, and add regression tests for items 1 and 2.
  - Update `TCB_and_limitations.md`.
  - `./ci.sh` passes with 0 errors.
- [ ] **L2. rusty-cpp.**
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
