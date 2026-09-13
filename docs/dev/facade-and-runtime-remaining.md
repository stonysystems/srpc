# Remaining facade and C++ runtime dependencies

Investigated on 2026-09-12 at SRPC `c591960`, with the pinned rusty-cpp
`3e1d95059839e4bf1968891047ff1563a2f08c17`. Counts below describe that tree.
The September migration history is retained below; its emission deltas are
historical measurements, not new results from this investigation.

The Cargo lane already builds without a C++ runtime. It compiles canonical
Rust, the Rust-only `rusty-rustc` facade, nine C sources and one architecture's
fiber assembly. The generated C++ lane still uses rusty-cpp's C++ runtime.
Replacing `rusty::Arc` with `std::sync::Arc` in canonical Rust reduces facade
coupling but still emits `rusty::Arc` in C++.

There is work left in the facade, but several earlier blocker descriptions
were wrong. The pinned transpiler already lowers `std::thread::sleep` and
plain `Option<Box<dyn FnMut()>>`. Nullable callbacks with `Send`/`Sync` bounds
remain unsupported by that special lowering. Network and descriptor models
also have concrete API and empty-state differences that a path-map edit
alone cannot remove.

## Active removal goal

The target is a canonical Cargo lane that uses Rust std plus the reviewed
native kernel, with no dependency on `rusty-rustc` or C++ runtime models.
Compiler-only annotations and C++ consumer adapters may remain outside that
runtime dependency. Moving the facade into another Rust module does not meet
this goal.

Completion requires both Rust runtime tests and generated-C++ checks, followed
by a standalone Cargo build with the C++ toolchain and facade sources absent.
C++ compatibility work must keep serialization and scheduling policy in the
canonical Rust implementation.

Current implementation progress, updated 2026-09-13:

- The combined working tree has 17 facade declarations, down from 195.
  Integrated removals cover direct std imports, Arc access, owned descriptors,
  lazy standard collections, sleep/PID/source locations, boxed callbacks,
  standard TCP streams/listeners/errors, standard threads, standard Future/Wake,
  STL serialization models, source/sink forwarding, and native C/assembly
  type bindings, tuple/vector aliases, optional reply callbacks, job storage
  and load-balancer traits.
- The first combined std/descriptor/location batch passes the full C++ build,
  the ABI gate with 2,045 exact provider-owned strong symbols, and all 26 SRPC
  CTest tests. The later combined batch passes the Rust workspace tests;
  its complete generated-C++ validation is pending.
- Standard Future tests cover pending work with non-default, non-cloneable
  outputs. Upstream runtime tests cover move-only payloads, retained wakers,
  nested suspension, cancellation and context ownership with address/undefined
  sanitizers. Whole-crate generation passes in the isolated executor slice.
- Callback borrowing now supports shared/mutable views, guards and empty
  unwraps. Fresh C++ compilation found a further unit-return inference gap in
  a boxed callback. A missing canonical thread-helper import is corrected;
  the thread runtime already handles unit outputs. Callback return/argument
  corrections are in progress before the next combined C++ build.
- Standard threads retain the canonical abort-on-panic policy. Worker shutdown
  compares kernel thread IDs to avoid joining itself. Rust tests include that
  self-shutdown path.
- STL wire loops and error handling stay canonical. C++ adapters expose single
  container operations, including insert-if-vacant for first-value-wins map
  decoding. Twenty-one isolated Rust tests and nine import-only C++ parity
  tests passed. TCP's isolated C++ check preserves all strong and SRPC-owned
  provider symbols.
- The integrated compiler commits include standard Future/Wake, callback
  borrowing, standard networking, module epilogues, typed generic C++
  declarations, and explicit native C type bindings. The optimized compiler build is clean at
  `51001c606857116e5b280f7011706ac58edd834c`.

Remaining source work is now concentrated in these areas:

1. Validate the combined generated C++ build. Its Rust tests
   and eleven isolated C++ serialization tests pass.
2. Standard IPv4 parsing/formatting and logging remove the remaining platform
   helpers. Logging and IPv4 helpers are now removed; their isolated Rust and
   C++ checks pass. Pthread, FILE and fiber bindings are integrated with a separate
   native ABI audit; all defined symbols match in five affected C++ providers.
3. Tuple/vector aliases used by existing C++ consumers. Rust tuples and Vec
   need scoped C++ type mappings that retain the current pair/vector ABI.
4. Optional handle and callback presence. Required Rust owners must stop
   carrying facade methods that return constant validity values.
5. Standard panic payload handling. Job identity ordering and load-balancer
   trait ownership are integrated and pass Rust tests.
6. Delete the facade and marker Cargo packages/dependencies, revise the
   independence checks, and prove a copied Cargo tree builds and tests without
   facade sources or the C++ runtime/toolchain.

The inventory and probe tables below record the starting investigation.
Completed removals above supersede their candidate status.

## Ownership and removal criteria

Canonical Rust owns SRPC scheduling, transport, serialization and reliability
policy. The [facade](../../rusty-rustc/src/lib.rs) supplies Rust representations
for the C++ contracts that the same source must emit. The transpiler omits
that package by identity. There is no remaining facade scheduler, fake event
implementation, archive implementation or separate SRPC runtime owner.
The [runtime ownership notes](../canonical-rust-runtime.md) identify the
canonical owners.

Prefer actual Rust standard-library types when their lowering preserves the
required behavior and C++ interface. A facade model can remain for an explicit
C ABI layout, a C++ consumer type, a runtime behavior difference or an emitter
contract. Removing those models requires either compiler support or a reviewed
change to the corresponding contract. It does not follow that every remaining
adapter is permanent, or that every removal needs an upstream feature.

## Current facade inventory

[scripts/facade-adapters.json](../../scripts/facade-adapters.json) contains
195 declarations. The facade has 1,482 lines, with 1,328 in `lib.rs` and 154
in `task.rs`. Both facade audit entry points pass against these files.

| Category | Declarations | What the count includes |
|---|---|---|
| `import` | 52 | 23 `use` declarations, 19 modules, 8 aliases and 2 file-attribute records. Some aliases name C++ ABI adapters; these are not all std re-exports. |
| `standard` | 108 | 23 structs, 67 impl blocks, 13 functions, 4 traits and 1 static. This combines removable wrappers with C++ contracts. |
| `trait-dispatch` | 14 | Archive/source/sink forwarding plus load-balancer trait contracts and impls. Only the archive/source/sink part is paired with `misc/serializable_support.hpp`. |
| `c-layout` | 12 | pthread and FILE types, opaque C void, fiber state and both architecture-specific register frames, IPv4 C layouts. |
| `future` | 9 | `TaskPoller`, `Task`, `Poll`, `Context`, `Waker` and their impl blocks. Scheduling remains in canonical Rust. |

These are AST declaration counts, not counts of replacement types or call
sites. In particular, an impl block counts once regardless of how many methods
it contains. The audit pins declaration tokens and rejects stale or changed
entries; a pass does not establish semantic parity between Rust and C++.

## Work that can start in SRPC

| Candidate | Evidence and next step |
|---|---|
| Replace `StdArcGetMutExt` with imported `Arc::get_mut` | `use std::sync::Arc; Arc::get_mut(...)` emits and compiles against an existing static runtime overload. The previous claim that the runtime only has a member was wrong. Six canonical uses across serialization, envelopes and TCP are candidates. Fully qualified `std::sync::Arc::get_mut` emitted an invalid C++ path in a separate probe, so use the imported spelling and validate the whole crate. |
| Replace the two `rusty::sys::time::sleep_us` calls | The calls are in `base/threading.rs` and `rpc/server.rs`. `std::thread::sleep(std::time::Duration::from_micros(...))` already emits `rusty::thread::sleep(rusty::time::Duration::from_micros(...))`. The emitted probe compiles. Review behavior before removing the facade function: `sys::time::sleep_us` calls `nanosleep` once without retrying EINTR, while `thread::sleep` uses the duration-based platform sleep path. Whole-crate imports and call behavior still need validation. |
| Move always-present `Function` uses to `Box<dyn Fn...>` | Owned boxed callbacks already lower to `rusty::Function`, including `Send`/`Sync` bounds. Review each alias and its callers for empty-state behavior before selecting a slice. The facade has explicit size/alignment padding, so Rust layout assumptions also need review. A successful type spelling probe alone does not establish a safe migration. |
| Replace the remaining const-initialized facade collections through lazy initialization | `misc/serializable.rs::registry` and `rpc/server.rs::g_rpc_id_missing` keep the facade `HashMap` and `HashSet`. `misc/any_message.rs` already demonstrates `Mutex<Option<RegistryMap>>`, initially `None`, with std maps created under the lock. This is an SRPC-only alternative to hasher lowering, but it changes representation and initialization code. Measure its C++ layout, import and symbol effects before choosing it. |
| Probe the TCP owning-fd alias | Canonical TCP already uses `Option<Arc<LegacyOwnedFd>>` and constructs the inner owner only with `from_raw_fd`. A std `OwnedFd` alias plus `AsRawFd`/`FromRawFd` imports may remove the custom inner optional owner. The existing `LegacyOwnedFd` type-map row may preserve the C++ type. This has not been transpiled or built as a migration. |
| Review redundant facade imports and stale mapping/index metadata | Direct std re-exports can move out of canonical imports independently of model removal. Do not treat all 52 `import` entries this way: `StdVector`, `SerializableStdString`, and the source/sink adapter aliases preserve distinct C++ types. `cpp-module-index.toml` still describes `log_line` with `const std::string&`; canonical `base/logging.rs` takes `&str` and the current ABI gate expects `std::string_view`. That index row needs a separate checked cleanup. |

These are next probes, not migrations completed by this investigation. The
previous statement that nothing could move with today's transpiler was too
strong. The existence of a type mapping is insufficient evidence for
removing a facade model.

## Models and the remaining blockers

| Model | Current use and removal condition |
|---|---|
| `Function<F>` | 19 `standard` declarations, including 14 signature-specific constructors. Canonical source has 23 direct `rusty::Function<...>` spellings and 9 constructors through its aliases. The old constructor count included canonical `CallbackWrapper` and `Waker` calls. Plain nullable boxed callbacks already flatten to `rusty::Function`; nullable callbacks with `Send`/`Sync` or explicit higher-ranked bounds do not. Extend the recognizer while preserving Rust bounds, then migrate uses and validate operations. Real empty-state resets occur in `reactor/reactor.rs`; the TCP callback defaults cited previously belong to canonical `CallbackWrapper`, not directly to this model. See F2. |
| `RustyFunctionIsEmpty` | The trait and its `Box<T>` impl are two additional declarations, separate from `Function`. `rpc/server.rs::ServerConnection::run_async` and `sconn_reply` use it on plain boxed callbacks so C++ callers can pass empty functions. Deleting the `Function` model would not automatically delete this contract. |
| `HashMap` / `HashSet` | Eight `standard` declarations plus the `NativeHashMap` alias. Their const constructors support the two registries above. An explicit std hasher still reaches the emitted C++ template as an extra parameter. Either add hasher lowering, or probe the existing lazy-initialization pattern and accept its measured ABI effects. |
| `SerializableStd*` | Eight underlying structs and 26 impls, plus the `SerializableStdString = std::string` alias. The 15 serialization/deserialization impl targets in `misc/serializable.rs` preserve overloads for C++ STL fields. `StringView` only has serialization. Removing them requires a way to emit additional STL-targeted trait impls without replacing the existing Rust-container overloads. See F3. |
| `StdVector<T>` | An omitted item in the earlier inventory. This is a `Vec<T>` alias in Rust, explicitly mapped to `std::vector<T>`. `rpc/frame_codec.rs::FrameBytes` and `rpc/tcp_channel.rs::TcpOutBuf` use it. Replacing it with ordinary `Vec<T>` changes those C++ types to `rusty::Vec<T>`; account for consumer and layout changes, or retain this small ABI alias. |
| `StdPair`, `std::make_pair`, `borrowed_std_pair` | The `::janus` quorum and promise interfaces need `std::pair`; `misc/serializable.rs` also has two explicit pair wire impls, and std-map serialization uses `.first`/`.second`. Rust tuples emit `std::tuple`. `borrowed_std_pair` is private facade iteration glue, not a canonical call site. See F4. |
| `RustcTcpStream`, `RustcTcpListener`, `RustcIoError`, `RustcOwnedFd`, `RustcBorrowedFd` | The differences are substantive. Stream exposes `into_owned_fd`; listener returns IPv4 addresses directly and exposes `is_bound`/`as_owned_fd`; error exposes `what`, although no canonical or Rust-test caller was found; owning and borrowed fd models can represent `-1`. std's listener/address/error/fd APIs do not have the same shape. In particular `BorrowedFd` cannot represent `-1`, and `OwnedFd` has no invalid default. Current canonical TCP already places owners inside `Option<Arc<...>>` and only constructs valid inner owners. The default-listener use found is a facade test. Probe these models separately instead of assuming their internal invalid states are required by canonical code. |
| `rusty::net` address helpers | `RustcSocketAddrV4` and `RustcIoErrorKind` already alias std types. Parse/format helpers still bridge C++ helper functions; replacing them needs working std parse/display lowering and error compatibility. `sockaddr_in_from_socket_addr_v4` copies a C ABI layout and remains native-bound. |
| `task::{Task, Poll, Context, Waker}` | `Poll` is a ready flag plus stored value, `Context` holds a raw `Waker*`, and `Waker::from_callable` owns a callback. Mapping std type names does not translate enum construction or context/waker behavior. `Task` adapts Rust futures to the C++ coroutine handle and has no direct std counterpart. See F5. |
| `sys::process::getpid` | One canonical use, in the server instance ID. `std::process::id()` currently emits `rusty::process::id()`, which the runtime lacks. Add a supported target and account for std's `u32` result versus the current signed PID. See F1. |
| `std::Cout` / `std::cout` | The logger's `write`/`put`/`flush` calls preserve capture through C++ `std::cout.rdbuf`. Rust `stdout` uses a different API, and routing bytes through a C fd write would change capture behavior. Retain the model unless that contract or its lowering changes. |
| `SourceLocation` | `base/debugging.rs::verify` preserves a C++ `std::source_location::current()` default argument. The facade already uses Rust `#[track_caller]` internally. What is missing is equivalent generated caller-location behavior, not Rust location support. See F6. |

The network/fd group needs its own migration design. Adding `TcpListener` or
`OwnedFd` type-map rows alone does not supply methods, Rust trait imports,
address conversion, or valid empty-state representations.

## Behavior and ABI contracts to preserve

| Adapter | Measured reason it remains |
|---|---|
| `thread::spawn`, `JoinHandle::{join, detach}` | C++ `thread.hpp::run_into_state` lets an escaping exception terminate the process. The facade catches Rust unwinds and aborts to match. Ordinary Rust `spawn` captures the panic in the join result, changing detached-thread failure behavior. Preserve this behavior if replacing the wrapper. |
| `thread::ThreadId`, `thread::current_id` | The canonical reactor converts IDs to/from `u64` and uses zero as unset. The facade stores `Option<std::thread::ThreadId>`. A direct std replacement cannot accept the existing zero transmute; the sentinel and conversion contract must change first. |
| `panic::{catch_unwind, payload_message, PanicPayload, do_panic}` | The shutdown-hook handler inspects an exception message. Rust has an `Any` panic payload, while C++ carries `std::exception_ptr`. Canonical sites that only swallow an unwind already use std directly. |
| `make_box` | The emitter recognizes it for source/sink adapter and trait-object coercion. A textual `Box::new` replacement needs equivalent generated construction and dispatch. |
| `RustyHandleIsValid::is_valid` | Rust `Box`/`Arc` are non-null, but C++ callers can supply empty handles. The constant Rust answer emits a real C++ validity check. |
| `StdArcGetMutExt` | The facade forwards to std's associated function. The C++ runtime has both member and static `get_mut` overloads; the imported `Arc::get_mut` probe works. This adapter is a removal candidate, subject to the six call-site probes and the existing strong/weak ownership regressions. |
| `ReactorJobSet`, `ReactorJobSetKey` | The C++ field remains `std::set` of job handles. The Rust model orders/deduplicates by handle identity; `dyn Job` does not implement `Ord`. Replacing it requires a matching ordering and ABI decision. |
| C layouts and markers | pthread storage, `FILE`, socket structures, fiber register order and `cpp_inherit` express native or compiler contracts. `LegacyCChar` is a canonical alias for C string pointers, outside the facade declaration census. Its `std::string::value_type` spelling does not make it an owned-string adapter. |

These are reasons to preserve behavior, not proof that no future standard-type
implementation is possible. Several are small compatibility contracts that may
remain after the larger callable and container models disappear.

## C++ runtime and native dependencies

[build.rs](../../build.rs) and
[scripts/native-kernel-sources.txt](../../scripts/native-kernel-sources.txt)
show exactly what Cargo compiles. Nine `.c` files provide OS operations,
clock/calendar reads, entropy and fiber resources. The manifest lists two
`.S` files; a build selects only the one matching `x86_64` or `aarch64`.
Cargo links them as `libsrpc_native.a`. It does not compile or link the
rusty-cpp C++ runtime. Removing this remaining C/assembly dependency would be
a separate platform-porting task.

The generated C++ lane has 37 canonical module providers. It also retains:

- 20 reviewed SRPC headers, comprising 12 import shims, 6 C declaration/layout
  headers, `misc/serializable_support.hpp`, and `base/rustc_markers.hpp`.
  The previous total of 16 was incorrect. `std_compat.hpp` is one of the
  import shims. Removing include shims is a C++ consumer migration.
- The same nine C sources and selected assembly file.
- The vendored rusty-cpp runtime, including handwritten `Arc`, `String`,
  `Cell`/`RefCell`, synchronization, callable, thread and coroutine headers.
  The CMake runtime dependency also brings in generated port modules. Its
  explicit archive closure has 13 entries in `CMakeLists.txt`, including
  `rusty`, `rusty_async`, and the collection, cell, string and Arc ports.
  Handwritten top-level runtime types remain in use alongside those ports.

The [native kernel audit](../../scripts/check_native_kernels.py) covers root
headers and `base/`, `misc/`, `rpc/`, `reactor/`. It does not inventory the
vendored runtime. A green native audit therefore means that SRPC's native
boundary matches its reviewed inventory, not that the generated C++ lane is
free of handwritten runtime code.

`rust-type-map.toml`, `module-preambles.toml`, `cpp-module-index.toml` and
`rusty-cpp-markers` remain compiler/ABI inputs. Their entries need review as
models disappear; they are not evidence of a second SRPC implementation.

## September migration history

| Commit | Change | Historical emission delta |
|---|---|---|
| `d6d899f` | Deleted Mutex/Condvar wrappers and six dead facade items; 53 sites use std synchronization | 38/38 files byte-identical |
| `3f5a099` | Non-static HashMap/HashSet uses moved to std | 1 file, 20 lines in a never-instantiated template |
| `52ec8e3` | Last free downgrade caller moved to `Arc::downgrade` | 1 line |
| `14be655` | Removed `LegacyStdString`; C++ interfaces use `rusty::String` | 11 modules, 208 lines of type spellings |
| `f237f1f` | Reduced the facade string model to the `SerializableStdString` wire target | 5 modules |
| `8a094ff` | 20 string parameters/callback types moved to `&str` | 5 modules, 128 lines |

The previous notes recorded 49 `std::string` mentions in emitted module bodies
after these changes. That was an emission-text measurement, not a count of
remaining facade models, and it has not been remeasured here.

## How to remove one more item safely

For a canonical-source migration, validate both consumers of the edited Rust.
This investigation changed documentation only and did not run this full gate.

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
   with `rg`, not by lint.
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

## Remaining compiler work and probe results

F1-F6 retain their earlier identifiers so existing references remain useful.
They are not six entirely missing features. The current implementation already
covers some parts, and local migrations still require whole-crate validation.

- F1 now means process-ID lowering. Add a supported target for
  `std::process::id()` and preserve the server ID conversion. Sleep lowering
  already exists; the remaining question there is semantic acceptance of its
  different implementation.
- F2 means extending nullable-callback recognition to required `Send`/`Sync`
  bounds and any needed higher-ranked signatures. Plain
  `Option<Box<dyn Fn/FnMut/FnOnce>>` already collapses to `rusty::Function`.
  The emitter supports `None`, `Some`, default construction, extraction,
  `take`, `is_some`, `is_none`, `unwrap` and supported pattern forms. Other
  Option operations can be rejected, so review the actual migrated operations.
  Do not remove Rust thread-safety bounds to make recognition succeed.
- F3 is additional STL-target emission for Rust trait impls. Foreign C++ impl
  targets and type-map overrides already exist. What is missing is a way to
  retain both the Rust-container and STL overloads without duplicate Rust impls.
  Turning `SerializableStdVector<T>` into an alias of `Vec<T>` conflicts with
  existing `Serialize`/`Deserialize` impls; String has the same problem. The
  design must also translate operations in the emitted bodies. An attribute is
  one possible design, not an implemented contract.
- F4 is opt-in pair lowering with pair construction, field access and wire
  impl targets preserved. Ordinary two-tuples still become `std::tuple` and
  `std::make_tuple`. A type spelling change alone would not handle the two
  existing pair serialization impls.
- F5 needs Poll construction, Context access and Waker ownership support.
  `Poll::Pending` currently needs a stored default `T` in the runtime, unlike
  Rust's enum variant. The canonical reactor also builds stable raw-pointer
  Waker/Context bindings, and retained wakes must outlive task teardown safely.
  The facade's `Task::from_future` already bridges real std futures and wakers.
  Replacing that representation requires source and lifetime changes as well
  as lowering. `Task` remains the C++ coroutine adapter.
- F6 is generated caller-location preservation. `#[track_caller]` plus
  `Location::caller()` does not yet synthesize the C++ default argument used by
  `verify`. Preserve the caller's location and the public function contract.
- Explicit hasher lowering must handle `BuildHasherDefault` and its constructor
  consistently. The C++ runtime already permits a third HashMap template
  parameter, so this is not simply a two-parameter arity limitation. Preserve
  default-hasher ABI/overload identity when erasing a Rust hasher, or implement
  the intended hasher semantics. The SRPC lazy-initialization alternative
  avoids needing this compiler change for the two remaining registries.

The pinned source evidence is in
[callback type mapping](../../third-party/rusty-cpp/transpiler/src/codegen/type_mapping.rs),
[codegen regressions](../../third-party/rusty-cpp/transpiler/src/codegen/tests.rs),
[nullable callback runtime tests](../../third-party/rusty-cpp/transpiler/tests/runtime_nullable_callback.rs),
and the runtime
[Arc](../../third-party/rusty-cpp/include/rusty/arc.hpp),
[thread](../../third-party/rusty-cpp/include/rusty/thread.hpp),
[sys time](../../third-party/rusty-cpp/include/rusty/sys/time.hpp), and
[async](../../third-party/rusty-cpp/include/rusty/async.hpp) implementations.

Small temporary inputs were transpiled with a release binary whose
`--build-info` matched the pinned revision. These are observed emission results:

| Rust input | Observed C++ result |
|---|---|
| `std::thread::sleep(Duration::from_micros(50))` | `rusty::thread::sleep(rusty::time::Duration::from_micros(50))`; Clang syntax check passed. |
| Imported `Arc::get_mut(arc)` | `Arc<int32_t>::get_mut(arc)` with `using rusty::Arc`; three variants, including mutation through `unwrap`, passed Clang syntax checks. |
| Fully qualified `std::sync::Arc::get_mut(arc)` | Left an invalid `std::sync::Arc<int32_t>` C++ path. |
| `std::process::id()` | `rusty::process::id()`; the runtime provides `rusty::sys::process::getpid` instead. |
| `Option<Box<dyn FnMut()>>` | `rusty::Function<void()>`. |
| `Option<Box<dyn FnMut() + Send>>` | `rusty::Option<rusty::Function<void()>>`. |
| `Box<dyn FnMut() + Send>` | `rusty::Function<void()>`. |
| `Poll::Ready(3)` / `Poll::Pending` | `rusty::Poll<int32_t>::Ready(3)` / `Pending()`, which do not match runtime `ready_with` / `pending`. |
| `Context::from_waker(w)` / `cx.waker()` | Calls to missing C++ members. |
| Explicit `HashMap<..., BuildHasherDefault<...>>` | Retains the hasher as a third template argument. |
| Two-tuple construction | `std::tuple` / `std::make_tuple`. |
| `#[track_caller]` with `Location::caller().line()` | Leaves the Rust location path in C++; no source-location default argument appears. |

Only the sleep and imported-Arc probes were C++ syntax checked, using the
configured Clang 22 compiler with these flags:

```sh
clang++ -std=c++23 -stdlib=libc++ -DRUSTY_PORTABLE_INTRINSICS=1 \
  -I third-party/rusty-cpp/include -fsyntax-only probe.cpp
```

No whole-crate migration or runtime parity run was performed. Successful transpilation alone
is not evidence that the emitted API exists or that behavior is preserved.

## Audit follow-up and verification

The `standard` category combines replacement candidates and explicit C++
contracts; `import` combines plain std exports and ABI aliases. Splitting or
annotating those groups would make the remaining work easier to track. It
would require coordinated changes to `facade_audit.py`, the inventory and its
negative controls. It should not reclassify every currently justified model as
permanent, or treat a lower declaration count as proof of runtime correctness.

Verified for this documentation revision:

```sh
python3 scripts/check_facade_shadow.py
python3 scripts/check_facade_stubs.py
python3 scripts/check_native_kernels.py
python3 scripts/tests/test_facade_audit.py
```

All three audit commands passed; all 42 facade audit tests passed. Declaration
and native-header counts were recomputed from the reviewed JSON inventories.
The source investigation and temporary probes above supplied the new findings.
The full Cargo/CMake/ctest and sanitizer gates were not rerun for this doc-only
change. Their historical results should not be read as acceptance of a proposed
adapter migration.
