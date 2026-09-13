# Rust lane independence

Updated 2026-09-13. The target is a canonical Cargo lane that runs on Rust std
and the small C/assembly kernel, without a Rust facade package or C++ runtime.
The generated C++ lane remains a separate consumer of the same Rust sources.

## Current status

The integrated tree passes its Cargo independence check. The check
copies the canonical Rust modules, tests, Cargo inputs and native kernel into a
new directory. It copies no facade, C++ runtime, C++ source or transpiler, and
runs Cargo with a restricted tool path and `CXX=/bin/false`.

The isolated copy passes 266 Rust tests and two doctests, with one existing layout
test ignored, plus clippy with warnings denied. The canonical body audit,
native source audit, native ABI audit and 47 negative controls also pass.
The regular workspace tests, doctests and clippy also pass on the integrated
tree. Complete generated-C++ validation is still in progress.

Remaining acceptance work:

1. Build all generated C++ providers with the updated compiler. Preserve the
   existing public symbols and measure necessary additions from canonical
   helpers and load-balancer traits.
2. Run the full CMake/CTest gates and runtime sanitizer checks. Record their
   results here before declaring the migration complete.

## Removed dependencies

The migration replaces the original 195 facade declarations with standard Rust
APIs, canonical implementations and explicit native ABI declarations:

| Former facade dependency | Canonical replacement |
| --- | --- |
| Arc, collections, synchronization and ownership extensions | Rust std types, `Arc::get_mut`, and lazy initialization for shared registries |
| Required and nullable callable models | `Box<dyn Fn...>` and `Option<Box<dyn Fn...>>` with the original thread-safety bounds |
| Invalid/default owner models and constant validity methods | Required owners or explicit `Option<Arc<...>>` / `Option<Box<...>>` |
| Descriptor and TCP models | Standard owned/borrowed descriptors, `TcpStream`, `TcpListener`, and I/O errors |
| Sleep, process ID, thread and logging helpers | Standard thread/process APIs and stdout `Write` / `flush` |
| Task, Poll, Context and Waker models | Standard `Future`, `Pin`, `Poll`, `Context`, `Wake` and `Waker`; scheduling remains in `reactor/reactor.rs` |
| Panic payload helpers | `panic_any`, `catch_unwind`, `AssertUnwindSafe`, and typed payload downcasts |
| STL serialization models and source/sink dispatch | Canonical byte/container algorithms plus C++ consumer forwarders |
| Pair/vector models | Rust tuples and `Vec`, with scoped compiler mappings for existing C++ pair/vector interfaces |
| Job-set model | Canonical `BTreeMap` keyed by shared job identity |
| Load-balancer contracts | Canonical traits and implementations |
| IPv4 parsing/formatting models | Standard Rust address parsing and formatting |
| pthread, FILE, C void and fiber layout models | Explicit canonical C ABI declarations checked against the native binding inventory |

C++ lowering support was extended where standard Rust and the existing C++
interface differ. This includes callback ownership/borrowing, nullable owner
aliases, future and waker lifetimes, move-only thread captures, standard panic
payloads, native type bindings, and scoped container mappings. Ordinary Rust
owners retain their normal Rust semantics.

The integrated retirement deletes `rusty-rustc` and `rusty-cpp-markers`, their
Cargo dependencies and lockfile entries, the old facade inventory/audit entry
points, the unused C++ marker header, and obsolete compiler metadata. The
canonical AST audit retains the reviewed constant-function inventory and
rejects new missing or default-returning runtime bodies.

## What remains by design

Cargo compiles the nine C files in
[scripts/native-kernel-sources.txt](../../scripts/native-kernel-sources.txt),
plus the fiber assembly selected for `x86_64` or `aarch64`.
[build.rs](../../build.rs) links them as `libsrpc_native.a`. They provide OS
operations, clock/calendar fields, entropy, native resource/layout access and
fiber context switching. Linux, a supported architecture, a C compiler and an
archiver remain required. Scheduling, transport decisions and serialization
policy stay in canonical Rust. Removing the C/assembly kernel is outside this
goal.

The C++ build continues to use the vendored rusty-cpp runtime, compiler type
mappings, module preambles and C++ compatibility headers. Cargo does not load
or link these inputs. The C++ serialization adapters perform container
operations and forward into canonical implementations; they retain existing
C++ overloads and erased adapter entry points.

## Validation record

The first integrated std/descriptor/location batch passed the complete C++
build, the existing ABI inventory, and all 26 then-configured SRPC CTests.
Later slices have independent C++ checks:

- Native C/fiber type binding changes preserve every defined symbol in five
  affected providers.
- Standard IPv4 parsing matches 637 Rust-derived cases. TCP retains all
  existing strong provider symbols.
- Standard stdout logging preserves all 40 defined symbols and passes byte,
  embedded-NUL, newline and flush checks.
- Serialization retains all 596 existing strong symbols and adds one
  canonical `serialize_bytes` helper. All 12 C++ parity tests pass.
- Future runtime checks cover non-default, move-only outputs, retained wakers,
  nested suspension, cancellation and ownership, including address/undefined
  sanitizer tests.

These slice results do not replace final combined C++ and sanitizer checks.
The authoritative symbol expectations remain in
[scripts/check_srpc_crate_mode.py](../../scripts/check_srpc_crate_mode.py).

## Starting investigation

The initial investigation used SRPC `c591960` and rusty-cpp
`3e1d95059839e4bf1968891047ff1563a2f08c17`. The facade then contained 195 AST
declarations across 1,482 lines: 52 import records, 108 standard/model records,
14 trait-dispatch records, 12 C-layout records and nine future records.
The old inventory and F1-F6 probe details are available in this file's Git
history. They describe the starting tree, not remaining work.

See [canonical-rust-runtime.md](../canonical-rust-runtime.md) for runtime
ownership and the migration's behavioral contracts.
