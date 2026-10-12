# Plan: SRPC's TCP transport on Lion's TcpStream

Status legend: `[ ]` not started · `[~]` deferred with reason · `[x]` done.

**Status: DONE (2026-10-05).** SRPC's TCP transport runs on Lion's
`lion-net` `TcpStream`/`TcpListener` on SRPC branch `lion-tcp`, pinned to
Lion `d697b77` (local branch `lion/net`) and rusty-cpp `3535f2aa` (local
branch `lion/l3-copy-unwrap`). It is accepted (L4), and performance is
unchanged against `c544c0c` (L5). The owner requested this after the Lion
runtime migration (`docs/dev/lion-runtime-plan.md`) was accepted and
published.

**Pins published (2026-10-12), SRPC branch still local.**
- rusty-cpp `3535f2aa` is merged into rusty-cpp `main` as `92b74759`.
- Lion `srpc-lion` is fast-forwarded to `d697b77`.
- `.gitmodules`' branch hints, `main` and `srpc-lion`, now contain both
  pins, and a clean clone of `lion-tcp` fetches them from GitHub.
- `lion-tcp` itself is not pushed. SRPC `main` has since moved to
  `6f5ca63`, five commits from 2026-10-07 that touch `reactor/reactor.rs`,
  `rpc/client.rs`, `rpc/tcp_channel.rs` and gate scripts. A trial merge is
  textually clean, but the merged tree needs a gate run before it goes to
  `main`.

L1b is optional and was not taken.

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
  stream's readiness flags are owner-thread state: since L1 the stream is
  `Send`, but every use off its reactor's thread fails closed. The
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
- [x] **L2. rusty-cpp.** No *lowering* gap on SRPC's path (L0). G1-G3 (a
  return-type-only generic, `&dyn Trait` from another crate, generic dispatch
  to an escaped method name) are worth general fixes later.

  L3's first C++ run found one **runtime-header** bug, now fixed as rusty-cpp
  `3535f2aa` (local branch `lion/l3-copy-unwrap` on `7e0c201f`; pinned by
  SRPC `006547a`).
  - **The bug:** `rusty::Option<T>::unwrap()` on an lvalue emptied the
    Option for every `T`. Rust's `place.unwrap()` on a `Copy` Option copies
    and leaves the place `Some`.
  - **What it broke:** lion-net's verified `IoKernel::poll_step` reads
    `self.rid.unwrap()` on every poll, so every stream's second poll
    panicked, and 5 of 51 suites failed.
  - **Already in the tree:** the same bug was dropping lion-timer-wheel's
    still-valid cached minimum (`invalidate_min`), which cost performance
    only.
  - **The fix:** header and test only, so the generated code is
    byte-identical. T6's Lion snapshot is not regenerated: it pins its own
    copy of Lion at `3496113`.
  - **rusty-cpp's own suite at `3535f2aa`:** the new option test passes, and
    fails on the old header. `lion_parity` passes 1/1 and
    `runtime_move_semantics` 39/0. The full transpiler suite gives 2,853
    passed and 9 failed, against T6's 2,859 and 3 at `7e0c201f`. The L3 run
    attributes all 9 to its environment, before any C++ compiles against the
    header:
    - a `missing-rustc` cargo-metadata message;
    - the opaque-expansion check;
    - seven parity-harness tests that need
      `tests/transpile_tests/either/run_parity_harness.sh`, absent from its
      worktree, or that hit cargo-lock contention.

    It did not re-run `7e0c201f` in the same worktree. So that the 6 extra
    failures are environmental is **unverified**. T8, rusty-cpp's gate
    baseline, remains set aside by the owner.
  - Fix the lowering gaps from L0.
  - Regenerate T6's Lion snapshot (`transpiler/tests/fixtures/lion/regen.sh`),
    whose per-crate counts and crate list are pinned.
  - Bump SRPC's pin (four edits).
  - **Reopened by L3 for one runtime fix (2026-10-04).** L0 and L1 only
    compiled lion-net as C++; L3's battery was its first run, and every
    accept and stream poll after the first aborted the poll thread.
    `rusty::Option<T>::unwrap()` on an lvalue moved the payload out and
    emptied the Option for every `T`, while Rust's `self.rid.unwrap()` on a
    `Copy` `Option<u64>` copies: the verified `IoKernel::poll_step` reads
    `self.rid.unwrap()` on every poll, so the second poll panicked with
    "Called unwrap on None". The same lowering made lion-timer-wheel's
    `invalidate_min` drop a still-valid `cached_min` (a performance-only
    divergence) since S1. Fixed in the runtime header, not the emitter, as
    rusty-cpp `2eaae959` (local branch `lion/l3-copy-unwrap` on top of
    `7e0c201f`): `unwrap()` keeps a trivially copyable payload, the test the
    move constructor already used (`f5767705`). Generated text is unchanged,
    so T6's snapshot needs no regeneration. SRPC pins it in its own commit.
- [x] **L3. SRPC.** Done (2026-10-04) on SRPC branch `lion/tcp-l3` (local):
  `2a0f98e` (Lion pin `3496113` -> `d697b77`), `006547a` (rusty-cpp pin
  `7e0c201f` -> `3535f2aa`, the L2 reopening above), `33cc30d` (the
  transport) and the docs commit that carries this note. See *Result* below.
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
  - **Result.**
    - **Shape.** `TcpTransport` holds `RefCell<Option<lion_net::TcpStream>>`
      and the accept task a `lion_net::TcpListener`; each is created on the
      PollThread and dropped before the descriptor lease. Every poll passes
      SRPC's own operation as a closure (`poll_read_with`, `poll_write_with`,
      `poll_accept_with`) that returns `WouldBlock` exactly on `EAGAIN`:
      `recv(2)` through `srpc_tcp_recv_bytes` into the thread's scratch; the
      outbound drain under the outbound gate, which trims what it sent
      before reporting `EAGAIN`; and the whole accept step (std's accept and
      SRPC's policy). The read budget, close and EOF ordering are unchanged.
    - **Borrowing.** The stream is borrowed only for one poll call; frame
      delivery, `on_error` and `on_closed` run after it ends. The writer's
      closure takes the outbound gate inside the borrow; the cork takes only
      the gate, so the order is fixed.
    - **The cork** is byte-identical: write-through, the idle test and
      `last_send_us_` stay on the raw descriptor and never touch the stream.
    - **No `SocketOps` impl.** Its `accept` must return an `accept4(2)` fd,
      SRPC has no reviewed accept kernel (accept is std's in Rust and
      rusty's in C++, and SRPC needs the peer address), and the writer's
      drain is a closure; an impl would carry two dead methods. No native
      code was added.
    - **Kept:** the `add_proxy` adapter stays on `AsyncFd` (a pollable is any
      descriptor and drains without reporting `EAGAIN`), so
      `lion_fd_poll_ready`/`lion_fd_consume_ready` stay as its private
      helpers (no longer `pub`). Removed: `tcp_transport_{ready,consume,fd}`.
    - **Gate.** `lion_net` is the sixth Lion provider (28 strong symbols;
      Lion total 374 -> 379 at the bump, then 407). `srpc.tcp_channel`
      re-exports `lion_executor` and `lion_net` (no longer `lion_reactor`).
      SRPC's ABI is count-neutral, 2107: `srpc.tcp_channel` +3/-3 and three
      private respellings.
    - **Found:** the first C++ run of lion-net aborted on the second poll of
      every stream and listener: rusty-cpp's `Option::unwrap()` emptied a
      Copy `Option<u64>` (see L2). L0's and L1's transpile checks compiled
      lion-net but never ran it; only `ctest` caught this.
    - **Measured at `33cc30d`:** cargo 400 passed / 0 failed / 1 ignored
      (two new tests); build exit 0 with both gates; `ctest -L srpc` 51/51;
      `verify_lion.sh` on `d697b77`: 10 of 10 crates (net 10 verified).
    - **Tests** (`tcp_transport_rust`):
      - `a_handler_that_closes_its_connection_inside_on_frame_retires_the_transport`:
        a poll-thread abort if delivery held the borrow;
      - `the_reader_and_the_writer_share_one_stream_in_both_directions`: red
        when either task polls the other's direction.
    - **For L4/L5.** Sanitizers were not run here. Per poll, each stream
      operation now passes through lion-net's observe, guard and
      `poll_step` (no allocation: the kernel's log is ghost), and an
      `EAGAIN` builds a `rusty::io::Error` in C++; the separate
      consume-readiness poll the old reader and writer made after an
      `EAGAIN` is gone. The writer's 16-drain budget now wakes the task
      before checking writability, which costs at most one extra poll.
- [x] **L4. Acceptance.** Done (2026-10-05) on `22d8e7f`, whose code
  `e8875e0` shares; only plan docs differ.
  - **From L3's run:**
    - cargo 400/0/1, doctests 2/2, clippy clean;
    - fresh configure and build exit 0, both gates; `ctest -L srpc` 51/51;
    - ABI: SRPC 2107, Lion 407;
    - TSan battery 35/35;
    - `verify_lion.sh` 10/10.
  - **L4 itself:**
    - **Sanitizers:** ASan 35/35 and UBSan 35/35 in fresh
      configurations. Verbose reruns had 0 `AddressSanitizer`, `runtime
      error:` or `LeakSanitizer` lines, with no suppression entries. Probes
      confirmed LSan and UBSan were live.
    - **Drop order:** a gdb trace of `test_rpc_tcp_channel` shows every
      stream and listener registering, then deregistering, then closing,
      in that order.
    - **Mako:** the scratch clone, refreshed to `e8875e0` with Lion
      `d697b77` and rusty-cpp `3535f2aa`, configures and builds `all`
      (exit 0, 0 errors) and passes 24/24. It needed no Mako source change
      beyond S8's adaptations. Only `lion_net` was added to its
      `SRPC_LION_PROVIDERS`. All 44 generated `.cppm` files are
      byte-identical to SRPC's. `test_mako_nontxn_distributed`'s TCP was
      observed on lion-net: 169 `poll_step` calls, 165 Read and 4 Accept.
      Patches: scratchpad `l4-mako-patches/`.
  - **Not a transport defect:**
    `AttachedTcpConnectionTest.ForeignSendsReachThePeerInWireOrder` fails
    only under gdb with breakpoints. Its helper `peer_read_exactly`
    (`tests/rpc_tcp_channel_test.cc:487`) treats an `EINTR` read as end of
    stream. It is a test follow-up.
  - the full gate and `ctest -L srpc`;
  - ASan, UBSan and TSan;
  - `verify_lion.sh`;
  - the D5 allowlist;
  - a Mako source-compat refresh.
- [x] **L5. Performance, before (`c544c0c`) against after.** Done
  (2026-10-05); see *Result* at the end of this item.
  - rpcbench: one interleaved sitting, all five modes, 6 trials, build order
    rotated (`s8/rpcbench_interleave.sh`).
  - `run_rpc_echo_bench.sh --compare c544c0c <after>`.
  - Per-request counters (sends, write-throughs, reader and writer polls,
    allocations per wait), so any delta has a mechanism.
  - Read the spread, not the best number.
  - **Result.**
    - **Builds:** A = `c544c0c` (before); B = `006547a` (Lion `d697b77`
      and rusty-cpp `3535f2aa`, with the old `AsyncFd` transport);
      C = `22d8e7f` (the transport on lion-net); B' = `2a0f98e` (Lion bump
      only, for the Rust lane).
    - **Method:** rpcbench defaults, interleaved, build order rotated per
      trial. Medians (min-max) in qps.
    - **Sitting 1** (n=6 per cell, load1 5.8 / 9.2 / 28.5 min / median /
      max):

      | mode | A | B | C |
      | --- | --- | --- | --- |
      | fast | 1153k (973-1188) | 1135k (1035-1173) | 1141k (1121-1162) |
      | fiber | 656k (628-661) | 645k (640-660) | 641k (614-651) |
      | defer | 636k (613-640) | 633k (620-638) | 631k (617-639) |
      | async | 746k (733-755) | 743k (695-755) | 737k (713-754) |
      | fast_vec | 366k (357-371) | 367k (353-370) | 366k (359-370) |

    - **Sitting 2** (fiber, async and defer, n=8, load1 6.3 / 9.3 / 11.9).
      Paired by trial and pooled (14 trials per mode), B to C is: fiber
      -0.00%, async +0.67%, defer -0.11%. In sitting 1: fast +0.6%,
      fast_vec -0.4%. Server CPU per request moves by at most 0.008 us.
      The only consistent paired lean is fiber at -1.1% (11 of 14 trials;
      +0.017 us server CPU per request). It is inside the spread and lies in
      A to B, the pin bumps, not in the transport.
    - **Rust echo** (`run_rpc_echo_bench.sh --compare c544c0c 2a0f98e
      22d8e7f`, 12 alternating trials per window, load1 about 7-9):

      | | A | B' | C |
      | --- | --- | --- | --- |
      | qps, window 1 | 11.7k (7.8k-14.6k) | 10.9k (8.9k-14.7k) | 10.9k (7.1k-14.7k) |
      | qps, window 64 | 241k (230k-258k) | 235k (228k-259k) | 242k (218k-260k) |
      | qps, window 512 | 369k (353k-390k) | 372k (357k-391k) | 366k (331k-402k) |
      | CPU/req at 512 | 4.08 us | 4.12 us | 4.11 us |
      | p50 / p99, one in flight | 72.5 / 299 us | 74.0 / 301 us | 74.8 / 300 us |

    - **Counters** (instrumented scratch copies of A and C, per completed
      request):
      - readiness operations: `AsyncFd::poll_ready` and consume polls
        before, against observe-and-`poll_step` after. These fall from 9
        to 7 at one in flight, and from 0.225 to 0.161 at window 512.
      - the old consume poll is gone (from 2 to 0 at one in flight).
      - the following are unchanged: `recv`, `send` and `EAGAIN`,
        write-throughs, waker registrations, edge wakes, eventfd signals,
        blocking epoll waits, and allocations (25.011 against 25.011 at one
        in flight).
      - the writer-budget self-wake fired in none of 60 phase-runs.
      - a `rusty::io::Error` per `EAGAIN` in C++ is not new. A built one in
        `lion_fd_consume_ready`; C builds it in the transport closures. Its
        message fits libc++'s short-string buffer, so it costs no
        allocation.
    - **Verdict:** moving SRPC's TCP transport onto lion-net neither cost nor
      gained anything measurable, in either lane. It swaps AsyncFd readiness
      polls for lion-net's verified decision path with slightly fewer
      readiness operations, and identical syscalls, wakes and allocations.
      Logs: scratchpad `l5/runs/`.
