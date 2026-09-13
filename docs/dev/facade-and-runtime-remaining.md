# Remaining facade and runtime adapters

Status as of 2026-09-12, branch `apas/srpc`, after commits `d6d899f` through
`8a094ff`. This is a working inventory for the next person who picks up the
facade: what is left in `rusty-rustc/`, why each item is still there, what
would remove it, and how to remove one safely. Every count below was measured
against the tree at that revision; re-measure before trusting a number.

## The principle

The `rusty` C++ runtime API is designed so that canonical Rust can use the
*real* standard-library types and the transpiler maps them: `Arc` is
`std::sync::Arc`, `Mutex` is `std::sync::Mutex`, `String` is `String`, and the
emitter spells them `rusty::Arc`, `rusty::Mutex`, `rusty::String`. The
rustc-only facade (`rusty-rustc/src/lib.rs`, `task.rs`) exists so the canonical
sources type-check and test under rustc; the emitter omits the package by
identity. Under that principle a hand-written replacement type in the facade
is debt unless it has a measured reason, and the reason belongs at the
definition.

Two things follow. A type the facade merely re-exports (`pub use
::std::sync::Arc`) already honours the principle. A type the facade *models*
by hand is acceptable only when it stands for something Rust has no word for:
a C++ ABI type the shipped surface names, a runtime semantic std cannot
express, or an emitter contract.

## What was removed in September 2026

| Commit | Change | Emission delta |
|---|---|---|
| `d6d899f` | `rusty::Mutex`/`Condvar` wrappers deleted; 53 sites spell `std::sync::*`; six dead facade items removed | none (38/38 files byte-identical) |
| `3f5a099` | non-static `HashMap`/`HashSet` spell `std::collections::*` | 1 file, 20 lines, in a never-instantiated template |
| `52ec8e3` | last `rusty::sync::downgrade` caller spells `Arc::downgrade`; facade fn deleted | 1 line (a `rusty::clone` disappears) |
| `14be655` | `LegacyStdString` alias and its `std::string` type-map row deleted; the C++ surface says `rusty::String` | 11 modules, 208 lines, all type spellings; 2 of ~50 test TUs needed 3 lines |
| `f237f1f` | facade `std::string` byte model retired from canonical code; kept only as `SerializableStdString`, the wire impl target | 5 modules; provider symbols 2046 -> 2045 |
| `8a094ff` | 20 `&String` parameters and callback types take `&str`, so the C++ surface takes `std::string_view` | 5 modules, 128 lines |

`std::string` mentions inside emitted module bodies went from 281 to 49; the
49 are the wire overloads in `serializable`, `rand`'s explicit
`std_string_bytes` ABI carrier, and the `std::string::value_type` spelling of
a C `char`. Canonical Rust now names the C++ string type in exactly two lines,
the two wire impls in `misc/serializable.rs`.

## The inventory today

`scripts/facade-adapters.json` pins 195 declarations (down from 220). The
gate (`scripts/facade_audit.py`, run as `check_facade_shadow.py` and
`check_facade_stubs.py`) fails on any unreviewed, changed, or stale entry.

| Category | Count | Standing against the principle |
|---|---|---|
| `import` | 52 | Re-exports of std. Already the principle. |
| `standard` | 108 | Hand-written models. This is where the remaining debt and the remaining necessities both live; see below. |
| `trait-dispatch` | 14 | Bounded trait forwarding into canonical Rust (the ADL `Serialize` seam, the `LoadBalancerClient*` contracts). By design, paired with `misc/serializable_support.hpp`. |
| `c-layout` | 12 | Opaque C ABI types (`pthread_*`, `FILE`, the fiber register frame, `sockaddr_in`, `LegacyCVoid`). Required. |
| `future` | 9 | `Task`/`Poll`/`Context`/`Waker`, the rustc half of the coroutine ABI. See F5. |

The facade is 1482 lines (`lib.rs` 1328, `task.rs` 154), down from 1678 at
the start of the pass.

## `standard`: what could still go, and what blocks each

Site counts are canonical call sites across `base/`, `misc/`, `rpc/`,
`reactor/`, comments excluded. "Blocker" names the transpiler feature (F1-F6,
specified at the end) or the probe still owed.

| Item | Sites | Why it is still here | Blocker |
|---|---|---|---|
| `Function<F>` (struct, `Default`/`Deref`/`DerefMut`, a generic and 14 signature-specific `from_callable` impls, `RustyFunctionIsEmpty`; 21 declarations) | 23 type spellings, 22 `from_callable` calls | `Box<dyn Fn..>` already lowers to `rusty::Function<..>`, so the non-nullable sites can move today. About nine sites need the *empty* state (`Default::default()` fields in `rpc/tcp_channel.rs`, `is_empty()` checks in `rpc/server.rs`), and `Option<Box<dyn Fn>>` lowers to `rusty::Option<rusty::Function>`, a different field type. | F2 for the nullable sites; the rest is a measured slice with no blocker. |
| `HashMap`/`HashSet` (structs, 6 impls) | 5, two `static`s | Their `new()` is `const fn`; std's is not. std's const route, `with_hasher(BuildHasherDefault::new())`, compiles but the emitter propagates the hasher parameter into a 3-parameter `rusty::HashMap<K, V, BuildHasherDefault<..>>` and moves the `Serialize_` overloads onto it (132 emitted lines). | Strip the hasher parameter when mapping, or lower `OnceLock`/`LazyLock`. |
| `sys::time::sleep_us`, `sys::process::getpid` | 5, 1 | No mapping for `std::thread::sleep` / `std::process::id`. | F1: two path rows. Smallest item on the list. |
| `StdPair`, `std::make_pair`, `borrowed_std_pair` | 6 | Rust tuples lower to `std::tuple`; the `::janus` surface (`QuorumDanglingVec`, `make_promise`) is pinned on `std::pair`. | F4: opt-in 2-tuple to `std::pair`. |
| `SerializableStd{Vector,Map,Set,List,UnorderedMap,UnorderedSet,StringView,String}` (8 structs, 26 impls; 34 declarations) | 15, all in `misc/serializable.rs` | They exist so `impl Serialize for rusty::SerializableStdVector<T>` emits the `Serialize_::serialize(const std::vector<T>&)` overload C++ consumers and rpcgen-generated services use. A plain `Vec<T>` lowers to `rusty::Vec`, a different type. `SerializableStdString` is the same thing for `std::string`, reduced to `size`/`resize`/`data`. | F3 extended to containers: a way to write a trait impl whose target is a C++ STL type. Otherwise wire-bound and stays. |
| `task::{Task, Poll, Context, Waker}` (`future`) | 20, two files | The transpiler maps `std::task::{Poll, Context, Waker}` to `rusty::*`, but the C++ `rusty::Poll<T>` is a `{ready, value}` struct with `ready_with`/`pending()`, not an enum, and `Context` holds a raw `Waker*`. `Task<T>` is the C++ coroutine handle and has no std counterpart. | F5 for `Poll`/`Context`/`Waker`; `Task` stays. |
| `RustcTcpStream`, `RustcTcpListener`, `RustcIoError`, `RustcOwnedFd`, `RustcBorrowedFd`, the three `rusty::net::*` address helpers | 1, 1, 1, 3, 5 | `std::net::TcpStream`, `std::net::SocketAddrV4` and `std::io::Error` are already mapped to `rusty::net::*` / `rusty::io::Error`; the models remain because nobody has checked that the C++ member names match std's. `OwnedFd` and `TcpListener` have no mapping row. `sockaddr_in_from_socket_addr_v4` is C-ABI glue and stays. | A probe per type (swap, `cargo check`, transpile, build), then mapping rows for the two unmapped types. |
| `std::Cout` / `std::cout` | 3, `base/logging.rs` | The logger writes through `std::cout` so that C++ tests and the importer program can capture it by `rdbuf`; a raw `write(2)` would bypass them. The facade models `write`/`put`/`flush`. | A mapping from `std::io::stdout()` writes to `std::cout`, or accept the model as a C++ I/O contract. |
| `SourceLocation` | 1, `base/debugging.rs` | Models the `std::source_location` default argument the C++ `verify` keeps (`cpp_default_argument(source_location)`). | F6: lower `#[track_caller]` / `Location::caller()` to a `std::source_location::current()` default argument. |

Everything in this table is a candidate. Nothing in it migrates with today's
transpiler, which is why the list is here rather than in a commit.

## `standard`: what stays, and the measured reason

These were each checked against the runtime source, not the facade's own
comment. Do not "migrate" them; the reason is also written at each
definition in `rusty-rustc/src/lib.rs`.

| Item | Sites | Reason |
|---|---|---|
| `thread::spawn`, `JoinHandle::{join, detach}` | 4 | The runtime's `run_into_state` (`thread.hpp`) runs a spawned body with no try/catch: an escaping exception reaches `std::thread` and terminates the process ("Rust panic-abort semantics", the runtime's own words). The facade's `catch_unwind(..).unwrap_or_else(abort)` gives rustc the same behaviour. `std::thread::spawn` would capture the panic into the handle; a detached client thread's panic would die silently and a test awaiting it would hang. A worktree probe that swapped them type-checked, which is exactly why `cargo check` is not evidence here. |
| `thread::ThreadId`, `thread::current_id` | 20, 22 | `reactor/reactor.rs` transmutes thread ids to and from `u64` with `0` meaning "unset" (`u64_to_thread_id`, `thread_id_to_u64`). std's `ThreadId` is a `NonZero<u64>`; transmuting `0` into it is undefined behaviour. The facade's `ThreadId(Option<std::thread::ThreadId>)` makes `0` a sound `None`. |
| `panic::{catch_unwind, payload_message, PanicPayload, do_panic}` | 3 | Code that only swallows an unwind already uses `std::panic::catch_unwind` directly (`rpc/callbacks.rs`, `rpc/request_queue.rs`). The model exists for the one site that inspects the payload (the shutdown-hook invoker in `rpc/server.rs`): std's `Err(Box<dyn Any + Send>)` has no C++ spelling, the runtime carries a `std::exception_ptr`. `do_panic` takes `&str`, which is the `std::string_view` the C++ one takes. |
| `make_box` | 5 (spelled `rusty::make_box::<T>(..)`) | A real runtime function (`box.hpp`) with a dedicated trait-object coercion path in the emitter. An emitter contract, not a wrapper over `Box::new`. |
| `RustyHandleIsValid::is_valid` | 18 | Always `true` under rustc; exists to emit the real C++ null check on handles that C++ callers can hand in empty. |
| `StdArcGetMutExt::get_mut` | ~6 | `rusty::Arc::get_mut` is a C++ *member*; std's is an associated function, and swapping emits a free call the runtime lacks. |
| `ReactorJobSet`, `ReactorJobSetKey` | 1 | The incumbent `std::set` type in the reactor; `dyn Job` is not `Ord`. |
| `SerializableStdString`, `SerializableStdStringView` | 2, 1 | The impl targets that keep the C++ `std::string` / `std::string_view` wire overloads for consumers' own fields. See the containers row above; this is the same reason, already reduced to its minimum. |

Two items in the C-layout family are worth naming because they look like
string business and are not: `LegacyCChar` (21 sites) is the `char` of a C
string pointer, mapped to `std::string::value_type`, and `CFile` (3) is
`FILE*`. Both are C ABI, not std adapters.

## C++-side adapters and native kernels

These are not facade debt. CLAUDE.md defines them as intended seams, and
`scripts/check_native_kernels.py` plus `scripts/native-kernels.json` pin them.

- 16 hand-written headers: `srpc.hpp` (57 lines) and the per-module
  compatibility headers that import generated modules for consumers who keep
  `#include "srpc/..."`; `misc/serializable_support.hpp` (55 lines), the ADL
  forwarding half of the `trait-dispatch` category; `base/rustc_markers.hpp`
  (8 lines); `std_compat.hpp`; `reactor/srpc_epoll.h` and `reactor/srpc_fiber.h`
  (the latter's `srpc_fiber_ctx` field order *is* the assembly contract).
- 9 C sources and 2 assembly files listed in
  `scripts/native-kernel-sources.txt`: individual OS operations, entropy and
  clock reads, and context switching. Policy lives in canonical Rust.

Dropping the compatibility headers is a consumer-policy decision (their
`#include` lines would become `import` lines), not a correctness one.

## How to remove one more item safely

This is the procedure that worked six times in a row this month. Each step is
there because skipping it cost a twenty-minute gate.

1. **Write the edit as a script with exact, unique anchors** that aborts on a
   missing or ambiguous match. Apply it to a detached scratch worktree first
   (`git worktree add --detach`, `git apply` any uncommitted diff), and to the
   real tree only after the probe passes; `cmp` the files between the two.
2. **Transpile whole-crate in the worktree and diff against a baseline
   transpile** (`rusty-cpp-transpiler --crate Cargo.toml ...` with the exact
   flags CMake uses; `diff -rq`). Read every changed line. A type spelling is
   fine; a changed method call, import line, or `Slot manifest` is the thing
   to understand before building.
3. **`cargo check`, `cargo test`, `cargo clippy -D warnings` in the worktree**
   with `CARGO_TARGET_DIR` in scratch. clippy's `ptr_arg` skips `pub` items
   under `avoid-breaking-exported-api`, so count `&String`/`&Vec` parameters
   by grep, not by lint.
4. **Build the real tree with `cmake --build build -- -k 0`** so one pass
   collects every consumer error, every layout pin, and the oracle failure.
   Never edit a tracked file while a gate runs: `srpc_goal0_cargo` reads the
   tree.
5. **Re-pin from measurement.** Surface fragments come from the emitted text.
   Symbol rows come from `llvm-nm --defined-only --demangle build/libsrpc.a`,
   grouped by `check_srpc_crate_mode.symbol_owner_module` and compared per
   module against `ABI_SPECS[m].symbols + RAW_ABI_ALIASES[m] + initializer`.
   Do it for all modules in one pass; the gate itself stops at the first
   mismatching module. A row may be pinned in up to three places: `ABI_SPECS`,
   a module's incumbent-oracle table (single quotes), and a double-quoted
   reviewed-additions list. `scripts/tests/test_goal0_contracts.py` also
   hard-codes the provider total and per-module `(unique, with initializer)`
   counts. `EXPECTED_IMPORTS` rows may be a one-line `[]`. Layout pins in
   `tests/*.cc` are measured with the undefined-template probe under
   `-ferror-limit=0`.
6. **Run the full gate and record the numbers** in the `Verified:` paragraph.

Emitter spellings that do not lower, found this month and worth not
rediscovering:

- `String::from_utf8_lossy(&bytes).into_owned()` lowers to a `rusty::into_owned`
  the runtime does not define, and `&bytes` reaches `from_utf8_lossy(span)` as
  a pointer. Write `String::from(String::from_utf8_lossy(bytes.as_slice()))`:
  `as_slice()` lowers to the runtime's `std::span`, and `String::from` lands on
  the `std::string_view` constructor through `rusty::String`'s implicit view
  conversion.
- `out.push_str(&symbols[i])` keeps the `&` as a C++ address-of. Write
  `symbols[i].as_str()`.
- Passing `&String` where `&str` is expected lowers to
  `rusty::to_string_view(x)` and is fine; a C++ caller can pass `std::string`,
  `rusty::String` or a literal to a `std::string_view` parameter.
- `rusty::String` is reachable through module exports but not *nameable*; a TU
  that spells it needs `#include <rusty/string.hpp>` textually, before its
  `import` lines.
- A `Vec<u8>` in a module that had none adds `import vec_port.vec;` to its
  emitted module and moves `EXPECTED_IMPORTS`.

## A gate improvement worth making

`facade_audit.py`'s `standard` category conflates "adapter over a std type"
with "model of a C++ ABI or runtime contract". Splitting the second group
into its own category (`cpp-abi-model`: the `SerializableStd*` targets,
`StdPair`, `SourceLocation`, `thread`, `panic`, `make_box`, `is_valid`,
`get_mut`, `ReactorJobSet`, `Cout`) would make the principle enforceable:
anything still in `standard` is, by definition, work.

## Transpiler features that would finish the job

Each of these is a rusty-cpp change, measured against the pinned transpiler
`3e1d9505`. File them upstream; none is an SRPC edit.

- **F1** Path rows for `std::thread::sleep` and `std::process::id` (to
  `rusty::sys::time::sleep_us` and `rusty::sys::process::getpid`, with the
  `Duration` argument lowered to microseconds). Removes 2 facade functions.
- **F2** Lower `Option<Box<dyn Fn..>>` as `rusty::Function<..>` with `None`
  as the empty callable and `is_some()`/`is_none()` as `!is_empty()`/
  `is_empty()`. Removes the `Function` model (21 declarations) once the
  non-nullable sites have moved to `Box<dyn Fn..>`.
- **F3** STL-alias trait targets: allow a Rust type (`Vec<T>`, `String`,
  `BTreeMap<K, V>`, ...) to carry an attribute naming the C++ STL type an
  `impl` should be emitted for, so `impl Serialize for Vec<T>` can also emit
  `serialize(const std::vector<T>&)`. Removes the eight `SerializableStd*`
  models (34 declarations).
- **F4** Opt-in lowering of a 2-tuple to `std::pair` where an attribute says
  so. Removes `StdPair`, `make_pair`, `borrowed_std_pair`.
- **F5** Lower `std::task::Poll::{Ready(v), Pending}` onto the runtime's
  struct-shaped `rusty::Poll<T>` (`ready_with`/`pending`), and
  `Context::from_waker`/`waker()` onto the `Waker*` field. Removes
  `Poll`/`Context`/`Waker` from the `future` category; `Task` stays.
- **F6** Lower `#[track_caller]` + `core::panic::Location::caller()` to a
  `std::source_location::current()` default argument. Removes
  `SourceLocation`.
- **Hasher stripping**: map `HashMap<K, V, BuildHasherDefault<H>>` and
  `with_hasher(BuildHasherDefault::new())` to the 2-parameter runtime type
  and its default constructor. Lets the two const-initialised registries
  spell `std::collections::HashMap` and removes the last facade containers.
