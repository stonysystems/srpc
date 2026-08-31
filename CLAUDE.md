# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## The one thing to understand first

SRPC is a **C++23 named-module RPC library whose every module provider is generated, not hand-written.**
All 37 production modules are canonical **Rust** files living at their historical C++ paths
(`base/`, `misc/`, `reactor/`, `rpc/`), and the pinned `rusty-cpp` transpiler generates
`srpc.<name>.cppm` from them. Two consumers read the *exact same bytes*:

- **rustc/Cargo** — via `src/lib.rs`, a *generated* crate index of
  `#[path = "../rpc/frame_codec.rs"] pub mod frame_codec;` declarations. `src/` holds nothing else.
- **rusty-cpp** — one whole-crate invocation (`--crate Cargo.toml`) emits all 37 `.cppm` providers,
  which are the only *providers* in `libsrpc.a`.

The hand-written C++/C that remains is seam, never logic: **`reactor/epoll_platform_linux.cc`**
(the platform *implementation* unit for `srpc.epoll_wrapper` — the one hand-maintained C++ TU, and the
sole inline-Rust DSL carrier), eight plain-C kernels (`*/srpc_*.c`) and their five `srpc_*.h` headers,
`reactor/fiber_context_{x86_64,aarch64}.S`, and the C++ headers: `srpc.hpp`, `std_compat.hpp`,
`base/all.hpp` (a five-import `base/` umbrella), seven `#pragma once` shims whose whole body is
`import srpc.<module>;`, `rpc/frame_codec.hpp` (that import plus load-bearing `<queue>`/`<stack>`), and
`rpc/fiber_channel.hpp` (an `#include <memory>` anchor with no import at all). Finding
`rpc/frame_codec.hpp` beside `rpc/frame_codec.rs` is not a second implementation — never change behavior
by editing a shim.

"Dual compile" is not two implementations. It recompiles each generated `.cppm` into its own object
(imports resolved against CMake's configured BMIs), links one shared importer program twice — once over
those fresh objects placed ahead of `libsrpc.a`, once over `libsrpc.a` alone — runs both, and requires
the per-module `nm` strong-symbol sets to satisfy `production == generated + PLATFORM_IMPL_SYMBOLS`
(1961 provider symbols plus 5 platform symbols).

Consequence that governs almost every edit: **a change to a `.rs` file is simultaneously a Rust change
and a C++ ABI change.** A green `cargo test` does not mean the C++ still builds or keeps its ABI.

## Commands

**Before you commit.** There is no CI — no `.github/`, nothing runs on push. This sequence *is* the
safety net, and the `Verified:` paragraph the commit convention demands is copied out of its output:

```sh
RUSTFLAGS=-Dwarnings cargo test --locked --workspace --all-targets  # -> passed/failed counts
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release             # -> configure exit code
cmake --build build --parallel 4                                    # -> build exit code (ALL pulls in both gates)
ctest --test-dir build -L srpc --output-on-failure                  # -> must say 15 tests, not 6
```

Submodules must be initialized before anything CMake- or transpiler-related
(`third-party/rusty-cpp` pinned at `21fc8f7b…` on branch `goal0-on-main`, plus `third-party/googletest`):

```sh
git submodule update --init --recursive
```

**Rust lane** (no C++ toolchain needed — the fast inner loop):

```sh
cargo test --locked --workspace --all-targets
RUSTFLAGS=-Dwarnings cargo test --locked --workspace --all-targets   # what the gate actually runs
cargo clippy --locked --workspace --all-targets -- -D warnings       # a lint here breaks the C++ build

cargo test --test frame_codec_rust                                   # one test file (stem of tests/<stem>.rs)
cargo test --test stat_rust -- --exact some_test_fn_name             # one test function
```

**C++ lane** (needs Clang ≥ 22 with libc++, CMake ≥ 3.30, Ninja, Cargo, Python 3):

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 4 --target srpc_goal0_dual_compile
ctest --test-dir build -L srpc --output-on-failure
```

Budget for it: a cold C++ lane is minutes, not seconds — CMake builds the pinned transpiler from
source, builds vendored googletest, and compiles every module BMI under `-march=native`; the battery
suites are `RUN_SERIAL` with `TIMEOUT 600` because they drive real epoll threads and fibers. Don't start
one to answer a Rust-only question; always budget for one before committing a canonical `.rs` change.

```sh
ctest --test-dir build -L runtime_battery --output-on-failure   # the 8 battery binaries
ctest --test-dir build -R '^test_fiber$' --output-on-failure    # one suite (name = CMake TARGET name)
./build/test_fiber --gtest_filter='FiberTest.SleepUsZero'       # one gtest case
```

Seven of the eight battery binaries are gtest; `test_reactor_minimal` is a plain program with no gtest,
so `--gtest_filter` does nothing to it.

**Individual gates** (all also run inside `srpc_goal0_source_gate`). Only the two Python suites run
standalone — the other two exec the *built* transpiler at
`third-party/rusty-cpp/target/release/rusty-cpp-transpiler` and fail closed without it
(`inline-Rust emitter is unavailable`, exit 1; `no transpiler at …`, exit 2). Build it with
`cmake --build build --target build_rusty_cpp_transpiler`, or point at one explicitly — but note the two
take it differently: `extract_srpc_rust.py --transpiler <path>` (also honours `$RUSTY_CPP_TRANSPILER`),
versus `srpc_dsl_check.sh <path>` as a bare positional argument (it reads neither the flag nor the env var):

```sh
python3 scripts/tests/test_goal0_standalone.py   # manifest <-> lib.rs <-> CMakeLists inventory agreement
python3 scripts/tests/test_goal0_contracts.py    # fail-closed negative controls on the ratchets
python3 scripts/check_facade_shadow.py           # no rusty-rustc stub may shadow a canonical fn
python3 scripts/extract_srpc_rust.py --check     # needs transpiler; src/lib.rs vs rust-modules.toml (--write regenerates)
bash scripts/srpc_dsl_check.sh                   # needs transpiler; DSL drift in reactor/epoll_platform_linux.cc
```

`test_goal0_contracts.py` is itself a "green is not proof" trap: without a transpiler the whole
`GateContractTests` class is skipped in `setUpClass`, so its 12 tests — every ABI/import/digest negative
control — are never counted. The run prints `Ran 10 tests` / `OK (skipped=1)`, one skip line for twelve
lost tests, and exits 0. A real run says `Ran 22`.

**Verus** (separate lane, not wired into CMake or ctest):

```sh
VERUS_HOME=/path/to/verus-dist scripts/verify_srpc.sh
```

**Sanitizers** are a whole-configuration switch, so use a separate build dir:
`cmake -S . -B build-asan -G Ninja -DSRPC_SANITIZER=address` (`none|address|thread|undefined`).

There are two gate targets, both in `ALL`: `srpc_goal0_source_gate` (source side — DSL check,
extraction check, both Python suites, `cargo test`, `cargo clippy -D warnings`) and
`srpc_goal0_dual_compile` (archive side — the `nm`/ABI oracle in `check_srpc_crate_mode.py`). The `srpc`
library target depends on the source gate, so *any* C++ build runs the whole Rust suite first, and a new
clippy warning breaks the C++ build. A green source gate says nothing about ABI.

## Invariants that will bite you

**`#[cfg_attr(any(), …)]` is the emitter's directive language, and rustc never sees it.** `any()` is
always false, so these 37 attributes are invisible to `cargo build`, `cargo test` and clippy while being
the only way to state a C++ contract Rust cannot: `thread_local` (9, all in `reactor/reactor.rs`),
`cpp_namespace(::janus)` (9 — the Quorum surface, which must live in *global* `::janus`; `srpc::janus::QuorumEvent`
mangles differently and is not a substitute), `cpp_noexcept` (4), `cpp_no_fieldwise_ctor` (3),
`cpp_no_auto_traits` (3), `cpp_abi` (3), `cpp_trait_member_dispatch` (2), `cpp_default_argument` (2),
`cpp_marker_trait` (1), `cpp_abi_alias` (1). Deleting or mistyping one is silent in the Rust lane and
changes the emitted module. The mirror form `#[cfg_attr(not(any()), derive(...))]` (19 sites) is the
opposite — derives rustc *does* apply but the emitter must not see, so plain `#[derive(...)]` is not the
same edit and emits C++ operators that were deliberately withheld. (`IdempotencyKey`'s hand-written
`impl PartialEq` is the only source of the `operator==` symbol the ABI table pins, precisely because its
derive is hidden behind `not(any())`.)

**`#[allow(clippy::…)]` in canonical sources are measured emitter pins, not style waivers.**
`rpc/client.rs` opens with a block recording exactly what each costs — taking clippy's suggestion renames
`DisconnectBehavior_QUEUE()`, retypes `clientpool_select`, changes a method signature, or deletes
`FutureAttr::default_()`. And of the 68 `explicit_auto_deref` sites, 42 change emitted C++ — that family's
suggestions are `MachineApplicable`, so `clippy --fix` applies them without ever seeing the consequence.
**Never run `clippy --fix` over `base/ misc/ rpc/ reactor/`.** The module-level `#![allow(static_mut_refs)]`
at the top of `reactor/reactor.rs` (15 findings, all in that file) is the same kind of pin: where rustc
offers a fix at all, it does not compile.

**`src/lib.rs` is generated — never hand-edit it.** It carries a sha256 of `rust-modules.toml` in its
header, so touching the manifest without `extract_srpc_rust.py --write` fails the gate. Never add any
other file, or any symlink, under `src/`: the census in `extract_srpc_rust.py` rejects symlinks and
orphan `.rs` files, and `test_goal0_standalone.py` asserts `src/` contains exactly `lib.rs`.

**Adding a canonical module is a seven-place edit**, and missing one is a hard error:
1. a `[[module]]` row in `rust-modules.toml` — **append at the end**. The list is historical *promotion*
   order, not a dependency order (`srpc.utils` precedes `srpc.logging`, which it imports); the gate
   topologically re-sorts at build time. What *is* enforced is that CMake's inventories match this file's
   order element-for-element, so an alphabetical insertion fails;
2. `python3 scripts/extract_srpc_rust.py --write` to regenerate `src/lib.rs`;
3. `SRPC_GOAL0_CANONICAL_MODULES`, `set(SRPC_GOAL0_SOURCE_<name> …)` and `SRPC_GOAL0_RETIRED_CARRIER_SRC`
   in `CMakeLists.txt`;
4. the hard-coded provider total `37` in `CMakeLists.txt` (`math(EXPR _SRPC_EXPECTED_INLINE_COUNT "37 - …")`
   and the two `EQUAL 37` checks) — otherwise configure aborts with
   *"Goal-0 production must contain all 37 retained module providers"*;
5. the hard-coded `37` in `scripts/tests/test_goal0_contracts.py` (`ExtractionContractTests`) — this one
   fires in the transpiler-free standalone lane, so it is the first failure you will hit;
6. the ratchet tables in `scripts/check_srpc_crate_mode.py` (`ABI_SPECS`, `EXPECTED_IMPORTS`,
   `EXPECTED_GENERATED_MODULE_SHA256`, `IMPORTER_USE_MARKERS`) **and** the ~3,500-line C++ importer
   program embedded as a Python string in that same file (`importer_source()`) — `require_importer_coverage`
   demands each module be imported there exactly once *and* actually used;
7. rows in `module-preambles.toml` / `cpp-module-index.toml` / `rust-type-map.toml` if the module needs
   C++ includes, foreign symbols, or exact legacy type spellings.

**Ordinary edits trip the ABI ratchets too.** `EXPECTED_TOTAL_PROVIDER_SYMBOLS = 1961` (plus
`EXPECTED_TOTAL_PLATFORM_SYMBOLS = 5`) and the per-module `ABI_SPECS` freeze the public surface, so a real
fix normally touches the `.rs`, its test, *and* `check_srpc_crate_mode.py` in one commit. `EXPECTED_IMPORTS`
is an exact, *ordered* transcript of each generated `.cppm`'s import lines, so introducing the first `std`
`Vec`, `BTreeMap`/`BTreeSet`, `Rc` or `HashMap`/`HashSet` into a module that had none adds a port BMI
(`vec_port.vec`, `btree_port.btree.*`, `rc_port`, `std_port`) to its import list and fails the gate with
nothing in the `.rs` diff to explain it. The `rusty::`-spelled containers pull the `rusty` umbrella instead
and are not affected, and there is no string port. Two more ratchets fire the same way: the generated crate
must report `0 slot(s) requiring hand-attention`, and `TODO`/`UNSUPPORTED`/`skipped` in generated output is
rejected — so Rust the emitter cannot lower surfaces as a gate error, not a compile error. Generated-C++
byte digests, by contrast, are **advisory only**.

When a surface change is intended, don't read the new symbol count off the gate's success line — that line
echoes the constant back. The measured value appears only in the failure text
(*"must contain exactly 1961 unique strong symbols; got N"*). Take `N` from there, update the constant and
the affected `ABI_SPECS` entry, and note the delta rationale in a comment above the constant (the existing
`1897 -> 1961` reactor note is the house form).

**Canonical `.rs` files are byte-policed:** UTF-8, LF only (CRLF is rejected, not normalized), a trailing
newline, no NUL. They may only live under `base/`, `misc/`, `rpc/`, `reactor/`, and the basename must equal
the module name.

**`-march=native` is an ABI requirement, not an optimization.** Clang refuses to load a BMI whose
target-feature set differs from the importer's, so removing it produces ~133 bogus errors. Build trees are
therefore not portable across CPUs.

**Verus specs use `#[cfg(verus)]`, never `verus_only`.** The pinned transpiler special-cases that exact
ident; renaming it breaks the whole-crate transpile. `verify/.cargo/config.toml` forces `--cfg verus`
locally because `cargo verus` itself only sets `verus_only`.

**Errno values are spelled as raw numerics** (`SERVER_ERR_INVALID_ARGUMENT = 22`, the `TCP_ERR_*` block)
so generated modules stay valid alongside `errno.h`. Syscall numbers and build flags are the *opposite*:
`SYS_gettid` and `REUSE_FIBER` must never be Rust constants — their values are arch- and
build-dependent, so they go behind the plain-C seam (`srpc_reactor_gettid`, `srpc_reactor_reusing_fiber`).

**Bumping the transpiler pin means four edits**: the gitlink, plus the literal in
`scripts/extract_srpc_rust.py`, `scripts/check_srpc_crate_mode.py`, and `scripts/tests/test_goal0_standalone.py`.

## Testing

**Rust lane.** 38 auto-discovered integration tests in `tests/*_rust.rs`; the rule is
`<dir>/<module>.rs` → `tests/<module>_rust.rs`. The counts only *look* one-to-one: `frame_codec` has two
(`frame_codec_rust.rs` plus the bug-named `frame_codec_desync_rust.rs`), and
`client_teardown_drains_queue_rust.rs`, despite its name, imports only `srpc::request_queue`.
`rpc/client.rs` and `rpc/server.rs` share one real test, `tests/rpc_roundtrip_inmemory_rust.rs` — a full
client→server→client RPC over the in-memory channel, possible under rustc only because everything on that
path is synchronous: the RPC is registered with `reg_fast_rpc` (inline dispatch, no fiber). The real
facade `PollThread` runs the deferred close jobs now, so teardown is safe in either order; the test keeps
an explicit order anyway. Beyond that one path, a green `cargo test` still
says little about `rpc/client.rs` (3.3k lines, the second-largest module) — and the largest,
`reactor/reactor.rs`, is untested for a different reason given below.

Tests import the library as an external consumer (`use srpc::<module>::…`), never via `#[path]` or `mod`.
No canonical source has a `#[cfg(test)]` module; the workspace's only one is in `rusty-rustc/src/lib.rs`.

Four non-obvious things about these tests:

- **There is no `build.rs`, so the plain-C kernels are never linked.** A test touching a module with a C
  seam must define the stubs itself (`#[unsafe(no_mangle)] pub extern "C" fn srpc_clock_monotonic_us…`);
  ~12 test files already do. Otherwise it fails to *link*.
- **A test that reaches through the `cpp::`/`rusty` facade may prove nothing.** `rusty-rustc/src/lib.rs` is a
  2.4k-line hand-written facade that rusty-cpp omits from generated C++ by package identity — so it is
  allowed to lie, and still does where it must: `fiber_sleep` only records the duration and
  `RandomGenerator::rand(min, max)` returns `min`. Its `with_test_fiber` / `take_test_sleep_calls` hooks
  are what such tests are actually for. It is no longer all mock, though: `PollThread` is a REAL epoll
  loop (edge-triggered, 1 ms tick, command queue, job queue -- a faithful port of
  `pollworker_poll_loop`'s semantics minus reactor coupling and fibers), which is what lets the canonical
  TCP transport run under rustc. Reaching a *new* C++ runtime API from a canonical module means writing
  its facade here first, plus a `rust-type-map.toml` row.

  What it may no longer do is *shadow* a canonical implementation. `scripts/check_facade_shadow.py` (in the
  source gate and in `ctest -L srpc`) fails if a facade item shares a name with a canonical one, unless it
  is listed in that script's `ALLOWED_SHADOWS` with a reason. The ten entries there are the real
  boundary of the Rust lane, each one measured against the pinned transpiler rather than reasoned about:
  `debugging::verify` is generic, and the flat-import contract requires a route-(a) leaf to be a
  non-generic free function; `rand::RandomGenerator` carries `cpp_abi` markers that make it an adapted
  sibling; `reactor::Fiber` collides with the pre-existing `pub type Fiber` alias in `rpc/client.rs`;
  retargeting `reactor::PollThread` makes the emitter rewrite unrelated `HashMap::remove` receivers into
  `__rusty_alias_PollThread_remove`; and the four remaining `reactor` entries need a live reactor and a
  current fiber that rustc does not have. Everything else was retired — `basetypes` and `logging` are gone
  from the facade entirely, and 80-odd call sites now reach canonical Rust.

  Serialization is the sharpest example of what the boundary means in practice. The canonical GENERIC
  dispatchers (`Serialize_::serialize` / `Deserialize_::deserialize`) are **C++-only**: in C++ the
  qualified call resolves the generated module's concrete leaf overloads before the generic template
  (poison-scoped ADL alone cannot see them — using-directives are invisible to ADL, and primitives have no
  associated namespace), while under rustc a `T: Serialize` bound there would cascade into the generic
  container impls, which the emitter degrades to hand slots. So they stay unbounded and their facade
  terminal `rusty::srpc_adl_serialize` is a LOUD `unimplemented!` — replacing years of silently writing
  nothing. **Rust-lane leaf serialization is real** through two spellings: the `Serialize`/`Deserialize`
  traits directly, and the facade route `cpp_serializable::Serialize_::serialize` the wire sites use —
  which emits the same qualified `::srpc::Serialize_::serialize` call C++ always made (its
  cpp-module-index row makes it a known foreign symbol) and under rustc dispatches through the
  `RustcAdlSerialize`/`RustcAdlDeserialize` bound, satisfied by srpc's blanket impls on its archives over
  the canonical traits. Container serialization panics wholesale under rustc; C++ container emission is
  byte-for-byte untouched.
- **`reactor/reactor.rs` is deliberately not executable as Rust** — its own header says so. The nine
  `#[cfg_attr(any(), thread_local)]` statics are plain process-global `static mut` under rustc, so TLS, race
  and teardown behavior are covered only by the C++ battery.
- Many tests assert C++-visible layout (`size_of` / `align_of` / `offset_of`), and a few assert on the
  *text* of the canonical source via `include_str!` — `tests/reactor_rust.rs` pins exact substrings and even
  drop order by byte offset. A cosmetic refactor turns these red.

**C++ lane (narrow).** `tests/` holds 76 `.cc` files but CMake builds exactly **9**, named in explicit
`set()` lists — there is no glob for test sources, so adding a `.cc` to `tests/` does nothing. Three of the
eight have target names differing from their file names (`tests/fiber_test.cc` → `test_fiber`). The other 68
are dead: nothing compiles them, so nothing proves they still build. Five reference the Mako monorepo
directly (`deptran/…` in `rpc_log_storage_test.cc`, `rpc_marshallable_proxy_test.cc`,
`rpc_rocksdb_log_storage_test.cc`, `testharness.cc`; `mako/…` in `test_mako_core_minimal.cc`), and 17 more
pull `tests/benchmark_service.h`, whose `#include "srpc/srpc.hpp"` is monorepo-relative and does not resolve
here. The rest include the same headers the built suites do — assume nothing without trying.

**Always run `ctest -L srpc`, never a bare `ctest`.** `add_subdirectory(third-party/rusty-cpp)` also
registers ~69 tests of its own whose executables are *not* in `ALL`, so a bare `ctest --test-dir build`
reports 83 tests, marks those 69 "Not Run" and exits 8 — a failure that says nothing about SRPC. Every
test this project owns carries the `srpc` label.

`ctest -L srpc` selects 15: the 8 battery binaries (also labelled `runtime_battery`),
`test_rpc_docs_symbols` (also `docs`), `srpc_goal0_standalone_structure`, `srpc_goal0_cargo`,
`srpc_goal0_contracts`, `srpc_goal0_rand_kernel_smoke`, `srpc_facade_shadow` and
`srpc_docs_snippet_lint`. `srpc_goal0_cargo` just re-runs the whole Cargo suite. If the googletest
submodule is missing, CMake only *warns* and silently registers 6 instead of 15 — a green run is not
proof the battery ran.

**Verus lane.** `verify/` is a workspace-excluded crate that `#[path]`-links the real sources and runs
`cargo verus verify` against them; only `misc/stat.rs` and `rpc/internal_protocol.rs` carry specs today. A
canonical module may carry any contract that proves with **no in-body proof steps** — the transpiler's
preflight rejects opaque macros, so no in-body `proof!`. (`internal_protocol.rs` uses purely definitional
`ensures r == <wire-bit expression>`; `stat.rs` uses `requires old(self)…` / `ensures final(self)…` and still
stays in the module.) Only theorems needing `by (bit_vector)` move to `verify/src/*_proofs.rs`, which is
never transpiled. Adding a spec is a two-file edit: annotate the module, then add a `#[path]` line to
`verify/src/main.rs` — hand-maintained, nothing cross-checks it. Per `docs/verification.md`, **always run a
negative control**: perturb the body, confirm it goes red, revert. A green that never went red proves nothing.

## Runtime architecture

Layering is `base/` → `misc/` → `reactor/` → `rpc/`, but these are directories, not crates — everything is
one flat `srpc` crate. Two inversions to know: `reactor/reactor.rs` imports `crate::pollable_proxy` (which
lives under `rpc/`), and *nothing* uses `crate::reactor` — every consumer reaches the reactor through the
foreign-module facade `use cpp::srpc::reactor` (`use rusty as cpp`) inside `unsafe` blocks, because that is
what models the C++ module boundary. `idempotency` and `completion_tracker` are consumer-facing utilities
that neither `client.rs` nor `server.rs` uses.

**Request path.** Generated proxy → `Client::request` → `ClientConnection::request` →
`clientconn_request_via_channel` (circuit-breaker gate → stale-request expiry → offline-queue check →
`Future::create(xid)` into `pending_fu_` → serialize `v64 xid | i32 rpc_id | args`) →
`ChannelConnectionProxy::send_frame` → **the TCP backend adds the 4-byte header** → poll thread → the
server-side `TcpConnection`'s `FrameStreamReader` re-frames and fires `on_frame` (this lives in
`rpc/tcp_channel.rs`, *not* `server.rs`) → `sconn_decode_request_and_dispatch` → fast RPCs dispatch inline on
the poll thread, everything else spawns a stackful fiber → `sconn_reply` writes
`v64 xid | v32 error | v64 server_instance_id | payload` → client `clientconn_decode_response_and_notify`
resolves the async slot (`xid % 16384`) first, then the `pending_fu_` map. A reply matching neither is
silently dropped — the normal outcome after a timeout, and it leaves no trace.

**Wire format** (`rpc/internal_protocol.rs`, `rpc/frame_codec.rs`): 4-byte **native-endian** header — bit 31
is the extended-header flag, bits 0-30 the payload size. `kMaxFramePayloadSize` (64 MiB) is a
*stream-integrity* bound, not a resource policy: without it a desynced stream returns `NeedMoreBytes`
forever and the connection wedges silently. It must stay ≤ `i32::MAX - 4`. Note the TCP *send* path
open-codes the header rather than calling `frame_codec_write_header`, so a header-layout change means
editing both places.

**Channels** (`rpc/channel.rs`): two implementations — TCP (`rpc/tcp_channel.rs`) and in-memory
(`rpc/inmemory_channel.rs`, which is frameless and synchronous, so it can never reproduce a framing bug).
`FiberChannel` is *not* an implementation; it adapts callback delivery into a fiber-blocking `recv_frame()`.
TCP is auto-installed by `Client::connect` / `Server::start`; to use in-memory you must
`set_channel_factory` *before* connect/start.

**Concurrency is both stackful and stackless.** Fibers are mmap'd stacks (1 MiB default + guard page)
switched by `reactor/fiber_context_{x86_64,aarch64}.S`; the field order of `srpc_fiber_ctx` in
`reactor/srpc_fiber.h` *is* the ABI contract with that assembly. The `Reactor` is thread-local *in the
generated C++* (under rustc those markers are inert) and also drives stackless `rusty::Task` pollers.

**Reliability layers** (circuit breaker, heartbeat, reconnect policy, connection state machine, request
queue, metrics) are embedded by value in `ClientConnection`. Only four configs are staged on `Client` and
applied at `connect` (keepalive, heartbeat, circuit breaker, reconnect policy). The request queue is the
trap: its `BufferingConfig` is *not* staged — `Client::set_buffering_config` silently no-ops until a
connection exists, so it must be called *after* `connect`. `LoadBalancer` is used only by `ClientPool`.

## The hand-written C++ seam

`srpc.hpp` is the consumer umbrella nearly every C++ test includes (72 of 76, and eight of the nine built
suites — the ninth, `rpc_docs_symbols_test.cc`, only reads files).
Its `import srpc.*;` list is hand-maintained and nothing generates or checks it, so a newly consumer-facing
module is not reachable *through the umbrella* until it is added there — a test needing one of the eight
modules commented out as "trimmed from consumer umbrella: nothing outside srpc names it (build-time opt)"
names it directly instead (`import srpc.epoll_wrapper;`, as `tests/test_reactor.cc` does). Nothing in the
repo measures that build-time cost, so treat re-adding a trimmed import as a claim to measure, not an
obvious fix.

`misc/serializable_support.hpp` holds the real open-set ADL `serialize`/`deserialize` dispatch whose Rust
spelling in `misc/serializable.rs` is an inert facade, and `base/rustc_markers.hpp` declares
`rusty::cpp_inherit`; both reach generated modules only via `module-preambles.toml`. In `srpc.hpp` and
`base/all.hpp` the `import srpc.*;` lines must sit after every textual `#include`, and `std_compat.hpp` —
the only file here that spells `import std;` — exists solely to do the same for the std module: libc++
rejects the other order with ODR errors inside its own internals (llvm-project #61465).

In `reactor/epoll_platform_linux.cc`, the bodies inside `#if RUSTYCPP_RUST` are the source; the C++ between
`/*RUSTYCPP:GEN-BEGIN … rust_sha256=… */` and `GEN-END` is generated — edit the Rust and regenerate with
`rusty-cpp-transpiler inline-rust`, never the C++ between the fences. `srpc_dsl_check.sh` hard-codes the
census (exactly this file, exactly 5 blocks) and also scans `*.rs`, so adding a DSL block anywhere under
`base/ misc/ reactor/ rpc/` fails it until the script's counts are updated too.

## Code generation (`pylib/`)

`.rpc` IDL → C++ header and Python stub, via a yapps-2 parser. Not wired into CMake at all; outputs are
checked in, and `tests/benchmark_service.rpc` is the only input. Three traps:

**`pylib/simplerpcgen/rpcgen.py` is the live generator and has been hand-edited since generation.**
`rpcgen.g` is a stale grammar whose epilogue lacks `load_existing_rpc_codes`, the `existing_codes` argument,
and the `archive` flag. **Never regenerate `rpcgen.py` from `rpcgen.g`** — it would drop the id
stabilization below. (There is no yapps compiler vendored anyway; `pylib/yapps/` is runtime-only.)

**RPC method ids are `random.randint(...)`, stabilized only by scraping them back out of the previously
generated `.h`.** Never delete the generated header before regenerating, or every id changes and wire
compatibility silently breaks. Renaming a service function reassigns its id for the same reason.

**`bin/rpcgen`, which both in-repo rpcgen tests shell out to, does not exist here** (it lived in the upstream
Mako checkout) — drive the generator by importing `simplerpcgen.rpcgen` with `pylib/` on `sys.path`.
`rpcgen_typed_structs_test.py` would run if the driver came back; `rpcgen_compile_test.py` is dead
regardless, since its `RPC_SOURCES` name `src/deptran/*.rpc` paths that do not exist here.

## Conventions

**Commits:** `<scope>: <lowercase imperative>` where scope is the canonical module basename
(`frame_codec:`, `stat:`, `client:`) or an area (`build:`, `docs:`, `tests:`, `verify:`, `gate:`, `goal0:`).
`srpc:` for tree-wide changes. The legacy `rrr:` prefix is retired — do not reuse it. Because there is no CI,
bodies carry the audit trail: a narrative of the defect and a `Verified:` paragraph with *measured* numbers
(test counts, configure/build exit codes, the ABI symbol count). A minority of test-touching commits (31 of
338) also add a `Tests:` paragraph naming the new test — `b8be721` and `e376fd6` are the recent examples,
while `0e51bce` changed `tests/stat_rust.rs` without one. Transpiler pin bumps get their own commit:
`build: bump rusty-cpp <old> -> <new>`.

**Style:** `//` line comments only, with long "why this constant exists" blocks as the house norm. Most
constants are `SCREAMING_SNAKE_CASE` (`TCP_ERR_AGAIN`, `SERVER_ERR_INVALID_ARGUMENT`); 14 keep C++-style
`k`-prefixed camelCase — mostly framing and reactor (`kFrameHeaderSize`, `kResponseSizeMask`,
`kDefaultStackBytes`) but also `kAsyncSlotCount` in `client.rs`, `kDefaultDrainTimeoutMs` in `server.rs` and
`kRequestQueue*Error` in `request_queue.rs`. Match the surrounding file. `unsafe_code` is denied crate-wide;
eight files carry a file-scope `#![allow(unsafe_code)]` (`reactor/{reactor,fiber}.rs`,
`rpc/{client,server,tcp_channel,inmemory_channel,fiber_channel}.rs`, `misc/any_message.rs`) and elsewhere
`unsafe` gets a narrow per-item `#[allow(unsafe_code)]` — never relax the crate-level deny. There is no
rustfmt/clippy/clang-format config.

**`.apas` in the repo root is an agent-harness session file, untracked and not in `.gitignore`.** Never
commit it; watch out for `git add -A`.

## Documents that are stale — do not trust them

- **`RUST_CANARY.md`** — says 23 canonical modules and 14 remaining inline modules (actually 37 and 0), a
  15-file/326-block DSL inventory (actually 1 file / 5 blocks), 332 provider symbols (actually 1961), and an
  old transpiler pin. Its build commands still work; its numbers do not.
- **`reactor/CANONICAL_CHECKPOINT.md`** — a HOLD checkpoint for an older `rrr`-namespaced tree, citing
  `scripts/extract_rrr_rust.py` (renamed to `extract_srpc_rust.py`) and machine-local `/var/tmp` scratch
  paths. `reactor` *is* a canonical provider now. Still valuable for the ABI oracles: the 300-symbol
  incumbent manifest (which `check_srpc_crate_mode.py` still names), the size/align table, the global-`::janus`
  requirement, and the enumerated emitter contracts.
- **`scripts/verify_srpc.sh`'s header comment** — claims `#[cfg(verus_only)]`. The sources use
  `#[cfg(verus)]`. The script's *behavior* is correct; only its comment is wrong. `docs/verification.md`'s
  expected "11 verified" count also disagrees with the commit that added it — run it and read the output.
- **`README.md`'s `git log --follow` claim** — true for 20 of the 37 canonical files, not all of them. Seven
  reach the 2018 genesis (`base/{debugging,logging,misc,threading}.rs`, `reactor/epoll_wrapper.rs`,
  `rpc/{client,server}.rs`); thirteen more reach real 2026-era C++ (`reactor/{reactor,future,fiber}.rs`,
  `rpc/{tcp_channel,inmemory_channel,fiber_channel,channel,callbacks,idempotency,pollable_proxy}.rs`,
  `misc/{serializable,serializable_envelope,any_message}.rs`). For the other 17 the rewrite was too dissimilar
  for rename detection and `--follow` stops at the promotion commit — reach the C++ era by naming the old
  path: `git log --all -- rpc/frame_codec.cpp`.
- **`docs/rpc/migration-guide.md`** does not exist, though `tests/rpc_docs_symbols_test.cc` reads it.
  `docs/srpc-book.md` now does exist — ported from the Mako monorepo and rewritten chapter by chapter
  against these sources — but neither doc test is wired into CMake, and the symbols test's `required` list
  is itself partly stale (it demands `server.reg_service(`, an `RpcResult<…>` type that exists nowhere in
  this repo, and three spellings that name stubs). Fix that list before wiring the test up, or it will pin
  bad guidance in place.

When prose and code disagree, `CMakeLists.txt`, `rust-modules.toml`, and `scripts/` are the truth.
