# The SRPC Book

A developer's guide to **SRPC** — a Simple RPC framework for C++23.

You describe a service in a small `.rpc` file, run the code generator, and get a typed
server base class and a typed client proxy. Underneath sits an epoll reactor, stackful
fibers, a pluggable transport, binary serialization, and the usual production plumbing:
timeouts, retries, reconnect, circuit breaking, connection pooling.

SRPC is a standalone library. It descends from
[simple-rpc](https://github.com/santazhang/simple-rpc), so the IDL will look familiar;
the generated C++ will not. Handlers take a typed request struct and return
`rusty::Result`, and everything lives in namespace `srpc::`.

### How to read this book

The repository's `README.md` is the quick start — clone, write a `.rpc` file, generate,
build, run. This book is the reference behind it, taking the same machinery apart layer
by layer: the fiber context switch, the reactor, the wire format, the client and server
state machines, and the code generator that ties them to your service definition.

Two conventions to know before you start.

**Code blocks are tagged.** Every C++ block carries exactly one of `srpc-compile`,
`srpc-compile-client`, `srpc-compile-server`, `srpc-compile-codegen` or
`srpc-no-compile` after the language word. `tests/rpc_docs_snippet_compile_test.py`
reads those tags and compiles the ones that claim to be compilable, against a build
tree's own module map. `srpc-no-compile` marks a fragment that is illustrative: it
needs a generated header, a surrounding function, or some other context the harness
cannot supply. A tagged-compilable snippet is a claim the harness checks; an untagged
one fails the test outright.

**Ground truth is Rust.** SRPC's 37 production modules are canonical Rust files living
at their historical C++ paths in `base/`, `misc/`, `reactor/` and `rpc/`; the C++
modules that ship in `libsrpc.a` are generated from exactly those bytes. When this book
and a `.cc` file under `tests/` disagree, believe the `.rs` — CMake compiles only 9 of
the 76 test files, and the rest have drifted. Chapter 1 introduces that arrangement, and
Chapter 14 goes into the memory-safety machinery that comes with it.

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Architecture Overview](#2-architecture-overview)
3. [Fibers (Stackful)](#3-fibers-stackful)
4. [The Reactor Pattern](#4-the-reactor-pattern)
5. [Event System](#5-event-system)
6. [I/O Layer: Polling and Connections](#6-io-layer-polling-and-connections)
7. [RPC Protocol](#7-rpc-protocol)
8. [RPC Client](#8-rpc-client)
9. [RPC Server](#9-rpc-server)
10. [Serialization](#10-serialization)
11. [Reliability Features](#11-reliability-features)
12. [Service Definition and Code Generation](#12-service-definition-and-code-generation)
13. [Threading and Synchronization](#13-threading-and-synchronization)
14. [Memory Safety (RustyCpp)](#14-memory-safety-rustycpp)
15. [Performance Tuning](#15-performance-tuning)
16. [API Reference](#16-api-reference)
17. [Pitfalls and Best Practices](#17-pitfalls-and-best-practices)
18. [Troubleshooting](#18-troubleshooting)

---

## 1. Introduction

SRPC is a Simple RPC framework for C++23. It has two halves, and it helps to think
about them separately.

The first is a **code generator**. You write an interface in a small `.rpc` file, run
`simplerpcgen.rpcgen` over it, and get a C++ header containing a service base class with
one typed virtual per method, a matching client proxy, and a request/response struct
pair per method. You never write serialization code and you never touch a method id.

The second is the **runtime** those generated stubs sit on: an epoll reactor driving one
or more poll threads, stackful fibers so a handler may block, a framed binary transport
over TCP (or an in-process switchboard for tests), futures, and a layer of reliability
machinery — reconnect policy, circuit breaker, request timeouts and retries, a
connection pool.

Everything lives in namespace `srpc::`. The one alias shipped is `namespace base = srpc;`.

### The shape of a service

An interface definition is short. Inputs and outputs are separated by `|`, and integers
must carry an explicit width:

```
namespace demo

abstract service Demo {
    sum(i32 a, i32 b, i32 c | i32 result);
};
```

The generator turns that into `DemoService` — with `RpcSumRequest` and `RpcSumResponse`
as *members* of the class — and `DemoProxy`, which re-exports both structs. You supply
the body:

```cpp srpc-no-compile
class MyDemoService : public demo::DemoService {
public:
    rusty::Result<RpcSumResponse, srpc::i32> sum(const RpcSumRequest& req) override {
        RpcSumResponse resp{};
        resp.result = req.a + req.b + req.c;
        return rusty::Result<RpcSumResponse, srpc::i32>::Ok(resp);
    }
};
```

A handler returns `Ok(resp)` with the response filled in, or `Err(code)` with an
`errno`-style integer of your choosing. There are no out-parameters: among the handler
shapes, only `defer` writes through a reference, and only a `raw` method's *proxy* keeps
the old pointer-out-parameter calling convention.

The caller side is the mirror image. Wrap a connected `Client` in the generated proxy
and call the method:

```cpp srpc-no-compile
demo::DemoProxy proxy(const_cast<srpc::Client*>(client.get()));

demo::DemoProxy::RpcSumRequest req;
req.a = 1; req.b = 2; req.c = 3;

auto result = proxy.sum(req);              // rusty::Result<RpcSumResponse, srpc::i32>
if (result.is_ok()) {
    printf("%d\n", result.unwrap().result);
}
```

Every method also gets an `async_` form; for everything but `raw` it returns a typed
future. Chapter 12 covers the IDL and the generator in full; Chapters 8 and 9 cover the
client and server.

### Why a custom framework?

gRPC and Thrift solve a problem SRPC does not have: talking to a heterogeneous fleet
over a contract that has to stay stable for years. If that is your problem, use them.
SRPC is built for the opposite situation — a cluster of machines you control, running
binaries you built together, where you would rather be able to read and change the whole
stack than negotiate with it.

Four things follow from that.

**It is small enough to read.** The entire library is 37 modules in one archive: no
protobuf, no HTTP/2, no external RPC runtime, no dependency you did not build. The
request path — generated proxy, `Client::request`, the connection, the channel, the poll
thread, the server's dispatch, the reply — runs through four files you can walk end to
end in an afternoon, and instrument anywhere along it.

**The wire format is minimal.** A 4-byte frame header, then `v64 xid`, `i32 rpc_id`, then
the arguments; replies carry `v64 xid`, `v32 error_code`, `v64 server_instance_id`, then
the payload. No metadata map, no content negotiation, no compression handshake. The
header is **native-endian**, which is a deliberate trade: it is free to write and read on
both ends, and it only works between machines that agree on byte order. Chapter 7 has the
details.

**Handlers are allowed to block.** By default a request runs in its own stackful fiber,
so a handler can make a nested RPC call, wait on an event, or sleep, without stalling the
poll thread it arrived on. Frameworks built purely on callbacks or stackless coroutines
make that pattern awkward; here it is the default. When you do not want a fiber, the IDL
lets you say so per method — `fast` runs inline on the poll thread, `defer` hands you a
reply handle to use whenever the answer is ready, `async` runs as a stackless
`rusty::Task`, and `raw` hands you the undecoded request. Chapter 12 works through all
six, the explicit `fiber` attribute included; Chapter 9 covers what the server does
around your handler.

**It is meant to be modified.** The generator is about a thousand lines of readable
Python in `pylib/`, not a plugin API. Adding a dispatch mode, changing the header layout, or
instrumenting the scheduler are all normal-sized edits rather than upstream negotiations.

What you give up is real, and worth stating plainly: there is no TLS, no authentication,
no streaming, no cross-language client beyond a generated Python stub, and no wire
compatibility guarantee across generator runs — method ids are random integers kept
stable only by scraping them back out of the header the generator is about to overwrite.

### Key features

| Feature | Description |
|---------|-------------|
| **Typed code generation** | `.rpc` IDL to a typed service base class, a client proxy, and per-method request/response structs |
| **Stackful fibers** | 1 MiB mmap'd stacks with a guard page, switched by hand-written x86_64 / aarch64 assembly |
| **Stackless tasks** | `rusty::Task` coroutines for `async` handlers, driven by the same reactor |
| **Reactor** | epoll-based poll threads, timers, and event primitives (int, timeout, wait-any, wait-all, quorum) |
| **Pluggable transport** | framed TCP, or an in-process switchboard with fault injection for tests |
| **Binary serialization** | `srpc::Serialize_` / `Deserialize_` over `BinaryWriteArchive` / `BinaryReadArchive` |
| **Reliability** | reconnect policy, circuit breaker, per-request timeouts and retries, connection pooling |
| **Generated from Rust** | all 37 production modules are canonical Rust, gated against the shipped archive's ABI |
| **Machine-checked contracts** | Verus specifications on `misc/stat.rs` and `rpc/internal_protocol.rs` |

### The unusual part: the sources are Rust

SRPC is a C++ library whose production modules are not hand-written C++. All 37 of them
are Rust files living at their historical C++ paths in `base/`, `misc/`, `reactor/` and
`rpc/`, and a pinned `rusty-cpp` transpiler generates an `srpc.<name>.cppm` C++23 module
from each. Two toolchains read the same bytes: rustc, through a generated crate index at
`src/lib.rs`, and rusty-cpp, in one whole-crate invocation.

The hand-written non-Rust that remains is seam rather than logic —
`reactor/epoll_platform_linux.cc` for the Linux epoll layer, eight plain-C syscall
kernels, the two fiber context-switch assembly files, and a handful of C++ headers
including the `srpc.hpp` umbrella.

Two consequences matter to you as a reader. First, a change to a `.rs` file is
simultaneously a Rust change and a C++ ABI change, and the build enforces that: a gate
recompiles every generated module, links an importer program against both the fresh
objects and the shipped archive, and compares `nm` symbol sets. Second, because the
source of truth is Rust, functional contracts can be machine-checked in place. Two
modules carry Verus specifications behind `#[cfg(verus)]` — the response-header codec
round-trip in `rpc/internal_protocol.rs`, and a first-sample invariant in `misc/stat.rs`
that pins a bug which actually shipped. `docs/verification.md` is the standing reference
for that lane, and it is honest about how narrow it is.

Chapter 14 returns to this in detail.

### Performance

SRPC ships no throughput or latency numbers, because nothing in this repository produces,
records, or corroborates any.

What the tree does ship is three benchmark drivers you can run yourself:
`tests/rpcbench.cc`, a full client/server load generator with `fast`, `fiber`, `defer`,
`async` and `fast_vec` modes and knobs for payload size, epoll instances, client threads
and worker threads; `tests/bench_future.cc`; and `tests/bench_marshal.cc`, which measures
the archive serialization paths in isolation and reports ns/op and ops/sec. CMake builds
none of them — there is no benchmark target — so wiring one up and running it on your own
hardware is the only number worth quoting.

What can be said without measuring is where the costs sit structurally. A `fast` handler
runs inline on the poll thread with no fiber at all, which makes it the cheapest option
and also means it must never block: if it does, every connection on that thread stalls. A
default handler costs one fiber per request — a 1 MiB mmap'd stack (`kDefaultStackBytes`
in `reactor/reactor.rs`) plus a guard page, and a context switch in and out — in exchange
for being allowed to block. `defer` also takes a fiber but decouples the reply from the
handler's return. `async` is entered inline on the poll thread and then resumed as a
stackless coroutine, so it has the same don't-block rule up to its first suspension point.

Two facts will shape your first measurements more than anything on that list. The
blocking `Future::wait()` is hard-capped at **one second** and then latches `ETIMEDOUT`
(110) — a later reply cannot mark it ready — so any call slower than a second looks like
a timeout. Escaping it takes the whole recipe: go through `request_with_options`, call
`set_options` on the future it hands back (the coordinator future is deliberately
created with `timeout_ms = 0`), and then wait with `wait_with_options()` — the only
waiter that reads those options. Plain `wait()` ignores them and re-imposes the
one-second cap. And `-march=native` is mandatory: it is a module-compatibility
requirement rather than a tuning knob, which incidentally means a build tree cannot be
copied to a machine with a different CPU. Chapter 15 covers tuning properly.

### Rough edges

Some parts of the surface are present but not live. They are called out again where they
belong, and collected in the shipping-status table in Chapter 11, but knowing them up
front will save you a debugging session:

- `Client::metrics()` returns a permanently zeroed stub. The live counters are on
  `ClientConnection::metrics()`, reached through `client->connection()`, which returns an
  `Option`. Because `ClientPool`'s `LEAST_CONNECTIONS` and `LEAST_LATENCY` strategies and
  its health checks read the stub, they do not currently differentiate and nothing is
  ever evicted.
- Buffered requests are parked with a TTL and expired, never replayed after a reconnect.
- The heartbeat protocol is implemented end to end, but nothing currently ticks the
  client-side timer.
- `set_keepalive()` stores its configuration; pushing it to the socket is an empty stub.
- Retries are opt-in twice over: `max_retries` does nothing unless `idempotent` is also
  set.

### Scope and requirements

SRPC targets **Linux**. The poll layer is epoll and nothing else — the kqueue twin and
the macOS branches were removed, leaving `reactor/epoll_platform_linux.cc` as the single
platform implementation unit. Fiber context-switch assembly exists for x86_64 and
aarch64.

Building the C++ needs **Clang 22 or newer with libc++**, CMake 3.30+, Ninja, Cargo (with
clippy), and Python 3.11+. The Rust-only lane — `cargo test --locked --workspace
--all-targets` — needs none of the C++ toolchain and is the fast inner loop. There is no
`install()` and no CMake package config, so downstream consumption is `add_subdirectory`
and repeating srpc's toolchain settings by hand; and there is no CI, so the pre-commit
sequence in `CLAUDE.md` is the entire safety net.

One thing this book cannot do is compile itself against a fully checked-out tree; the
`third-party/` submodules are not required to be present to read it. Snippets tagged
`srpc-no-compile` are illustrative by construction, and the rest are checked by
`tests/rpc_docs_snippet_compile_test.py` only when you run it against a configured build
directory.

---

## 2. Architecture Overview

### One source, two toolchains

Before any diagram of layers, there is a fact about this repository that governs
everything else: **SRPC is a C++23 named-module library whose production modules are not
hand-written C++.** All 37 of them are canonical **Rust** files living at their historical
C++ paths — `base/`, `misc/`, `reactor/`, `rpc/` — and the pinned `rusty-cpp` transpiler
turns each one into a complete C++23 module provider named `srpc.<module>`.

Two toolchains read the exact same bytes:

- **rustc / Cargo**, through the *generated* crate index `src/lib.rs`, which is nothing
  but a list of `#[path = "../rpc/frame_codec.rs"] pub mod frame_codec;` declarations.
  `src/` contains that one file and nothing else.
- **rusty-cpp**, in one whole-crate invocation, which emits all 37
  `srpc.<name>.cppm` files into the build tree (`build/goal0-crate-cpp/`). Those
  generated interface units are the only *providers* inside `libsrpc.a`.

So when this book says "`rpc/client.rs`", it means both the Rust module `srpc::client`
that `cargo test` compiles and the C++ module `srpc.client` that your `import srpc.client;`
resolves against. They are one file. The practical consequence, and the one worth
remembering: **a change to a `.rs` file is simultaneously a Rust change and a C++ ABI
change.** A green `cargo test` proves nothing about whether the C++ still builds or kept
its ABI.

The build checks that equivalence rather than trusting it. The `srpc_goal0_dual_compile`
target recompiles every generated module on its own, links one importer program twice —
once over those fresh objects placed ahead of `libsrpc.a`, once against `libsrpc.a` alone
— runs both, and compares per-module `nm` strong-symbol sets. The archive must carry
exactly what the freshly generated objects carry, plus the symbols of the one platform
implementation unit. Today those constants are 1961 provider symbols and 5 platform
symbols, frozen in `scripts/check_srpc_crate_mode.py`.

Hand-written non-Rust does survive, but only as *seam*, never as logic:

- `reactor/epoll_platform_linux.cc` — the single hand-maintained C++ translation unit,
  supplying the Linux epoll implementation behind `srpc.epoll_wrapper`.
- Eight plain-C syscall kernels (`base/srpc_base.c`, `misc/srpc_io.c`, `misc/srpc_rand.c`,
  `misc/srpc_timing.c`, `reactor/srpc_fiber.c`, `rpc/srpc_connect.c`, `rpc/srpc_net.c`,
  `rpc/srpc_server.c`) and the five `srpc_*.h` headers that declare them. Syscall numbers and build flags live here
  precisely because their values are arch- and build-dependent and must not be frozen into
  a Rust constant.
- Two assembly files, `reactor/fiber_context_x86_64.S` and `reactor/fiber_context_aarch64.S`,
  which perform the actual fiber context switch.
- A handful of C++ headers: the umbrella `srpc.hpp`, `std_compat.hpp`, `base/all.hpp`,
  `misc/serializable_support.hpp` (the real open-set ADL serialize/deserialize dispatch),
  `base/rustc_markers.hpp`, and several `#pragma once` shims whose entire body is
  `import srpc.<module>;`.

That last category is a trap for readers browsing the tree. Finding `rpc/frame_codec.hpp`
next to `rpc/frame_codec.rs` does not mean there are two implementations; the `.hpp` is a
one-line shim. Never look for behavior in one.

### The layers

Directories, not crates. Everything is one flat `srpc` crate, and the layering is a
convention the sources honor rather than something the build enforces:

```
+------------------------------------------------------------+
|  Your application: generated service classes and proxies    |
+------------------------------------------------------------+
|  rpc/     22 modules                                        |
|    client.rs   Client, ClientConnection, ClientPool, Future |
|    server.rs   Server, Service, Request, DeferredReply      |
|    channel.rs  transport trait; tcp_channel / inmemory      |
|    frame_codec.rs, internal_protocol.rs   wire format       |
|    circuit_breaker, heartbeat, reconnect_policy,            |
|    connection_state, request_queue, connection_metrics      |
|    pollable_proxy.rs   the Pollable facade the reactor uses |
+------------------------------------------------------------+
|  reactor/  4 modules                                        |
|    reactor.rs  Reactor, Fiber, PollThread, PollThreadWorker |
|                IntEvent / SharedIntEvent / TimeoutEvent /   |
|                NeverEvent / BoxEvent<T> / WaitAll / WaitAny |
|                / QuorumEvent, and the stackless Task driver |
|    fiber.rs    this_fiber:: operations on the current fiber |
|    future.rs   FiberPromise / FiberFuture                   |
|    epoll_wrapper.rs   Epoll over the platform seam          |
+------------------------------------------------------------+
|  misc/     5 modules                                        |
|    serializable.rs, serializable_envelope.rs, any_message.rs|
|    rand.rs, stat.rs                                         |
+------------------------------------------------------------+
|  base/     6 modules                                        |
|    basetypes.rs (i8..i64 aliases, SparseInt, v32/v64,       |
|      Counter, Time, Timer), threading.rs, logging.rs,       |
|      debugging.rs, misc.rs, callback_wrapper.rs             |
+------------------------------------------------------------+
|  Plain-C kernels + fiber assembly + epoll_platform_linux.cc |
+------------------------------------------------------------+
|  System: POSIX sockets, epoll, pthreads                     |
+------------------------------------------------------------+
```

The nominal direction is `base/` → `misc/` → `reactor/` → `rpc/`, and if you grep the
`use crate::` lines you will find it mostly holds: `misc/serializable.rs` reaches down to
`basetypes` and nothing else, `rpc/client.rs` reaches down to eighteen modules spread over
`base/`, `misc/` and `rpc/`, and no module under `base/` has a `use crate::` line at all.

Two inversions are worth knowing before you go looking for them.

**`reactor/reactor.rs` imports `crate::pollable_proxy`, which lives under `rpc/`.** The
poll thread stores what it is watching as `FdPollableMap = HashMap<i32, PollableProxy>`,
and `PollableProxy` — an alias for `Box<dyn PollableBase>` — along with the trait itself
and the typed `Arc` adapter `PollableArcShim<T>`, is owned by `rpc/pollable_proxy.rs`.
So the reactor's central data structure is typed by an `rpc/` module. (Do not confuse
`PollableBase` with `epoll_wrapper::Pollable`; `reactor.rs` imports both, and they are
different traits.) The layering is upside down at exactly this one seam.

**Nothing uses `crate::reactor` at all.** Not `rpc/client.rs`, not `rpc/server.rs`, not
even the sibling files `reactor/fiber.rs` and `reactor/future.rs`. Every consumer reaches
the reactor through the foreign-module facade instead:

```rust
use rusty as cpp;
use cpp::srpc::reactor as cpp_reactor;
```

...and calls into it inside `unsafe` blocks. That is deliberate: `cpp::srpc::reactor`
models the *C++ module boundary*, which is how the reactor is actually reached in the
shipped library, and routing through it keeps the Rust view honest about where the
boundary sits. `rusty` here is the `rusty-rustc` facade crate in this workspace;
rusty-cpp omits it from generated C++ by package identity, which is why a Rust test that
only reaches through this facade proves very little about runtime behavior.

Nine of the 37 modules sit outside the internal dependency graph entirely: they carry no
`use crate::` line of their own, and no other module names them. `rpc/idempotency.rs` and
`rpc/completion_tracker.rs` are the two that most often surprise people — consumer-facing
utilities that neither `client.rs` nor `server.rs` uses. `rpc/utils.rs`, `misc/stat.rs`,
`misc/any_message.rs`, `misc/serializable_envelope.rs` and `base/threading.rs` are in the
same position, as are `reactor/fiber.rs` and `reactor/future.rs`, which reach outward only
through the `cpp::srpc::…` facade above.

One naming caution while reading the reactor layer. There are two unrelated "future"
types. `reactor/future.rs` holds `FiberPromise<T>` / `FiberFuture<T>`, a fiber-blocking
handoff. The RPC `Future` you get back from `Client::request` is a *different* type
declared in `rpc/client.rs`. Chapter 8 covers the second; do not go looking for it under
`reactor/`.

### The path of one request

The layers above are easier to hold onto with one request traced through them. Chapters 7
through 9 do this properly; here is the shape.

A generated proxy calls `Client::request(rpc_id, attr, write_fn)`, which hands off to
`ClientConnection`. That connection is where the reliability machinery lives — the
circuit-breaker gate, stale-request expiry and the offline queue are all consulted before
anything is written. A `Future` is created against the transaction id and parked in the
pending map, and the body is serialized as `v64 xid | i32 rpc_id | args`. The channel then
sends the frame, and **the TCP backend, not the codec, prepends the 4-byte header** — a
native-endian word whose bit 31 is the extended-header flag and whose low 31 bits are the
payload size, capped by `kMaxFramePayloadSize` (64 MiB) as a stream-integrity bound rather
than a resource policy.

On the far side, the server's `TcpConnection` re-frames the byte stream — that logic lives
in `rpc/tcp_channel.rs`, not `server.rs` — and dispatches. Methods registered on the fast
path — `fast`, `prefix` and `async` — run inline on the poll thread; everything else gets a
stackful fiber. The reply goes back as
`v64 xid | v32 error_code | v64 server_instance_id | payload`, and the client resolves the
async slot (`xid % 16384`, `kAsyncSlotCount`) first, then the pending-future map. A reply
matching neither is dropped silently — the normal outcome after a timeout, and it leaves
no trace in any log.

Concurrency underneath is both stackful and stackless. Fibers are mmap'd stacks (1 MiB
default plus a guard page) switched by the two `.S` files, and the field order of
`srpc_fiber_ctx` in `reactor/srpc_fiber.h` *is* the ABI contract with that assembly. The
`Reactor` is thread-local in the generated C++; under rustc those
`#[cfg_attr(any(), thread_local)]` markers are inert and the statics are ordinary process
globals, which is why `reactor/reactor.rs` is not meant to be exercised as plain Rust. The
same reactor also drives stackless `rusty::Task` pollers.

### Directory structure

The tree as it actually is. `.rs` files under `base/ misc/ reactor/ rpc/` are the
canonical sources; `.hpp` files beside them are shims or seam, never a second
implementation.

```
srpc/
  base/                     6 canonical modules + seam
    basetypes.rs              primitive aliases, SparseInt, v32/v64, Counter,
                              Time, Timer
    callback_wrapper.rs       callback ownership adapter
    debugging.rs              assertions and debug helpers
    logging.rs                FATAL/ERROR/WARN/INFO/DEBUG
    misc.rs                   general utilities
    threading.rs              SpinLock plus pthread mutex/cond wrappers
    all.hpp                   five-import umbrella for base/
    rustc_markers.hpp         declares rusty::cpp_inherit for the C++ side
    srpc_base.c               plain-C: execinfo backtrace capture and a
                              path-basename scan (no header)

  misc/                     5 canonical modules + seam
    any_message.rs            the AnyMessage payload container
    rand.rs                   random generator
    serializable.rs           BinaryWriteArchive / BinaryReadArchive over
                              BufferSink / BufferSource / FdSink / FdSource
    serializable_envelope.rs  SerializableEnvelope<PayloadSet>
    stat.rs                   statistics (carries Verus specs)
    serializable_support.hpp  the real open-set ADL serialize/deserialize
    any_message.hpp, serializable.hpp, serializable_envelope.hpp   import shims
    srpc_io.c, srpc_rand.c, srpc_timing.c   plain-C kernels (only rand and
                              timing have headers; srpc_io.c is declared
                              extern "C" at its call sites)

  reactor/                  4 canonical modules + seam
    reactor.rs                3.7k lines: Reactor, Fiber, PollThread,
                              PollThreadWorker, the whole event family,
                              QuorumEvent, stackless Task support
    fiber.rs                  this_fiber:: surface
    future.rs                 FiberPromise / FiberFuture
    epoll_wrapper.rs          Epoll type over the platform seam
    epoll_platform_linux.cc   the one hand-maintained C++ TU (inline-Rust DSL)
    fiber_context_x86_64.S    context-switch assembly
    fiber_context_aarch64.S
    srpc_fiber.c, srpc_fiber.h   mmap+guard stacks, the ABI context seed, the
                                 thread-local active-fiber slot, and the
                                 resume/yield/finish state machine. The struct
                                 field order in the .h is the ABI contract
                                 with the .S files
    CANONICAL_CHECKPOINT.md   stale, rrr-era; useful only for its ABI tables

  rpc/                      22 canonical modules + seam
    client.rs                 Client, ClientConnection, ClientPool, Future,
                              FutureAttr, BufferingConfig, PoolConfig
    server.rs                 Server, Service, Request, DeferredReply,
                              ServerConnection, shutdown state machine
    channel.rs                ChannelConnectionBase / ChannelFactoryBase traits
    tcp_channel.rs            TcpConnection, TcpListener, TcpFactory; also the
                              server-side re-framing and the outgoing header
    inmemory_channel.rs       frameless, synchronous loopback transport
    fiber_channel.rs          adapts callback delivery to a blocking recv_frame
    frame_codec.rs            header write/parse, FrameStreamReader
    internal_protocol.rs      reply-header bit layout (carries Verus specs)
    errors.rs                 error codes
    callbacks.rs              connection lifecycle callbacks
    circuit_breaker.rs        breaker state machine
    connection_state.rs       connection state machine
    connection_metrics.rs     per-connection counters
    heartbeat.rs              keep-alive protocol
    reconnect_policy.rs       backoff calculation
    request_options.rs        per-request timeout/retry configuration
    request_queue.rs          offline request buffering
    load_balancer.rs          ClientPool selection strategies
    pollable_proxy.rs         the Pollable facade the reactor imports
    completion_tracker.rs     standalone utility, unused by client/server
    idempotency.rs            standalone utility, unused by client/server
    utils.rs                  port and hostname helpers
    *.hpp                     import shims (frame_codec.hpp also carries the
                              load-bearing <queue>/<stack> includes)
    srpc_connect.c, srpc_net.c, srpc_server.c   plain-C syscall kernels
                              (srpc_connect.h and srpc_server.h only)

  src/
    lib.rs                    GENERATED crate index; never hand-edit, and never
                              add any other file under src/

  pylib/                    the .rpc code generator
    simplerpcgen/             rpcgen.py (the live generator), lang_cpp.py,
                              lang_python.py, misc.py, rpcgen.g (stale grammar)
    yapps/                    parser runtime only, no compiler

  tests/                    37 tests/*_rust.rs, 76 .cc files (8 are built),
                            plus the benchmark_service IDL example
  scripts/                  the gates: extract_srpc_rust.py,
                            check_srpc_crate_mode.py, srpc_dsl_check.sh,
                            verify_srpc.sh, tests/
  verify/                   workspace-excluded Verus harness that #[path]-links
                            the real sources
  rusty-rustc/              the hand-written `rusty` facade crate (rustc only)
  rusty-cpp-markers/        proc-macro markers
  docs/                     verification.md, and this book
  third-party/              rusty-cpp (pinned transpiler), googletest

  srpc.hpp                  the consumer umbrella header
  std_compat.hpp            the only library file that spells `import std;`
  CMakeLists.txt            module inventories, gates, ABI checks
  rust-modules.toml         the 37-row canonical module manifest
```

Root-level TOML files beyond the manifest configure the emitter:
`module-preambles.toml` (C++ includes and foreign symbols a module needs),
`cpp-module-index.toml`, and `rust-type-map.toml` (exact legacy type spellings).

Two absences in that tree are worth calling out, because older documentation promises
them. There is no `src/rrr/` and no `src/srpc/`: `src/` holds the generated `lib.rs` and
nothing else, and adding any other file — or a symlink — under it fails the gates.
And `pylib/` contains only the *generator*, not a Python runtime. There is no
`pylib/simplerpc/` package here, so the Python stub that `lang_python.py` emits — which
opens with `from simplerpc.marshal import Marshal` — has nothing to import against in a
standalone checkout. `pylib/` is not wired into CMake either; generator output is checked
in, and `tests/benchmark_service.rpc` is its only input.

### What the umbrella header gives you

`srpc.hpp` is the header nearly every consumer includes. It is a textual `#include`
chain followed by a hand-maintained list of `import srpc.*;` lines, in that order — libc++
rejects the reverse and fails with ODR errors inside its own internals, which is also why
`std_compat.hpp` exists to do the same for the `std` module. At the very bottom it
declares the one alias the library ships:

```cpp srpc-no-compile
namespace base = srpc;
```

Nothing generates or checks that import list, so it can drift, and eight modules are
deliberately commented out of it: `srpc.circuit_breaker`, `srpc.connection_metrics`,
`srpc.epoll_wrapper`, `srpc.heartbeat`, `srpc.internal_protocol`, `srpc.load_balancer`,
`srpc.reconnect_policy` and `srpc.request_options`. Each carries the same note — *nothing
outside srpc names it (build-time opt)* — though nothing in the repository measures that
build-time cost, so treat re-adding one as a claim to measure rather than an obvious fix.

If you name a type from one of those — `ConnectionMetrics` to read live counters, say, or
`RequestOptions` to set a per-request timeout — including `srpc.hpp` is not enough and you
must import the module yourself:

```cpp srpc-no-compile
#include "srpc.hpp"
import srpc.connection_metrics;
import srpc.request_options;
```

Finally, a name that used to be here and is gone: there is no `Marshal` class. Binary
serialization is `srpc::Serialize_::serialize` / `Deserialize_::deserialize` over
`BinaryWriteArchive` and `BinaryReadArchive`, which you build from proxies. Chapter 10
covers it.

---

## 3. Fibers (Stackful)

srpc runs almost every request handler on a **fiber**: a stackful execution
context with its own 1 MiB stack, scheduled cooperatively by the thread-local
`Reactor`. Because the stack is real, a fiber can suspend from any depth — deep
inside a nested call, inside a library you did not write — and resume later on
that same stack. That is what lets a handler that *looks* like ordinary blocking
code make a nested RPC call without blocking its thread.

There is a second, stackless lane as well: `rusty::Task`, a C++20 coroutine type
polled by the same reactor, which the IDL's `async` attribute targets. It gets
its own section at the end of this chapter.

Everything here lives in namespace `srpc`. The `Fiber` class and the `Reactor`
come from module `srpc.reactor`; the `this_fiber` helpers come from
`srpc.fiber`. Both are re-exported through the umbrella header, so
`#include "srpc.hpp"` is enough to reach either.

### Why fibers instead of threads

| | Threads | Fibers |
|---|---------|--------|
| Stack | whatever `ulimit -s` says, typically 8 MiB | exactly `kDefaultStackBytes` = 1 MiB, plus one guard page |
| Allocation | pthread stack | one `mmap(MAP_PRIVATE\|MAP_ANONYMOUS)` per fiber, faulted in lazily |
| Switch | kernel scheduler | `fiber_swap_context` — eight register stores and eight loads on x86-64 |
| Synchronization | mutexes and atomics | none needed for state confined to one reactor |
| Parallelism | true, multi-core | concurrent only; one reactor is one thread |

The switch cost is a handful of instructions, but this repository ships no
benchmark, so treat "fibers are cheaper than threads" as a structural claim, not
a measured one. What *is* structural: a fiber's 1 MiB is an anonymous mapping,
so an idle fiber that has touched only a few frames costs a few pages of
resident memory rather than a megabyte.

Fiber reuse blunts the allocation cost further. The library's default flags
include `-DREUSE_FIBER`, and with it a fiber that finishes is pushed onto the
reactor's `available_fibers_` list, stack and all, and handed out again to the
next `create_run` instead of being torn down.

### No locks needed — within one reactor

Fibers belonging to the same reactor **never run simultaneously**. Control
passes from one to another only at an explicit suspension point, so state that
lives entirely inside one reactor thread needs no synchronization:

```cpp srpc-no-compile
// Safe as long as every toucher runs on this reactor's fibers.
class Counter {
    int value = 0;
public:
    void increment() { value++; }
    int get() const { return value; }
};
```

The scoping matters. The reactor is thread-local, so a server with a pool of
poll threads has *several* reactors, each with its own fiber set. State shared
across poll threads is shared across threads in the ordinary sense and still
needs a mutex or an atomic. The fiber discipline buys you freedom from locks
inside a thread, not across the process.

### The fiber API

`Fiber::create_run` takes any callable and returns `rusty::Rc<Fiber>`. The
`this_fiber` namespace holds the operations that address whichever fiber is
currently running:

```cpp srpc-no-compile
#include "srpc.hpp"
using namespace srpc;

auto reactor = Reactor::get_reactor();

auto fiber = Fiber::create_run([]() {
    uint64_t id = this_fiber::get_id();     // this fiber's id; ids start at 0, so
                                            // `id != 0` is NOT a fiber-context test
    (void)id;

    this_fiber::yield();                    // give up the CPU, stay runnable
    this_fiber::sleep_ms(100);              // park on a timeout event

    assert(this_fiber::in_fiber_context());
});
```

The complete surface of `srpc.fiber` is small enough to list:

| Call | Returns | Behaviour |
|---|---|---|
| `this_fiber::get_id()` | `uint64_t` | the running fiber's id, or `0` outside fiber context |
| `this_fiber::current()` | `rusty::Option<rusty::Rc<Fiber>>` | `None` outside fiber context |
| `this_fiber::in_fiber_context()` | `bool` | whether a fiber is installed on this thread |
| `this_fiber::yield()` | `void` | suspends the fiber; a **no-op** outside fiber context |
| `this_fiber::sleep_us(u64)` | `void` | parks on a timeout event |
| `this_fiber::sleep_ms(u64)` | `void` | `sleep_us(ms * 1000)` |
| `this_fiber::sleep_s(u64)` | `void` | `sleep_us(s * 1000000)` |
| `this_fiber::sleep_until_us(u64)` | `void` | absolute monotonic deadline; a past deadline returns immediately |

Two of these are traps in the same place. `yield()` outside a fiber quietly does
nothing, but a **sleep of non-zero duration outside a fiber aborts the process**:
it creates a timeout event and waits on it, and `Event::wait` asserts that a
fiber is running to park. The two exceptions are `sleep_us(0)`, which returns
before creating an event at all, and `sleep_until_us` with a deadline already
past, which does the same. Anything else must run on a fiber.

The fiber object itself also exposes `yield_()`, `continue_()`, `finished()` and
the static `Fiber::current_fiber()` and `Fiber::sleep(us)`. `this_fiber::yield()`
is the polite spelling of

```cpp srpc-no-compile
Fiber::current_fiber().unwrap()->yield_();
```

with the difference that `this_fiber::yield()` tolerates being called outside a
fiber, where `unwrap()` on a `None` would not.

Ids come from a counter that is thread-local in the generated C++, so ids are
unique **per reactor thread**, not process-wide, and a recycled fiber is stamped
with a fresh id when it is handed out again. Do not use an id as a global key.
The counter also starts at **zero** and returns its pre-increment value, so the
first fiber on a thread has id `0` — the very value `get_id()` returns outside
fiber context. `get_id() != 0` is therefore not a fiber-context test;
`in_fiber_context()` is the only correct one.

### `create_run` runs the body immediately

This surprises everyone once. `Fiber::create_run` does not queue anything: it
takes or recycles a fiber, installs it as the running fiber, registers it with
the reactor, and *runs the body on the spot*, right up to the first suspension
point. Only then does it make a single non-blocking reactor pass and return.

```cpp srpc-no-compile
int step = 0;
auto reactor = Reactor::get_reactor();

auto fiber = Fiber::create_run([&step]() {
    step = 1;
    this_fiber::yield();
    step = 2;
});

assert(step == 1);              // already true — the body ran inside create_run

reactor->continue_fiber(fiber); // resume it explicitly
assert(step == 2);
```

Because the current fiber is saved and restored around that call, `create_run`
nests: a fiber may create and run another fiber, and control returns to the
creator when the child suspends or finishes.

### Lifecycle

The state machine is the `FiberStatus` enum:

```
INIT      constructed — nothing has run yet
  |
  v
STARTED   the body is executing on the fiber stack
  |
  |-- yield_() / event wait --> PAUSED
  |                               |
  |                    continue_fiber() / the reactor
  |                    waking the event's fiber
  |                               v
  |                            RESUMED  (executing again)
  |<------------------------------+
  v
FINISHED  the body returned
  |
  |-- REUSE_FIBER build --> RECYCLED  (pushed onto available_fibers_,
  |                                    stack retained, re-armed with a new
  v                                    body and a new id by create_run)
destroyed
```

`FINALIZING` also exists in the enum and is accepted by `yield_`, but nothing in
the canonical reactor drives a fiber into it; treat it as vestigial.
`Fiber::finished()` reports true for both `FINISHED` and `RECYCLED`.

The recycling trick is worth understanding, because it explains why a fiber's
stack survives its body. The engine-level entry point is an infinite loop: it
invokes the body, clears it, marks the fiber `FINISHED`, and yields back to the
caller. When that fiber is later handed a new body and continued, the loop's
next iteration invokes *that* body — on the same stack, from the same
`srpc_fiber`, with no new `mmap`.

### Abandoning a paused fiber

A fiber that has suspended and is never continued simply never resumes. Its
stack is not unwound, so destructors for its locals never run and any resource
it was holding is not released:

```cpp srpc-no-compile
int step = 0;
{
    auto fiber = Fiber::create_run([&step]() {
        step = 1;
        this_fiber::yield();
        step = 2;               // never reached if nobody continues the fiber
    });
    assert(step == 1);
}   // dropping the handle does NOT unwind the fiber

assert(step == 1);
```

Dropping your `rusty::Rc<Fiber>` does not even destroy the fiber: the reactor's
`fibers_` registry holds its own reference until the fiber finishes and is
recycled. The mapping stays alive until the reactor itself is destroyed, which
is exactly the shape of leak the built `fiber_test.cc` suite documents in its
`DriveUntil` helper — a fiber parked on a millisecond timeout, a single
`run_loop(false, true)` pass that returns long before the timeout fires, and a
suspended frame still holding the reactor alive. If you park a fiber on a timer,
drive the reactor until it actually completes.

### Where fibers come from in the RPC path

You rarely call `create_run` yourself on the server side; the dispatcher does it
for you. When a request arrives for an RPC id that was **not** registered on the
fast path, the server connection spawns a fiber for the handler precisely so it
can yield — for a nested RPC call, a timeout, an event. RPC ids registered
through `reg_fast_rpc` (the `fast`, `prefix` and `async` attributes) are
dispatched inline on the poll thread with no fiber at all, which is why a `fast`
handler must never block: there is nothing to yield to, and every connection on
that poll thread stalls behind it.

### Implementation: the C engine and the assembly

The raw-memory half of the fiber runtime is plain C, in
`reactor/srpc_fiber.{h,c}`. It owns the stack mapping, the thread-local
active-fiber slot, and the resume/yield/finish state machine. The C++ side
supplies exactly one callback — `entry_fn(entry_arg)`, invoked on the fiber
stack.

The register bag is architecture-specific, and **its field order is the ABI
contract** with `reactor/fiber_context_{x86_64,aarch64}.S`. Reordering the
struct silently breaks the assembly, which addresses the fields by numeric
offset:

```cpp srpc-no-compile
// reactor/srpc_fiber.h — x86_64 arm of the #if. Do not reorder.
typedef struct srpc_fiber_ctx {
    void*     rsp;   // offset  0
    void*     rip;   // offset  8
    uintptr_t rbx;   // offset 16
    uintptr_t rbp;   // offset 24
    uintptr_t r12;   // offset 32
    uintptr_t r13;   // offset 40
    uintptr_t r14;   // offset 48
    uintptr_t r15;   // offset 56
} srpc_fiber_ctx;

extern "C" void fiber_swap_context(srpc_fiber_ctx* from, srpc_fiber_ctx* to);
```

The AArch64 arm of the same `#if` holds `sp`, `pc`, `x19`–`x28` and `fp`, at
offsets 0 through 96, matching the `stp`/`ldp` pairs in the ARM64 trampoline.
Those two are the only architectures the assembly covers; on anything else the
`.S` files compile to nothing and the link fails on `fiber_swap_context`.

On x86-64, `fiber_swap_context` does not save the caller's actual return address.
It stores the address of a local label (`.Lfiber_resume`) whose entire body is
`ret`, so resuming a saved context lands on that `ret` and returns to whoever
called `fiber_swap_context` the first time. The AArch64 arm needs no such trick:
it stores the real link register (`str x30, [x0, #8]`) and restores it straight
back into `x30` before its own `ret`.

Stack setup, in `srpc_fiber_init`:

- `mmap` `stack_bytes + one page`, anonymous and private. Aborts on failure.
- `mprotect` the *first* page to `PROT_NONE`. The stack grows down, so that page
  is the guard: an overflow faults instead of scribbling on the neighbouring
  mapping.
- Align the top of the mapping down to 16 bytes; on x86-64 push a null return
  address so `%rsp % 16 == 8` at entry, as the SysV ABI expects.
- Seed `rip`/`pc` with the entry trampoline, so the first `srpc_fiber_resume`
  enters it.

The trampoline sets the state to `RUNNING`, calls the entry callback, sets
`FINISHED`, and swaps back to the caller — then `abort()`s, because a finished
fiber must never be resumed past that point. `srpc_fiber_resume` is a no-op on a
finished fiber, and `srpc_fiber_yield` aborts if called on a fiber that is not
running. Invariant violations in this layer abort; they are not exceptions.

The only caller of `srpc_fiber_init` passes `kDefaultStackBytes`, which is
`1 << 20`, a compile-time constant. The C function takes a size, but there is no
public way to ask for a different one — the stack size is fixed at 1 MiB in
practice.

`Fiber` itself has deleted move operations (spelled in the canonical Rust as a
`PhantomPinned` field). It hands its own address to the C engine, so a move
would leave the engine pointing at the old object.

The same C translation unit answers two questions that canonical Rust cannot ask
because they are C preprocessor facts: `srpc_reactor_gettid()` returns
`syscall(SYS_gettid)` using the target's own syscall number, and
`srpc_reactor_reusing_fiber()` reports whether the library was compiled with
`REUSE_FIBER` or `REUSE_CORO`. Both are deliberately *not* Rust constants, which
would freeze one architecture and one build configuration into portable source.

### The other lane: stackless `rusty::Task`

The reactor drives stackless C++20 coroutines as well. `Reactor::run_loop` calls
`process_stackless_tasks()` on every pass, alongside the event queues, so both
lanes make progress on the same thread.

A task is spawned with

```cpp srpc-no-compile
srpc::reactor_spawn_stackless_task_with_result(
    *srpc::Reactor::get_reactor(),
    std::move(task),                    // rusty::Task<T>
    [](auto value) { /* completion */ });
```

which polls the task once inline — if it completes immediately the callback runs
right there and nothing is registered — and otherwise parks it in the reactor's
poller table with a `rusty::Waker` that re-queues it. Spawning must happen on
the reactor's own thread; a spawn refused during reactor teardown destroys the
task and its callback rather than pretending to succeed, so a waiter fails
instead of hanging.

This is the machinery behind the IDL's `async` attribute. An `async` method's
handler signature is

```cpp srpc-no-compile
virtual rusty::Task<rusty::Result<RpcMethodResponse, srpc::i32>>
method(const RpcMethodRequest& req);
```

and the generated dispatcher registers it on the fast path, calls it inline on
the poll thread, and hands the returned task to
`reactor_spawn_stackless_task_with_result` with a completion lambda that upgrades
the weak server connection and replies. Finish such a handler with `co_return`:

```cpp srpc-no-compile
rusty::Task<rusty::Result<BenchmarkService::RpcAsyncNopResponse, i32>>
BenchmarkService::async_nop(const RpcAsyncNopRequest& req) {
    (void)req;
    co_return rusty::Result<RpcAsyncNopResponse, i32>::Ok(RpcAsyncNopResponse{});
}
```

Two consequences follow from "entered inline on the poll thread". First, an
`async` handler must not block *before* its first suspension point, exactly like
a `fast` handler. Second, srpc ships no ready-made awaitable for its own events:
`co_await`ing anything means writing an awaiter that reaches
`rusty::current_context()` and copies the `Waker` out of it, then wakes that
copy when the result is ready. Copy the waker; never alias the reactor's
`Context`, which may be retired the moment the task completes.

Choose the stackful lane when the code reads better as straight-line blocking
logic, or when the thing you need to suspend inside is a call you do not
control — only a real stack can suspend from arbitrary depth. Choose the
stackless lane when you can express the flow as a coroutine and want to avoid a
1 MiB mapping per in-flight request.

### Observability

The reactor keeps public counters you can read directly while debugging:
`n_created_fibers_`, `n_busy_fibers_`, `n_idle_fibers_` and `n_active_fibers_`,
each a `Cell<i64>`. The reactor also logs a line every 1024 fibers created, and
warns at reactor construction when the library was built *without* fiber reuse.

---

## 4. The Reactor Pattern

srpc runs on two cooperating loops, and most of the confusion about the runtime comes
from treating them as one.

The **`Reactor`** is a fiber and event scheduler. It owns the fibers, events and
stackless tasks belonging to a single thread, and it never touches a file descriptor.
The **`PollThreadWorker`** is the epoll loop: it owns the poll set, dispatches readiness
to connections, and calls into that thread's reactor once per pass so the fibers those
callbacks unblocked actually get to run. **`PollThread`** is the handle you hold onto
for the worker thread — a mailbox, not the worker itself.

Both live in `reactor/reactor.rs`, exported from module `srpc.reactor`, which the
umbrella `srpc.hpp` imports for you.

### The Reactor

`Reactor::get_reactor()` returns the calling thread's reactor as `rusty::Rc<Reactor>`,
creating it on first use and stamping the creating thread's id into it. Its state is
four event queues — `all_events_`, `waiting_events_`, `timeout_events_`,
`composite_events_` — a registry of live fibers with a free list of recycled ones, and a
slab of stackless `rusty::Task` pollers.

Thread affinity is checked, not merely documented. `run_loop`, `process_stackless_tasks`,
`enqueue_stackless_task`, both stackless-task spawn paths and the destructor each begin
with a comparison against the recorded owner id, routed through `srpc::verify`, which
prints a stack trace and panics on failure. Getting this wrong aborts loudly instead of
corrupting the queues.

Two things you will notice the first time a thread calls `get_reactor()`. It logs
`create a fiber scheduler` at DEBUG, and unless the build defines `REUSE_FIBER` it also
logs `reusing fiber not enabled!` at WARN. Fiber recycling is a compile-time constant
read through a plain-C seam (`srpc_reactor_reusing_fiber`), not a runtime switch, so
that warning is a build property and not something you can fix at run time.

There is a second, entirely separate reactor per thread: `Reactor::get_disk_reactor()`
("create a disk fiber scheduler" in the log). It is an ordinary `Reactor` with its own
queues and its own owner check. Nothing under `rpc/` uses it — the RPC path only ever
touches the main one.

### Running the loop

```cpp srpc-no-compile
auto reactor = Reactor::get_reactor();

reactor->create_run_fiber([]() {
    this_fiber::yield();
});

reactor->run_loop(false, true);   // drain whatever is ready, then return
```

The signature is `run_loop(bool infinite, bool do_check_timeout)`. (The name is
`run_loop`, and it always takes both booleans.) A single pass does this, in this order:

1. **Stackless tasks.** Wakeups that arrived on the private owner-thread ingress are
   enqueued, then every ready `rusty::Task` poller is polled; a poller that reports
   ready has its slot closed and released.
2. **`waiting_events_`.** Every event's `test()` is called, then the ones that turned
   `READY` are moved to a local ready list and the `DONE` ones are dropped from the queue.
3. **`composite_events_`.** The same two steps for `WaitAll` / `WaitAny` / `QuorumEvent`.
4. **Timeouts**, only when `do_check_timeout` is true. `check_timeout` walks
   `timeout_events_`, and for each event whose wakeup time has passed marks it `READY` if
   its condition happens to hold and `TIMEOUT` otherwise, then moves both kinds onto the
   ready list.
5. **Dispatch.** For each collected event that is not already `DONE`, upgrade the weak
   handle to the waiting fiber; if that fiber is gone, or is no longer in the registry,
   skip it. Otherwise assert the fiber is `PAUSED`, flip a `READY` event to `DONE` —
   a `TIMEOUT` event deliberately keeps its status, which is how a waiter can tell
   afterwards that it timed out — and resume the fiber via `continue_fiber`.

The pass repeats as long as it found work. With `infinite == false` it returns once a
full pass produces nothing. That is the form the runtime itself uses — both of srpc's own
calls, the one at the end of `create_run_fiber` and the one at the end of every poll-worker
pass, pass `false` — and the one you want in tests: "run everything that is currently
runnable, then give me control back."

`infinite == true` keeps looping until something clears the reactor's public `looping_`
flag, which `run_loop` seeds from `infinite` on entry and re-reads at the end of every
outer pass. A fiber running on that reactor ends the loop with
`Reactor::get_reactor()->looping_.set(false);`. Nothing else clears it, and the loop does
not block anywhere — waiting on a descriptor is the poll thread's job — so until then it
is a spin, not a sleep. Use it only as the last statement of a thread whose whole purpose
is to drive a reactor.

Note what is *not* in that list: there is no `epoll_wait`, no descriptor handling, and no
command channel. The reactor is pure scheduling.

Event creation touches the reactor too. Every `create_sp_*` factory registers the new
event in `all_events_` and then calls `prune_finished_events()`, which compacts the
queue once it exceeds a high-water mark (64 to start, then twice the surviving length
plus 64). So the sweep is amortized over event creation rather than run per pass.

### `create_run_fiber` runs the body immediately

`create_run_fiber` is not a "queue it for later" call. It takes a fiber from the free
list or makes a new one, saves the currently running fiber, registers the new one, and
**runs it on the calling thread right there** — to its first yield, or to completion.
If it finished, the fiber is recycled. Only then does it call `run_loop(false, true)`,
and finally restore the fiber that was running before.

Two consequences worth internalizing: a fiber body with no yield point runs
synchronously inside `create_run_fiber`, and calling `create_run_fiber` from inside a
fiber nests correctly (the outer fiber is saved and restored around the inner one).

### PollThread and PollThreadWorker

```cpp srpc-no-compile
auto poll = PollThread::create();   // rusty::Arc<PollThread>; spawns the thread
// ... build a Server or a Client on it ...
poll->shutdown();                   // sends Shutdown, then joins
```

`PollThread::create()` spawns the worker thread and returns an `rusty::Arc<PollThread>`.
The `PollThread` object itself holds almost nothing: the sending half of an mpsc channel,
the join handle, the worker's thread id as raw bits, and a `shutdown_called_` flag. The
`PollThreadWorker` — the epoll descriptor, the fd-to-pollable map, the mode map, the
deferred-removal set and the job set — is constructed *on the poll thread* and never
leaves it.

Every mutator on the handle is therefore a message, not a call. Chapter 6 owns that
material — the command table, the worker's ten-step pass and its shutdown protocol. Two
steps of the pass matter here. `epoll_wait` runs with a **1 ms timeout**, so the poll
thread turns at least a thousand times a second whether or not there is traffic; timers
and the fiber scheduler make progress on their own, and nothing has to poke the loop to
get a timeout delivered. And every pass ends with
`Reactor::get_reactor()->run_loop(false, true)` on the poll thread's own reactor —
**that is where server handler fibers actually run.**

`PollMode` and `PollReady` live in module `srpc.epoll_wrapper`, which is trimmed from the
consumer umbrella; name them and you need an explicit `import srpc.epoll_wrapper;`.

### Jobs

`PollThread::add(rusty::Arc<Job>)` hands the worker a `Job` — `Ready()`, `Work()`,
`Done()`. Three times per pass the worker asks each job whether it is `Ready()`; the
first time one says yes, the worker spawns a fiber to run its `Work()` and **drops the
job from the set**. So a job fires at most once and is not a recurring timer, and
`Done()` is never consulted by the worker at all. `PollCommand::RemoveJob` exists in the
enum and is handled by the worker, but no `PollThread` method sends it. Internally srpc
uses jobs for deferred connection close and for the client's receive pump.

### Thread-local design

Each thread gets its own reactor because the storage behind `get_reactor()` is
per-thread. Be precise about where that is true: in the **generated C++** those are
namespace-scope `thread_local` variables, and the nine `#[cfg_attr(any(), thread_local)]`
markers in `reactor/reactor.rs` exist to carry that contract through the transpiler.
Under plain `rustc` the condition `any()` is false, the markers vanish, and what is left
is ordinary mutable globals — which is one of the reasons that file is explicitly not
meant to be executed as Rust, and why native C++ TLS and multithread tests remain the
gate for this module.

What is real in both worlds is the owner check, and that is what catches misuse:

```cpp srpc-no-compile
// WRONG - the reactor belongs to the thread that created it
auto reactor = Reactor::get_reactor();
std::thread t([reactor]() {
    // This panics before the body ever runs: create_run_fiber calls
    // Fiber::run(), whose first assertion counts the live fibers of
    // THIS thread's own (empty) reactor, not of the captured one.
    // The rusty::Rc copy is not atomic either.
    reactor->create_run_fiber([]() { /* ... */ });
});

// RIGHT - ask for the reactor on the thread that will use it
std::thread t2([]() {
    auto reactor = Reactor::get_reactor();   // this thread's own
    reactor->create_run_fiber([]() { /* ... */ });
    reactor->run_loop(false, true);
});
```

The same rule covers the objects the reactor hands out: fibers are `rusty::Rc`, and
events are owned by the reactor that created them. Do not move either across a thread
boundary. When you use the RPC framework you rarely construct any of this yourself —
`Server` and `Client` take a `PollThread`, and everything above happens on it.

Events themselves are chapter 5; the pollables the worker dispatches to are chapter 6.

---

## 5. Event System

Events are how a fiber waits for something. Calling `wait()` on an event suspends the
*calling fiber* and hands control back to the reactor loop (Chapter 4); the OS thread
keeps running other fibers. When the event becomes ready the loop resumes the fiber
exactly where it yielded.

Three things to know before the vocabulary below, because none of these names is the one
you would guess:

* **There is no class called `Event`.** The polymorphic base is `EventPollable` — a Rust
  trait in `reactor/reactor.rs` that the emitter lowers to an abstract C++ class. Every
  concrete event inherits it.
* **There is no `Reactor::create_sp_event<T>()` template.** Each event type has its own
  named free function, and those are the only constructors.
* **Methods are lowercase**: `wait`, `wait_timeout`, `test`, `set`, `get`, `is_ready`,
  `prunable`, `set_prunable`.

There is also no `WaitN` and no `NEvent`. The N-of-M primitive is `QuorumEvent`.

### The event vocabulary

| factory | type | ready when |
| --- | --- | --- |
| `create_sp_int_event(target)` | `IntEvent` | `value_ >= target_` |
| `create_sp_timeout_event(wait_us)` | `TimeoutEvent` | the deadline fixed at construction has passed |
| `create_sp_never_event()` | `NeverEvent` | never |
| `create_sp_box_event<T>()` | `BoxEvent<T>` | a `T` has been stored in the slot |
| `create_sp_waitany(a, b)` | `WaitAny` | *either* of exactly two children is ready |
| `create_sp_waitall()`, `create_sp_waitall_from(vec)` | `WaitAll` | every child is ready or already `DONE` |
| `janus::create_sp_quorum_event(n_total, quorum)` | `QuorumEvent` | a quorum of yes votes, or enough no votes |

Every one of these returns a `rusty::Arc<T>` of the *concrete* type — you keep the typed
handle, and it converts to `rusty::Arc<EventPollable>` where a base handle is wanted. The
factory also stamps the event with the current thread id, records the creating fiber, and
files the event in the calling thread's reactor registry. `Reactor::get_reactor()` creates
that reactor on demand, so the factory never fails for lack of one — but an event created
on a thread whose reactor is never run can never be woken, and `wait()` *does* abort unless
the calling thread has a reactor whose thread id matches. Create events on the thread that
will run the loop.

`SharedIntEvent` is the one member of the family that is *not* built this way; it is
described at the end of the chapter.

### EventStatus

```cpp srpc-no-compile
EventStatus::INIT     // 0
EventStatus::WAIT     // 1
EventStatus::READY    // 2
EventStatus::DONE     // 3
EventStatus::TIMEOUT  // 4
EventStatus::DEBUG    // 5
```

Those are the six members, in that order, with those values; the enum is `i32`-wide.
`DEBUG` is declared and never assigned — the only place it appears in the runtime is an
assertion that the status *isn't* `DEBUG`. Read the current status off any event with
`ev->status_.get()`.

The lifecycle is worth knowing, because half the questions about "my fiber didn't wake up"
are answered by it:

* An event is born `INIT`.
* `wait()` first re-checks `is_ready()`. If the condition already holds, the status goes
  straight to `DONE` and **the fiber never suspends** — no reactor round trip at all.
* Otherwise the event is filed in the reactor's `waiting_events_` queue (plus
  `composite_events_` if it is a `WaitAll`/`WaitAny`/`QuorumEvent`, plus `timeout_events_`
  if a non-zero timeout was given), the status becomes `WAIT`, and the fiber yields.
* `test()` is what re-evaluates the condition. `IntEvent::set()` calls it inline, and the
  reactor loop calls it on everything in the waiting and composite queues. A ready event
  in `WAIT` becomes `READY`.
* The loop moves `READY` events out of the queues, flips them to `DONE`, and resumes the
  waiting fiber.
* A timed-out event goes `WAIT` → `TIMEOUT` and its fiber is resumed too — with the status
  left at `TIMEOUT`, which is how the fiber tells the two outcomes apart.

Note the asymmetry: after a successful wait the status the fiber observes is `DONE`, not
`READY`. Test against `DONE` and `TIMEOUT`.

### IntEvent

`IntEvent` is the workhorse: a counter with a threshold. The target is a required
constructor argument, the value starts at zero, and the ready predicate is
`value_ >= target_` — greater-or-equal, not equality, so overshooting still fires.

```cpp srpc-no-compile
auto reactor = Reactor::get_reactor();
auto ev = create_sp_int_event(1);        // ready once value_ >= 1

reactor->create_run_fiber([ev]() {
    ev->wait();                          // suspends this fiber
    // resumed: ev->status_.get() == EventStatus::DONE
});

ev->set(1);                              // tests inline; returns the PREVIOUS value
reactor->run_loop(false, true);          // the loop resumes the fiber
```

`set()` returns the value the event held before the write, which is occasionally useful
for detecting a double-signal. `get()` reads the current value; `value_` and `target_`
are also public `Cell` fields (`ev->value_.get()`, `ev->target_.set(n)`) if you need to
retarget an event after construction.

The RPC layer uses exactly this shape. `FiberChannel` (in `rpc/fiber_channel.rs`) turns
callback-style frame delivery into a fiber-blocking `recv_frame()` by arming a *fresh*
`IntEvent` before each wait, having the inbound callback `set(1)` it, and waiting on it:

```cpp srpc-no-compile
// The arm / signal / wait shape, as the fiber channel uses it.
auto pending = create_sp_int_event(1);   // arm before you can block
// ... producer side, when a frame lands:
pending->set(1);
// ... consumer fiber:
pending->wait();
```

A new event per wait is the point, not an inefficiency — see the reuse rule below.

### TimeoutEvent

A `TimeoutEvent` bakes its deadline in at construction: `create_sp_timeout_event(wait_us)`
records `Time::now(true) + wait_us` (all durations in this subsystem are microseconds).
It exposes `wait()` only — the duration is already in the object, so there is no
`wait_timeout` overload on this type.

```cpp srpc-no-compile
auto t = create_sp_timeout_event(1000000);   // one second, in microseconds
t->wait();
// one second has elapsed (assuming the loop was run with do_check_timeout = true)
```

`Fiber::sleep(microseconds)` is implemented as precisely this — create a timeout event,
wait on it — with a zero duration short-circuiting to a no-op. Prefer `Fiber::sleep` for a
plain delay and keep `TimeoutEvent` for cases where you want the deadline as a first-class
object you can hand to `create_sp_waitany`.

### NeverEvent

`create_sp_never_event()` builds an event whose `is_ready()` is unconditionally `false`.
It exposes `wait_timeout(us)` and no plain `wait()`, for the obvious reason. Use it as a
park: a fiber that should sleep for a bounded time but be woken by nothing except its own
deadline, or a placeholder in a composite where one branch must never fire.

### BoxEvent&lt;T&gt; — a typed one-shot slot

`create_sp_box_event<T>()` is an event that carries a payload. `set(const T&)` stores a
value and marks the slot full (which also runs `test()`); `get()` copies the value out;
`clear()` empties it again; `is_ready()` reports whether the slot is full.

```cpp srpc-no-compile
auto slot = create_sp_box_event<int>();

reactor->create_run_fiber([slot]() {
    slot->wait();
    int v = slot->get();
    (void)v;
});

int payload = 7;
slot->set(payload);
reactor->run_loop(false, true);
```

`T` must be default-constructible and copyable — `clear()` default-constructs and `get()`
copies.

This is not a curiosity: `BoxEvent<T>` is the engine underneath `FiberPromise<T>` and
`FiberFuture<T>` in the `srpc.future` module (`reactor/future.rs`). The promise owns the
box, `set_value` writes it, and `FiberFuture::get()` waits on the box and copies the value
out. `FiberFuture::wait_for(timeout_us)` forwards to `wait_timeout` and treats a zero
timeout as "wait indefinitely", matching the plain `wait()`. Do not confuse
`FiberFuture<T>` with the RPC-level `srpc::Future`, which is a different type in a
different module with its own (much less forgiving) timeout behaviour.

### WaitAny — either of exactly two

```cpp srpc-no-compile
auto e1 = create_sp_int_event(1);
auto e2 = create_sp_int_event(1);
auto any = create_sp_waitany(e1, e2);

reactor->create_run_fiber([any]() {
    any->wait();     // returns as soon as EITHER child is ready
});
e2->set(1);
reactor->run_loop(false, true);
```

The arity is fixed: `create_sp_waitany` takes exactly two `rusty::Arc<EventPollable>`
arguments and there is no `add_event` on `WaitAny`. For three or more, nest — or, if what
you actually want is "the first of these, or a deadline", pair the real event with a
`TimeoutEvent`, which is the common use.

If a child is already ready when the `WaitAny` is created, the wait returns immediately
without suspending.

### WaitAll — all of them

`WaitAll` is the composite that grows. Either build it empty and add children, or build it
from a vector:

```cpp srpc-no-compile
auto event1 = create_sp_int_event(1);
auto event2 = create_sp_int_event(1);

rusty::Vec<rusty::Arc<EventPollable>> events = {event1, event2};
auto and_event = create_sp_waitall_from(events);

reactor->create_run_fiber([and_event]() {
    and_event->wait();
});

event1->set(1);
reactor->run_loop(false, true);   // still waiting: only one child is ready
event2->set(1);
reactor->run_loop(false, true);   // now the fiber resumes
```

The incremental form is `auto all = create_sp_waitall();` followed by
`all->add_event(child);` for each child, where `child` is a `rusty::Arc<EventPollable>`.

`WaitAll::is_ready()` accepts a child that is *either* currently ready *or* already
`DONE`. That matters: a child that was individually waited on and consumed earlier still
counts as satisfied, so a `WaitAll` over already-finished events fires immediately rather
than hanging.

### QuorumEvent — the N-of-M primitive

`QuorumEvent` is the only N-of-M event in the tree. It counts yes and no votes against a
total and a threshold, and becomes ready as soon as the outcome is decided in either
direction.

Its C++ spelling is `janus::QuorumEvent`, in the **global** `::janus` namespace, not
`srpc::`. That placement is an ABI contract carried by an inert
`#[cfg_attr(any(), cpp_namespace(::janus))]` marker on each of the three types and five
free functions in the quorum surface; `srpc::QuorumEvent` and `srpc::janus::QuorumEvent`
mangle differently and are not substitutes. Everything else in this chapter is plain
`srpc::`.

```cpp srpc-no-compile
auto q = janus::create_sp_quorum_event(3, 2);   // 3 replicas, 2 needed

q->vote_yes();
// q->is_ready() is still false
q->vote_yes();
// q->is_ready() is true, and q->yes() is true
```

`vote_yes()` / `vote_no()` bump the counters, run `test()`, and also tick an internal
`IntEvent` that counts *all* replies, which is what `finalize()` waits on.

* `yes()` is `n_voted_yes_ >= quorum_`.
* `no()` is `n_voted_no_ > n_total_ - quorum_` under the default policy — i.e. enough
  refusals that a yes-quorum is now impossible.
* `is_ready()` is `timeouted_ || yes() || no()` under the default policy — setting the
  public `timeouted_` cell makes the event ready on its own — plus the policy adjustments
  below. Only `ALL_NO` ignores `timeouted_`.

`QuorumPolicy` has four members: `DEFAULT = 0`, `ALL_NO = 1`, `COMMITTED_SHORT = 3`, and
`ALWAYS_READY = 4` (there is no `2`). Under `ALL_NO`, `no()` requires *every* participant
to have refused. Under `COMMITTED_SHORT`, a set `committed_seen_` flag short-circuits to
ready. Under `ALWAYS_READY`, `is_ready()` is unconditionally true.

The policy lives in a public `Cell` field, as do `committed_seen_`, `timeouted_`,
`highest_term_`, `leader_id_`, `par_id_` and `id_`. srpc initialises them and then never
writes them again — they are knobs for the caller to set directly. Two of them the reactor
reads back: `timeouted_` and `committed_seen_` are consulted by `is_ready()` above, so
setting either one changes when the event fires. The rest carry meaning only for the
caller's protocol.

Two more pieces of the surface:

* `add_xid(site, xid)` / `remove_xid(site)` maintain a map of outstanding request ids per
  site. `finalize(timeout_us, fn)` spawns a fiber that snapshots that map *first*, then
  waits up to `timeout_us` for *all* replies, and — only if that wait times out — calls
  `fn` with that snapshot so the caller can cancel the requests. The order is deliberate:
  the quorum event may already have been freed by the time the wait returns, so the map
  cannot be re-read afterwards. What `fn` receives is therefore the set outstanding at
  `finalize()` time, **not** the set still outstanding at the deadline — a site that
  replied and was `remove_xid`'d in between is still in the list, so the caller must
  tolerate cancelling a request that already finished.
* `is_slow()` reads and clears the reactor's global `slow_` flag. It ignores the event it
  is called on entirely; the flag is per-reactor, not per-quorum.

`janus::QuorumEventWrapper` is a thin by-value handle, built from the same
`n_total` / `quorum` pair, that owns an `Arc<QuorumEvent>` and forwards `wait`, `wait_timeout`, `vote_yes`, `vote_no`, `yes`,
`no`, `is_ready`, `is_slow`, `test`, `add_xid`, `remove_xid`, `finalize`, `log` and
`get_fiber_id`, with `q()` returning the underlying event by reference.

### SharedIntEvent — many waiters on one counter

`SharedIntEvent` breaks the "one waiter per event" rule from the outside by not being a
single event at all. It is a plain struct — a current value plus a vector of per-waiter
`IntEvent`s — and it is constructed directly, not through a `create_sp_*` factory.

```cpp srpc-no-compile
// `counter` is a plain SharedIntEvent value — a member of your own type, say.
// There is no create_sp_* factory for it.

// fiber A — returns true if it gave up before the value arrived
bool timed_out = counter.wait_until_gte(5, 100000);   // want >= 5, 100 ms budget

// fiber B
counter.set(5);                     // wakes every waiter whose target is satisfied
```

`set(v)` overwrites the value, returns the previous one, and signals every parked waiter
whose target is now met. `wait_until_gte(x, timeout_us)` returns immediately (with
`false`) if the value already satisfies `x`; otherwise it mints a fresh `IntEvent` seeded
with the current value, waits with the timeout, unregisters itself, and returns whether it
timed out. Note the timeout here is a 32-bit microsecond count, unlike the `u64` timeouts
everywhere else in the chapter. `wait(pred)` is the general form: it installs a predicate
over the current value on a fresh event, which is the supported way to get a condition
other than "value reached target".

Note the methods are non-const: a `SharedIntEvent` you intend to signal cannot be held
behind a const reference.

### Composite events need the loop to poll them

Ordinary events are self-notifying — `set()` runs `test()` inline. Composites are not:
nothing about `e1->set(1)` tells the enclosing `WaitAll` to re-evaluate. That is why
composite events are filed in a second queue and re-tested by the reactor on every pass,
and why a composite only makes progress while the loop is running.

Timeouts have the same dependency, plus one more: they are only checked when the loop is
asked to check them.

```cpp srpc-no-compile
reactor->run_loop(false, true);   // (infinite, do_check_timeout)
```

With `do_check_timeout = false` a `wait_timeout` deadline is never examined and the fiber
waits forever. The runtime test suites all pass `true`. `run_loop(false, ...)` drains
until no further progress is possible and returns; `run_loop(true, ...)` keeps spinning
until a fiber clears the reactor's `looping_` flag (Chapter 4).

### Rules and gotchas

**One waiter per event.** The event stores a single weak fiber handle, and a second
`wait()` from a different fiber overwrites it — the first fiber is then never resumed.
The runtime says so in as many words at the point where it captures the fiber. If you need
several fibers to wait on one condition, give each its own event: that is exactly what
`SharedIntEvent` and `FiberChannel` do.

**Waiting twice on a finished event returns immediately.** `wait()` on an event already in
`DONE` is an explicit early return, not a block. Conversely an event whose condition
*stops* holding is quietly rearmed: `test()` moves a `DONE` event back to `INIT` when
`is_ready()` goes false again. Neither behaviour is a substitute for a fresh event, and a
fresh event is the idiom.

**Wait from inside a fiber, on the owning thread.** `wait()` aborts unless there is a
reactor on the current thread, that reactor's thread id matches, and — in the case where
the event is not already ready — a fiber is running. These are `verify()` calls, which
panic in every build; they are not `NDEBUG`-gated asserts. Events capture their owner
thread at construction and are `Cell`/`RefCell`-based throughout: treat an event as
belonging to one reactor thread and never share one across threads.

**Events stay registered until they are pruned.** Every factory files the event in the
reactor's `all_events_` registry, and `prune_finished_events()` — run on each creation,
past a growing high-water mark — drops the ones nobody else holds a reference to. If you
want an event to survive that with no outstanding handle, call `set_prunable(false)`.

**A zero timeout means "forever".** `wait_timeout(0)` registers no deadline and behaves
like `wait()`.

**`IntEvent` fires on `>=`, not `==`.** Setting a value past the target still triggers it.

---

## 6. I/O Layer: Polling and Connections

Every socket in srpc is owned by exactly one poll thread. That thread runs a Linux
`epoll` loop, dispatches readiness to registered *pollables*, drains a command
channel that other threads write to, and drives the reactor's fibers and stackless
tasks between passes. Nothing else touches the epoll set, and nothing else owns a
registered pollable — which is why the whole layer has no locks around its
descriptor tables.

The layer is four files:

| File | Role |
|------|------|
| `reactor/epoll_wrapper.rs` | `PollMode` / `PollReady` constants, the `Pollable` trait, the `Epoll` RAII wrapper |
| `reactor/epoll_platform_linux.cc` | the Linux syscall bodies (`epoll_open`, `add`, `remove`, `update`) |
| `rpc/pollable_proxy.rs` | `PollableBase`, `PollableProxy`, the `Arc`-backed shim |
| `reactor/reactor.rs` | `PollThread`, `PollThreadWorker`, `PollCommand`, the poll loop, and job *scheduling* (the `Job` trait and `OneTimeJob` themselves live in `base/misc.rs`, module `srpc.misc`) |

`srpc.pollable_proxy` and `srpc.reactor` are both re-exported by the `srpc.hpp`
umbrella. `srpc.epoll_wrapper` is **not** — it is one of the eight modules the
umbrella trims. Naming `Epoll`, `PollMode`, `PollReady`, or `Pollable` from C++
means importing it yourself:

```cpp srpc-no-compile
#include "srpc.hpp"
import srpc.epoll_wrapper;   // trimmed from the umbrella; name it directly
```

### Linux only

There is no kqueue path and no macOS support. `reactor/epoll_platform_linux.cc` is
the *only* platform implementation unit in the tree, and `CMakeLists.txt` adds it
unconditionally rather than selecting among several. The `old_mode` parameter that
survives on `epoll_update_impl` is the last trace of a kqueue twin that was removed:
`EPOLL_CTL_MOD` replaces the whole interest set, so Linux ignores it.

Do not treat the layer as portable. It includes `<sys/epoll.h>` directly and calls
`epoll_create` / `epoll_ctl` / `epoll_wait` with no abstraction underneath.

### Poll modes and readiness bits

Both sets live in `reactor/epoll_wrapper.rs` as plain `i32` constants inside
namespace-shaped modules:

```cpp srpc-no-compile
PollMode::READ       // 0x1  — interest in readability
PollMode::WRITE      // 0x2  — interest in writability
PollMode::NO_CHANGE  // -1   — handle_write()'s "leave my interest alone"

PollReady::READABLE  // 0x1
PollReady::WRITABLE  // 0x2
PollReady::ERROR     // 0x4  — EPOLLERR | EPOLLHUP | EPOLLRDHUP, collapsed
```

`PollMode` values are what you *ask for*; `PollReady` values are what the kernel
*reports*. They happen to share the low two bit positions, but they are separate
vocabularies and the code never mixes them.

### The Epoll wrapper

`Epoll` is a move-only RAII owner of the poll descriptor. Its constructor allocates
eagerly — `epoll_create(10)`, whose size hint Linux has ignored since 2.6.8 but
still requires to be positive — and aborts through `verify` if the call fails. Note
the capitalized method names; they are the historical C++ spelling and the Rust keeps
them verbatim:

```cpp srpc-no-compile
class Epoll {
    Epoll();                                            // allocates the poll fd
    int32_t fd() const;                                 // the poll fd itself

    int32_t Add(int32_t fd, int32_t poll_mode);         // register
    int32_t Remove(int32_t fd);                         // unregister
    int32_t Update(int32_t fd, int32_t new_mode, int32_t old_mode);

    template <class F> void Wait(F on_ready);           // on_ready(int fd, int ready_events)
};
```

`Wait` is one poll pass, not a loop: a fixed 100-entry event array and a **1 ms**
timeout, hard-coded. It translates `EPOLLIN` to `READABLE`, `EPOLLOUT` to `WRITABLE`,
and any of `EPOLLERR | EPOLLHUP | EPOLLRDHUP` to `ERROR`, then calls `on_ready` once
per event that carried at least one of those bits. A failed `epoll_wait` returns a
negative count and produces zero callbacks — the loop counter is deliberately signed
so that falls out for free.

Registration is **edge-triggered**. `Add` sets `EPOLLET | EPOLLIN | EPOLLRDHUP`, plus
`EPOLLOUT` when the requested mode has `WRITE`. There is an asymmetry worth knowing:
`Add` registers `EPOLLIN` unconditionally regardless of whether `PollMode::READ` was
requested, while `Update` starts from `EPOLLET | EPOLLRDHUP` and adds `EPOLLIN` and
`EPOLLOUT` only for the bits actually set in `new_mode`. A pollable can therefore lose
read interest through `Update` that it could never have avoided at `Add` time.

Because it is edge-triggered, a `handle_read` that stops before the socket returns
`EAGAIN` will simply not be woken again. `TcpConnection::handle_read` drains in a loop
until a short read for exactly this reason.

The kernel's event payload is the **file descriptor**, not a pointer: `Add` and
`Update` both write `ev.data.fd = fd`, and the wait path reads `events[i].fd` back.
Nothing in this layer stores a `Pollable*` as epoll userdata, so a pollable freed
between an event and its dispatch cannot be dereferenced through a stale event.

Three errno cases are tolerated rather than fatal, all of them teardown races:

- `EPOLL_CTL_ADD` returning `EEXIST` — a stale registration for a reused descriptor.
  The code issues a `DEL` and retries the `ADD` once.
- `EPOLL_CTL_ADD` returning `EBADF` — the fd was closed between the registration
  request being queued and this syscall. `Add` returns `-1` and the caller drops the
  pollable.
- `EPOLL_CTL_MOD` returning `ENOENT` or `EBADF` — the fd was closed or removed
  concurrently. Treated as success.

Anything else trips `verify(result == 0)` and aborts. `Remove` is the exception: it
discards `epoll_ctl`'s return entirely and always reports `0`, on the grounds that a
closed descriptor is already out of the epoll set.

### The one hand-written C++ unit

`reactor/epoll_platform_linux.cc` is the only C++ translation unit in the repository
that a human maintains, and it is the only carrier of the inline-Rust DSL. Its four
entry points — `epoll_open`, `epoll_add_impl`, `epoll_remove_impl`,
`epoll_update_impl` — are declared in `epoll_wrapper.rs` as
`unsafe extern "Rust"` and defined here; a fifth block is the small
`epoll_event_zeroed()` factory the three `epoll_ctl` bodies build on, which
replaced a plain-C kernel. (`epoll_open` needs no event struct: it is
`epoll_create(10)` plus a `verify`.)

The file's structure matters if you ever edit it:

```
#if RUSTYCPP_RUST
fn epoll_open() -> i32 { ... }        // ← this is the source
#endif
/*RUSTYCPP:GEN-BEGIN id=... rust_sha256=...*/
int32_t epoll_open() { ... }          // ← this is generated; never hand-edit
/*RUSTYCPP:GEN-END id=...*/
```

Edit the Rust inside `#if RUSTYCPP_RUST` and regenerate with
`rusty-cpp-transpiler inline-rust`. `scripts/srpc_dsl_check.sh` hard-codes the census
— exactly this file, exactly five blocks — and also scans `*.rs`, so introducing a
DSL block anywhere under `base/ misc/ reactor/ rpc/` fails the check until the script
is updated too.

### Pollable, PollableBase, and the proxy

There are two interfaces here, and the distinction is the thing to get right.

`Pollable` lives in `srpc.epoll_wrapper` and is close to vestigial. Only two
signatures still take one — `PollThread::remove` and `PollThreadWorker::update_mode`
— and both do nothing with it but read `fd()`:

```rust srpc-no-compile
pub trait Pollable {
    fn fd(&self) -> i32;
    fn poll_mode(&self) -> i32;
    fn content_size(&mut self) -> usize;
    fn handle_read(&mut self) -> bool;
    fn handle_write(&mut self) -> i32;
    fn handle_error(&mut self);
    fn close(&mut self);
    fn check_pending_write_update(&self) -> bool;
    fn is_closed(&self) -> bool;
}
```

`PollableBase` in `srpc.pollable_proxy` declares the same nine methods and is the
interface the poll worker actually dispatches through. Everything the worker stores
is a `PollableProxy`, which is `Box<dyn PollableBase>` — `rusty::Box<PollableBase>`
in C++. Nothing inherits `Pollable` any more; connection types are wired in by
handing the worker a proxy that forwards to them.

`pollable_proxy.rs` also ships a generic `Arc`-backed adapter,
`PollableArcShim<T>` with the factory `make_pollable_proxy_from_typed_arc(arc)`,
whose forwarding methods take `&self` on the target so several proxies can share one
`Arc`. Note that it is bounded on a *private* trait (`PollableSharedTarget`), so no
out-of-crate Rust type can satisfy it, and no library code calls it — the TCP backend
writes its own shims instead. It is exercised only by the test battery: `test_reactor`
and `test_rpc_pollthread_proxy_storage`, two of the eight suites CMake builds, both
register their pollables through it. Treat it as available surface, not as the
production path.

The method contract as the worker enforces it:

| Method | When the worker calls it | What it does with the result |
|--------|--------------------------|------------------------------|
| `fd()` | once at registration, to key the tables | `< 0` means "already closed" — the proxy is dropped |
| `poll_mode()` | once at registration | seeds the interest set |
| `handle_read()` | on `READABLE` | **return value ignored** |
| `handle_write()` | on `WRITABLE` | any value but `PollMode::NO_CHANGE` becomes an interest update |
| `handle_error()` | on `ERROR`, after read/write for the same event | — |
| `check_pending_write_update()` | once per loop pass, for every registered fd | `true` re-arms read **and** write interest |
| `is_closed()` | once per loop pass, for every registered fd | `true` unregisters, calls `close()`, drops the proxy |
| `close()` | from the `is_closed` sweep and from `ClosePollable` | — |
| `content_size()` | **never** | — |

### What is actually registered

In the current tree exactly two types are pollable, and both live in
`rpc/tcp_channel.rs`: `TcpConnection` and `TcpListener`. Each is wrapped by a private
shim (`TcpPollableShim`, `TcpListenerPollableShim`) holding an `Arc`, and registration
happens at three points — when a listener binds, when it accepts, and when the client
factory connects.

`ServerConnection` and `ClientConnection` are **not** pollables and are never handed
to a poll thread. `ClientConnection` still carries a set of Pollable-shaped methods
from an older design, and they are inert placeholders: `fd()` returns `-1`,
`handle_read()` returns `false`, `handle_write()` returns no-change,
`content_size()` returns `0`. Do not read them as the client's I/O path; the real one
is the `TcpConnection` underneath the channel proxy.

### PollThread — the cross-thread handle

`PollThread` is what every other thread holds. It owns the `Sender` end of the command
channel, the join handle for the worker thread, and two atomics for shutdown
bookkeeping. It never touches the epoll set directly — every mutator is a message.

```cpp srpc-no-compile
rusty::Arc<PollThread> pt = PollThread::create();

pt->add_proxy(std::move(proxy));   // register a Box<dyn PollableBase>
pt->remove_fd(fd);                 // unregister, do NOT close
pt->request_close(fd);             // unregister, close() through the proxy, drop it
pt->update_mode(fd, PollMode::READ | PollMode::WRITE);
pt->add(job);                      // rusty::Arc<Job>
pt->shutdown();                    // stop the loop and join
```

`create()` spawns the worker thread, which publishes its own thread id back into the
handle before entering the loop. There is also a `remove(Pollable&)` overload that
does nothing but call `remove_fd(p.fd())`.

Every one of these sends is best-effort: the only way `Sender::send` fails is that
the receiver is gone, which means the worker already exited and there is no epoll set
left to mutate. The code discards those errors deliberately — except `update_mode`,
which logs at `ERROR`.

`get_remove_count()` is a stub: it returns a constant `0` and reads nothing. Worker
state is not reachable across the channel. The counter it used to expose still exists
inside `srpc.epoll_wrapper` as `epoll_remove_count`, bumped by every
`epoll_remove_impl`, but you have to read that static yourself.

`shutdown()` is idempotent (an atomic `swap` on `shutdown_called_` returns early on
the second call) and self-join aware: if it is invoked *from* the poll thread it sends
the command and returns without joining. `PollThread`'s destructor calls it, so a
handle going out of scope is a clean shutdown.

### PollThreadWorker and the loop

The worker runs on the spawned thread and is the sole owner of everything mutable:

```cpp srpc-no-compile
class PollThreadWorker {
    Receiver<PollCommand>             receiver_;         // commands from other threads
    Epoll                             poll_;             // the epoll fd
    HashMap<int32_t, PollableProxy>   fd_to_pollable_;   // registered proxies, by fd
    HashMap<int32_t, int32_t>         mode_;             // current interest set, by fd
    HashSet<int32_t>                  pending_remove_;   // deferred unregistrations
    std::set<rusty::Arc<Job>>         jobs_;             // pending jobs
    bool                              stop_;
};
```

While the worker is running it publishes a raw pointer to itself into
`g_current_poll_worker` so fibers on the same thread can ask
`pollworker_is_on_poll_thread()`. That predicate is what the TCP send path uses to
decide between a direct flag and a channel message — see below. Like the reactor's
other per-thread statics, `g_current_poll_worker` carries a
`#[cfg_attr(any(), thread_local)]` marker: it is genuinely thread-local in the
generated C++, and a plain process-global `static mut` when the file is checked by
rustc, where the predicate is stubbed to `false` anyway.

One pass of `poll_loop` does, in order:

1. **Run ready jobs.**
2. **`poll_.Wait(...)`** — one 1 ms epoll pass. The readiness batch is collected into
   a vector *first*, then dispatched, so a handler that re-enters the worker is not
   running inside a borrow of the epoll object.
3. **Dispatch each ready fd.** `handle_read()` on `READABLE`; `handle_write()` on
   `WRITABLE`, applying the returned mode if it is not `NO_CHANGE`; `handle_error()`
   on `ERROR`, re-looked-up by fd because the earlier calls may have removed it.
4. **Drain the command channel** with non-blocking `try_recv` until it is empty.
5. **Run ready jobs** again.
6. **Process `pending_remove_`.**
7. **Run ready jobs** a third time.
8. **`Reactor::get_reactor()->run_loop(false, true)`** — one non-infinite reactor
   pass, so fibers that became runnable during dispatch actually run, with timeout
   checking on.
9. **Sweep `check_pending_write_update()`** over every registered fd; a `true` re-arms
   `READ | WRITE`.
10. **Sweep `is_closed()`** over every registered fd, collecting first and mutating
    second because `close()` can re-enter. For each closed fd: `Epoll::Remove` if it
    is still in `mode_`, then `close()` through the proxy, then erase both table
    entries.

Both sweeps in steps 9 and 10 share one snapshot of the key set, which is sound
because nothing between them can add an fd.

When `stop_` is set, the loop unregisters every remaining fd from epoll and clears all
three tables. It does **not** call `close()` on the survivors — that is the difference
between shutdown and the `is_closed` sweep.

### The command channel

`PollCommand` is a Rust enum carried over an mpsc channel. The poll thread is the sole
consumer and the sole owner of everything the commands manipulate, so the whole layer
needs no locks.

| Command | Effect on the worker |
|---------|----------------------|
| `AddPollable { pollable }` | drop it if `fd() < 0` or the fd is already registered; otherwise insert into `fd_to_pollable_` / `mode_` and `Epoll::Add`, rolling both back if `Add` fails |
| `RemovePollable { fd }` | insert into `pending_remove_`; the actual unregistration happens later in the pass |
| `ClosePollable { fd }` | cancel any pending removal, `Epoll::Remove`, `close()` through the proxy, erase both entries |
| `UpdateMode { fd, new_mode }` | no-op unless the fd is registered; record `new_mode` and call `Epoll::Update` only if it differs from the old one |
| `AddJob { job }` / `RemoveJob { job }` | insert into / erase from `jobs_` |
| `Shutdown` | set `stop_` |

`RemovePollable` is the only deferred one. Removing an fd from the tables in the
middle of a dispatch batch would invalidate the lookup for events already collected
from the same `epoll_wait`, so it is parked and applied at step 6.

In practice the TCP backend never sends `RemovePollable` or `ClosePollable` at all: a
connection closes itself (`TcpConnection::close` shuts the socket down and drops its
`OwnedFd`), and the worker notices on its next `is_closed()` sweep. That ordering is
why `epoll_remove_impl` has to tolerate `EBADF` — by the time the worker unregisters,
the descriptor is usually already gone, and the kernel has dropped it from the epoll
set anyway.

### Handing write interest back to the poll thread

Write interest is the one piece of state two threads race for, and the mechanism is
worth spelling out because it explains an otherwise odd `Pollable` method.

`TcpConnection::poll_mode()` reports `READ`, plus `WRITE` while the outbound buffer is
non-empty. When something queues a frame, the connection has to get `WRITE` armed. It
picks one of two routes:

- **Off the poll thread** — send `PollThread::update_mode(fd, READ | WRITE)` and let
  the command channel do it.
- **On the poll thread** (`pollworker_is_on_poll_thread()` is true, i.e. a fiber
  running inside the loop) — set the connection's `pending_write_update_` atomic and
  return. Sending a command here would work, but it would not be processed until the
  *next* pass; the flag is picked up at step 9 of the current one.

`check_pending_write_update()` is that flag's reader, and it is a `swap(false)` — the
answer is consumed. That is also the route taken when a connection has no poll thread
installed yet.

### The job system

A job is a unit of work the poll thread runs from inside its own loop, so that it lands
in order with respect to the commands already queued. The trait and its one
implementation live in `base/misc.rs` (module `srpc.misc`), not in the reactor —
`reactor.rs` imports them and owns only the scheduling. The trait is three methods:

```cpp srpc-no-compile
class Job {
    virtual bool Ready() = 0;   // may this run now?
    virtual void Work() = 0;    // do it
    virtual bool Done() = 0;    // declared, but nothing in srpc calls it
};
```

The worker's scheduling rule is simple and has one surprise in it. On each of the
three job passes it takes the whole set, and for every job asks `Ready()`. A job that
answers `true` is **spawned in a new stackful fiber running `Work()` and is not put
back** — so a plain `Job` runs at most once. A job that answers `false` is reinserted
and asked again next pass. `Done()` is never consulted anywhere in the tree.

The only implementation shipped is `OneTimeJob`, which matches that rule exactly: it
starts `Ready()`, and `Work()` clears ready, invokes the callback, and sets done.
There is no `FrequentJob` — a job that should repeat has to re-add itself.

The set is `std::set<rusty::Arc<Job>>` in C++ and is identity-keyed, so `RemoveJob`
must be handed the same `Arc` that was added.

Both in-tree uses are deferred teardown or spawn, never a repeating timer. `Server`'s destructor schedules
the channel-mode listener close as a `OneTimeJob` so it is ordered behind commands
already in the queue, and the client does the same for its channel-mode close and for
spawning its receive loop:

```rust srpc-no-compile
// rpc/server.rs, Server::drop — close the listener on the poll thread so the
// close is ordered behind whatever commands are already queued.
let close_job: Arc<OneTimeJob> = Arc::new(OneTimeJob::new(Box::new(move || {
    listener_box.close();
})));
pt.add(close_job);
```

That is the pattern to copy when you need something to happen *on* the poll thread
from outside it and `update_mode` / `request_close` do not cover it.

---

## 7. RPC Protocol

SRPC's wire protocol has two layers that are worth keeping separate in your head,
because they live in different files and are owned by different pieces of code.

The **framing layer** turns a byte stream into a sequence of length-delimited
frames. It knows nothing about RPC: a frame is a 4-byte header followed by an
opaque payload. Its decoder is `rpc/frame_codec.rs`, its header bit-layout helpers
are `rpc/internal_protocol.rs`, and its encoder — for the TCP transport — is
open-coded inside `rpc/tcp_channel.rs`.

The **RPC layer** decides what goes in the payload. Requests are encoded by
`clientconn_request_via_channel` in `rpc/client.rs`; replies by `sconn_reply` in
`rpc/server.rs`. Neither one writes a length prefix — they hand a finished body to
`send_frame` and the transport frames it.

There is no handshake, no magic number, no protocol version, and no checksum.
A connection starts with the first frame.

### The frame header

Four bytes, laid out as one `i32` in **native byte order**:

```
   byte 0        byte 1        byte 2        byte 3
 +-------------+-------------+-------------+-------------+
 |         encoded_size : i32, NATIVE endian             |
 +-------------------------------------------------------+
        bit 31       extended-header flag   (kResponseHeaderExtFlag = 0x80000000)
        bits 30..0   payload size in bytes  (kResponseSizeMask      = 0x7fffffff)
```

The size **excludes** the four header bytes. `kFrameHeaderSize` is 4.

Native endianness is a real constraint, not an accident of the writing: the encode
side does `encoded.to_ne_bytes()` and the decode side `i32::from_ne_bytes(..)`.
A little-endian client and a big-endian server cannot talk to each other. Every
piece of SRPC assumes a homogeneous fleet.

`rpc/internal_protocol.rs` is the whole bit layout, and it is three functions long:

```rust
pub const kResponseHeaderExtFlag: u32 = 0x80000000;
pub const kResponseSizeMask: u32 = 0x7fffffff;

pub fn response_has_extended_header(encoded_size: i32) -> bool {
    ((encoded_size as u32) & kResponseHeaderExtFlag) != 0
}

pub fn response_payload_size(encoded_size: i32) -> i32 {
    ((encoded_size as u32) & kResponseSizeMask) as i32
}

pub fn encode_response_size(payload_size: i32, extended_header: bool) -> i32 {
    let base: u32 = (payload_size as u32) & kResponseSizeMask;
    let out: u32 = if extended_header { base | kResponseHeaderExtFlag } else { base };
    out as i32
}
```

Each of these carries a `#[cfg(verus)]`-gated contract pinning its result to
exactly that bit expression, and the two theorems built on them — that a decoded
size is always in `[0, kResponseSizeMask]`, and that encode/decode round-trips
losslessly for any non-negative size and either flag — are machine-checked in
`verify/src/internal_protocol_proofs.rs`. See `docs/verification.md`.

#### The extended-header flag is vestigial

The flag bit exists in the format and both sides handle it correctly, but **nothing
in the live path ever sets it**. `tcp_channel.rs`'s send path hardcodes
`let extended_header_flag = false;` for every frame it writes, request and reply
alike. On the read side `frame_codec_peek_header` faithfully decodes the bit into
`FrameHeader::extended_header_flag`, and then the TCP read loop throws it away —
it forwards only `{payload, size}` to the frame callback. No code above the codec
ever sees it.

So do not read the flag as a negotiation mechanism. The reply body described below
is always the long form, and the bit is not what tells you so — nothing consults it.
It is a hook a future format change could use, decoded and discarded today.

#### Two encoders, one format

`frame_codec.rs` exports a proper encode side — `frame_codec_write_header` and
`frame_codec_encode_into`, both of which validate the size and both of which route
through `encode_response_size`. The live TCP transport calls **neither**. It
open-codes the same four bytes inline in `tcpconn_send_frame`:

```rust
let encoded_size = if extended_header_flag {
    (frame.size as u32 | 0x8000_0000u32) as i32
} else {
    frame.size as i32
};
let header = encoded_size.to_ne_bytes();
```

The codec's encode functions are reached only from the Rust test suite. That means
a change to the header layout has to be made in two places, and only one of them is
covered by `tests/frame_codec_rust.rs`. If you touch the header, touch
`tcp_channel.rs` too.

### kMaxFramePayloadSize, and why it exists

```rust
pub const kMaxFramePayloadSize: i32 = 64 * 1024 * 1024;   // 64 MiB
```

This is a **stream-integrity bound, not a resource policy**. It is not there to cap
memory; it is there so that a desynchronised stream fails loudly.

The 4-byte header is the only framing signal on the wire. If a connection ever
desynchronises — a short write, a reconnect that resumes mid-frame, a bug upstream —
the decoder reads payload bytes as a header and gets a garbage length. Without a
bound it *accepts* that length and waits for bytes that will never arrive:
`next_frame()` returns `NeedMoreBytes` forever, `consume_frame()` never advances the
cursor, the buffer is never compacted, and the connection wedges silently. No error,
no log, no close, no reconnect. The bound converts that into
`Malformed` → `on_error` → close → reconnect, which is a failure a caller can see.

64 MiB is far above any real SRPC message and rejects 31 of every 32 values in the
31-bit size space, so a desync is caught on the first bad header with high
probability. It is the only knob; raise it if a legitimate message ever needs to be
larger. Two invariants constrain it:

- It must stay `<= i32::MAX - kFrameHeaderSize`, so `FrameHeader::total_frame_size()`
  cannot overflow. That method uses `saturating_add`, not wrapping: casting a wrapped
  negative `i32` to `usize` sign-extends, and an 18-quintillion-byte "total" would make
  the `rem.len() < total` guard true forever — the exact wedge the bound exists to
  prevent.
- The TCP transport applies the same ceiling on send (`TCP_MAX_FRAME_PAYLOAD_SIZE`
  is an alias for it) and refuses an oversized frame with `ChannelError::Internal`.
  On the client that surfaces as `EIO` (5) from `request`; on the server,
  `sconn_dispatch_response_frame_via_channel` discards the send result, so an
  oversized *reply* is dropped silently and the caller only learns about it when the
  future times out.

### FrameDecodeStatus and the stream reader

```rust
pub enum FrameDecodeStatus {
    NeedMoreBytes = 0,
    Complete = 1,
    Malformed = 2,
}
```

`frame_codec_peek_header` returns `NeedMoreBytes` for fewer than 4 buffered bytes,
`Malformed` when the decoded size exceeds `kMaxFramePayloadSize`, and `Complete`
otherwise. (It also guards `payload_size < 0`, but that arm is defence in depth:
`response_payload_size` masks the sign bit off, and that non-negativity is now a
proven theorem rather than a comment.) `frame_decode_status_to_string` gives you the
variant name for logging.

`FrameStreamReader` is the accumulating decoder each `TcpConnection` owns:

- `append(data, size)` — copy freshly `recv`'d bytes onto the tail.
- `next_frame(&mut FrameView)` — peek without consuming. `Complete` fills the view
  with a borrowed pointer into the reader's own buffer; the payload is valid only
  until the next mutation.
- `consume_frame()` — advance past the frame just returned, then compact the buffer
  once the read cursor passes 64 KiB.
- `reset()`, `buffered_bytes()`, `empty()`.

The read loop in `tcp_channel.rs` drives these in a cycle: `next_frame`, fire
`on_frame`, `consume_frame`, repeat until `NeedMoreBytes`. A `Malformed` breaks the
loop, fires `on_error` with `"malformed frame on inbound stream"`, resets the fd and
the inbound buffer, and delivers `on_closed` — which is what triggers the client's
reconnect path.

### Request body

```
  +----------------+----------------+---------------------------+
  | xid            | rpc_id         | arg1 | arg2 | ... | argN   |
  | v64, 1-9 bytes | i32, 4 bytes   | (serialized arguments)     |
  +----------------+----------------+---------------------------+
```

`xid` is a `v64` — the historical *sparse integer* varint from `base/basetypes.rs`.
Its first byte selects the total length (1 to 9 bytes) and carries the high bits;
small values cost one byte. `rpc_id` is **not** a varint: `Serialize for i32` writes
the four raw object bytes, so it is a fixed 4-byte native-endian field.

The encode site is short enough to read in full (`rpc/client.rs`):

```rust
let mut body_sink: BufferSink = BufferSink { bytes: Vec::<u8>::new() };
let mut ar_store = BinaryWriteArchive { sink_: client_sink_proxy(&mut body_sink) };
let ar: &mut BinaryWriteArchive = &mut ar_store;
crate::serializable::Serialize_::serialize(&crate::basetypes::v64::new((*fu).xid_), ar);
crate::serializable::Serialize_::serialize(&rpc_id, ar);
write_fn(ar);
```

`write_fn` is the lambda the generated proxy passes to `Client::request`; it appends
the arguments. Nothing else is added. The finished `Vec<u8>` goes to `send_frame`,
which prepends the 4-byte header.

xids come from a per-connection `Counter` starting at 0 and incremented by 1. They
are not globally unique and are not reused across connections.

#### Where rpc_id values come from

`rpcgen` assigns each method a random id in `[0x10000000, 0x70000000]` and emits it
as an enumerator inside the generated service class. On regeneration it *re-parses
the existing generated header* and reuses every id it finds there. That is why the
generated `.h` must be kept under version control and must not be deleted before
regenerating: lose it and every id changes, silently breaking the wire for anything
already built.

The server keeps a `HashMap<i32, usize>` from rpc_id to service index, plus a
`HashSet<i32>` of ids registered as "fast". An id with no entry gets a reply with
`ENOENT` and a one-shot warning log (the id is remembered so the log does not
repeat).

#### The internal heartbeat id

```rust
pub const kInternalHeartbeatRpcId: i32 = i32::MIN;   // -2147483648
```

It sits far outside the generator's range, so it can never collide with a real
method. A heartbeat frame is a request body with nothing after the header: a `v64`
xid and that rpc_id. The server recognises it before the dispatch-table lookup and
answers with `error_code = 0` and an empty payload — unless the connection's
`drop_heartbeat_replies` flag is set, which is a test hook for simulating a silent
peer.

Two caveats on the client side. First, the heartbeat *reply* is indistinguishable
from any other reply, and the client does not try to distinguish it: the decode path
calls `heartbeat_manager_.on_pong_received()` for **every** inbound reply. Second,
and more importantly, nothing sends heartbeats in practice. The only caller of
`enqueue_heartbeat_probe` is `ClientConnection::check_pending_write_update`, which is
a plain inherent method with no callers anywhere in the tree: `ClientConnection` does
not implement `PollableBase`, so the reactor can never drive it. The only registered
pollables are the `TcpConnection` and the `TcpListener`. The protocol is
complete and the timer is never ticked. Do not plan liveness detection around it.

### Reply body

```
  +----------------+----------------+--------------------+------------------+
  | xid            | error_code     | server_instance_id | ret1 | ... | retN |
  | v64, 1-9 bytes | v32, 1-5 bytes | v64, 1-9 bytes     | (serialized)     |
  +----------------+----------------+--------------------+------------------+
```

All three header fields are always present. `sconn_reply` is the *only* reply
encoder in the codebase — the typed wrappers, the `defer` path's `DeferredReply`,
the `fiber` and `async` paths, and the two error replies from the dispatcher all
funnel through it — and it writes the full triple unconditionally:

```rust
crate::serializable::Serialize_::serialize(&v64::new(req.xid), ar);
crate::serializable::Serialize_::serialize(&v32::new(error_code), ar);
crate::serializable::Serialize_::serialize(
    &v64::new(sconn.ctx_.server_instance_id as i64),
    ar,
);
if !write_fn.is_empty() {
    let mut write = write_fn;
    write(ar);
}
```

The client mirrors it unconditionally, deserializing all three before handing the
remaining bytes to the caller. There is no short form and no branch on the header
flag. `error_code` is whatever the handler returned: a typed handler returning
`Err(e)` replies with `error_code = e` and **no payload**, so a client must check
the error code before attempting to decode return values — which is exactly what the
generated `resolve()` does.

`server_instance_id` is generated once per `Server`, from the monotonic clock XORed
with a random `u64` and the pid shifted left 48, masked to 63 bits (it crosses the
wire as a signed `i64`) and forced non-zero. It exists for restart detection: the
client caches the first id it sees and, when a later reply carries a different one,
logs `"Server restart detected"` and fires the `on_server_restart` callback. A
zero cached id means "not yet known", so the first reply never triggers it.

#### One sharp edge in the v64 encoding

The sparse-integer format has a legacy defect at exactly one length. When
`val_size(v) == 8`, `dump64` writes a `0xFE` marker plus eight payload bytes but
*reports* eight, so the archive emits only the marker and seven payload bytes; the
decoder then reconstructs the value from its own zero-filled scratch and the low
byte comes back as zero. Framing is unaffected — writer and reader agree on eight
bytes, so the cursor stays in sync — but the value does not round-trip.
`tests/basetypes_rust.rs` pins this exactly:
`36_028_797_018_963_967` decodes as `36_028_797_018_963_712`.

It affects `|v|` roughly between 2^48 and 2^55. An xid would need 2^48 requests on
one connection to get there. A `server_instance_id` is effectively random over 63
bits, so it lands in that band about 0.4% of the time — but the mangling is
deterministic, so restart detection still compares like with like and keeps working.
No production consequence has been observed; know about it before you put a large
`v64` on the wire yourself.

### Request/response flow

```
Client                                                          Server

generated proxy: Rpc<M>Request -> Client::request(rpc_id, attr, write_fn)
  |
  ClientConnection::request -> clientconn_request_via_channel
    circuit-breaker gate  -> EBUSY if open
    expire stale queued requests
    if not connected: park in the offline queue, or fail ENOTCONN
    Future::create(xid) -> pending_fu_ map
    serialize  v64 xid | i32 rpc_id | args   into a BufferSink
  |
  ChannelConnectionProxy::send_frame(body)
    TcpConnection prepends the 4-byte header, buffers, arms POLLOUT
  |                                                                |
  ================== bytes ==================>                     |
                                                                   |
                            server TcpConnection::handle_read      |
                              FrameStreamReader::append            |
                              next_frame -> Complete               |
                              on_frame(payload, size)              |
                            sconn_decode_request_and_dispatch      |
                              copy body into Request               |
                              read v64 xid, then i32 rpc_id        |
                              rpc_id == heartbeat? reply 0, done   |
                              rpc_to_service lookup -> ENOENT?     |
                              fast id  -> dispatch inline          |
                              otherwise -> spawn a stackful fiber  |
                            <Svc>Service::__dispatch__(rpc_id, req, weak_sconn)
                              generated wrapper decodes args,
                              calls the handler, then sconn_reply  |
                                                                   |
  <================= bytes ===================                     |
  |
  client TcpConnection::handle_read -> on_frame
  ClientConnection::decode_response_and_notify
    read v64 xid, v32 error_code, v64 server_instance_id
    check_server_instance -> maybe fire on_server_restart
    async slot (xid % 16384) occupied? -> invoke that callback, return
    else look up xid in pending_fu_  -> set error, copy payload, notify_ready
    else drop the reply
  |
  Future::wait() returns; the generated resolve() decodes the payload
```

Three details in that trace deserve to be called out, because they are easy to get
wrong from the outside.

**The frame header is added and removed by the transport, not by the RPC layer.**
`client.rs` and `server.rs` only ever see bodies. This is why the in-memory transport
below can be frameless and still work.

**Fast RPCs run on the poll thread.** `reg_fast_rpc` (which the generator emits for
`fast`, `prefix`, and `async` methods) puts the id in `fast_rpc_ids`, and those
handlers are called inline from the frame callback. Everything else gets a 1 MiB
stackful fiber so the handler can block on a nested call. A `fast` handler that
blocks stalls the entire poll thread.

**The async slot is checked before the future map, and it is keyed only by
`xid % 16384`.** `request_async` reserves `pending_cb_slots_[xid % 16384]` (refusing
with `EBUSY` if it is taken), but the decode path takes whatever callback occupies
the slot the *reply's* xid hashes to, without comparing xids. The future path does
verify `fu->xid_ == reply xid`; the async path does not. If you mix `request_async`
and `request` on one connection, a reply can be delivered to an unrelated async
callback. A reply matching neither a slot nor the map is dropped silently — the
normal outcome after a timeout, and it leaves no trace.

### Error codes

Only two error codes are ever produced *by the protocol itself*; everything else on
the wire is a value your handler returned.

| Code | Name | Written by | Meaning |
|------|------|-----------|---------|
| 0 | — | `sconn_reply` | Success. Payload follows. |
| 2 | `ENOENT` | dispatcher | No handler registered for this `rpc_id`. Empty payload. |
| 22 | `EINVAL` | dispatcher | Request body had an xid but fewer than 4 bytes left for `rpc_id`. Empty payload. |
| any | — | your handler | Whatever `Err(e)` your handler returned. Empty payload. |

A request body with *no* xid at all — a zero-length frame — is logged and dropped
without a reply, because there is no xid to reply against.

These are the client-local failures, which never appear on the wire. They are
returned by `request` / `request_async` or latched into the future:

| Code | Name | Raised when |
|------|------|-------------|
| 5 | `EIO` | `send_frame` refused the frame (channel closed, over the outbound high-water mark, or larger than 64 MiB). |
| 11 | `EAGAIN` | The offline queue rejected or evicted the request (`kRequestQueueRejectedError`). |
| 16 | `EBUSY` | Circuit breaker open, or the async slot was already occupied. |
| 107 | `ENOTCONN` | Not connected and no offline queue configured; also fanned out to every pending future when the channel closes. |
| 110 | `ETIMEDOUT` | `Future::wait()` gave up — it is hard-capped at one second, see chapter 8 — or a queued request outlived its TTL (`kRequestQueueExpiredError`). |

`EAGAIN` in particular is a client-side code, not a server one. SRPC's server has no
queue policy and no admission control — it dispatches every frame it decodes. The
only queue that can reject you is the client's own offline buffer.

`rpc/errors.rs` also defines a structured `RpcError` enum — `NOT_CONNECTED = 100`,
`UNKNOWN_RPC_ID = 201`, `RESPONSE_TIMEOUT = 402`, and so on, banded by category with
`get_error_category` / `is_retryable_error` helpers. It is a client-side
classification layer that `clientconn_map_system_error` maps errno-shaped codes into.
None of those values ever travel on the wire.

### The in-memory transport, for tests

`rpc/inmemory_channel.rs` is a second implementation of the same
`ChannelConnectionBase` / `ChannelListenerBase` / `ChannelFactoryBase` contracts,
built for deterministic tests. An `InMemorySwitchboard` maps address strings (any
string you like — `"inmemory://server-1"` works) to listeners via
`register_listener` / `unregister_listener` / `find_listener`. `InMemoryFactory::connect`
looks up the listener, builds a channel pair over one shared mutex-protected state
object, fires the listener's `on_accept` synchronously, and hands back the client
half; with no listener registered it returns `ChannelError::ConnectionRefused`.
`send_frame` copies the bytes and calls the *peer's* `on_frame` synchronously;
`close()` fires the *peer's* `on_closed`, mirroring TCP's "remote saw FIN".
Fault injection is available through free functions —
`inmemory_channel_inject_drop_next_sends`, `inmemory_channel_inject_send_error`,
`inmemory_channel_clear_fault_injection` — with drops taking priority over errors,
and a closed channel beating both.

The property to keep in mind when using it: **it is frameless**. There is no 4-byte
header and no `FrameStreamReader` anywhere in that file; each `send_frame` delivers
exactly one `on_frame` with exactly those bytes. That makes it a good foundation for
reconnect and partition tests, and completely unable to reproduce a framing bug.
Coverage of `frame_codec` has to come from the Rust tests
(`tests/frame_codec_rust.rs`, `tests/frame_codec_desync_rust.rs`), not from an
end-to-end run over this transport.

To use it you must call `set_channel_factory` on the `Server` and the `Client`
*before* `start` / `connect`; otherwise both auto-install a TCP factory and the
in-memory one never gets a chance. The module is also not re-exported through the
umbrella header, so name it explicitly:

```cpp srpc-no-compile
// srpc.hpp does not import these two; a consumer that names them says so.
import srpc.inmemory_channel;
import srpc.internal_protocol;
```

`srpc.frame_codec` is the exception among the protocol modules: `srpc.hpp` pulls it
in textually via `#include "rpc/frame_codec.hpp"`, so `kFrameHeaderSize`,
`kMaxFramePayloadSize`, `FrameHeader`, and `FrameDecodeStatus` are already visible to
anyone who includes the umbrella.

---

## 8. RPC Client

`Client` is the whole public client API: one object, one connection, one poll
thread behind it. `ClientConnection` is the machinery underneath — framing, the
pending-reply table, the circuit breaker, the metrics counters. You never
construct one directly; you reach it through `Client::connection()` when you
want something `Client` does not forward.

Everything named here lives in `srpc.client`, which `srpc.hpp` already imports.
`RequestOptions` and `LoadBalancingStrategy` do not: those were trimmed out of
the umbrella and need `import srpc.request_options;` and
`import srpc.load_balancer;` of their own.

### Creating a client and connecting

```cpp srpc-no-compile
auto poll = PollThread::create();        // rusty::Arc<PollThread>
auto cl   = Client::create(poll);        // rusty::Arc<Client>

const char* addr = "127.0.0.1:8848";
if (cl->connect(reinterpret_cast<const int8_t*>(addr), true) != 0) {
    // connect failed
}

// ... issue requests ...

cl->close();
poll->shutdown();
```

Two details trip up every first call site. `connect` takes `const int8_t*`, not
`const char*`, so the `reinterpret_cast` is mandatory. And it takes a second
argument — a `bool` marking this end as the client side of the connection. Pass
`true`.

`connect` returns 0 on success and an errno-shaped `int32_t` otherwise: 111
`ECONNREFUSED` when the peer refuses, 22 `EINVAL` for an address the channel
layer will not parse, 107 `ENOTCONN` for anything else the factory reports.
There is no separate "install a transport" step — if you have not called
`set_channel_factory` yourself, `connect` installs a TCP factory before it
dials.

Each `connect` call builds a fresh `ClientConnection` and installs it only if
the dial succeeds, so a failed retry leaves whatever connection you already had
in place. Dropping a connection aborts any reconnect in flight and fails every
future still waiting on it.

`Client` is handed out as a `rusty::Arc<Client>` and its destructor calls
`close()`, so an explicit `close()` is a courtesy rather than a requirement. It
is a useful courtesy: closing the client before you shut down the poll thread
keeps teardown ordered.

The accessors worth knowing are `connected()`, `connection_state()` (a
`ConnectionState`; see chapter 11), `server_instance_id()`, and `connection()`,
which returns `rusty::Option<rusty::Arc<ClientConnection>>`. Skip `host()` —
the field behind it is never assigned, so it always returns an empty string.

### Issuing a request

There is exactly one request entry point, and it takes three arguments:

```cpp srpc-compile-client
auto fu_result = client->request(RPC_METHOD_ID, FutureAttr(),
    [&](BinaryWriteArchive& m) {
        srpc::Serialize_::serialize(arg1, m);
        srpc::Serialize_::serialize(arg2, m);
    });

if (fu_result.is_ok()) {
    auto fu = fu_result.unwrap();
    fu->wait();
    if (fu->get_error_code() == 0) {
        i32 result = 0;
        srpc::deserialize_from(fu->get_reply(), result);
    }
}
```

The three-argument `request(rpc_id, attr, write_fn)` is the only form. The
overload set was deliberately collapsed to it — the code generator emits this
shape even for methods with no arguments, passing an empty lambda — because the
canonical Rust the client is generated from has no method overloading.

`FutureResult` is `rusty::Result<rusty::Arc<Future>, srpc::i32>`, so the call
can fail before a single byte goes out. The error side is again errno-shaped:
107 `ENOTCONN` if the client was never connected or the channel is already
closed, 16 `EBUSY` if the circuit breaker is open, 5 `EIO` if the channel
refuses the frame, and 11 `EAGAIN` if the request was offered to the
disconnected-request queue and the queue rejected it.

Your `write_fn` writes *only the arguments*. The connection has already written
the request header into the same archive by the time your lambda runs: a `v64`
transaction id drawn from the connection's counter, then the `int32_t` rpc id.
Chapter 7 has the byte layout. The xid is what the reply is matched against —
the future is filed in a per-connection map under it, and
`decode_response_and_notify` looks it up when the reply lands. If you decide
you no longer care about a request, `client->handle_free(xid)` drops the map
entry so a late reply is discarded instead of parked forever.

Serialization goes through `srpc::Serialize_::serialize` over a
`BinaryWriteArchive`. There is no `Marshal` and no `operator<<`; chapter 10 has
the details.

### The one-second wall

This is the single most surprising thing about the client, so it is worth
stating flatly: **`Future::wait()` waits one second and then gives up
permanently.**

`Future`'s internal deadline is a private field fixed at 1,000,000
microseconds when the future is constructed. Nothing in the public API changes
it. When that second elapses without a reply, the future latches: it sets
`timed_out = true`, sets its error code to 110 `ETIMEDOUT`, and records a
timeout type of `RESPONSE_TIMEOUT`. The latch is one-way: when the reply
finally does arrive, `notify_ready` refuses to mark a timed-out future ready,
so `ready()` stays false forever.

All three blocking accessors funnel through that same deadline. `wait()`
obviously does; `get_error_code()` runs the identical timed wait before reading
the code; and `get_reply()` calls `wait()` first. The generated typed future is
no escape either — its `wait()`, `get_error_code()` and `resolve()` all
delegate to the same `Future`.

Nothing else about the future is protected, though. A timeout removes nothing
from the pending-reply map, so `decode_response_and_notify` still finds the
future, overwrites its error code with the server's and fills its reply buffer
before it reaches `notify_ready` — and once `timed_out` is set, every blocking
accessor returns immediately instead of waiting. A caller that re-reads
`get_error_code()` after the timeout therefore sees the server's 0, and
`get_reply()` hands over the payload, with `ready()` still false. Call
`client->handle_free(xid)` when you give up, so a late reply is discarded
instead; that is exactly what the retry coordinator does after each timed-out
attempt.

There is exactly one way out, and it is `wait_with_options()` on a future whose
`RequestOptions::timeout_ms` you have set to something non-zero. That is what
the next section is for.

A practical consequence: a server handler that takes longer than a second to
answer will look like a network failure to a client using `request` +
`wait()`, no matter how healthy the connection is.

### Timeouts and retries

`request_with_options(rpc_id, options, write_fn)` swaps the `FutureAttr` for a
`RequestOptions` and gives you a real timeout budget plus optional retries:

```cpp srpc-compile-client
auto opts = RequestOptions::defaults();
opts.timeout_ms = 500;       // per-attempt budget
opts.max_retries = 3;
opts.idempotent  = true;     // without this, retries are silently disabled

auto fu_result = client->request_with_options(RPC_METHOD_ID, opts,
    [&](BinaryWriteArchive& m) {
        srpc::Serialize_::serialize(arg1, m);
    });

if (fu_result.is_ok()) {
    auto fu = fu_result.unwrap();

    // The coordinator future is created with timeout_ms = 0, and
    // wait_with_options() then falls back to the same hard 1s cap.
    // Give the waiter a budget that covers the whole retry chain.
    auto wait_opts = opts;
    wait_opts.timeout_ms = 5000;
    fu->set_options(wait_opts);

    if (fu->wait_with_options()) {
        auto attempts = fu->get_retry_count();
        (void)attempts;
    }
}
```

Two gates guard retries and you have to open both. `max_retries` on its own
does nothing: the first thing `request_with_options` does is copy your options
and, if `idempotent` is false, force `max_retries` to zero. The presets
`with_retry(max_retries, timeout_ms)`, `idempotent_retry(max_retries)`,
`fast()` and `patient()` set `idempotent` for you; `defaults()`, `new_()` and
`no_timeout()` do not.

`RequestOptions` is a plain struct with public fields — `timeout_ms`,
`total_timeout_ms`, `max_retries`, `base_delay_ms`, `max_delay_ms`,
`jitter_factor`, `idempotent`. `defaults()` gives `timeout_ms = 1000`, no
total budget, no retries, a 50 ms base delay, a 5000 ms maximum delay, and 0.1
jitter. `total_timeout_ms = 0` means "no overall budget"; a non-zero value
clamps each attempt's timeout to whatever is left and ends the chain with
`TOTAL_TIMEOUT` when it runs out.

The mechanics are worth understanding, because the `set_options` dance above
looks like superstition otherwise. `request_with_options` serializes your
arguments once, into a byte buffer that every attempt replays verbatim, then
returns a *coordinator* future immediately and spawns a detached thread to do
the work. Each attempt is an ordinary `request` on the connection, waited on
with the per-attempt `timeout_ms`; on failure the thread sleeps for an
exponential backoff with jitter and tries again. When an attempt finally
succeeds, its reply bytes are copied into the coordinator future and the
coordinator is notified.

The coordinator future is deliberately created with `timeout_ms = 0` so that
the internal attempts own timeout behaviour — which means that if *you* call
`wait_with_options()` on it without setting a budget first, the zero sends you
straight down the `wait()` path and back into the one-second cap. Setting the
options afterwards is safe: the retry thread captured its own copy of the
options before it was spawned, so your `set_options` only changes what the
waiting side does.

After the wait, `get_retry_count()` tells you how many retries were consumed
and `get_timeout_type()` distinguishes `CONNECT_TIMEOUT`, `REQUEST_TIMEOUT`,
`RESPONSE_TIMEOUT` and `TOTAL_TIMEOUT` from `NONE`.

One limitation: the `Client`-level `request_with_options` builds an empty
`FutureAttr` internally, so you cannot attach a completion callback to a
retrying request the way you can to a plain one.

This request-level idempotency flag is unrelated to `rpc/idempotency.rs`, which
is a standalone deduplication utility not wired into the request path.

### Being notified instead of waiting

Passing a populated `FutureAttr` gives you a callback instead of a wait. The
callback receives the `rusty::Arc<Future>` itself, and it fires from
`notify_ready` — that is, on whichever thread delivered the reply, which for
TCP is the poll thread. Do not block in it.

```cpp srpc-no-compile
FutureAttr attr{FutureCallback::from_callable(
    [](rusty::Arc<Future> fu) {
        if (fu->get_error_code() == 0) {
            i32 result = 0;
            srpc::deserialize_from(fu->get_reply(), result);
        }
    })};

auto fu_result = client->request(RPC_METHOD_ID, attr,
    [&](BinaryWriteArchive& m) { srpc::Serialize_::serialize(arg1, m); });
```

`Future::add_completion_callback` registers a second kind of callback — a
plain `FnMut()` that runs before the `FutureAttr` one — but it returns `false`
and registers nothing if the future is already ready or already timed out, so
it races with the reply unless you install it from inside the request path.

### Fire and forget: `request_async`

When you never intend to wait, `request_async` skips the `Future` and the
pending-future map entirely and hands the reply straight to a callback:

```cpp srpc-no-compile
srpc::AsyncReplyCallback on_reply{
    [](srpc::i32 err, const std::uint8_t* payload, std::size_t size) {
        if (err != 0) { return; }
        // decode `size` bytes at `payload` here — they are only valid
        // for the duration of this call
    }};

auto sent = client->request_async(RPC_METHOD_ID,
    [&](BinaryWriteArchive& m) { srpc::Serialize_::serialize(arg1, m); },
    std::move(on_reply));

if (sent.is_err()) {
    // 107 ENOTCONN, 16 EBUSY, 5 EIO
}
```

The callback slots are a fixed table of 16384 entries indexed by `xid % 16384`.
If the slot for a new request is still occupied, the call fails with 16 `EBUSY`
rather than blocking — so this path assumes you keep fewer than 16384 requests
in flight per connection. There is no timeout on this path at all: if the reply
never comes, the callback is simply never invoked and the slot stays taken
until the connection is torn down. There are no retries either; `RequestOptions`
does not apply here.

The payload pointer is borrowed. Copy anything you want to keep before
returning.

### Reading metrics

`Client::metrics()` is a permanently zeroed stub — it hands back a per-`Client`
`ConnectionMetrics` that nothing ever writes to. The live counters are on
`ClientConnection::metrics()`, reached through `client->connection()`, and
`ConnectionMetrics` needs an `import srpc.connection_metrics;` of its own, being
outside the umbrella too. Chapter 11 lists the counters, says which of them are
never written, and covers what the zeroed stub costs the pool strategies and
health check described below.

Note also that `Client::pending_request_count()` counts *parked* requests in
the disconnected-request queue, not requests in flight. In-flight futures are
counted by `ClientConnection::pending_future_count()`.

### ClientPool

`Client` is one connection. `ClientPool` keeps a set of connections per
address and picks one per call:

```cpp srpc-no-compile
import srpc.load_balancer;   // outside the srpc.hpp umbrella

auto config = PoolConfig::defaults();
config.load_balancing = LoadBalancingStrategy::ROUND_ROBIN;
config.max_connections = 8;

auto pool = ClientPool::new_(rusty::Some(poll), config);

auto client_opt = pool.get_client("127.0.0.1:8848");   // Option<Arc<Client>>
if (client_opt.is_some()) {
    auto client = client_opt.unwrap();
    // use it
}
```

The constructor is `ClientPool::new_(rusty::Option<rusty::Arc<PollThread>>,
PoolConfig)` — both arguments are required, and passing `rusty::None` makes the
pool create a poll thread of its own. Either way the destructor closes every
pooled client and then calls `shutdown()` on whichever poll thread it holds —
including one you passed in, so never share a poll thread between a
`ClientPool` and anything that has to outlive it. There is no
default-constructed `ClientPool`. It is a value, not an
`Arc`: call methods on it with `.`, not `->`. The constructor asserts
`min_connections > 0` and `max_connections >= min_connections`.

`get_client` returns `rusty::Option<rusty::Arc<Client>>` and does a fair amount
of work behind that: on a cache miss it opens `min_connections` connections; on
a hit it runs the load balancer, then walks the pool from that index looking
for a connected-and-healthy client, attempting a reconnect on any client in
`FAILED` or `DISCONNECTED` state as it goes. If nothing survives, it closes
everything, rebuilds `min_connections` fresh connections, and returns a random
one — or returns `None` if even that fails. `None` is the normal signal that
the address is unreachable; check it.

`PoolConfig` is a plain struct with `min_connections`, `max_connections`,
`idle_timeout_ms`, `health_check_enabled`, `unhealthy_threshold_percent`,
`min_requests_for_health` and `load_balancing`, plus the presets `defaults()`,
`aggressive()`, `conservative()` and `no_health_check()`. `defaults()` is 1
minimum / 4 maximum connections, a five-minute idle timeout, health checks on,
a 50% success-rate floor, and `RANDOM` balancing. `set_pool_config` swaps the
whole struct at any time.

Of the four strategies, only `RANDOM` and `ROUND_ROBIN` actually
differentiate. `LEAST_CONNECTIONS` and `LEAST_LATENCY` read
`Client::metrics()`, which is the zeroed stub, so both always score every
client identically and settle on index 0.

The health check has the same problem from the other direction, but only in
its second half. It tests connectivity first; only then does it read
`requests_sent()` off the stub, see zero, decide there is not enough data to
judge (the threshold is `min_requests_for_health`, 10 by default) and return
"healthy". So a *connected* client is always judged healthy — but the
connectivity test that runs ahead of it still bites. `remove_unhealthy_clients`
and `remove_all_unhealthy` do close and drop clients that are not connected,
never taking an address below `min_connections`, and `get_healthy_client_count`
does not count them. What never happens is metrics-driven eviction: a connected
client with a terrible success rate is never removed. The idle-based helpers
`close_idle_clients(addr, now_ms)` and `close_all_idle(now_ms)` do work: they
go through `Client::is_idle`, which reads the connection's real activity clock.
Both take the current time in milliseconds from you rather than reading a
clock themselves.

### Keepalive: configured, not applied

`KeepaliveConfig` exists, `Client::set_keepalive` stores it, and
`keepalive_config()` reads it back. Nothing pushes it to the socket:
`ClientConnection::apply_keepalive_options` is an empty function body. Treat
the whole feature as configuration that is currently inert.

```cpp srpc-no-compile
auto keepalive = KeepaliveConfig::aggressive();   // idle 10s, interval 2s, 3 probes
cl->set_keepalive(keepalive);                     // stored, never applied

auto stored = cl->keepalive_config();             // reads back what you stored
```

The presets are `new_()` (enabled; 60 s idle, 10 s interval, 5 probes),
`aggressive()` (10 / 2 / 3), `relaxed()` (identical to `new_()`) and
`disabled()`. The fields are `enabled`, `idle_sec`, `interval_sec` and `count`.
There is no `KeepaliveConfig::defaults()`.

Set it before `connect` if you set it at all: `Client` holds a pending copy and
pushes it into the connection at connect time; setting it afterwards updates
the live connection's stored copy instead.

For liveness detection that actually does something on the wire, look at the
heartbeat protocol in chapter 11 — though note the caveat there about what
drives its timer.

---

## 9. RPC Server

A `Server` owns three things: the service objects you register, a table mapping RPC
ids to them, and a listener. Everything below that — accepting sockets, framing,
delivering payloads — belongs to the channel layer (`rpc/tcp_channel.rs`,
`rpc/inmemory_channel.rs`). `rpc/server.rs` picks up where a decoded request frame
arrives and stops where a reply frame is handed back to the channel.

The whole module is written for a single dispatch thread — the poll thread you hand
to `Server::new_`. That is not a performance note; it is the contract that makes the
service table sound, and the last section of this chapter spells out what it costs
you.

### Service Implementation

The normal way to implement a service is to inherit from the class the generator
produced for your `.rpc` file and override the typed handlers. For a service
declared `abstract` in the IDL, every handler is pure, so the compiler will not let
you forget one:

```cpp srpc-no-compile
#include "demo.h"

using namespace srpc;
using namespace demo;

class MyDemoService : public DemoService {
public:
    rusty::Result<RpcSumResponse, srpc::i32> sum(const RpcSumRequest& req) override {
        RpcSumResponse resp{};
        resp.result = req.a + req.b + req.c;
        return rusty::Result<RpcSumResponse, srpc::i32>::Ok(resp);
    }

    void slow_echo(const RpcSlowEchoRequest& req, RpcSlowEchoResponse& resp,
                   srpc::DeferredReply defer) override {
        resp.echoed = req.msg;
        defer.reply();
    }
};
```

Return `::Ok(resp)` and the generated wrapper serializes the response and replies
with error code 0; return `::Err(code)` and it replies with your code and an empty
body. You never touch an archive on this path. Chapter 12 covers the IDL and the
per-method attributes — the default, `fast`/`prefix`, `defer`, `fiber`, `async` and
`raw` — that decide *how* the server runs each handler; this chapter covers what the
server does around them.

One trap is easy to hit and fails late: if the service is **not** declared
`abstract`, do not subclass it. Its virtuals are then declared but never defined, so
the class has no key function and its vtable is never emitted — the link fails even
if you override everything. For a non-`abstract` service, define the generated
virtuals out of line in a `.cc`, the way `tests/benchmark_service.cc` does for all
twelve methods of `BenchmarkService`.

Register the implementation with `reg_service_typed`:

```cpp srpc-no-compile
auto server = Server::new_(rusty::Some(poll_thread.clone()));
server.reg_service_typed(rusty::make_box<MyDemoService>());
```

`reg_service_typed<T>(rusty::Box<T>)` is the entry point for generated services
because **a generated service class has no base class**. It wraps your box in a
`ServiceBoxShim<T>` — a small adapter that does inherit `srpc::Service` and forwards
`__reg_to__` / `__dispatch__` to the object inside — so `T` only has to *have* those
two members, not inherit them.

`reg_service(rusty::Box<Service>)` is the other door, and it is for hand-written
services that really do inherit `srpc::Service`. Both call `__reg_to__` immediately
and then park the service in the pending list until `start()`.

### The Service Interface

Under the typed layer, a service is just two methods: `__reg_to__` claims RPC ids and
`__dispatch__` routes one request. The generated class writes both for you. Writing
them yourself — by inheriting `srpc::Service`, which is where those two are virtual —
is how you get at the undecoded request, and it is the shape a `raw` IDL method hands
your handler anyway:

```cpp srpc-compile-server
class MyRawService : public Service {
public:
    enum : i32 { RPC_DO_WORK = 0x1001 };

    int __reg_to__(Server& svr, size_t svc_index) override {
        return svr.reg_rpc(RPC_DO_WORK, svc_index);
    }

    void __dispatch__(i32 rpc_id, rusty::Box<Request> req,
                      WeakServerConnection weak_sconn) override {
        if (rpc_id != RPC_DO_WORK) {
            return;
        }
        // The read cursor is already past the xid and the rpc_id — the server
        // consumed those through an archive over this same `req->src`.
        srpc::i32 arg = 0;
        srpc::BinaryReadArchive __req_ar__(srpc::make_source_proxy_buffer(&req->src));
        srpc::Deserialize_::deserialize(arg, __req_ar__);
        srpc::i32 result = compute(arg);

        auto sconn_opt = weak_sconn.upgrade();
        if (sconn_opt.is_some()) {
            auto sconn = sconn_opt.unwrap();
            const_cast<ServerConnection&>(*sconn).reply(*req, 0, [&](BinaryWriteArchive& out) {
                srpc::Serialize_::serialize(result, out);
            });
        }
        // `req` is a Box — dropping it releases the request and its pending-request
        // guard.
    }
};
```

The connection handle is *weak* on purpose: by the time a handler finishes, the peer
may be gone. `upgrade()` returns an `Option`; if it is empty the reply is simply
dropped, which is the same thing the generated wrappers do.

`__reg_to__` gets a `svc_index` and must hand it to `reg_rpc(rpc_id, svc_index)` for
every id it wants, or `reg_fast_rpc` for ids that should dispatch inline. `reg_rpc`
returns `EEXIST` (17) if that id is already claimed, and the generated `__reg_to__`
reacts by jumping to an error label that calls `svr.unreg(id)` for *all* of its ids
and returns the error.

Two consequences worth knowing, both verifiable in `rpc/server.rs` and in the
generator:

- `reg_service` and `reg_service_typed` **discard** `__reg_to__`'s return value. A
  failed registration is silent; the service is still stored, just unreachable.
- `unreg` removes an id from the table regardless of which service owns it. So if two
  services collide on one id, the loser's cleanup unregisters the *winner's* mapping
  for that id too, and neither handler is reachable afterwards.

The generator assigns each method a random id in `0x10000000`–`0x70000000` and keeps
it stable by reading the previous header, so collisions are unlikely — but they fail
quietly, so if a method mysteriously answers `ENOENT`, look here first.

### Server Lifecycle

```cpp srpc-compile-server
auto poll_thread = PollThread::create();
auto server = Server::new_(rusty::Some(poll_thread.clone()));

server.reg_service(rusty::make_box<MyService>());

// Start listening. TCP is installed automatically.
if (server.start(reinterpret_cast<const int8_t*>("0.0.0.0:8100")) != 0) {
    return;
}

// Blocks until someone calls graceful_shutdown() or do_shutdown().
server.wait_for_shutdown();

poll_thread->shutdown();
```

`start()` takes `const int8_t*` — hence the `reinterpret_cast` — and returns 0 or -1.
It does four things, in this order: it drains the pending registrations into an
immutable `RpcServiceContext`, auto-installs a `TcpFactory` on your poll thread if you
have not bound a channel factory yourself, makes a listener and wires its `on_accept`
/ `on_error` callbacks, and binds. Bind to port `0` and read the real port back with
`get_bound_port()`.

Three things follow from that order:

- **Register everything before `start()`.** The pending tables are emptied by
  `start()`; anything registered afterwards sits in a list nobody reads.
- **Call `start()` once.** A second call rebuilds the context from whatever is pending
  now — which is normally nothing — and replaces the live table.
- **A failed `start()` takes your services with it.** Both failure paths (no listener,
  or a bind error) drop the freshly built context, and the services went into it. If
  you are retrying on another port, re-register first.

To use the in-memory transport instead of TCP, call `set_channel_factory()` *before*
`start()`; the auto-install only fires when nothing is bound.

`addr()` reads the bound address back out of the context and `unwrap()`s it, so it is
only *callable* after a successful `start()`: before that — or after a `start()` that
failed, which drops the context again — it panics rather than returning an empty
string.
`service_count()` works either side of the line, reporting the pending count before
`start()` and the live count after.

Teardown order is: close the clients, destroy the `Server`, shut down the poll thread.
Destroying the server is what closes connections that were already accepted — it
schedules the listener close as a job on the poll thread so it stays ordered against
in-flight work, then closes each accepted connection eagerly so peers see EOF.
`graceful_shutdown()` does not do that part.

### Graceful Shutdown

The server carries a phase that only moves forward:

```
RUNNING -> STOP_ACCEPTING -> DRAINING -> CLOSING -> STOPPED
```

`graceful_shutdown(drain_timeout_ms)` walks the whole sequence:

1. **STOP_ACCEPTING** — `stop_accepting()` closes the listener. It is a no-op unless
   the phase is still `RUNNING`, so calling it yourself first is harmless.
2. **DRAINING** — `drain(timeout_ms)` polls the pending-request counter every
   millisecond until it reaches zero or the timeout expires. It returns whether the
   drain completed, and logs a warning with the surviving count if it timed out.
   `graceful_shutdown` ignores that result and continues either way.
3. **CLOSING** — every hook registered with `add_shutdown_hook` runs, in registration
   order, while the hooks mutex is held. A hook that throws is caught and logged, and
   the remaining hooks still run.
4. `do_shutdown()` flips the shutdown flag and broadcasts, releasing everyone parked
   in `wait_for_shutdown()`.
5. **STOPPED**.

```cpp srpc-compile-server
auto poll_thread = PollThread::create();
auto server = Server::new_(rusty::Some(poll_thread.clone()));
server.reg_service(rusty::make_box<MyService>());
if (server.start(reinterpret_cast<const int8_t*>("0.0.0.0:8100")) != 0) {
    return;
}

server.add_shutdown_hook([]() { /* flush state, close files, ... */ });

// Stop accepting, wait up to 30s for in-flight requests, run hooks, wake waiters.
server.graceful_shutdown(30000);
poll_thread->shutdown();
```

`phase()` reads the current phase and `shutdown_phase_to_string()` names it for a log
line. `do_shutdown()` on its own only wakes `wait_for_shutdown()` — it stops nothing
— which is exactly what you want from a signal handler that then lets `main` run the
real shutdown. The default drain timeout constant is `kDefaultDrainTimeoutMs`
(30000); C++ has no default argument here, so pass the value.

What the drain actually counts is worth understanding. Every request that gets far
enough to have an xid attaches a `PendingRequestGuard` to itself, which increments a
shared atomic; the guard decrements it when the request object is destroyed. For an
ordinary handler that is when the dispatcher returns. For a `defer` handler the
request lives inside the `DeferredReply`, so the count stays up until that handle is
replied through and destroyed — which is what makes draining meaningful for deferred
work. `increment_pending()` / `decrement_pending()` / `pending_request_count()` are
exposed if you need to park something else on the same counter.

### The Dispatch Path

When the channel layer hands over one request frame (the 4-byte size header is
already stripped), the server does the following, all on the poll thread:

1. If the connection is already `CLOSED`, drop the frame.
2. Copy the payload into the `Request` — the channel contract only guarantees those
   bytes for the duration of the callback, and everything after this point may yield.
3. An empty body has no xid to reply against: log and drop.
4. Read `v64 xid`, then attach the pending-request guard.
5. Fewer than 4 bytes left means there is no `i32 rpc_id`: reply `EINVAL` (22) with an
   empty body.
6. Read the `rpc_id`. If it is `kInternalHeartbeatRpcId` (`i32::MIN`), reply
   immediately with error 0 and an empty body — before any service lookup — unless
   `set_drop_heartbeat_replies(true)` was set, which is a fault-injection knob for
   tests. (The heartbeat protocol is complete on both sides, but nothing currently
   ticks the client-side timer, so in practice these frames rarely arrive.)
7. Look the id up. A miss replies `ENOENT` (2) and logs a warning — once per id for
   the life of the process, so a client hammering a wrong id does not flood the log.
8. Hit: if the id was registered with `reg_fast_rpc`, borrow the service and call
   `__dispatch__` inline. Otherwise spawn a stackful fiber and call it there. The
   fiber starts running immediately and returns to the poll thread when the handler
   finishes or blocks; the fiber's copy of the context `Arc` keeps the services alive
   even if the connection dies mid-flight.

Replies are written by `sconn_reply`: `v64 xid`, `v32 error_code`, `v64
server_instance_id`, then whatever your writer appends. The server always writes that
extended form (chapter 7 has the wire details). The instance id is generated once per
`Server` from time, PID and 64 random bits, masked non-negative because it crosses the
wire signed; clients use it to notice that a server restarted.

Send errors are deliberately not surfaced from `reply()` — the return value of
`send_frame` is discarded. You learn about a dead connection through the channel's
`on_closed` / `on_error` callbacks, which close the `ServerConnection`, not from the
reply call.

### Replying

`ServerConnection::reply(const Request&, i32 error_code, ServerReplyFn)` is the whole
reply API. The writer is a `rusty::Function<void(BinaryWriteArchive&)>`; pass a
default-constructed `srpc::ServerReplyFn{}` for a header-only reply, which is what the
generated code does on the error path.

`DeferredReply` is the handle for answering later. It owns the request, a weak
connection handle, the writer that closes over your response struct, and a cleanup
callback. `reply()` and `reply_error(code)` each fire at most once — the second call
logs and returns — and simply destroying the handle without replying is safe: the
caller just never hears back, and the cleanup callback still runs from the destructor.
If the connection died in the meantime, both paths log and drop the reply.

`run_async()` on either type is not a thread pool. `ServerConnection::run_async`
invokes the callback inline on the calling thread and returns `EINVAL` (22) if you
hand it an empty one; `DeferredReply::run_async` invokes it inline and always returns
0. To actually move work off the dispatch path, keep the `DeferredReply` and reply
from wherever the work completes.

### Dispatch Context and the Single-Thread Contract

`RpcServiceContext` is the immutable object `start()` builds and shares by `Arc` with
every accepted connection. Its definition in `rpc/server.rs` is the honest summary of
the server's concurrency model:

```rust
pub struct RpcServiceContext {
    pub rpc_to_service: HashMap<i32, usize>,       // rpc id -> index into `services`
    pub fast_rpc_ids: HashSet<i32>,                // ids that dispatch inline
    pub services: Vec<RefCell<ServiceProxy>>,      // ServiceProxy = Box<dyn Service>
    pub addr: String,
    pub pending_requests: Arc<AtomicI32>,          // what drain() watches
    pub drop_heartbeat_replies: Arc<AtomicBool>,
    pub server_instance_id: u64,
}
```

Every field is const after construction except the interior of those `RefCell`s, which
is how a `const` context can still call a non-const `__dispatch__`. `RefCell` is a
single-threaded cell, and both `RpcServiceContext` and `ServerConnection` are `Send +
Sync` only by an `unsafe impl` in the module whose stated justification is the
single-dispatch-thread contract — not locking. `ServerConnection`'s own mutable state
(`status_`, `channel_mode_`) is `Cell`, written from the dispatch thread and read
elsewhere as a monotone latch. So:

**Do not dispatch into one server from two threads.** One `Server`, one `PollThread`,
one thread running handlers. Two servers can share a poll thread — their service
tables are separate — but one server must not be driven by two.

There is a second, subtler consequence of `Vec<RefCell<..>>`. The borrow is taken
before `__dispatch__` and released only when it returns, so it spans the *entire*
handler — including a fiber suspension. If a default (fiber) handler blocks on a
nested RPC, an event, or a sleep, its service stays mutably borrowed while it is
parked, and a second request routed to the same service in that window cannot take
the borrow — a conflicting `borrow_mut()` fails outright rather than waiting its
turn (rusty-cpp's `RefCell` throws; the Rust build panics). Two ways out, in order of
preference:

- Use `defer` for anything that has to wait. The dispatcher calls your handler, you
  stash the `DeferredReply`, and the handler returns — releasing the borrow — long
  before the answer exists.
- Split independent work across separate service objects, which get separate
  `RefCell`s and separate indices.

And `fast` handlers have the mirror-image constraint: they run inline on the poll
thread with no fiber to yield from, so anything that blocks there stalls every
connection on that thread.

---

## 10. Serialization

There is no `Marshal` class in SRPC. Serialization is three small layers, and the
canonical source for all of them is `misc/serializable.rs` (module
`srpc.serializable`, which `srpc.hpp` pulls in for you):

1. a **sink** or **source** — where the bytes go, or come from;
2. an **archive** — `BinaryWriteArchive` / `BinaryReadArchive`, which owns the wire
   format and nothing else;
3. the **dispatchers** — `srpc::Serialize_::serialize` and
   `srpc::Deserialize_::deserialize`, which pick the encoding for a value's type.

Splitting sink from archive is what lets the same encoder write into a memory
buffer, a file descriptor, or anything else you can express as a sink, without the
wire format knowing which. (If an old comment or a third-party header mentions
`Marshal`, `Marshallable` or `MarshallDeputy`: none of those types exist here.)

### Sinks, sources and archives

Four concrete sinks and sources ship:

| Type | What it is |
|------|-----------|
| `BufferSink` | Appends to `bytes`, a `rusty::Vec<uint8_t>` it owns |
| `BufferSource` | Reads from a borrowed `(const uint8_t*, size_t)` range; `pos()`, `remaining()`, `eof()` |
| `FdSink` | Writes to a raw fd it does **not** own |
| `FdSource` | Reads from a raw fd it does **not** own |

An archive does not take a sink directly; it takes a type-erased *proxy* built by
one of four named free functions — there is no bare `make_source_proxy`:

```cpp srpc-no-compile
srpc::SinkProxy   p1 = srpc::make_sink_proxy_buffer(&buffer_sink);
srpc::SourceProxy p2 = srpc::make_source_proxy_buffer(&buffer_source);
srpc::SinkProxy   p3 = srpc::make_sink_proxy_fd(&fd_sink);
srpc::SourceProxy p4 = srpc::make_source_proxy_fd(&fd_source);
```

Every one of these **borrows**. The proxy holds a pointer to your sink or source;
it does not copy it and does not extend its life. The sink must outlive the archive
built from it, and must not be moved while that archive exists. `FdSink` and
`FdSource` additionally do not own their descriptor — you open and close it.

Both archives are single-field aggregates holding their proxy, so you construct one
by naming the proxy:

```cpp srpc-no-compile
using namespace srpc;

BufferSink sink;
{
    BinaryWriteArchive ar(make_sink_proxy_buffer(&sink));
    i32 answer = 42;
    std::string name = "hello";
    Serialize_::serialize(answer, ar);
    Serialize_::serialize(name, ar);
}   // archive dies here; `sink` still owns the bytes

auto src = BufferSource::new_(sink.bytes.data(), sink.bytes.size());
BinaryReadArchive rd(make_source_proxy_buffer(&src));
i32 answer_back = 0;
std::string name_back;
Deserialize_::deserialize(answer_back, rd);
Deserialize_::deserialize(name_back, rd);
// src.pos() is now the number of bytes consumed; src.eof() says whether any remain.
```

This is exactly the shape the generated code uses: every reply decoder `rpcgen`
emits is a `BinaryReadArchive` built by `make_source_proxy_buffer` over the
`BufferSource` that the client or server already parked the frame body in.

The fd variants are for snapshots and log replay, where you want bytes to land in a
file without an intermediate buffer:

```cpp srpc-no-compile
int fd = ::open("/tmp/snap.bin", O_WRONLY | O_CREAT | O_TRUNC, 0644);
{
    srpc::FdSink sink = srpc::FdSink::new_(fd);
    srpc::BinaryWriteArchive ar(srpc::make_sink_proxy_fd(&sink));
    srpc::Serialize_::serialize(entry, ar);
}
::close(fd);
```

Be aware of what that costs. Neither fd type buffers: every leaf write becomes a
`write(2)` and every leaf read a `read(2)` (`misc/srpc_io.c`, EINTR-retry ladders),
so serializing a thousand-element vector of `i32` is a thousand-and-one syscalls.
For anything but small, occasional payloads, serialize into a `BufferSink` and
write the buffer out yourself.

### Failure model

Nothing in this layer returns an error. `BinaryReadArchive::read_exact(p, n)`
returns a `bool`, but every built-in decoder wraps it in `verify()` — the
fixed-width integer and `double` leaves go through `read_or_abort`, while the `v32`,
`v64` and `std::string` leaves call `verify(read_exact(...))` themselves (the varint
ones twice: one byte, then the tail length that byte selects). Either way a truncated
or desynchronised stream prints a stack trace and panics. `FdSink` aborts on any
write error that is not `EINTR`, `SerializableRegistry::create` panics on an
unregistered kind, and `AnyMessage::load` panics on an unregistered type name.

Decoders also trust the length prefix: `std::vector` and `rusty::Vec` `reserve` the
decoded count before reading a single element, and `std::string` resizes to the
decoded length, so a corrupt prefix is an enormous allocation attempt. The only
upstream guard is the framing layer's 64 MiB `kMaxFramePayloadSize` cap. Decode
frames from peers you trust.

### Supported types

| Type | Wire form |
|------|-----------|
| `i8`, `i16`, `i32`, `i64` | 1 / 2 / 4 / 8 raw bytes, host byte order |
| `u8`, `u16`, `u32`, `u64` | 1 / 2 / 4 / 8 raw bytes, host byte order |
| `double` | 8 raw bytes, host representation |
| `v32` | SparseInt varint, 1–5 bytes |
| `v64` | SparseInt varint, 1–9 bytes |
| `std::string` | `v64` length, then the bytes |
| `std::string_view` | same bytes — **write side only** |
| `std::pair<A, B>` | `A` then `B`, no prefix |
| `std::vector`, `std::list`, `std::set`, `std::unordered_set`, `rusty::Vec`, `rusty::BTreeSet`, `rusty::HashSet` | `v64` count, then each element |
| `std::map`, `std::unordered_map`, `rusty::BTreeMap`, `rusty::HashMap` | `v64` count, then key, value, key, value… |

One exception to that table: the `rusty::HashSet` / `rusty::HashMap` **encoders** must
stay uninstantiated. Any hashbrown enumeration — `iter()`, `begin()`, `drain()` — routes
through the `rusty::iter(table)` dispatcher in `slice.hpp`, whose return-type name
crashes clang-22's Itanium mangler (SIGSEGV in `mangleSourceName`), so serializing one
is a compiler crash rather than a runtime fault. Nothing in the tree does it. The
decoders are insert-only and are safe — they are what the `RustyHashSetPrimitives` /
`RustyHashMapPrimitives` tests exercise. Reach for `rusty::BTreeSet` /
`rusty::BTreeMap` for anything you actually encode.

There is no `float`, no `bool`, and no pointer support. The fixed-width integers
and `double` are written by copying the object's own bytes — no byte-order
normalisation — which is the same native-endian assumption the frame header makes
(Chapter 7). A heterogeneous-endian fleet does not work.

Two decode-side rules follow from the implementations. Element and key/value types
must be default-constructible, because the decoder default-constructs one and then
fills it. And every container is `clear()`ed before decoding, so decoding into a
non-empty container replaces its contents rather than appending. `std::string_view`
has no decoder at all, for the obvious reason.

Unordered containers serialize in iteration order, so the exact byte sequence for a
`std::unordered_map` is not reproducible across runs. It still decodes correctly;
just do not hash or diff the encoded bytes and expect stability.

### `v32` and `v64`

`v32` and `v64` are wrapper structs from `base/basetypes.rs`, not arithmetic types.
You read and write the value with `.get()` and `.set()`, and IDL fields declared
`v32`/`v64` come out as `srpc::v32` / `srpc::v64`.

Both encode with `SparseInt`, a signed varint whose *first byte* announces the
total length via a unary prefix of high bits (`0xxxxxxx` is one byte, `10xxxxxx`
two, `110xxxxx` three, and so on) and carries the value's high bits, sign-extended
on decode. Bytes after the first are the remaining value bytes, most significant
first. Small magnitudes are cheap:

| Value fits in | Bytes |
|---------------|-------|
| −64 … 63 | 1 |
| ±2^13 | 2 |
| ±2^20 | 3 |
| ±2^27 | 4 |
| ±2^34 | 5 — a `v32` never needs more than this |
| ±2^41 | 6 |
| ±2^48 | 7 |
| ±2^55 | 8 |
| anything larger | 9 |

Use a varint when values are usually small and occasionally large. Use fixed `i32`
/ `i64` when they are uniformly distributed — a random 64-bit value costs 9 bytes
as a `v64` and 8 as an `i64`.

One caveat, and it is a real defect rather than a quirk: at exactly length eight,
`SparseInt::dump64` writes a `0xFE` marker plus eight payload bytes but *reports*
eight, so the archive emits only seven of them; the decoder rebuilds the value from
its own zero-filled scratch and the low byte comes back zero. Framing stays in sync
— both sides agree the field is eight bytes — but the value does not round-trip.
`tests/basetypes_rust.rs` pins it: `36_028_797_018_963_967` decodes as
`36_028_797_018_963_712`. It affects magnitudes between roughly 2^48 and 2^55.
`v32` is unaffected at every length. Chapter 7 covers what this does, and does not
do, to the protocol's own `v64` fields.

### Your own types

A type becomes serializable by having a `serialize` / `deserialize` free-function
pair in **its own namespace**. Nothing is inherited and nothing is registered:

```cpp srpc-no-compile
namespace benchmark {

struct point3 {
    double x;
    double y;
    double z;
};

inline void serialize(const point3& o, srpc::BinaryWriteArchive& ar) {
    srpc::Serialize_::serialize(o.x, ar);
    srpc::Serialize_::serialize(o.y, ar);
    srpc::Serialize_::serialize(o.z, ar);
}

inline void deserialize(point3& o, srpc::BinaryReadArchive& ar) {
    srpc::Deserialize_::deserialize(o.x, ar);
    srpc::Deserialize_::deserialize(o.y, ar);
    srpc::Deserialize_::deserialize(o.z, ar);
}

}  // namespace benchmark
```

That is not an illustration: it is the in-tree generated header
`tests/benchmark_service.h`, and it is what `rpcgen` emits for every `struct` in a
`.rpc` file (`emit_struct` in `pylib/simplerpcgen/lang_cpp.py`). The generator also
drops a one-line `operator<<` / `operator>>` forwarder next to each function. Those
operators exist **only** for generated structs — there is no generic `ar << 42` for
primitives — so call the dispatchers directly.

The dispatch is deliberately open-set. `Serialize_::serialize` poisons ordinary
lookup — `misc/serializable_support.hpp` declares, and never defines, a
`void serialize();` in the detail namespace — and then makes a dependent unqualified
call. Only an overload reachable by argument-dependent lookup, from your type's
namespace or the archive's, can satisfy it. Practical consequences: put the pair in
the same namespace as the type (or write them as hidden friends, which is what
`rpcgen` does for the request/response structs it nests inside a service class),
and expect a compile error at the point of use, not a link error, if you forget.

If you need this for a type the IDL cannot express, write the pair by hand in your
`.rpc` file's header section (the text above the first `%%` line, Chapter 12) and
then use the type by name in your method signatures. That section is emitted *outside*
the generated namespace, at global scope, so declare the type there too — a pair
written at global scope for a type the IDL declares inside `namespace demo` is not
reachable by ADL.

### Polymorphic payloads

Everything above is static: both sides know the type. When the receiver must learn
the type from the bytes, you need a payload that can be constructed by tag, and
that is what `SerializableProxy` is — `rusty::Arc<SerializableBase>`, an owning
handle to a type-erased payload with `save`, `load`, `kind` and a `TypeId`.

A type qualifies by being default-constructible and providing three members:

```cpp srpc-no-compile
struct GraphPayload {
    static constexpr srpc::i32 kKind = 60;

    srpc::i32 node_count{0};
    std::string label;

    void save(srpc::BinaryWriteArchive& ar) const {
        srpc::Serialize_::serialize(node_count, ar);
        srpc::Serialize_::serialize(label, ar);
    }
    void load(srpc::BinaryReadArchive& ar) {
        srpc::Deserialize_::deserialize(node_count, ar);
        srpc::Deserialize_::deserialize(label, ar);
    }
    srpc::i32 kind() const { return kKind; }
};
```

`kind()` must exist because the holder that wraps your type implements the whole
`SerializableBase` interface, but whether its value reaches the wire depends on
which envelope carries the payload — the name-tagged one below ignores it. No base
class is involved; the holder does the type erasure, and it remembers
`TypeId::of<T>()` so unpacking is checked rather than a blind cast.

The kind-keyed registry lives in the same module:

```cpp srpc-no-compile
namespace srpc {
struct SerializableRegistry {
    template <typename T> static i32 reg(i32 kind);  // returns 0, for static init
    static SerializableProxy create(i32 kind);       // panics if kind is unregistered
    static bool is_registered(i32 kind);
    static void clear_for_testing();                 // tests only; not thread-safe
};
}
```

`reg` takes the kind explicitly — there is no overload that infers one — and
returns 0 so you can register at static-initialisation time:

```cpp srpc-no-compile
static int reg_graph_payload =
    srpc::SerializableRegistry::reg<GraphPayload>(GraphPayload::kKind);
```

The module also ships a helper base, `Serializable<KIND>`, whose `kind()` and
`static_kind()` return the `KIND` you instantiate it with; inheriting it saves
writing the accessor by hand. Kind `0` is reserved for "unset" and the helper
rejects it.

#### `AnyMessage` — open set, tagged by name

`AnyMessage` (`misc/any_message.rs`, module `srpc.any_message`, **not** in the
`srpc.hpp` umbrella — `import srpc.any_message;`) carries a payload tagged by a
registered string name, so a peer can add types without a central kind allocation.

```cpp srpc-no-compile
import srpc.any_message;

// Anywhere, at static-initialisation time.
static int reg_graph_payload =
    srpc::reg_any_message_as<GraphPayload>("demo.GraphPayload");

// Sender: pack a typed value and put it on an `srpc::AnyMessage` field.
GraphPayload p;
p.node_count = 42;
p.label = "x";
srpc::AnyMessage msg = srpc::AnyMessage::pack<GraphPayload>(
    rusty::Arc<GraphPayload>::make(p));

// Receiver: dispatch on the carried type.
if (msg.is_a<GraphPayload>()) {
    auto opt = msg.unpack<GraphPayload>();   // rusty::Option<rusty::Arc<GraphPayload>>
    auto sp = std::move(opt).unwrap();
}
```

The wire layout is `[v64 length][type name bytes][payload bytes]` — the name *is*
the discriminator, and no numeric kind is written, which is why the payload's
`kind()` value is irrelevant here. `AnyMessage` itself has the ADL
`serialize`/`deserialize` pair, so a struct field of type `srpc::AnyMessage`
encodes like any other field.

Registration details worth knowing: `reg_any_message_as<T>` requires `T` to be
default-constructible, and registering the same *name* twice panics. The first
name registered for a type is the one `pack` uses; registering the same type under
a second name leaves that mapping alone. `pack` panics if the type was never
registered — use `pack_as(name, sp)` to supply the name yourself. Packing stores
your `Arc` rather than copying the value, so later mutations through your handle
are visible to whatever is eventually encoded; `unpack` hands back a clone of that
same `Arc`.

#### `SerializableEnvelope` — closed set, tagged by kind

`SerializableEnvelope<PayloadSet>` (`misc/serializable_envelope.rs`, module
`srpc.serializable_envelope`, also outside the umbrella) is the compact
counterpart: `[v32 kind][payload bytes]`, one to five bytes of tag instead of a
length-prefixed name, at the cost of every participant agreeing on the kind
numbering up front.

Membership is a compile-time property. You declare a tag type for the set and
specialise the `PayloadMember` marker for each member, in `namespace srpc`:

```cpp srpc-no-compile
import srpc.serializable_envelope;

struct MyCommands {};   // a tag type; the set itself has no members at runtime

namespace srpc {
template <>
struct PayloadMember<MyCommands, GraphPayload> {
    static constexpr bool value = true;
    static constexpr int32_t KIND = GraphPayload::kKind;
};
}  // namespace srpc

using MyEnvelope = srpc::SerializableEnvelope<MyCommands>;
```

Only members can be packed or unpacked; anything else fails to compile. The API is
`pack<T>(value)` (copies), `pack_aliased<T>(arc)` (shares), `unpack<T>()` (raw
`const T*`, null on type mismatch), `unpack_shared<T>()` (`Option<Arc<T>>`),
`is_a<T>()`, `has_value()`, `kind()`, and `save`/`load`. `save` panics on an empty
envelope.

One sharp edge: the kind `save` writes comes from the payload's own `kind()`
member, and `load` looks the decoded kind up in `SerializableRegistry` — neither
reads `PayloadMember::KIND`. Three things must agree: the marker's `KIND`, what
`T::kind()` returns, and the kind `T` was registered under. Nothing checks that for
you.

No payload set ships in this repository. The envelope is a mechanism; the set is
yours to declare.

### What this format is not

The encoding is positional and untagged. There are no field numbers, no optional
fields, no length-delimited records to skip over, and no version marker anywhere in
a payload. A decoder reads the fields your struct declares, in declaration order,
and takes whatever bytes are next. Adding, removing or reordering a field is a
wire-format break, and a mismatched pair of peers will not report a type error —
it will decode nonsense, or run off the end of the frame and panic. Roll both sides
together, or carry your own version field as the first member and branch on it.

---

## 11. Reliability Features

Everything in this chapter lives on the client side. A `ClientConnection` embeds the
reliability machinery by value — a `ConnectionStateMachine`, a `ReconnectPolicy`, a
`CircuitBreaker`, a `HeartbeatManager`, a `RequestQueue` and a `ConnectionMetrics` are
all fields of that one struct — and `Client` is a thin front for whichever connection
it currently holds. The canonical sources are `rpc/connection_state.rs`,
`rpc/reconnect_policy.rs`, `rpc/circuit_breaker.rs`, `rpc/heartbeat.rs`,
`rpc/request_queue.rs`, `rpc/connection_metrics.rs`, `rpc/errors.rs` and the parts of
`rpc/client.rs` that wire them together.

Four of them are **not** in the `srpc.hpp` umbrella, and neither is
`srpc.load_balancer`. If you name their types you have to import them yourself:

```cpp srpc-no-compile
#include "srpc.hpp"

import srpc.reconnect_policy;     // ReconnectPolicy, ReconnectCalculator
import srpc.circuit_breaker;      // CircuitBreakerConfig, CircuitBreaker, CircuitState
import srpc.heartbeat;            // HeartbeatConfig, HeartbeatManager
import srpc.connection_metrics;   // ConnectionMetrics
import srpc.load_balancer;        // LoadBalancingStrategy
```

`srpc.connection_state`, `srpc.errors` and `srpc.request_queue` *are* in the umbrella,
and so is `srpc.client`, which is where `BufferingConfig`, `DisconnectBehavior`,
`KeepaliveConfig` and `PoolConfig` actually live.

### Shipping status

Some of this is finished and load-bearing; some of it is a complete data structure with
nothing driving it. The distinction matters more than the feature list, so here it is
up front.

| Capability | Status | Notes |
| --- | --- | --- |
| Connection state machine | **Works** | `ConnectionStateMachine` validates every transition; `force_state` bypasses the check and is what error paths use. |
| Auto-reconnect with backoff and jitter | **Works** | `ReconnectPolicy` + `ReconnectCalculator`. Fires from `handle_error` and from the channel-close fan-out, on a detached thread. On by default. |
| Lifecycle callbacks | **Works** | `add_on_connected` / `add_on_disconnected` / `add_on_error` / `add_on_reconnecting` / `add_on_reconnected`, dispatched by `CallbackManager` with each invocation wrapped in `catch_unwind`. |
| Circuit breaker | **Works**, off by default | Gates every request through `allow_request_with_circuit_metrics`. `Client`'s staged default is `CircuitBreakerConfig::disabled()`. |
| Server-restart detection | **Works** | Every reply carries a `server_instance_id`; a change fires the callback. But `set_on_server_restart` only reaches a live connection — see below. |
| Per-connection counters | **Works** on `ClientConnection` | `requests_sent` / `_completed` / `_failed`, in-flight, byte counts, reconnects, circuit transitions. |
| Latency metrics | **Not wired** | `record_request_completed_with_latency` has no caller, so `avg_latency_us`, `min_latency_us` and `max_latency_us` are always 0. |
| `Client::metrics()` | **Inert** | Returns a per-`Client` all-zero `ConnectionMetrics` that nothing ever writes. Read the connection's instead. |
| Request buffering while disconnected | **Partial** | Requests are parked with a TTL and then *expired*. `replay_pending_requests` returns 0 — nothing is ever re-sent. |
| Heartbeat | **Partial** | The protocol is complete end to end and the server answers probes, but nothing ticks the client-side send timer, so no probe is ever emitted. |
| TCP keepalive | **Not wired** | `set_keepalive` stores a `KeepaliveConfig`; `apply_keepalive_options` is an empty function. No socket option is set. |
| Pool health checks, `LEAST_CONNECTIONS`, `LEAST_LATENCY` | **Inert** | All three read `Client::metrics()`. A connected client is always judged healthy, nothing is evicted, and both "smart" strategies always pick index 0. |

### What is staged and what is not

`Client` keeps four pending configs and pushes them onto the connection inside
`connect`: keepalive, heartbeat, circuit breaker and reconnect policy. Setting them
*before* `connect` is the reliable order. Each of those four setters also forwards to a
live connection when there is one, so setting them afterwards takes effect immediately
as well — and that connection object survives auto-reconnect, so nothing is lost there.
What the staged copies decide is how the *next* `Client::connect` builds its connection.

Two things are **not** staged. `Client::set_buffering_config` and
`Client::set_on_server_restart` both look at the current connection and silently do
nothing when there isn't one, so they have to be called *after* `connect`.

The staged defaults are deliberately quiet, and they are not the same as the `defaults()`
presets:

| Config | `Client`'s staged default | What `defaults()` gives you |
| --- | --- | --- |
| `ReconnectPolicy` | `conservative()` — auto-reconnect **on**, 5 retries, 1s → 30s, ×2, jitter | `new_()` is the same thing |
| `HeartbeatConfig` | `disabled()` | **enabled**: 10s interval, 5s timeout, 3 missed |
| `CircuitBreakerConfig` | `disabled()` | **enabled**: 5 failures to open, 3 successes to close, 30s |
| `KeepaliveConfig` | `new_()` — enabled, 60s idle / 10s interval / 5 probes (but inert) | there is no `defaults()`; the presets are `new_()`, `aggressive()`, `relaxed()`, `disabled()` |

So heartbeats and the breaker are off until you ask for them, and asking for them with
`defaults()` turns them on:

```cpp srpc-compile-client
client->set_reconnect_policy(ReconnectPolicy::conservative());
client->set_heartbeat(HeartbeatConfig::defaults());
client->set_circuit_breaker(CircuitBreakerConfig::defaults());

client->connect(reinterpret_cast<const int8_t*>("127.0.0.1:8080"), true);

// Not staged — set_buffering_config and set_on_server_restart reach the
// connection directly, so they must come after connect() or they are
// silently dropped.
client->set_buffering_config(BufferingConfig::defaults());
```

### Connection state machine

Six states, and `ConnectionStateMachine::is_valid_transition` is the whole rulebook:

```
  NEW           -> CONNECTING
  CONNECTING    -> CONNECTED | FAILED | DISCONNECTED
  CONNECTED     -> DISCONNECTING | FAILED
  DISCONNECTING -> DISCONNECTED | FAILED
  DISCONNECTED  -> CONNECTING
  FAILED        -> CONNECTING
```

Stated as rules: `NEW` may only become `CONNECTING`. `CONNECTING` may become
`CONNECTED`, `FAILED` or `DISCONNECTED`. `CONNECTED` may only become `DISCONNECTING`
or `FAILED`. `DISCONNECTING` may become `DISCONNECTED` or `FAILED`. Both terminal
states, `DISCONNECTED` and `FAILED`, may only go back to `CONNECTING`. Nothing ever
returns to `NEW`.

`transition_to` refuses an invalid move and returns `false`. `force_state` skips the
check, and that is what the failure paths use — `handle_error` and the channel-close
fan-out force `FAILED` from wherever they were. Both notify the state-change callback.

The state lives in a `rusty::Cell<ConnectionState>`. That is single-threaded interior
mutability, not a synchronization primitive; the module's contract is that a
connection's `Cell` fields are written from its own poll thread.

### Automatic reconnection

```cpp srpc-compile
auto policy = ReconnectPolicy::new_();
policy.auto_reconnect = true;       // false disables reconnection entirely
policy.max_retries = 10;            // 0 means retry forever
policy.initial_delay_ms = 100;
policy.max_delay_ms = 30000;
policy.backoff_multiplier = 2.0;
policy.jitter_enabled = true;
```

Four presets: `new_()` and `conservative()` are identical (5 retries, 1000 ms initial,
30000 ms cap, ×2, jitter on); `aggressive()` retries forever with a 100 ms initial delay,
a 5 s cap and ×1.5; `no_retry()` clears `auto_reconnect`.

`ReconnectCalculator` computes the schedule. `next_delay_ms` multiplies the initial
delay by `backoff_multiplier` once per prior attempt, clamps at `max_delay_ms`, and —
if jitter is on — scales the result by a uniform factor in `[0.5, 1.5)`. Note that
jitter can therefore push a delay *above* `max_delay_ms`; the clamp happens first.
`peek_delay_ms` gives you the un-jittered value without consuming an attempt.

Reconnection is triggered for you. When a transport error or a channel close arrives and
the close was not user-initiated, the connection forces `FAILED`, invalidates every
pending future, fires `on_disconnected`, and — if `auto_reconnect` is set and it has a
remembered address — spawns a detached thread that runs the retry loop. The first
attempt is immediate; subsequent attempts follow the backoff schedule. You can also
drive it by hand with `Client::reconnect(callback)` or `Client::try_reconnect_if_needed()`,
which reconnects only from `FAILED` or `DISCONNECTED`.

In-flight requests do not survive. Every entry in the pending-future map and every
async callback slot is resolved with `ENOTCONN` (107) before the reconnect starts.

### Circuit breaker

The usual CLOSED / OPEN / HALF_OPEN pattern, checked on every request before anything
is serialized.

```cpp srpc-compile
auto cb = CircuitBreakerConfig::defaults();  // enabled: 5 / 3 / 30000
cb.failure_threshold = 5;     // consecutive failures before opening
cb.success_threshold = 2;     // successes in HALF_OPEN before closing
cb.timeout_ms = 5000;         // how long OPEN lasts before a probe is allowed
```

Presets: `new_()` / `defaults()` (5, 3, 30 s), `sensitive()` (3, 5, 60 s), `relaxed()`
(10, 2, 15 s), `disabled()`.

In `CLOSED` the breaker counts *consecutive* failures — any success resets the counter
to zero — and opens at `failure_threshold`. In `OPEN` every request is rejected until
`timeout_ms` has elapsed since the last failure, at which point the next
`allow_request` flips the state to `HALF_OPEN` and lets exactly one probe through.
A success in `HALF_OPEN` counts toward `success_threshold`; reaching it closes the
breaker. A single failure in `HALF_OPEN` sends it straight back to `OPEN` and restarts
the timeout.

Exactly eight error codes trip it: `ENOTCONN` (107), `ECONNREFUSED` (111), `ECONNRESET`
(104), `ECONNABORTED` (103), `ETIMEDOUT` (110), `EHOSTUNREACH` (113), `ENETUNREACH` (101)
and `EPIPE` (32). `should_trip_circuit_for_error` tests the integer, not where it came
from, and `record_circuit_result` runs it over the error code of *every* reply — so an
application error code returned by a handler counts against the breaker exactly like a
transport failure whenever its numeric value lands in that set. A handler returning
`Err(110)` to mean "your deadline, not mine" is indistinguishable from a real
`ETIMEDOUT`. Any other non-zero code is recorded as a *failure of the request* —
`requests_failed` goes up — and never touches the breaker. So pick application error
codes outside 32, 101, 103, 104, 107, 110, 111 and 113; chapter 17 works the consequence
through.

A rejected request comes back as `Err(16)` — `EBUSY` — which the error-callback path
maps to `RpcError::CIRCUIT_OPEN`. Every rejection bumps `circuit_open_rejections`, and
each state change bumps the matching `circuit_*_transitions` counter.

`Client::circuit_breaker_state()` reports the live state, and returns `CLOSED` when
there is no connection.

### Request buffering while disconnected

When the connection is down, a request can be parked instead of failing immediately:

```cpp srpc-no-compile
BufferingConfig buffering = BufferingConfig::defaults();
buffering.behavior = DisconnectBehavior::QUEUE;   // or FAIL_FAST
buffering.max_pending = 1000;
buffering.default_ttl_ms = 30000;
buffering.overflow = OverflowStrategy::DROP_OLDEST;  // or DROP_NEWEST, FAIL_FAST
client->set_buffering_config(buffering);          // AFTER connect
```

`BufferingConfig` is the `Client`-facing wrapper; it converts to the
`RequestQueueConfig` that `RequestQueue` actually stores (`max_size`, `default_ttl_ms`,
`overflow_strategy`, `enabled`). Setting a new config on a live connection first drains
whatever is queued with `ECONNABORTED` (103).

**Queued requests are never replayed.** This is the single most important thing to know
about the feature. `ClientConnection::replay_pending_requests` returns `0` — the source
comment describes the surviving entries as waiting "for a future replay path". A parked
request has exactly three possible ends:

* **TTL expiry.** `expire_stale` resolves it with `ETIMEDOUT` (110 on Linux, 60 on
  macOS). The sweep is not on a timer: it runs at the head of every new `request()`
  through the channel path, and once more after a successful reconnect.
* **Queue drain.** Closing or dropping the connection resolves everything still queued
  with `ENOTCONN` (107); a `set_buffering_config` on a non-empty queue uses
  `ECONNABORTED` (103).
* **Overflow.** With `DROP_OLDEST` the front entry is evicted with `EAGAIN` (11) to make
  room for the new one. `DROP_NEWEST` and `FAIL_FAST` are the same code path in
  `RequestQueue::enqueue`: both reject the *incoming* request with `EAGAIN` rather than
  touching the queue, and `request()` hands that back as `Err(11)` instead of a future.

Either of the first two resolves the future you were handed with that error code and
bumps `queue_dropped_requests`. Note also that `Future::wait()` is hard-capped at one
second and latches `ETIMEDOUT` when it fires, so a synchronous caller waiting on a
request parked with a 30-second TTL times out long before the queue ever looks at it.

With `DisconnectBehavior::FAIL_FAST`, or with `enabled` false, a request made while
disconnected returns `Err(107)` immediately — which is usually what you want until the
replay path exists. `request_async` never buffers at all: it fails with `Err(107)` the
moment it finds the connection down, whatever the buffering config says.

### Heartbeat / keep-alive

```cpp srpc-compile
auto hb = HeartbeatConfig::defaults();  // enabled: 10s interval, 5s timeout, 3 missed
hb.interval_ms = 5000;
hb.timeout_ms = 2000;
hb.max_missed = 2;
```

Presets: `new_()` / `defaults()` (10 s / 5 s / 3), `aggressive()` (5 s / 2 s / 2),
`relaxed()` (30 s / 15 s / 5), `disabled()`.

The protocol itself is finished. A probe is a normal request body — `v64 xid` followed
by `i32 rpc_id` where the id is `kInternalHeartbeatRpcId` (`i32::MIN`) — and the server
recognizes it in its dispatch path and replies with error code 0 and an empty payload.
`HeartbeatManager` tracks `pending_pong`, counts misses, and fires its timeout callback
once `max_missed` consecutive probes go unanswered. The receive side is live:
`on_pong_received` is called for *every* inbound reply, not just probes, so ordinary
traffic keeps the manager healthy.

What is missing is the tick. The logic that decides to send a probe lives in
`ClientConnection::check_pending_write_update`, and the reactor never calls it — the
pollable it has registered is the TCP connection's own shim, whose
`check_pending_write_update` just swaps a dirty flag. Nothing else calls
`should_send_heartbeat`. So with the current wiring the client emits no probes and
never observes a heartbeat timeout. Configure it if you like; do not depend on it to
detect a silent peer.

`set_keepalive` is a separate thing — OS-level TCP keepalive — and it is also inert:
the config is stored, and `apply_keepalive_options` is an empty function body.

### Connection metrics

`Client::metrics()` returns a reference to a per-`Client` `ConnectionMetrics` that
nothing ever writes to. It is a fallback for the no-connection case that ended up being
returned unconditionally, so it reads as all zeros forever. Do not use it.

The live counters are on the connection, reached through `Client::connection()`, which
returns an `Option`:

```cpp srpc-compile-client
auto conn_opt = client->connection();
if (conn_opt.is_some()) {
    auto conn = conn_opt.unwrap();
    const ConnectionMetrics& m = conn->metrics();

    auto sent          = m.requests_sent();
    auto completed     = m.requests_completed();
    auto failed        = m.requests_failed();
    auto in_flight     = m.in_flight_requests();
    auto bytes_out     = m.bytes_sent();
    auto bytes_in      = m.bytes_received();
    auto reconnects    = m.reconnect_count();
    auto success_pct   = m.success_rate_percent();
    auto queue_drops   = m.queue_dropped_requests();
    auto circuit_rejects = m.circuit_open_rejections();
    auto circuit_open  = m.circuit_open_transitions();
}
```

Every field is a relaxed `AtomicU64`. Two caveats on the numbers themselves:

* The latency fields — `avg_latency_us`, `min_latency_us`, `max_latency_us` — are always
  zero. They are fed by `record_request_completed_with_latency`, which has no caller;
  the completion path uses the plain `record_request_completed`.
* `requests_timed_out` and `retry_attempts` are only recorded by the
  `request_with_options` retry coordinator. A plain `request` that times out does not
  touch either.

Because `ClientPool`'s health check and its `LEAST_CONNECTIONS` / `LEAST_LATENCY`
strategies all read `Client::metrics()`, they read zeros: `requests_sent` is always
below `min_requests_for_health`, so any connected client is judged healthy and nothing
is ever evicted, and both strategies always land on index 0. `RANDOM` and
`ROUND_ROBIN` are unaffected.

### Connection callbacks

Callbacks are registered on the `Client` and held by a `CallbackManager` that the
connection shares. They accumulate — `add_*` appends rather than replaces — and each
invocation is wrapped so a throwing callback cannot take down the dispatch.

```cpp srpc-compile-client
client.add_on_connected([]() { /* connect succeeded */ });
client.add_on_disconnected([]() { /* channel closed or transport error */ });
client.add_on_error([](RpcError err, const std::string& msg) { /* ... */ });
client.add_on_reconnecting([]() { /* a reconnect attempt is starting */ });
client.add_on_reconnected([](bool success) { /* attempt finished */ });
```

`on_connected` fires from the successful connect path. `on_error` and `on_disconnected`
fire from `handle_error` and from the channel-close fan-out, and both are suppressed
there when the close was user-initiated. That makes `on_error` genuinely silent on a
close you asked for; `on_disconnected` is not. `ClientConnection::close()` has its own
unconditional fan-out at the tail: it invokes the disconnected callback whenever it
entered from `CONNECTED` or `DISCONNECTING`, and `DISCONNECTING` is exactly the state
`Client::close()` leaves behind — it calls `mark_closing()` (`CONNECTED` →
`DISCONNECTING`) and then runs `conn.close()` on the poll thread. `Client`'s destructor
calls `close()` too, so dropping a connected client also fires `on_disconnected`. If you
use it to trigger failover, gate it on your own shutdown flag. `on_reconnecting` and
`on_reconnected` bracket the reconnect loop, and `on_reconnected(false)` is what you get
when the retries are exhausted or cancelled. `clear_connection_callbacks()` drops them
all and waits for in-flight dispatches to finish.

### Server-restart detection

Every reply carries a `v64 server_instance_id`, generated once per `Server` from the
clock, a random draw and the pid. The client records the first one it sees and compares
on every subsequent reply; a change means the peer process was replaced.

```cpp srpc-no-compile
// After connect() — this setter no-ops when there is no connection.
client->set_on_server_restart([](uint64_t old_id, uint64_t new_id) {
    // invalidate caches, re-register leases, re-open sessions...
});
```

`Client::server_instance_id()` returns the last id seen, or 0 when there is no
connection. The callback does not fire on the first reply, only on a change.

### Error types

`RpcError` in `srpc.errors` is the *classification* vocabulary used by the error
callback and by the helper predicates. It is not what a handler returns — handlers
return `rusty::Result<Resp, srpc::i32>` with a plain integer — and the client's own
request functions return integer errno values too. `clientconn_map_system_error`
translates between them.

The enum is banded by hundreds, and `get_error_category` is nothing more than a range
check on the numeric value:

| Band | Category | Values |
| --- | --- | --- |
| 0 | `NONE` | `OK` |
| 100–199 | `CONNECTION` | `NOT_CONNECTED`, `CONNECTION_REFUSED`, `CONNECTION_RESET`, `NETWORK_UNREACHABLE`, `HOST_UNREACHABLE`, `CONNECTION_CLOSED`, `CIRCUIT_OPEN` |
| 200–299 | `PROTOCOL` | `INVALID_MESSAGE`, `UNKNOWN_RPC_ID`, `MARSHALLING_ERROR`, `VERSION_MISMATCH`, `CHECKSUM_ERROR` |
| 300–399 | `APPLICATION` | `RPC_FAILED`, `SERVICE_UNAVAILABLE`, `PERMISSION_DENIED`, `INVALID_ARGUMENT`, `NOT_FOUND`, `ALREADY_EXISTS` |
| 400–499 | `TIMEOUT` | `CONNECT_TIMEOUT`, `REQUEST_TIMEOUT`, `RESPONSE_TIMEOUT`, `IDLE_TIMEOUT`, `HEARTBEAT_TIMEOUT` |
| 500+ | `INTERNAL` | `UNKNOWN_ERROR`, `OUT_OF_MEMORY`, `INVALID_STATE`, `INTERNAL_ERROR` |

Note that `CIRCUIT_OPEN` is banded as a *connection* error, not an internal one.

The helpers alongside it are `rpc_error_to_string`, `rpc_error_category_to_string`,
`get_error_category`, `is_connection_error`, `is_timeout_error` and
`is_retryable_error` — the last of which whitelists exactly `CONNECTION_RESET`,
`NETWORK_UNREACHABLE`, `HOST_UNREACHABLE`, `CONNECT_TIMEOUT`, `REQUEST_TIMEOUT`,
`RESPONSE_TIMEOUT` and `SERVICE_UNAVAILABLE`.
It is a classification helper only: nothing in the request path consults it, and retry
behaviour is driven by `RequestOptions` (`max_retries` *and* `idempotent`) instead.

There is no RPC-specific exception class. SRPC reports failures as values.

`TimeoutType` — `NONE`, `CONNECT_TIMEOUT`, `REQUEST_TIMEOUT`, `RESPONSE_TIMEOUT`,
`TOTAL_TIMEOUT` — is a separate enum in `srpc.request_options`, and it is what
`Future::get_timeout_type()` reports after a `request_with_options` call gives up.

---

## 12. Service Definition and Code Generation

You do not write SRPC's wire code by hand. You describe a service in a small `.rpc`
file, run a Python generator over it, and get back a single C++ header containing a
typed server base class and a typed client proxy. This chapter is about that file, that
generator, and exactly what comes out the other end.

The fullest worked example in the tree is `tests/benchmark_service.rpc` together with
its committed output `tests/benchmark_service.h` and the out-of-line handler definitions
in `tests/benchmark_service.cc`. Nothing in the CMake build compiles any of the three —
codegen is not wired into the build at all — but they are the reference for what the
generator actually emits, and everything below was read out of them and out of
`pylib/simplerpcgen/`.

### The service definition language

A `.rpc` file is an optional namespace declaration, then any number of struct and
service declarations in any order:

```
namespace demo

struct point3 {
    double x;
    double y;
    double z;
};

abstract service Demo {
    sayhi(string hi);
    sum(i32 a, i32 b, i32 c | i32 result);
    fast dot_prod(point3 p1, point3 p2 | double v);
    defer slow_echo(string msg | string echoed);
};
```

The namespace may be qualified (`namespace demo::inner`), and it wraps the generated
structs and service classes — but only those; the `%%` sections described below sit
outside it. Structs become plain C++ structs with serialize/deserialize functions
attached; you can nest them and use them as parameter types anywhere.

Inside a service, each line is one method: an optional dispatch attribute, the method
name, and a parenthesized signature whose input and output parameter lists are separated
by `|`. The `|` is optional, so a method may have inputs only (`sayhi`), outputs only
(`nop( | i32 status)`), or neither. Parameter *names* are optional too — `fast nop(string)`
is legal, and the generator invents a name for you (see below).

Four rules about types are worth knowing before you write anything:

**Integers must carry an explicit size.** The type keywords are `i8`, `i16`, `i32`,
`i64`, and the varint-encoded `v32` and `v64`; they become `srpc::i8` … `srpc::v64` in
the generated header. Writing `bool`, `int`, `unsigned` or `long` is a hard parse error
whose message tells you to use a sized type instead. This is deliberate — the wire format
has no room for an implementation-defined `int`. Note that `v32`/`v64` come out as the
`srpc::v32` / `srpc::v64` wrapper structs rather than arithmetic types, so you read and
write them with `.get()` and `.set()`.

**Eight names get a `std::` prefix for free.** `pair`, `string`, `map`, `list`, `set`,
`vector`, `unordered_map` and `unordered_set` are rewritten to `std::pair`,
`std::string`, and so on. Templates nest, so `map<i32, vector<string>>` becomes
`std::map<srpc::i32, std::vector<std::string>>`.

**Every other name passes through untouched.** That is how `point3` above works, and how
you reach your own types — including qualified ones like `::mylib::Blob`. Floating-point
types are in this bucket: `double` and `float` are not keywords, they are just symbols
the generator copies verbatim — but only `double` actually serializes. There is no
`float` encoder or decoder in `misc/serializable.rs`, so a `float` field parses fine and
then emits a header with no overload for the generated `serialize` call to bind to. Use
`double`. Only an identifier that is *exactly* a reserved word collides; the scanner
takes the longest match, so `integer` and `asyncfoo` parse as ordinary symbols even
though `int` and `async` are keywords.

**Method and parameter names in `__NAME__` form are rejected.** The generator reserves
that shape for the glue it emits (`__reg_to__`, `__dispatch__`, the per-method wrappers),
and raises rather than let you collide with it.

Two lexical quirks round it out. Semicolons are ignored entirely, so use them or don't.
Comments run from `//` to end of line — but they must not be empty: a line containing
exactly `//` matches no token and is a syntax error. Expect no polish from the error
reporting: yapps prints a genuinely useful `line:col: Trying to find one of ...`
diagnostic and then dies inside its own error printer with an unrelated `TypeError`.
Read the first line and ignore the traceback.

### Splicing raw C++ into the header

If the generated header needs an `#include`, a `using namespace`, or a forward
declaration, you supply it with `%%` section markers. The rule is easy to get wrong, so
state it precisely: the generator counts lines that consist of *exactly* `%%`.

With **two** such lines you get three regions — everything above the first marker is
copied near the top of the generated header, everything between the two markers is
parsed as IDL, and everything below the second marker is copied to the bottom of the
header. That is the layout `tests/benchmark_service.rpc` uses.

With **one** marker there is no header section: the text above it is parsed as IDL and
the text below it becomes the footer. With none, the whole file is IDL.

Both spliced regions land *outside* the generated namespace — the header text goes above
the opening brace and the footer below the closing one, as `tests/benchmark_service.h`
shows. So code you splice in is at global scope (or wherever your own `namespace` block
puts it), not in `demo`. That matters most for hand-written `serialize`/`deserialize`
pairs: a pair at global scope for a type declared inside `namespace demo` is invisible
to the ADL dispatch of Chapter 10. Either declare the type in the header section too, so
both sit at global scope, or wrap the pair in your own `namespace demo { ... }`.

```
// this lands at the top of the generated header
#include <math.h>
%%

namespace demo
service Demo { ... };

%%
// this lands at the bottom of the generated header
```

### Choosing how each method is dispatched

The keyword in front of a method name decides how the server runs your handler and,
consequently, what signature the generated virtual has.

| Attribute | Generated handler signature | How it runs |
| --- | --- | --- |
| *(none)* | `rusty::Result<Resp, srpc::i32> m(const Req&)` | in a fiber the server spawns per request — may block or make nested calls |
| `fast` / `prefix` | same | inline on the poll thread, no fiber |
| `defer` | `void m(const Req&, Resp& resp, srpc::DeferredReply defer)` | in a fiber; you reply whenever you like |
| `fiber` | `rusty::Result<Resp, srpc::i32> m(const Req&)` | in a fiber inside the request fiber (see the caveat below) |
| `async` | `rusty::Task<rusty::Result<Resp, srpc::i32>> m(const Req&)` | entered inline on the poll thread, then resumed as a stackless coroutine |
| `raw` | `void m(rusty::Box<srpc::Request>, srpc::WeakServerConnection)` | in a fiber; you decode and reply yourself |

The mechanism behind the table has two halves. The generated `__reg_to__` registers each
method id with the server through either `reg_fast_rpc` (for `fast`, `prefix` and
`async`) or `reg_rpc` (for everything else). Then the server's connection dispatch, in
`rpc/server.rs`, checks whether the incoming id is in the fast set: if it is, the handler
is invoked inline on the poll thread; if it is not, the server spawns a fiber and runs
the dispatch there so the handler is free to yield.

`fast` and `prefix` are literally the same attribute — the grammar maps the `fast`
keyword onto `prefix`. A `fast` handler is the cheapest option, but there is no fiber
under it to yield from, so it must not block: if it does, every connection on that poll
thread stalls. The same warning applies to an `async` handler up to its first suspension
point, because those register through the fast path too.

A `defer` handler receives a `srpc::DeferredReply` by value along with a reference (not a
pointer) to a response struct the generator allocated for it. Fill in the response and
call `defer.reply()`, or `defer.reply_error(code)`, whenever the answer is ready — from a
callback, another thread, a later event. Both fire at most once, and dropping the handle
without replying is safe; the caller simply never hears back.

An `async` handler is a C++20 coroutine returning `rusty::Task<...>`; finish it with
`co_return`. The generated wrapper calls it, then hands the task to
`srpc::reactor_spawn_stackless_task_with_result` on the current reactor with a completion
callback that upgrades the weak connection handle and sends the reply.

The `fiber` attribute is the one to avoid. It registers on the slow path — so the server
has *already* put you in a fiber — and then the generated wrapper spawns a second fiber
inside it. Worse, that wrapper names `Fiber::create_run` unqualified, so the generated
header only compiles if you have spliced a `using namespace srpc;` into your `%%` header
section. The plain default already gives you a fiber; prefer it.

`abstract` is a *service-level* keyword: `abstract service Demo { ... }` makes every
generated handler pure virtual, which is what you want whenever you plan to subclass the
generated class, because then the compiler will not let you forget a method. If you want
just one method pure, leave the service concrete and put a trailing `= 0` on that method
instead:

```
service Demo {
    sum(i32 a, i32 b | i32 result) = 0;   // pure
    sayhi(string hi);                     // declared, you define it out of line
};
```

Be aware of the trap on the other side of that choice: for a non-`abstract` service, the
generated virtuals are declared but never defined, so the class has no key function and
its vtable is never emitted. Subclassing it fails at link time even when you override
everything. Define the virtuals out of line in a `.cc` instead — which is exactly what
`tests/benchmark_service.cc` does for the non-abstract `BenchmarkService`.

### Running the generator

There is no `bin/rpcgen` in this repo. That driver script lived in the upstream checkout
this code was extracted from; both in-tree rpcgen tests still shell out to it and both are
therefore dead. Drive the generator by importing it with `pylib/` on the Python path:

```sh
PYTHONPATH=/path/to/srpc/pylib python3 -c \
  "from simplerpcgen.rpcgen import rpcgen; rpcgen('demo.rpc', ['cpp'])"
```

That writes `demo.h` next to `demo.rpc`. Pass `['cpp', 'python']` to also emit `demo.py`,
a stub for the external `simplerpc` Python package. `rpcgen` takes a third keyword
argument, `archive`, which defaults to `True`; it controls whether the `serialize` /
`deserialize` functions and their `BinaryWriteArchive` / `BinaryReadArchive` operators are
emitted next to each struct. Leave it on. The generated dispatch wrappers call those
functions unconditionally, and every header committed here was generated with it on.

Generation is not part of the build. Run it by hand and commit the output, the way
`tests/benchmark_service.h` is committed here.

One fix-up you will need every time: the generated header opens with
`#include "srpc/srpc.hpp"`, which assumes SRPC sits in a directory named `srpc` on your
include path. For a header generated into a subdirectory of the repo itself, that line
has to become something like `#include "../srpc.hpp"`. The generator rewrites it on every
run, so re-apply the edit after each regeneration. (The committed
`tests/benchmark_service.h` still carries the unedited form — nothing in the build
compiles it, so nothing catches it there.)

### RPC method ids, and how to not break the wire

Each method gets an id from `random.randint(0x10000000, 0x70000000)`. Random ids are only
tolerable because the generator stabilizes them: before writing the header, it *reads the
header it is about to overwrite*, scrapes the `enum` of ids back out of it, and reuses any
id whose `Service.METHOD` key it recognizes. New methods get fresh ids drawn to avoid
collisions with the ones already in use.

Three consequences follow, and each of them is a way to silently break wire
compatibility:

**Never regenerate into a clean directory.** If the old header is gone, there is nothing
to scrape, and every id changes. Nothing fails loudly; the server simply answers
error 2 (`ENOENT`, the server's no-such-handler code) to every call from a peer built
against the old header.

**Renaming a method reassigns its id.** The scrape is keyed on the uppercased method name,
so a rename looks exactly like a new method. Plan for it the way you would plan any
wire-breaking change.

**Never regenerate `pylib/simplerpcgen/rpcgen.py` from `rpcgen.g`.** `rpcgen.py` is the
live generator and has been hand-edited since it was produced by yapps. `rpcgen.g` is a
stale grammar whose epilogue predates all of this: it has no `load_existing_rpc_codes`, no
`existing_codes` argument, and no `archive` flag. Regenerating would quietly drop the id
stabilization described above. (There is no yapps compiler vendored here anyway —
`pylib/yapps/` is runtime-only — so this is a hazard you have to opt into.)

### What the generated header contains

For each service you get two classes: `<Svc>Service` and `<Svc>Proxy`. Everything else is
a *member* of `<Svc>Service`.

Per method, the generator synthesizes one request struct and one response struct from the
input and output lists. The name is `Rpc` + the method name split on `_` with each part
capitalized + `Request`/`Response`. So `dot_prod` gives `RpcDotProdRequest` and
`RpcDotProdResponse`, and `slow_echo` gives `RpcSlowEchoRequest` / `RpcSlowEchoResponse`.
Fields take their names from the IDL parameter names; unnamed parameters fall back to
`in_0`, `in_1`, … and `out_0`, `out_1`, … by position. A method with no outputs still gets
a response struct — an empty one.

Alongside the structs, `<Svc>Service` carries an `enum` of the method ids (uppercased
method names: `SUM`, `DOT_PROD`), the `__reg_to__` registration function, the
`__dispatch__` switch, one private `__<method>__wrapper__` per non-`raw` method that does
the decoding and replying, and the typed virtual for you to override.

For `sum` above, inside `class DemoService`, that is:

```cpp srpc-no-compile
struct RpcSumRequest {
    srpc::i32 a;
    srpc::i32 b;
    srpc::i32 c;
};
struct RpcSumResponse {
    srpc::i32 result;
};

enum {
    SAYHI = 0x1234abcd,   // the real values are random draws in
    SUM   = 0x5678ef01,   // [0x10000000, 0x70000000], stable across regenerations
    // ...
};

// typed service signatures
virtual rusty::Result<RpcSumResponse, srpc::i32> sum(const RpcSumRequest& req) = 0;
```

The structs are nested, so from outside the class they are spelled
`DemoService::RpcSumRequest` — a bare `demo::RpcSumRequest` does not exist. `DemoProxy`
re-exports each of them with a `using`, which is why client code says
`DemoProxy::RpcSumRequest`.

The generated service class has **no base class**. It does not inherit
`srpc::Service`; that interface is satisfied by a type-erasure shim the server wraps you
in. Register with `reg_service_typed`, not `reg_service`:

```cpp srpc-no-compile
svr.reg_service_typed(rusty::make_box<MyDemoService>());
```

### Implementing the server side

Inherit from the generated class and override the typed virtuals. Return `::Ok(resp)` with
the response filled in, or `::Err(code)` with an error code of your choosing; the
generated wrapper does all the (de)serialization and sends the reply.

```cpp srpc-no-compile
class MyDemoService : public DemoService {
public:
    rusty::Result<RpcSumResponse, srpc::i32> sum(const RpcSumRequest& req) override {
        RpcSumResponse resp{};
        resp.result = req.a + req.b + req.c;
        return rusty::Result<RpcSumResponse, srpc::i32>::Ok(resp);
    }

    rusty::Result<RpcDotProdResponse, srpc::i32> dot_prod(const RpcDotProdRequest& req) override {
        RpcDotProdResponse resp{};
        resp.v = req.p1.x * req.p2.x + req.p1.y * req.p2.y + req.p1.z * req.p2.z;
        return rusty::Result<RpcDotProdResponse, srpc::i32>::Ok(resp);
    }

    void slow_echo(const RpcSlowEchoRequest& req, RpcSlowEchoResponse& resp,
                   srpc::DeferredReply defer) override {
        resp.echoed = req.msg;
        defer.reply();     // or defer.reply_error(EAGAIN)
    }
};
```

An `async` method is written as a coroutine on the same class:

```cpp srpc-no-compile
rusty::Task<rusty::Result<BenchmarkService::RpcAsyncNopResponse, srpc::i32>>
BenchmarkService::async_nop(const RpcAsyncNopRequest& req) {
    (void)req;
    co_return rusty::Result<RpcAsyncNopResponse, srpc::i32>::Ok(RpcAsyncNopResponse{});
}
```

### The generated client proxy

`<Svc>Proxy` wraps a `srpc::Client*` and gives every non-`raw` method three things: a
blocking call, an `async_` call, and a per-method future wrapper class named
`<method>TypedFuture` — note that this one uses the raw IDL method name, not the
capitalized form, so `dot_prod` yields `dot_prodTypedFuture`.

```cpp srpc-no-compile
DemoProxy demo(const_cast<srpc::Client*>(cl.get()));

DemoProxy::RpcSumRequest req;
req.a = 1; req.b = 2; req.c = 3;

// Blocking: async_sum(...).unwrap().resolve(), collapsed into one call.
auto result = demo.sum(req);
if (result.is_ok()) {
    printf("1 + 2 + 3 = %d\n", result.unwrap().result);
}

// Non-blocking: get the typed future back immediately.
auto fu = demo.async_sum(req);   // rusty::Result<sumTypedFuture, srpc::i32>
if (fu.is_ok()) {
    auto typed = fu.unwrap();
    // ... issue more calls, do other work ...
    auto resolved = typed.resolve();
    if (resolved.is_ok()) {
        printf("result = %d\n", resolved.unwrap().result);
    }
}
```

`async_<method>` takes an optional second argument, a `srpc::FutureAttr`, which is where a
completion callback goes if you would rather be notified than poll. The typed future
exposes `ready()`, `wait()`, `get_error_code()`, `raw_future()` and `resolve()`; `resolve()`
checks the error code first and returns `Err(code)` without decoding if it is nonzero.

Two things the proxy does *not* have, despite what you might expect from the shape of the
API: there is no `await_<method>` method and the typed future is not awaitable — the
generator emits no `co_await` support on the client side at all. `rusty::Task` appears only
in the *server* handler signature for `async` methods.

Be aware that the blocking form inherits the client's one-second future cap: `resolve()`,
`wait()` and `get_error_code()` all funnel through it, so a slow server yields `Err(110)`
(`ETIMEDOUT`) even if the reply arrives later. For a longer budget you have to leave the
proxy and call `Client::request_with_options` with the method id from the service enum —
see the chapter on timeouts and retries.

### Raw methods keep the old shape

A `raw` method is the escape hatch. The request and response structs are still
synthesized for it — the generator makes them for every method — but nothing uses them:
the handler gets the undecoded `srpc::Request` and a weak connection handle, and does its
own reading and replying.

The proxy side of a `raw` method is the one place in the generated code where the old
pointer-out-parameter style survives. Inputs are passed by const reference, outputs are
passed as pointers, and the sync form returns a bare `srpc::i32` error code rather than a
`rusty::Result`:

```cpp srpc-no-compile
// For the IDL line:  raw fetch(i64 key | string val);
srpc::FutureResult async_fetch(const srpc::i64& key,
                               const srpc::FutureAttr& attr = srpc::FutureAttr());
srpc::i32 fetch(const srpc::i64& key, std::string* val);
```

Everywhere else — every non-`raw` handler, every non-`raw` proxy call — the interface is
one request struct in, one response struct out, and no pointers.

---

## 13. Threading and Synchronization

SRPC has no thread pool, no work-stealing scheduler, and nothing that silently moves your
work onto another core. Concurrency comes from two mechanisms that are easy to keep apart
once you know which is which: **fibers**, which multiplex inside one thread and never run
in parallel, and the **poll thread**, which is exactly one OS thread per `PollThread`
object. Almost everything in this chapter follows from that split.

### One PollThread is one OS thread

`PollThread::create()` spawns a single worker thread and hands back a
`rusty::Arc<PollThread>`. There is no thread count to configure. The handle is an `Arc`, so
several clients and servers can share one thread, or each can have its own:

```cpp srpc-no-compile
auto poll = PollThread::create();               // rusty::Arc<PollThread>
Server svr = Server::new_(rusty::Some(poll.clone()));
auto cl = Client::create(poll.clone());         // rusty::Arc<Client>

// ... run ...

poll->shutdown();   // sends the Shutdown command, then joins the worker
```

The handle is the *only* thing that crosses a thread boundary. Nothing reaches into the
worker's epoll set directly: `add_proxy`, `remove` / `remove_fd`, `request_close`,
`update_mode` and `add` (for a job) each push a `PollCommand` onto an mpsc channel and
return immediately. The worker drains that queue between epoll waits and applies the
commands on its own thread. That is why the reactor suites sleep after registering a
pollable — the call has posted a message, not performed a registration:

```cpp srpc-no-compile
poll->add_proxy(make_pollable_proxy_from_typed_arc(p.clone()));
std::this_thread::sleep_for(std::chrono::milliseconds(50));  // let the worker drain
poll->remove_fd((*p).fd());
```

The one method that is synchronous is `shutdown()`. It latches an atomic so a second call
is a no-op, sends `Shutdown`, and then joins — except when it is called *from* the poll
thread itself, which it detects by comparing thread ids and skips the join rather than
deadlocking. `pollworker_is_on_poll_thread()` answers the same question for your own code.

### Fibers are cooperative, and that is the whole contract

Fibers belonging to one reactor never run simultaneously. A fiber keeps the thread until it
reaches a suspension point, and the suspension points are all explicit:

- `this_fiber::yield()`
- `this_fiber::sleep_us()` / `sleep_ms()` / `sleep_s()` / `sleep_until_us()`
- any event's `wait()` or `wait_timeout()`
- a blocking RPC call issued from inside the fiber, which is an event wait underneath

Between two of those, a fiber runs to completion. So data shared only by fibers of one
reactor needs no lock at all — what it needs is the discipline of never leaving a broken
invariant across a yield.

The sharpest form of that rule involves interior mutability. The reactor's own code is
written to release every `RefCell` borrow guard before it suspends, and says so where it
would otherwise be tempting to hold one: the event-wait path pushes onto each queue as its
own statement so the guard dies at the semicolon, before the yield, and `continue_fiber`
drops its borrow before resuming a fiber precisely because the resumed fiber may call
`create_run()` and re-borrow. If you hold a borrow across a yield in your own handler, the
second borrower panics.

### The reactor is per-thread

`Reactor::get_reactor()` returns the calling thread's `rusty::Rc<Reactor>`, creating it on
first use; `Reactor::get_disk_reactor()` returns a second, independent reactor on the same
thread — the historical disk-I/O slot.
Note the handle type: `Rc`, not `Arc` — a non-atomic refcount, which is itself a statement
that the object is not to be shared.

The reactor does not merely document that. `run_loop`, every stackless-task entry point,
the event wait path, and even the destructor compare `rusty::thread::current_id()` against
the `thread_id_` the reactor stamped at construction, and a mismatch trips a `verify` that
prints a stack trace and does not return.

```cpp srpc-no-compile
auto reactor = Reactor::get_reactor();
reactor->run_loop(/*infinite=*/false, /*do_check_timeout=*/true);
```

In the generated C++ those slots are namespace-scope `thread_local` variables, and they are
public — which is how the fiber suite gives each test a fresh scheduler:

```cpp srpc-no-compile
*srpc::sp_running_fiber_th_.borrow_mut() = rusty::None;
srpc::sp_reactor_th_ = rusty::None;   // drops this thread's reactor
```

One honest caveat about how that is verified. In the canonical Rust the `thread_local`
markers are `#[cfg_attr(any(), thread_local)]` attributes, which rustc never applies —
under `cargo test` they are ordinary process-global statics, and `reactor/reactor.rs` says
in its own header that direct Rust execution is unsupported. Per-thread behavior, races and
teardown are covered by the C++ runtime battery, not by the Rust lane.

### Events belong to the thread that created them

Every event records an `owner_thread_` at construction. `wait()` verifies that the calling
thread owns the reactor *and* that a fiber is running — you cannot wait on an event outside
fiber context. `test()` is the one method written to tolerate a foreign caller, and it
deliberately does less there: the liveness check that upgrades the event's `Weak<Fiber>` runs only when the
caller is the owner, because upgrading mutates a non-atomic `Rc` strong count and would
corrupt it from another thread.

Treat `Reactor`, `Fiber`, every event type, and every `Rc` as thread-bound. There is no
supported way to hand one to another thread.

### The single-dispatch-thread contract

`RpcServiceContext`, `ServerConnection`, `ClientConnection`, `TcpConnection` and
`TcpListener` all carry hand-written `unsafe impl Send` / `unsafe impl Sync`. Those are
assertions, not proofs, and they exist for a concrete reason: the channel layer's callbacks
are `Box<dyn Fn(...) + Send + Sync>` and the reactor's `OneTimeJob` callable is
`Box<dyn FnMut() + Send + Sync>`, so anything captured into them must be `Send` — while the
captured objects are full of `Cell` and `RefCell` state.

The SAFETY notes in `rpc/server.rs` and `rpc/client.rs` state what makes that sound, and it
is the contract you are relying on whether you read them or not: **every `Cell`/`RefCell`
field is written only from the connection's own poll thread**, and other threads either read
them as monotone latches or go through the mutex-guarded slots. Every `__dispatch__` runs on
that one thread.

What that means in practice:

- A `fast` / `prefix` handler runs inline on the poll thread. Blocking there stalls every
  connection on that thread. The same applies to an `async` handler up to its first
  suspension point, and to any channel callback you install.
- A default handler runs in a fiber — on the same thread. It may block and make nested
  calls, but it never runs in parallel with another handler on that server.
- The way to use more cores is more poll threads, not more threads per poll thread.

### What actually crosses threads on a client

A plain blocking call already involves two threads. Your thread serializes the arguments and
registers the pending call; the poll thread reads the reply off the socket and completes it.
The state they share is small and deliberate:

| Shared state | Guard |
| --- | --- |
| `pending_fu_` — xid to `Arc<Future>` | a mutex |
| `pending_cb_slots_` — 16384 async-callback slots, indexed `xid % 16384` | a mutex |
| the xid counter | `AtomicI64` (`fetch_add`) |
| `ConnectionMetrics` — 18 counters | `AtomicU64` each |
| a TCP connection's outbound buffer and callback slots | mutexes |

Everything else on the connection is `Cell`/`RefCell`, and the poll thread is the writer the
SAFETY notes name. The circuit breaker is the exception worth knowing about: it is plain
`Cell` state, but the gate at the head of every request
(`allow_request_with_circuit_metrics`, which can move the state to `HALF_OPEN` and set
`probe_in_progress`) runs on *your* thread, while the reply path updates the same cells from
the poll thread. Its state and counters are unsynchronised across those two threads — read
them as approximate, and do not add state of your own to that pattern.

`Future` itself is a mutex plus a condition variable, so `Future::wait()` is an OS-level
wait, not a fiber suspension: it blocks the calling thread outright. If that thread is the
poll thread that would have delivered the reply, nothing can complete the call. It is also
hard-capped at one second and then latches `ETIMEDOUT`; see chapter 8 for what that means
for long calls.

Do not drive one `Client` from two application threads. `Client::request` takes a `RefCell`
borrow of the connection slot, and that borrow counter is not atomic. Give each application
thread its own `PollThread` and its own `Client` — that is the shape the benchmark client in
`tests/rpcbench.cc` uses (one `PollThread::create()` and one `Client::create()` per client
thread), though note CMake does not build that file.

### Server shutdown is the one sanctioned cross-thread handshake

`do_shutdown()` locks the server's shutdown state, sets the flag, and broadcasts a condition
variable; `wait_for_shutdown()` blocks on the same pair. So a signal handler or a control
thread can stop a server whose main thread is parked:

```cpp srpc-no-compile
// control thread / signal handler
svr.do_shutdown();

// main thread
svr.wait_for_shutdown();
```

`stop_accepting()`, `drain(ms)` and `graceful_shutdown(ms)` are *not* in that category: they
move a plain `Cell` phase field. Call them from the thread that owns the `Server`.

### Stackless tasks: the one designed cross-thread wake

The reactor also drives stackless `rusty::Task` pollers, and those genuinely can be woken
from another thread. The design keeps the reactor itself out of the crossing. A waker is a
`Box<dyn Fn() + Send + Sync>` that holds only heap-allocated, thread-safe pieces: an `Arc`
ticket and an `Arc` ingress consisting of an `accepting` flag and a mutex-guarded pending
queue. Waking pushes the ticket onto that queue; the owning reactor drains it inside
`run_loop`. The `Reactor` pointer never travels.

Teardown flips `accepting` to false, which makes a late foreign wake a defined no-op — and,
because a silently dropped wake is exactly the shape of a client hang, every teardown path
that can strand a waiter also counts the cancellation and logs it at ERROR.

### The primitives that exist

**`SpinLock`** (`base/threading.rs`, exported through the `srpc.hpp` umbrella) is the only
lock SRPC defines itself. `lock()` tries one acquire CAS; on failure it spins up to a
thousand `pause` iterations while the lock still looks held, then falls back to a CAS loop
that sleeps 50 µs between attempts. `unlock()` is a release store. It is one byte,
`Send + Sync`, not recursive, and it has **no guard type** — you pair the calls yourself, or
wrap it in an RAII type of your own.

```cpp srpc-no-compile
SpinLock lock;
lock.lock();
// very short critical section: no yields, no syscalls, no allocation
lock.unlock();
```

There is no `SpinMutex<T>`: no guard-returning lock of any kind ships in the tree.

**The pthread wrappers** are thin, checked shims over caller-owned storage:
`Pthread_mutex_init` / `lock` / `unlock` / `destroy`, `Pthread_cond_init` / `destroy` /
`signal` / `broadcast` / `wait`, and `Pthread_spin_init` / `lock` / `unlock` / `destroy`.
They take real `pthread_mutex_t*`, `pthread_cond_t*`, `pthread_spinlock_t*`, and they
`verify` the return code rather than giving it back — a failure aborts, so there is nothing
to check.

```cpp srpc-no-compile
pthread_mutex_t m;
pthread_cond_t c;
Pthread_mutex_init(&m, nullptr);
Pthread_cond_init(&c, nullptr);

Pthread_mutex_lock(&m);
while (!ready) {
    Pthread_cond_wait(&c, &m);
}
Pthread_mutex_unlock(&m);
```

`cpu_pause()` is exported alongside them for hand-written spin loops.

The mutexes, condition variables and mpsc channels the canonical sources use internally
belong to the rusty runtime, not to SRPC's public surface. For your own code the C++
standard library is the straightforward choice, and it is what the built reactor and fiber
suites actually use (`std::atomic`, `std::this_thread::sleep_for`).

### When to use what

| Situation | Use |
| --- | --- |
| Two fibers on one reactor | nothing — they never run in parallel; just don't yield mid-invariant |
| State shared by a fiber and the poll thread running it | nothing — same thread |
| A flag or counter read from another thread | `std::atomic<T>` |
| A very short critical section across threads | `SpinLock` (no guard type; pair by hand) |
| A longer critical section across threads | `std::mutex` with a standard lock guard |
| Sharing an SRPC handle across threads | `rusty::Arc<T>` — `PollThread`, `Client` and connections are all handed out this way |
| Sharing within one thread | `rusty::Rc<T>` — what `Reactor::get_reactor()` returns; never send it |
| Breaking an ownership cycle | `rusty::Weak` — `WeakServerConnection` is the one that appears in handler signatures |

### Rules

1. **Never move a `Reactor`, `Fiber`, event, or any `Rc` to another thread.** The reactor
   re-checks its owning thread at its main entry points, and `verify` does not return.
2. **Never block on the poll thread.** That covers `fast` / `prefix` handlers, `async`
   handlers before their first suspension, and every channel callback.
3. **Never hold a `RefCell` borrow across a fiber yield.** Scope the guard so it dies before
   the suspension point.
4. **Talk to a poll thread by sending it work.** `PollThread::add` takes a job; the pollable
   and mode methods post commands. Nothing touches the epoll set from outside.
5. **One `Client` per application thread**, each on its own `PollThread`.
6. **`do_shutdown()` is the cross-thread stop button.** `graceful_shutdown()` is not.

---

## 14. Memory Safety (RustyCpp)

SRPC's memory-safety story is not a set of C++ comments that a tool reads back. It is the
build. All 37 production modules are **canonical Rust** files living at their historical
C++ paths under `base/`, `misc/`, `reactor/` and `rpc/`; rustc type-checks, borrow-checks
and auto-trait-checks those files; and the C++23 named modules that make up `libsrpc.a` are
generated by the pinned rusty-cpp transpiler from the same bytes rustc just checked.

That is the whole mechanism, and it is worth being precise about what it does and does not
buy you.

### Where the checking actually happens

Two consumers read the same source files. rustc reaches them through `src/lib.rs`, a
*generated* crate index of `#[path = "../rpc/frame_codec.rs"] pub mod frame_codec;` lines.
rusty-cpp reaches them through one whole-crate invocation over the same manifest:

```sh
rusty-cpp-transpiler --crate Cargo.toml --output-dir <build>/goal0-crate-cpp \
    --cxx-namespace srpc --flat-import-namespace srpc \
    --module-preamble module-preambles.toml --type-map rust-type-map.toml \
    --cpp-module-index cpp-module-index.toml
```

The Rust checks are not advisory. `srpc_goal0_source_gate` runs
`RUSTFLAGS=-Dwarnings cargo test --locked --workspace --all-targets` and
`cargo clippy --locked --workspace --all-targets -- -D warnings`, and the CMake `srpc`
library target *depends on that gate*. A borrow error, a failed auto-trait bound, or a new
clippy warning breaks the C++ build before a single `.cppm` is compiled.

The consequence to keep in mind while reading the rest of this chapter: a change to a `.rs`
file is simultaneously a Rust change and a C++ ABI change.

### `unsafe` is denied, then budgeted

The crate manifest denies it outright:

```toml
[lints.rust]
unsafe_code = "deny"
```

Eight files re-open it at file scope, and they are exactly the ones that live on the
syscall and C++-module boundary: `reactor/reactor.rs`, `reactor/fiber.rs`,
`rpc/client.rs`, `rpc/server.rs`, `rpc/tcp_channel.rs`, `rpc/inmemory_channel.rs`,
`rpc/fiber_channel.rs`, and `misc/any_message.rs`. Everywhere else, each `unsafe` item
carries its own narrow `#[allow(unsafe_code)]` — 125 of them across twenty files.

The split is measurable: of 333 `unsafe { … }` blocks in the canonical sources, 197 sit in
those eight file-scope-allowed modules and the remaining 136 sit under one of those 125
explicit per-item allows. That is the point of the arrangement. `grep -rn 'allow(unsafe_code)'` over
`base/ misc/ reactor/ rpc/` is a complete inventory of the deliberate exceptions, and
adding one is a visible act in review rather than a silent keyword.

The house convention is that an `unsafe fn` gets a `# Safety` doc section stating its
precondition (55 of them today) and an `unsafe` block gets a `// SAFETY:` comment saying
why it holds (138 today).

### Most `unsafe` here is a module boundary, not a memory hazard

This is the part that surprises readers who count the blocks. In the canonical Rust, a call
into another SRPC module goes through the `cpp::` facade — `use rusty as cpp;` — and most
functions on that facade are declared `unsafe fn` for the sole purpose of recording that the
call crosses a C++ named-module boundary rustc cannot see through. Reading the reactor's
thread-local current-fiber slot looks like this:

```rust
/// Return the running fiber's id, or zero outside fiber context.
pub fn get_id() -> u64 {
    // SAFETY: reading the reactor's thread-local current-fiber handle has
    // no caller-side precondition.
    let fiber: Option<Rc<rusty::ReactorFiber>> = unsafe { cpp_reactor::Fiber::current_fiber() };
    if let Some(fiber) = fiber {
        return fiber.id.get();
    }
    0_u64
}
```

There is no pointer arithmetic there and no aliasing question — only a boundary. The
genuinely hazardous `unsafe` is much rarer and concentrated where you would expect it: the
fourteen `unsafe extern "C"` blocks that declare the C seam, the pthread wrappers in
`base/threading.rs`, the raw-pointer paths in `misc/serializable.rs`, and the reactor's
`static mut` model of C++ thread-local storage.

### The assertions rustc cannot check for you

Three categories of statement in these sources are outside what the compiler proves, and
they are where real bugs would live.

**`unsafe impl Send` / `unsafe impl Sync`.** Five types carry them — `RpcServiceContext`,
`ServerConnection`, `ClientConnection`, `TcpConnection`, `TcpListener` — because the channel
callbacks (`Box<dyn Fn(..) + Send + Sync>`) and the reactor's `OneTimeJob`
(`Box<dyn FnMut() + Send + Sync>`) require `Send` captures, while the captured objects hold
`Cell`/`RefCell` state. What makes them sound is the single-dispatch-thread contract of
chapter 13, written out in a SAFETY paragraph above each impl. If you break that contract —
by driving one `Client` from two application threads, say — these `unsafe impl`s become
false and nothing will tell you.

**`#[cfg_attr(any(), …)]` emitter directives.** `any()` is always false, so these attributes
are invisible to `cargo build`, `cargo test` and clippy while being the only way to state a
C++ contract Rust has no syntax for: `thread_local` (all nine in `reactor/reactor.rs`),
`cpp_noexcept`, `cpp_no_fieldwise_ctor`, `cpp_no_auto_traits`, `cpp_abi`. Deleting one is
silent in the Rust lane and changes the emitted module.

**The generated object model is not Rust's.** The clearest instance is
`#[allow(clippy::arc_with_non_send_sync)]` on `Future::create` and `Client::create`: the
emitted C++ `Arc` erases Rust's auto traits, so the lint is correct about the Rust and wrong
about the artifact. Several dozen `#[allow(clippy::…)]` in these sources are pins of exactly
this kind — measured statements that taking the suggestion would change the emitted C++ —
not style waivers.

### The ownership types you actually hold

From a consumer's side the safety model shows up as types, not annotations. You never write
a raw `new` or `delete` against this API.

| Type | Role |
| --- | --- |
| `rusty::Box<T>` | unique ownership — `reg_service_typed(rusty::make_box<T>())`, and a `raw` handler's `rusty::Box<Request>` |
| `rusty::Arc<T>` | shared ownership across threads — `PollThread::create()`, `Client::create()`, server connections |
| `rusty::Rc<T>` | shared ownership within one thread — `Reactor::get_reactor()`, `Fiber` handles |
| `rusty::Weak` / `WeakServerConnection` | a non-owning handle that expires cleanly |
| `rusty::Option<T>` | nullability, made explicit — `Server::new_` takes one; `Client::connection()` returns one |
| `rusty::Result<T, E>` | fallibility, made explicit — a plain, `fast` or `prefix` handler returns `rusty::Result<Resp, srpc::i32>`; an `async` one returns `rusty::Task<rusty::Result<Resp, srpc::i32>>`, while `defer` and `raw` return `void` and reply out of band |

Nullability is the one that changes how code reads. `Client::connection()` hands back an
`Option`, so there is no way to skip the check:

```cpp srpc-no-compile
auto conn = cl->connection();
if (conn.is_some()) {
    printf("%lu\n", conn.unwrap()->metrics().requests_sent());
}
```

### Interior mutability, and how the choice is made

The canonical sources pick between three carriers by exactly one question: who writes it.

```rust
pub struct CircuitBreaker {
    pub config_field: Cell<CircuitBreakerConfig>,   // Copy state, unsynchronised
    pub state_field: Cell<CircuitState>,
    pub failure_count_field: Cell<u32>,
    // ...
}

pub struct RequestQueue {
    pub config_: Cell<RequestQueueConfig>,          // Copy state
    pub queue_: Mutex<VecDeque<QueuedRequest>>,     // touched from more than one thread
}

pub struct ConnectionMetrics {
    pub requests_sent_field: AtomicU64,             // written and read across threads
    pub requests_completed_field: AtomicU64,
    // ... 18 counters, all atomic
}
```

`Cell<T>` for trivially copyable state owned by one thread. `RefCell<T>` for containers and
non-`Copy` values owned by one thread — with the yield discipline from chapter 13, because a
borrow held across a suspension point is a panic waiting to happen. A mutex when two threads
genuinely touch the same structure. Atomics when the whole payload is a counter.

There is no `SpinMutex<T>` in this list, or anywhere else.

### Weak references break the cycles

Three places would otherwise hold an owning reference they must not, and each is fixed the
same way.

An event that a fiber is waiting on stores `RefCell<rusty::rc::Weak<Fiber>>`, downgraded
from the running fiber at wait time — the reactor holds the strong `Rc<Fiber>` in its
registry, so the event never keeps a finished fiber alive. A `ClientConnection` is built
with `Arc::new_cyclic` so that its own `weak_self_` can be captured into the callbacks it
installs on its channel. And `WeakServerConnection` is weak on purpose: it is what a `raw`
handler receives, and what `DeferredReply` holds internally, so a reply attempted after the
connection is gone fails an upgrade instead of writing into freed memory.

### What the borrow-check target does today: nothing

`CMakeLists.txt` still defines `ENABLE_BORROW_CHECKING` (default `OFF`) and, when it is on,
a `borrow_check_srpc` target that fans out over `SRPC_BORROW_SRC`. That variable is set from
`SRPC_INLINE_MODULE_SRC`, and `SRPC_INLINE_MODULE_SRC` is now empty — every module carrier
became canonical Rust. So configuring with `-DENABLE_BORROW_CHECKING=ON` reaches the
`elseif` branch and produces a target whose entire body is:

```
No SRPC files configured for borrow checking
```

Do not reach for it expecting analysis. The borrow checking that happens is rustc's, on
every build, whether you ask for it or not. The switch is a vestige of the era when hand-
written C++ carriers still needed a separate checker pass.

### The inline Rust DSL, and where it survives

One file still carries Rust embedded inside a C++ translation unit:
`reactor/epoll_platform_linux.cc`, the Linux implementation unit for `srpc.epoll_wrapper`
and the only hand-maintained C++ TU in the repository. It holds exactly five DSL blocks. The
pattern is a `#if RUSTYCPP_RUST` block — the source — followed by transpiler-emitted C++
between fences that carry a `rust_sha256` of the Rust they came from:

```cpp srpc-no-compile
#if RUSTYCPP_RUST
fn epoll_event_zeroed() -> epoll_event {
    Default::default()
}
#endif
/*RUSTYCPP:GEN-BEGIN id=epoll_platform_linux.1 version=1 rust_sha256=e64905dc...*/
epoll_event epoll_event_zeroed();

epoll_event epoll_event_zeroed() {
    return rusty::default_like<epoll_event>();
}
/*RUSTYCPP:GEN-END id=epoll_platform_linux.1*/
```

Edit the Rust and regenerate; never edit the C++ between the fences. The hash is what makes
the alternative detectable — a block whose Rust was edited without regeneration is *drift*,
and the compiler sees the stale C++ while the next extraction sees the new Rust. The drift
guard runs in the source gate and can be run alone — it needs the pinned transpiler built
(`cmake --build build --target build_rusty_cpp_transpiler`), and fails closed with
`no transpiler at …` and exit 2 if it is missing:

```bash srpc-no-compile
bash scripts/srpc_dsl_check.sh third-party/rusty-cpp/target/release/rusty-cpp-transpiler
```

It hard-codes the census — exactly this one file, exactly five blocks — and it scans `*.rs`
too, so introducing a DSL block anywhere under `base/ misc/ reactor/ rpc/` fails it until
the script's counts are updated. That is deliberate: the DSL is a shrinking surface, not a
place to add code.

### What backs up the static story at runtime

Static checking of the Rust says nothing about the emitted C++ actually running, so three
other lanes carry that weight. All three need the submodules
(`git submodule update --init --recursive`); without `third-party/googletest`, CMake only
warns and quietly registers four ctest entries instead of twelve, so a green `ctest` is not
by itself proof the battery ran.

**Sanitizers** are a whole-configuration switch, so use a separate build directory:

```bash srpc-no-compile
cmake -S . -B build-asan -G Ninja -DSRPC_SANITIZER=address   # none|address|thread|undefined
```

**The C++ battery** — eight binaries under `ctest -L runtime_battery` — is what actually
exercises thread-local storage, fiber teardown and the poll thread. This matters more than
it sounds: `reactor/reactor.rs` is deliberately not executable as Rust, so a green
`cargo test` proves nothing about the reactor's per-thread behavior.

**Verus** proves functional contracts on two modules today — `misc/stat.rs` and
`rpc/internal_protocol.rs` — against the real sources in place, not an extracted copy:

```bash srpc-no-compile
VERUS_HOME=/path/to/verus-dist scripts/verify_srpc.sh
```

`docs/verification.md` is the standing reference for that lane, including the rule that any
new spec must be shown to go red on a perturbed body before it is believed.

---

## 15. Performance Tuning

There is no benchmark *target*. `cmake --build build --target rpcbench` fails, because
`CMakeLists.txt` never declares one. But the load generator's source survives at
`tests/rpcbench.cc`, and it does build and run — you just have to compile it yourself.

Two obstacles, both mechanical. It pulls `tests/benchmark_service.h`, whose
`#include "srpc/srpc.hpp"` is written for the monorepo layout this repository was
extracted from; point an include directory at a directory containing a symlink `srpc`
back to the repository root and it resolves. And it consumes SRPC's C++ modules, so it
needs a module map: `scripts/emit_module_map.py --modules-json
build/CMakeFiles/srpc.dir/CXXModules.json --build-dir build --output bench.modmap`
produces one. Compile `tests/benchmark_service.cc` and `tests/rpcbench.cc` with
`-std=gnu++23 -stdlib=libc++ -march=native @bench.modmap`, then link them against
`libsrpc.a` and the rusty-cpp archives inside `-Wl,--start-group`.

`rpcbench` is a client/server pair: `-s <addr>` serves, `-c <addr>` drives load. `-m`
picks the dispatch mode, `-n` the duration in seconds, `-t` client threads, `-o`
outstanding requests, `-b` payload bytes, `-w` server worker threads.

### Measured throughput

Loopback, both ends on one host. `nop` RPC, 10-byte payload, 10-second runs, three
trials per mode, mean of the client-reported `avg qps`:

| Mode | Mean qps | Spread |
|---|---:|---|
| `fast` | 1,188,955 | ±1.9% |
| `async` | 995,920 | ±4.8% |
| `defer` | 753,513 | ±0.7% |
| `fiber` (default) | 737,705 | ±3.6% |

Measured 2026-08-29 at commit `24e9246`, on an AMD EPYC 7702P (64 cores / 128 threads,
Linux 6.8.0), Clang 22.1.8, `-O2 -march=native`, load average ~1.3. Invocation:
`rpcbench -s 127.0.0.1:18848 -m <mode> -e 2 -w 16` against
`rpcbench -c 127.0.0.1:18848 -m <mode> -n 10 -b 10 -e 2 -o 1000 -w 16 -t 8`.

The ordering is the one the code predicts. `fast` dispatches inline on the poll thread
and wins by ~60% over the fiber default. `async` gives most of that back without a
stack, since its coroutine only suspends if the handler actually awaits. `defer` and the
plain fiber path both pay for a stackful spawn per request and land together.

Treat these as a shape, not a spec: one box, loopback, one payload size, a handler that
does nothing. Your own driver against your own workload is still the only number that
matters — but the harness above is real, and these are its numbers.

### The Rust lane, measured

Since the rustc lane became executable end to end — first over the in-memory channel,
then over real TCP once the facade grew a real epoll poll thread — the same question can
be asked of it directly. All numbers below are the same host as the table above
(AMD EPYC 7702P), rustc 1.97.1, `-C opt-level=3 -C target-cpu=native`, thin LTO, `i64`
echo through the real wire format (`v64 xid | i32 rpc_id | payload` out, the four-field
reply header back), three trials each. The driver is `rust-inmemory-bench`, kept beside
the rpcbench results outside the repository.

| Metric | Mean | Spread |
|---|---:|---|
| TCP loopback, pipelined (1 driver thread, o=1000) | 286,935 op/s | ±3.0% |
| TCP loopback, pipelined (o=100) | 233,242 op/s | ±1.8% |
| TCP loopback, sequential (one in flight) | 1.12 ms/op | ±1.2% |
| in-memory round trip (synchronous) | 818,500 op/s | ±0.6% |
| wire serialization alone (both directions) | 62 ns/op | ±2.2% |

The comparable C++ number is not the eight-thread 1,188,955 qps in the table above but a
single-client-thread run of the same rpcbench (`-m fast -o 1000 -t 1`), measured in the
same minute on the same box: **344,231 qps ±0.7%**. Same topology, same wire, same
dispatch mode:

> **one driver thread, 1,000 outstanding — Rust 286,935 op/s vs C++ 344,231 qps: the
> rustc lane runs at 83% of the shipped C++.**

Since the two lanes compile the same wire protocol from the same sources, the cleanest
experiment crosses them: the **same rpcbench C++ client binary** against each server, and
the Rust driver against each server, every cell carrying identical traffic — `fast_nop`
(id `0x4b921bd9`), a 10-byte string argument, an empty reply, 1,000 outstanding per client
thread. The Rust server's handler is faithful to the generated C++ wrapper: it unmarshals
the string (v64 prefix + bytes, kept on the stack as libc++ SSO would) before replying.
Same box, same session, three trials per cell:

| aggregate op/s | → C++ server (`-e 2 -w 16`) | → **Rust server** (one poll thread) |
|---|---:|---:|
| **C++ client** (`rpcbench`), t=1 | 371,760 | 478,764 |
| **C++ client** (`rpcbench`), t=8 | 1,233,742 | **2,223,421** |
| Rust client, 1 thread | 252,126 | 329,601 |
| Rust client, 8 threads | 1,111,099 | 1,952,013 |

Three readings, each isolated by the matrix:

- **Swap only the server** (same C++ client, t=8): the Rust server carries **180%** of the
  C++ server's throughput. The lazy explanations do not survive measurement: sweeping the
  C++ server from `-e 1 -w 1` to `-e 8 -w 16` moves nothing (837K vs 830K — the apparatus
  is free and poller parallelism does not help), and `perf` shows the same canonical
  functions dominating both profiles in the same order (`sconn_decode_request_and_dispatch`,
  `send_frame`, `sconn_reply`, the SparseInt codecs). What differs is the layer *under*
  the canonical code: the C++ lane runs the rusty runtime **ports** — its profile shows
  the hashbrown port probing with a type-erased `std::function` equality predicate,
  `pthread_mutex_lock/unlock` as 5% of samples where rustc's inlined futex path is
  invisible, out-of-line `Arc` refcount helpers, and ~1.7× more allocator time per
  request — while rustc compiles the real Rust std and hashbrown with full
  monomorphization. Same architecture, same hot path; each request simply costs about
  half as much compiled by rustc. (Both servers, incidentally, spend 20–30% of their
  time in malloc and Vec growth — and acting on that is a working demonstration of the
  one-source-two-lanes premise: seeding the five hot serialization sinks with a single
  64-byte allocation, one canonical change, moved *both* lanes at t=8 — the C++ server
  +6.5%, the Rust cells +4–8% — and taught in passing that a module-scope const is ABI
  surface, ratcheted like any other symbol. What remains is the discrete per-request
  allocation floor: one sink, one `Box<Request>`, one body Vec.)
- **Swap only the client** (same C++ server, t=8): the Rust driver reaches 90% of
  rpcbench — it waits `Arc<Future>`s where rpcbench uses `request_async`, which exists
  (per its own comment) precisely to skip the future-map cost.
- **Pure lane vs pure lane** (t=8): Rust 1,952,013 against C++ 1,233,742 — 158% — on
  identical hardware, identical bytes on the wire.

Measured 2026-08-31 at commit `aa73202`, load average 1.4–2.2 (the C++ table's runs were
at ~1.3). Two shapes worth a sentence each. The sequential TCP figure *is* the ~1 ms poll
tick, not a rustc artifact: `tcpconn_send_frame` always queues and wakes the poll thread
through the command channel, and the C++ worker drains that channel on the same 1 ms
cadence — throughput comes from pipelining in both lanes. And serialization is noise
(62 ns against a 3.5 µs pipelined budget); the gap to C++ lives in the poll loop and
request-path bookkeeping, not in copying. Untested and known-untested: *sharing* one
`Client` across rustc threads (`rusty::Arc` erases auto traits; the multi-thread numbers
above use one client per thread, which is also what rpcbench does).

The `fiber`, `defer` and `async` modes, which an earlier revision of this section claimed
were blocked on the reactor's TLS model, turn out to run — and win — under rustc. The
claim was wrong for this topology: the nine `thread_local`-marked reactor statics are
process globals under rustc, which is only a hazard with *multiple* threads touching
them, and every dispatch runs on the server's single poll thread. With the fiber engine
linked (the same `srpc_fiber.c` + context-switch assembly the C++ lane uses, built with
`-DREUSE_FIBER` — without the pool, per-request 1 MiB stack mmaps cap fiber mode at
~72K/s), `reg_rpc` dispatch spawns a real fiber per request through the canonical
reactor code, and `DeferredReply` works as-is. Same client, same minutes, t=8, o=1000:

| Mode | C++ server | **Rust server** | ratio |
|---|---:|---:|---:|
| `fast` | 1,410,825 | 2,381,806 | 169% |
| `fiber` | 816,758 | 1,123,166 | 138% |
| `defer` | 806,244 | 1,062,478 | 132% |
| `async` | 1,076,875 | 2,331,930 | 217%* |

*The async row overstates the lane difference: rustc has no coroutine `Task`, so the
Rust server serves `async_nop` inline — wire-identical, but without the coroutine-frame
cost the C++ server pays. The genuinely multi-threaded reactor (several poll threads
sharing fibers and timers) remains the one TLS-blocked configuration.

The rest of this chapter is the map of where the cost sits, read off the code: which
dispatch decision spawns a stack, which client entry point allocates what, which limits
are real knobs and which only look like knobs.

### Server side: `fast` versus a fiber

Every inbound request reaches `sconn_decode_request_and_dispatch` in `rpc/server.rs`, which
makes exactly one branch that matters for throughput:

```
if (ctx_.fast_rpc_ids.contains(rpc_id))  -> dispatch inline on the poll thread
else                                     -> spawn a stackful fiber, dispatch there
```

The fiber is not free. `srpc_fiber_init` in `reactor/srpc_fiber.c` `mmap`s
`kDefaultStackBytes + one page` — 1 MiB plus a page — `MAP_PRIVATE | MAP_ANONYMOUS`, then
`mprotect`s the first page `PROT_NONE` as a guard. That is one mmap and one mprotect per
dispatched request unless the fiber is recycled.

Recycling is the reason the default path is tolerable at all. `Reactor::recycle` pushes a
finished fiber onto `available_fibers_` with its stack intact, and
`reactor_get_or_create_fiber_impl` pops one and re-stamps it with a fresh id and a new
closure. Both are gated on `reusing_fiber()`, which is not a Rust constant: it is the plain-C
seam `srpc_reactor_reusing_fiber()`, and it returns non-zero only when `REUSE_FIBER` (or
`REUSE_CORO`) was defined when `reactor/srpc_fiber.c` was compiled. The in-tree build passes
`-DREUSE_FIBER` through `BENCH_CXXFLAGS`, so recycling is on. If you ever build the C seam
without it, the reactor logs `reusing fiber not enabled!` at WARN the first time a thread
touches its reactor, and from then on every request pays the mmap.

Marking a method `fast` in the `.rpc` file removes the fiber entirely:

```
abstract service Demo {
    fast ping();                     // inline on the poll thread
    lookup(string key | string val); // fiber
};
```

The price is that a `fast` handler has no stack of its own to yield from. It runs on the poll
thread, so if it blocks — a mutex, a disk read, a nested RPC — every connection served by
that poll thread stalls behind it. The same caution applies to an `async` handler up to its
first suspension point, because those are registered through the fast path too.

The rule of thumb that follows from the code: `fast` for handlers that only read memory and
return; the default for anything that can block. `defer` does not change this branch — a
`defer` handler still runs in a fiber, it just holds the reply open.

### Client side: three entry points, three costs

`Client` exposes three ways to issue a request, and they differ in what they allocate.

**`request(rpc_id, attr, write_fn)`** — the path the generated proxy uses. In
`clientconn_request_via_channel` it allocates an `Arc<Future>`, takes the `pending_fu_`
mutex and inserts the future into a `HashMap<i64, Arc<Future>>` keyed by xid, builds a fresh
`Vec<u8>` body buffer, serializes `v64 xid | i32 rpc_id | args` into it, and hands the bytes
to the channel. The reply path re-takes that same mutex to find and erase the entry. So the
steady-state cost is one control-block allocation, one buffer allocation, and two acquisitions
of a per-connection lock that every reply also contends for.

**`request_async(rpc_id, write_fn, on_reply)`** — the same body buffer, but no future and no
map. The callback goes into a table that was allocated once at connection setup:
`make_prefilled_cb_slots()` builds a `Vec<Option<AsyncReplyCallback>>` of `kAsyncSlotCount`
= 16384 entries, and the request parks its callback at index `xid % 16384`. That is the
cheapest path in the client, and it is what to reach for when you never intend to wait.

Its limits — a direct-mapped table, `Err(16)` (`EBUSY`) on a collision, no timeout, and a
delivery path that does not re-check the xid — are in chapters 7 and 8, and none of them is
a tuning parameter. A parked slot is reclaimed by a reply, by a failed send on the same
call, or by `invalidate_pending_futures`, which on disconnect, `close()` or destruction
drains every occupied slot and fires each callback once with `107` (`ENOTCONN`) and a null
reply view.

What does matter for tuning is where that callback runs. Its signature is
`(i32 error_code, const u8* bytes, usize size)`, and it is invoked
from `clientconn_decode_response_and_notify`, which runs inside the connection's receive-loop
fiber on the client's poll thread. Blocking in it blocks every reply on that connection.

**`request_with_options(rpc_id, options, write_fn)`** — the retry coordinator, and by far
the most expensive of the three. `clientconn_request_with_options` serializes the arguments
once into a replay buffer, creates a coordinator future, and then does this:

```
rusty::thread::spawn(move || { ...retry loop... }).detach();
```

One detached OS thread per call. That thread issues an attempt through the plain `request`
path, blocks on the attempt future's condvar, computes a backoff, sleeps, and repeats. It is
the right tool for a handful of calls that must survive a flaky link; it is the wrong tool
for a hot loop.

Retries are opt-in twice over. `effective_options.max_retries` is forced to `0` whenever
`idempotent` is false, so setting `max_retries` alone silently disables retrying.

### The one-second wait

`Future::wait()` is hard-capped at one second and latches `ETIMEDOUT`; the only way past it
is `wait_with_options()` on a future whose `RequestOptions::timeout_ms` you have set to a
non-zero value, which `request_with_options` does not do for you — it writes `timeout_ms = 0`
into the coordinator future on purpose. Chapter 8 has the mechanism and the `set_options`
sequence. It appears here only as a warning: it is a correctness ceiling, not a tuning knob,
and no build flag, thread count or dispatch mode moves it.

### Frame size is not a throughput knob

`kMaxFramePayloadSize` in `rpc/frame_codec.rs` is 64 MiB, and it is a stream-integrity bound
rather than a resource policy. Chapter 7 explains what that bound catches — a desynchronised
stream that would otherwise wedge the connection silently, with no error, no log and no
reconnect — along with the two constraints on changing it.

Raising it does not make anything faster. It only widens the window in which a corrupt header
is believed.

Two related TCP-layer numbers, both in `rpc/tcp_channel.rs`:

- `kTcpConnectionOutboundHighWaterDefault` is 4 MiB. `send_frame` refuses to append to an
  outbound buffer that is already at or past it, returning `ChannelError::WouldBlock` — which
  the client turns into `Err(5)` (`EIO`), not a distinguishable backpressure signal. There is
  a `TcpConnection::set_outbound_high_water(bytes)`, but it is not on the
  `ChannelConnectionBase` trait, so it is unreachable through the `ChannelConnectionProxy`
  the runtime hands you. In practice the 4 MiB is fixed.
- The receive path reads into a 64 KiB stack scratch buffer (`kRecvScratchBytes`).

`TcpFactory::set_connect_timeout_ms(i32)` — default 5000 — *is* reachable, if you build the
factory yourself and `set_channel_factory` it before connecting.

### Build flags

`-march=native` appears three times in `CMakeLists.txt` and is not an optimization setting.
Clang refuses to load a BMI whose target-feature set differs from the importing translation
unit's, and every vendored `rusty-cpp` port target is compiled `-O3 -DNDEBUG -march=native`.
Drop the flag from the `srpc` side and you get roughly 133 errors of the form

```
error: precompiled file '.../vec_port.vec.pcm' was compiled with the target feature
       '+64bit' but the current translation unit is not
```

none of them real source errors. The accepted consequence is that a build tree — and the BMIs
in it — is not portable to a host with a different CPU.

The `srpc` library itself is compiled with `SRPC_CXXFLAGS`, which is `BENCH_CXXFLAGS`
verbatim:

```
-w -Wreturn-type -MD -MP -DRUSTYCPP_DISABLE_ARC_LOG -DREUSE_FIBER
-O2 -g -fno-omit-frame-pointer -march=native
```

That is a deliberately profileable build, not a maximally optimized one: `-O2`, debug info,
and frame pointers kept so `perf` can walk stacks. The vendored ports beside it are `-O3
-DNDEBUG`. If you want `-O3 -DNDEBUG` for a production measurement you have to edit
`BENCH_CXXFLAGS` — and be aware that the two flag sets already cause CMake 4.4 to synthesise
two different `std` module variants, which is exactly why the test battery is handed an
explicit module map instead of being scanned.

### Poll threads

`PollThread::create()` spawns one OS thread running one `PollThreadWorker::poll_loop`. Every
pollable registered on it, every fiber spawned by its dispatches, and every reply decoded on
its connections run on that single thread. A `Server` built as `Server::new_(rusty::Some(poll))`
does all of its work there; passing `rusty::None` makes it create a poll thread of its own.
`Client::create(poll)` takes one unconditionally.

Scaling out therefore means creating more `PollThread`s and distributing servers and clients
across them — there is no worker-pool setting inside a single poll thread to turn up.

### Settings that look like knobs but are not

Do not spend tuning effort on these; they are inert in this tree.

| Surface | What actually happens |
| --- | --- |
| `Client::metrics()` | Returns `&self.empty_metrics_field`, a permanently zeroed per-`Client` `ConnectionMetrics`. The live counters are on `ClientConnection::metrics()`, reached via `client->connection()` (which returns an `Option`). |
| `PoolConfig::load_balancing = LEAST_CONNECTIONS` | Reads `in_flight_requests()` off that zeroed stub for every candidate, so nothing is ever less than the first — always selects index 0. |
| `PoolConfig::load_balancing = LEAST_LATENCY` | Skips any candidate whose latency and completed count are both zero, which is all of them — also returns index 0. |
| `PoolConfig` health checking | `clientpool_is_client_healthy_with` reads `requests_sent()` from the stub, gets 0, and returns "healthy" before it ever compares the success rate. A *connected* client is therefore always judged healthy; only one that has actually lost its connection is ever evicted. |
| Offline request buffering | Requests are parked with a TTL and expired; `replay_pending_requests` returns `0`. They are never replayed after reconnect. |
| `Client::set_keepalive` | Stores the config. `apply_keepalive_options` is an empty function — no socket option is ever set. |
| Heartbeat | The protocol is complete on both ends, but nothing ticks the client-side timer, so probes are not sent on a schedule. |

`RANDOM` (the default) and `ROUND_ROBIN` are the two load-balancing strategies that do what
they say.

### What is worth measuring

Given the above, the changes most likely to move a real number are, in rough order:

1. Moving trivial handlers to `fast`, removing a fiber dispatch per call.
2. Switching fire-and-forget call sites from `request` to `request_async`, removing an `Arc`
   allocation and two `pending_fu_` lock acquisitions per call.
3. Keeping `request_with_options` off the hot path, so you are not spawning an OS thread per
   request.
4. Adding poll threads and spreading clients and servers across them.
5. Rebuilding with `-O3 -DNDEBUG` if you have decided you no longer need the frame pointers.

Each of those is a hypothesis. This repository gives you no numbers to check them against —
build the harness, and measure on the machine you care about.

---

## 16. API Reference

Everything here is the public C++ surface generated from the canonical Rust in
`base/ misc/ reactor/ rpc/`, in declaration form only. When this chapter and a `.rs` file
disagree, the `.rs` file wins — it is the only source there is. Where a signature needs
explaining, the explanation lives in the chapter that owns it — 5 for events, 7 for the wire
format, 8 for the client, 9 for the server, 11 for the reliability configs and their
defaults, 12 for the code generator, 18 for the error codes — and is not repeated here.
Every block below is tagged `srpc-no-compile`: these are signatures to read, not fragments
the snippet harness builds.

Names are C++ spellings. Rust `Self::new` becomes `new_` in C++ where `new` is reserved,
which is why you write `Server::new_(...)`. Ownership types come from `rusty`:
`rusty::Arc<T>`, `rusty::Rc<T>`, `rusty::Box<T>`, `rusty::Option<T>` (`rusty::Some(x)`,
`rusty::None`), `rusty::Result<T, E>`, `rusty::Function<Sig>`. A `Box<dyn Trait>` is
`rusty::Box<Trait>`, and a Rust `Vec<T>` is `rusty::Vec<T>`, not `std::vector<T>`.

Integers in the runtime signatures below are the fixed-width C types — `int32_t`,
`uint64_t`, `int8_t`, `size_t`. The `srpc::` integer aliases (`srpc::i8`, `srpc::i16`,
`srpc::i32`, `srpc::i64`, and only those four) exist for the *generated* IDL code, which is
why handler signatures out of `pylib` are spelled `srpc::i32` while `Client::connect` is
spelled `int8_t`. They are the same types.

`srpc.hpp` pulls in most of what follows. Eight modules carry an explicit
"trimmed from consumer umbrella" comment in `srpc.hpp` and must be imported by name if you
need their types: `srpc.circuit_breaker`, `srpc.connection_metrics`, `srpc.epoll_wrapper`,
`srpc.heartbeat`, `srpc.internal_protocol`, `srpc.load_balancer`, `srpc.reconnect_policy`,
`srpc.request_options`. That commented-out list is hand-maintained and nothing checks it.
Four more modules were never in the umbrella at all and also need naming:
`srpc.inmemory_channel` (where `make_inmemory_factory_proxy` below lives; the header
`rpc/inmemory_channel.hpp` does nothing but import it), `srpc.any_message`,
`srpc.serializable_envelope` and `srpc.callback_wrapper`.

### Client

`Client` is always held through an `Arc`, and it is a thin front for a `ClientConnection`
that only exists after a successful `connect`.

```cpp srpc-no-compile
class Client {
    static rusty::Arc<Client> create(rusty::Arc<PollThread> poll_thread);

    // Connection lifecycle. `addr` is "host:port"; the bool selects client mode.
    int32_t connect(const int8_t* addr, bool client);
    void close();
    int32_t reconnect(OnReconnectCompleteCallbackFn on_complete);
    bool try_reconnect_if_needed();
    void pause();
    void resume();

    // Issuing requests. write_fn is any callable taking BinaryWriteArchive&.
    template<class F>
    rusty::Result<rusty::Arc<Future>, int32_t>
        request(int32_t rpc_id, const FutureAttr& attr, F write_fn);

    template<class F>
    rusty::Result<rusty::Arc<Future>, int32_t>
        request_with_options(int32_t rpc_id, const RequestOptions& options, F write_fn);

    template<class F>
    /* Result carrying no value on success */
        request_async(int32_t rpc_id, F write_fn, AsyncReplyCallback on_reply);

    // State.
    bool connected() const;
    bool has_connection() const;
    ConnectionState connection_state() const;
    bool is_reconnecting() const;
    bool validate_connection() const;
    std::string host() const;
    uint64_t server_instance_id() const;
    bool is_idle(uint64_t idle_ms, uint64_t current_time_ms) const;

    // The live connection, if any.
    rusty::Option<rusty::Arc<ClientConnection>> connection() const;

    // Pending-request bookkeeping.
    size_t pending_request_count() const;
    void clear_pending_requests(int32_t error_code);
    void handle_free(int64_t xid);

    // Configuration. Only the first four are staged and applied at connect();
    // set_buffering_config forwards to the connection, so call it AFTER connect().
    void set_keepalive(const KeepaliveConfig& config);
    void set_heartbeat(const HeartbeatConfig& config);
    void set_circuit_breaker(const CircuitBreakerConfig& config);
    void set_reconnect_policy(const ReconnectPolicy& policy);
    void set_buffering_config(const BufferingConfig& config);
    void set_channel_factory(ChannelFactoryProxy factory);

    KeepaliveConfig keepalive_config() const;
    HeartbeatConfig heartbeat_config() const;
    CircuitBreakerConfig circuit_breaker_config() const;
    CircuitState circuit_breaker_state() const;

    // Connection-event callbacks.
    void add_on_connected(OnConnectedCallbackFn cb);
    void add_on_disconnected(OnConnectedCallbackFn cb);
    void add_on_reconnecting(OnConnectedCallbackFn cb);
    void add_on_reconnected(OnReconnectedCallbackFn cb);
    void add_on_error(OnErrorCallbackFn cb);
    void clear_connection_callbacks();
    void set_on_server_restart(OnServerRestartCallbackFn cb);

    // Returns `&self.empty_metrics_field`: a per-Client ConnectionMetrics that is
    // never written. The real counters are on ClientConnection::metrics().
    const ConnectionMetrics& metrics() const;
};
```

Three signature details bite at the call site. `request` takes exactly three arguments —
there is no overload set, so a method with no input parameters still passes an empty lambda,
which is what the generator emits. `connect` and `Server::start` take `const int8_t*`, so
call sites write `reinterpret_cast<const int8_t*>(addr)`. And `metrics()` is the zeroed stub
noted in the block. Chapter 8 works through what each of those means for a client.

### ClientConnection

Reached with `client->connection()`, which returns an `Option`. This is where the live
state is.

```cpp srpc-no-compile
class ClientConnection {
    const ConnectionMetrics& metrics() const; // the real counters

    bool connected() const;
    ConnectionState connection_state() const;
    bool is_closed() const;
    bool is_reconnecting() const;
    std::string host() const;
    uint64_t server_instance_id() const;

    size_t pending_future_count() const;  // outstanding Futures
    size_t pending_request_count() const; // offline queue depth

    uint64_t last_activity_time() const;
    void update_last_activity(uint64_t current_time_ms);
    bool is_idle(uint64_t idle_ms, uint64_t current_time_ms) const;

    ReconnectPolicy reconnect_policy() const;
    BufferingConfig buffering_config() const;
    CircuitState circuit_breaker_state() const;
};
```

Two methods on this class are stubs and should not be built on: `replay_pending_requests()`
returns `0` — queued requests are expired by TTL, never replayed — and
`apply_keepalive_options()` has an empty body, so `set_keepalive` stores a config that is
never turned into socket options. Chapter 11 lists the inert knobs in full.

### Future

```cpp srpc-no-compile
class Future {
    static rusty::Arc<Future> create(int64_t xid, FutureAttr attr); // attr by value

    bool ready() const;
    void wait() const;              // routes through timed_wait(1.0)
    void timed_wait(double sec) const;
    bool wait_with_options() const; // uses this future's RequestOptions
    bool timed_out() const;

    int32_t get_error_code() const;               // also waits
    rusty::RefMut<ReplyBuffer> get_reply() const; // also waits
    int64_t get_xid() const;

    RequestOptions get_options() const;
    void set_options(const RequestOptions& opts) const;
    TimeoutType get_timeout_type() const;
    uint16_t get_retry_count() const;
    bool should_retry() const;

    bool add_completion_callback(rusty::Function<void()> callback) const;

    static void safe_release(rusty::Arc<Future> fu); // no-op; Arc owns the lifetime
};
```

`Future::new` fixes the internal `timeout_` at 1,000,000 microseconds and nothing changes
it, so `wait()` and `get_error_code()` are capped at one second and latch error code `110`
on expiry. `wait_with_options()` is the only way past the cap, and only when the future's
`RequestOptions::timeout_ms` is non-zero. Chapter 8's "The one-second wall" explains the
consequences and the escape.

`get_reply()` hands back a `RefMut<ReplyBuffer>` — a borrow guard. Keep it alive for the
whole decode; the archive built over it reads through `&guard->src`.

`FutureAttr` carries one field, a completion callback, and default-constructs to empty. The
generated proxy passes a default-constructed one.

### ClientPool

```cpp srpc-no-compile
class ClientPool {
    static ClientPool new_(rusty::Option<rusty::Arc<PollThread>> poll_thread,
                           PoolConfig config);

    rusty::Option<rusty::Arc<Client>> get_client(const std::string& addr);

    void set_pool_config(PoolConfig config);
    PoolConfig pool_config() const;

    size_t total_client_count() const;
    size_t address_count() const;
    size_t get_healthy_client_count(const std::string& addr) const;

    size_t remove_unhealthy_clients(const std::string& addr);
    size_t remove_all_unhealthy();
    size_t close_idle_clients(const std::string& addr, uint64_t current_time_ms);
    size_t close_all_idle(uint64_t current_time_ms);

    bool is_client_healthy(const rusty::Arc<Client>& client) const;
};
```

The factory asserts `min_connections > 0` and `max_connections >= min_connections`, and
creates its own `PollThread` when handed `rusty::None`. Destroying the pool closes every
cached client and shuts that poll thread down. Health checking and the
`LEAST_CONNECTIONS` / `LEAST_LATENCY` strategies read `Client::metrics()`, the zeroed stub,
and are therefore inert; chapters 11 and 15 give the details. `RANDOM` (the default) and
`ROUND_ROBIN` work as documented.

### Server

`Server` is used by value, not through an `Arc`.

```cpp srpc-no-compile
class Server {
    static Server new_(rusty::Option<rusty::Arc<PollThread>> poll_thread);

    // Registration. reg_service_typed is the one you want for a generated service.
    template<class T> void reg_service_typed(rusty::Box<T> svc);
    void reg_service(rusty::Box<Service> svc);
    void reg_service_proxy(ServiceProxy proxy);
    int32_t reg_rpc(int32_t rpc_id, size_t svc_index);
    int32_t reg_fast_rpc(int32_t rpc_id, size_t svc_index);
    void unreg(int32_t rpc_id);

    // Transport. Call set_channel_factory before start() to override TCP.
    void set_channel_factory(ChannelFactoryProxy factory);
    bool is_channel_factory_bound() const;

    int32_t start(const int8_t* bind_addr); // 0 on success, -1 on failure
    int32_t get_bound_port() const;         // -1 if unparseable
    std::string addr() const;               // only after start()

    // Shutdown. kDefaultDrainTimeoutMs is 30000.
    void stop_accepting();
    bool drain(uint64_t timeout_ms);
    void graceful_shutdown(uint64_t drain_timeout_ms);
    void do_shutdown();
    void wait_for_shutdown();
    void add_shutdown_hook(ShutdownHook hook);
    ShutdownPhase phase() const;

    // Bookkeeping.
    int32_t pending_request_count() const;
    size_t service_count() const;
    uint64_t instance_id() const;
    void set_drop_heartbeat_replies(bool drop_replies);
    bool drop_heartbeat_replies() const;

    template<class F> void for_each_service(F callback) const; // only after start()
};
```

Generated service classes have **no base class**. Register one with
`reg_service_typed(rusty::make_box<MyService>())`; the adapter that wraps it is
`ServiceBoxShim<T>`. `reg_service` takes a `Box<dyn Service>` and is not what you want for a
generated class. There is no `add_service` and no `stop()`.

`start()` freezes the pending registrations into an immutable `RpcServiceContext`,
auto-installs a `TcpFactory` if none is bound, creates and wires the listener, and binds.
Registrations after `start()` do not take effect.

Shutdown progresses `RUNNING -> STOP_ACCEPTING -> DRAINING -> CLOSING -> STOPPED`
(`ShutdownPhase`, with `shutdown_phase_to_string`); `graceful_shutdown` runs the whole
sequence. Chapter 9's "Graceful Shutdown" covers the teardown order — in particular that
destroying the `Server`, not any method on it, is what closes already-accepted connections.

### Service and dispatch

```cpp srpc-no-compile
// The interface generated services satisfy.
class Service {
    virtual int32_t __reg_to__(Server& server, size_t svc_index) = 0;
    virtual void __dispatch__(int32_t rpc_id, rusty::Box<Request> req,
                                   WeakServerConnection sconn) = 0;
};

// One in-flight request. `src` is the cursor over the argument bytes.
struct Request {
    rusty::Vec<uint8_t> body;
    BufferSource src;
    int64_t xid;
};
```

Handlers you write are not `Service` methods directly — the generated `<Svc>Service` class
declares them. Their shape depends on the dispatch attribute in the `.rpc` file; the
grammar accepts six (`prefix`, `fast`, `raw`, `fiber`, `defer`, `async`) plus the unattributed
default. Chapter 12 explains what each one costs.

| Attribute | Generated handler signature |
| --- | --- |
| *(none)* | `rusty::Result<Rpc<M>Response, srpc::i32> m(const Rpc<M>Request&)` |
| `fast` / `prefix` | same, but registered with `reg_fast_rpc` and dispatched inline on the poll thread |
| `defer` | `void m(const Rpc<M>Request&, Rpc<M>Response& resp, srpc::DeferredReply defer)` |
| `fiber` | same as *(none)*, but the wrapper calls it inside a `Fiber::create_run` — avoid it, see chapter 12 |
| `async` | `rusty::Task<rusty::Result<Rpc<M>Response, srpc::i32>> m(const Rpc<M>Request&)` |
| `raw` | `void m(rusty::Box<srpc::Request>, srpc::WeakServerConnection)` |

The `Rpc<M>Request` / `Rpc<M>Response` structs are **members** of `<Svc>Service` and are
re-exported into `<Svc>Proxy` with `using`. Pointer out-parameters survive only on a `raw`
method's proxy.

### DeferredReply

```cpp srpc-no-compile
class DeferredReply {
    void reply();                   // send the filled-in response
    void reply_error(int32_t code); // send a header-only error reply
};
```

Both `reply()` and `reply_error()` fire at most once — a second call logs a warning and is
ignored — and dropping the handle without replying is safe. There is also a `run_async`
taking a callable, but it offloads nothing: the body invokes the callable on the calling
thread and returns 0. Chapter 9's "Replying" has the rest.

### Reactor

Thread-local in the generated C++ (the `#[cfg_attr(any(), thread_local)]` markers that say
so are inert under rustc, which is why `reactor/reactor.rs` is not meaningfully testable
from Rust).

```cpp srpc-no-compile
class Reactor {
    static rusty::Rc<Reactor> get_reactor();
    static rusty::Rc<Reactor> get_disk_reactor();

    rusty::Rc<Fiber> create_run_fiber(rusty::Function<void()> func);
    void continue_fiber(const rusty::Rc<Fiber>& fiber);
    void register_fiber(const rusty::Rc<Fiber>& fiber);
    void recycle(rusty::Rc<Fiber>& fiber);

    void run_loop(bool infinite, bool do_check_timeout);
    void prune_finished_events();
    void display_waiting_ev();

    size_t register_stackless_poller(rusty::Function<bool(rusty::Context&)> poller);
    void enqueue_stackless_task(size_t idx);
    bool process_stackless_tasks();
};
```

`run_loop` takes two booleans, not one. It asserts that it is running on the thread the
reactor was created on. `create_run_fiber` runs the fiber immediately and drives the loop
internally, so a fiber that never blocks has already finished by the time the call returns.

### Fiber and this_fiber

```cpp srpc-no-compile
class Fiber {
    template<class F> static rusty::Rc<Fiber> create_run(F func);
    static rusty::Option<rusty::Rc<Fiber>> current_fiber();
    static void sleep(uint64_t microseconds);

    void run() const;
    void yield_() const;
    void continue_() const;
    bool finished() const;
};

namespace this_fiber {
    uint64_t get_id();                         // 0 outside fiber context
    rusty::Option<rusty::Rc<Fiber>> current();
    bool in_fiber_context();
    void yield();                              // no-op outside a fiber
    void sleep_us(uint64_t microseconds);
    void sleep_ms(uint64_t milliseconds);
    void sleep_s(uint64_t seconds);
    void sleep_until_us(uint64_t abs_time_us); // past deadlines return at once
}
```

Fibers are stackful: `kDefaultStackBytes` is 1 MiB, `mmap`'d with one extra page
`mprotect`ed `PROT_NONE` as a guard, and switched by
`reactor/fiber_context_{x86_64,aarch64}.S`. The field order of `srpc_fiber_ctx` in
`reactor/srpc_fiber.h` *is* the ABI contract with that assembly.

### Events

There is no `Event` base class in the C++ surface and no `create_sp_event<T>` template.
Events are concrete types built by **named free functions**, and the methods are lowercase.

```cpp srpc-no-compile
rusty::Arc<IntEvent> create_sp_int_event(int32_t target);
rusty::Arc<TimeoutEvent> create_sp_timeout_event(uint64_t wait_us);
rusty::Arc<NeverEvent> create_sp_never_event();
rusty::Arc<WaitAny> create_sp_waitany(rusty::Arc<EventPollable> a,
                                      rusty::Arc<EventPollable> b);
rusty::Arc<WaitAll> create_sp_waitall();
rusty::Arc<WaitAll> create_sp_waitall_from(const rusty::Vec<rusty::Arc<EventPollable>>& evs);
template<class T> rusty::Arc<BoxEvent<T>> create_sp_box_event();

// The N-of-M primitive lives in global namespace ::janus, not in srpc.
namespace janus {
    rusty::Arc<QuorumEvent> create_sp_quorum_event(int32_t n_total, int32_t quorum);
}
```

The common surface, from the `EventPollable` interface plus each type's own methods:

```cpp srpc-no-compile
bool test();
bool is_ready();
uint64_t wakeup_time();
bool prunable();
void set_prunable(bool v);

// Status is reachable two ways: the EventPollable methods, and the public field
// they read through. Chapter 5 uses the field.
EventStatus status();                 // == status_.get()
void set_status(EventStatus s);       // == status_.set(s)
rusty::Cell<EventStatus> status_;     // public field on every concrete event

void wait(); // IntEvent, BoxEvent, WaitAny, WaitAll, TimeoutEvent, QuorumEvent
void wait_timeout(uint64_t timeout_us);

int32_t IntEvent::get();
int32_t IntEvent::set(int32_t n);
template<class T> T BoxEvent<T>::get();
template<class T> void BoxEvent<T>::set(const T& c);
template<class T> void BoxEvent<T>::clear();
void WaitAll::add_event(rusty::Arc<EventPollable> ev);

void janus::QuorumEvent::vote_yes();
void janus::QuorumEvent::vote_no();
bool janus::QuorumEvent::yes();
bool janus::QuorumEvent::no();
void janus::QuorumEvent::add_xid(uint16_t site, int64_t xid);
void janus::QuorumEvent::remove_xid(uint16_t site);
void janus::QuorumEvent::finalize(uint64_t timeout, QuorumFinalizeFn f);
```

There is no `WaitN` or `NEvent`; `QuorumEvent` is the N-of-M primitive. The `::janus`
placement is a hard ABI contract — `srpc::QuorumEvent` and `srpc::janus::QuorumEvent` mangle
differently and are not substitutes.

`FiberPromise<T>` / `FiberFuture<T>` in `srpc.future` wrap a `BoxEvent<T>`:
`make_promise<T>()` returns the pair, `make_ready_future<T>(value)` returns a satisfied one,
and `FiberFuture<T>::wait_for(timeout_us)` treats a zero timeout as "wait forever".

### PollThread

```cpp srpc-no-compile
class PollThread {
    static rusty::Arc<PollThread> create(); // spawns exactly one OS thread

    void add_proxy(PollableProxy poll);
    void remove(Pollable& poll);
    void remove_fd(int32_t fd);
    void request_close(int32_t fd);
    void update_mode(int32_t fd, int32_t new_mode);
    void add(rusty::Arc<Job> job);
    void shutdown(); // idempotent; skips self-join
};
```

`PollableProxy` is `rusty::Box<PollableBase>` from `srpc.pollable_proxy`; the `Pollable`
that `remove` takes comes from `srpc.epoll_wrapper`, one of the modules trimmed out of the
umbrella.

Every method except `create` and `shutdown` posts a command down an mpsc channel to the
worker thread, so they are fire-and-forget: if the worker has already exited, the command is
dropped silently — except `update_mode`, which logs at `ERROR`
("PollThread::update_mode: send failed! Channel disconnected?") when the send fails.
`get_remove_count()` exists but always returns 0 — worker state is not reachable across the
channel.

### Channel layer

`rpc/channel.rs` is the transport facade. TCP (`srpc.tcp_channel`) and in-memory
(`srpc.inmemory_channel`) implement it; `FiberChannel` is not an implementation but an
adapter that turns callback delivery into a fiber-blocking `recv_frame()`.

```cpp srpc-no-compile
enum class ChannelError {
    None = 0, WouldBlock, ConnectionRefused, ConnectionReset, Timeout,
    AddressInUse, AddressInvalid, PermissionDenied, TooManyOpenFiles, Internal,
};
const char* channel_error_to_string(ChannelError error);

struct ChannelFrame { const uint8_t* payload; size_t size; };

class ChannelConnectionBase {
    virtual ChannelError send_frame(const ChannelFrame& frame) = 0;
    virtual void flush() = 0;
    virtual void close() = 0;
    virtual bool is_closed() const = 0;
    virtual std::string peer_address() const = 0;
    virtual void set_on_frame(OnFrameCallback cb) = 0;
    virtual void set_on_closed(OnClosedCallback cb) = 0;
    virtual void set_on_error(OnErrorCallback cb) = 0;
};

class ChannelListenerBase {
    virtual ChannelError listen(const std::string& address) = 0;
    virtual void close() = 0;
    virtual bool is_closed() const = 0;
    virtual std::string local_address() const = 0;
    virtual void set_on_accept(OnAcceptCallback cb) = 0;
    virtual void set_on_error(OnErrorCallback cb) = 0;
};

struct ConnectResult {
    rusty::Option<ChannelConnectionProxy> connection;
    ChannelError error;
};

class ChannelFactoryBase {
    virtual ConnectResult connect(const std::string& address) = 0;
    virtual rusty::Option<ChannelListenerProxy> make_listener() = 0;
    virtual std::string backend_name() const = 0;
};
```

The handle typedefs are `ChannelConnectionProxy`, `ChannelListenerProxy` and
`ChannelFactoryProxy`, all `rusty::Box<dyn ...>`. To install a non-default transport, build
the factory and hand it over *before* `connect` / `start`:
`make_tcp_factory_proxy(arc_of_tcp_factory)` and `make_inmemory_factory_proxy(...)` — the
latter needs `import srpc.inmemory_channel;`, which the umbrella does not supply.
`TcpFactory::set_connect_timeout_ms(i32)` defaults to 5000.

### Framing and wire format

```cpp srpc-no-compile
// srpc.frame_codec
constexpr size_t kFrameHeaderSize = 4;
constexpr int32_t kMaxFramePayloadSize = 64 * 1024 * 1024;

enum class FrameDecodeStatus { NeedMoreBytes = 0, Complete = 1, Malformed = 2 };

// srpc.internal_protocol (not in the srpc.hpp umbrella)
constexpr int32_t kInternalHeartbeatRpcId = INT32_MIN;
constexpr uint32_t kResponseHeaderExtFlag = 0x80000000;
constexpr uint32_t kResponseSizeMask = 0x7fffffff;

bool response_has_extended_header(int32_t encoded_size);
int32_t response_payload_size(int32_t encoded_size);
int32_t encode_response_size(int32_t payload_size, bool extended_header);
```

The header is 4 bytes, **native-endian**: bit 31 is the extended-header flag, bits 0–30 the
payload size. A request body is `v64 xid` (a 1–9 byte varint) then `i32 rpc_id` (fixed four
bytes) then the arguments. A reply body is `v64 xid`, `v32 error_code`,
`v64 server_instance_id`, then the payload. Chapter 7 walks the format byte by byte,
including why the extended flag is vestigial.

### Serialization

There is no `Marshal` class. Serialization goes through two free-function dispatchers over
an archive:

```cpp srpc-no-compile
// The two dispatchers the generated code calls.
template<class T> void srpc::Serialize_::serialize(const T& value, BinaryWriteArchive& ar);
template<class T> void srpc::Deserialize_::deserialize(T& value, BinaryReadArchive& ar);

// Archives hold a type-erased proxy over a sink or source.
struct BinaryWriteArchive { SinkProxy sink_; };
struct BinaryReadArchive { SourceProxy source_; };

// Concrete backings.
struct BufferSink { rusty::Vec<uint8_t> bytes; };
struct BufferSource {
    BufferSource(const uint8_t* data, size_t len);
    size_t pos() const;
    size_t remaining() const;
    bool eof() const;
};

// Proxy factories.
SinkProxy make_sink_proxy_buffer(BufferSink* sink);
SourceProxy make_source_proxy_buffer(BufferSource* source);
SinkProxy make_sink_proxy_fd(FdSink* sink);
SourceProxy make_source_proxy_fd(FdSource* source);
```

There is no bare `make_source_proxy`. Building a read archive over a reply looks like the
generated code's own decode:

```cpp srpc-no-compile
auto guard = fu->get_reply();
srpc::BinaryReadArchive ar(srpc::make_source_proxy_buffer(&guard->src));
srpc::Deserialize_::deserialize(resp.field, ar);
```

`Serialize_::serialize` resolves through an ADL bridge, so a user type joins the protocol by
declaring `serialize` / `deserialize` overloads findable from its own namespace — which is
what the generator emits for every struct in a `.rpc` file. Chapter 10 covers the rest.

### Configuration structs and their defaults

All eight are plain aggregates with static factory functions. Where a `defaults()` exists it
is an alias for `new_()`; `KeepaliveConfig` and `ReconnectPolicy` have no `defaults()` at
all, so spell those `KeepaliveConfig::new_()` and `ReconnectPolicy::new_()`. What a fresh
`Client` actually stages is not each struct's own default — chapter 11's "What is staged and
what is not" has that table.

```cpp srpc-no-compile
struct RequestOptions {        // srpc.request_options — not in the umbrella
    uint64_t timeout_ms;       // 1000
    uint64_t total_timeout_ms; // 0 = unlimited
    uint16_t max_retries;      // 0
    uint16_t base_delay_ms;    // 50
    uint16_t max_delay_ms;     // 5000
    float jitter_factor;       // 0.1
    bool idempotent;           // false

    static RequestOptions defaults();
    static RequestOptions with_retry(uint16_t max_retries, uint64_t timeout_ms);
    static RequestOptions idempotent_retry(uint16_t max_retries);
    static RequestOptions no_timeout(); // timeout_ms = 0
    static RequestOptions fast();       // 100ms, 2 retries, idempotent
    static RequestOptions patient();    // 10s / 60s total, 5 retries, idempotent

    bool can_retry(uint16_t current_retry_count) const;
    uint64_t calculate_delay_ms(uint16_t attempt) const;
    bool is_total_timeout_exceeded(uint64_t elapsed_ms) const;
    uint64_t remaining_time_ms(uint64_t elapsed_ms) const;
};
```

`can_retry` is `idempotent && current_retry_count < max_retries` — both halves are required,
and `request_with_options` forces `max_retries = 0` on a non-idempotent request. `defaults()`
leaves `idempotent` false; the four presets other than `no_timeout()` set it true.

```cpp srpc-no-compile
struct PoolConfig {                       // defaults()   aggressive()  conservative()
    int32_t min_connections;              // 1            2             1
    int32_t max_connections;              // 4            8             2
    uint64_t idle_timeout_ms;             // 300000       60000         600000
    bool health_check_enabled;            // true (also no_health_check())
    uint64_t unhealthy_threshold_percent; // 50 / 70 / 30
    uint64_t min_requests_for_health;     // 10 / 5 / 20
    LoadBalancingStrategy load_balancing; // RANDOM
};

struct BufferingConfig {         // defaults()
    DisconnectBehavior behavior; // QUEUE   (or FAIL_FAST)
    size_t max_pending;          // 1000
    uint32_t default_ttl_ms;     // 30000
    OverflowStrategy overflow;   // DROP_OLDEST
    bool enabled;                // true    (disabled() flips both)
};

struct KeepaliveConfig {  // new_()      aggressive()  relaxed()
    bool enabled;         // true        true          true
    int32_t idle_sec;     // 60          10            60
    int32_t interval_sec; // 10          2             10
    int32_t count;        // 5           3             5
};                        // also disabled(); no defaults()

struct HeartbeatConfig {  // defaults()
    bool enabled;         // true
    uint32_t interval_ms; // 10000
    uint32_t timeout_ms;  // 5000
    uint32_t max_missed;  // 3
};

struct CircuitBreakerConfig {   // defaults()
    uint32_t failure_threshold; // 5
    uint32_t success_threshold; // 3
    uint32_t timeout_ms;        // 30000
    bool enabled;               // true
};

struct ReconnectPolicy {       // new_(); conservative() is the same values.
    bool auto_reconnect;       // true    aggressive(): true / 0 / 100 / 5000 / 1.5 / true
    uint32_t max_retries;      // 5       no_retry():   false / 0 / 0 / 0 / 1.0 / false
    uint32_t initial_delay_ms; // 1000
    uint32_t max_delay_ms;     // 30000
    double backoff_multiplier; // 2.0
    bool jitter_enabled;       // true
};                             // no defaults()

struct RequestQueueConfig {             // defaults()
    size_t max_size;                    // 1000
    uint32_t default_ttl_ms;            // 30000
    OverflowStrategy overflow_strategy; // DROP_OLDEST
    bool enabled;                       // true
};
```

### Enumerations

The blocks below give the value names and their numeric assignments, which is what you need
to read a log line or a stored code. They are not a transcription of the emitted
declaration — at use sites the values are spelled `Type::VALUE`.

```cpp srpc-no-compile
enum class ConnectionState { NEW=0, CONNECTING, CONNECTED, DISCONNECTING, DISCONNECTED, FAILED };
enum class CircuitState { CLOSED=0, OPEN, HALF_OPEN };
enum class ShutdownPhase { RUNNING, STOP_ACCEPTING, DRAINING, CLOSING, STOPPED };
enum class EventStatus { INIT=0, WAIT, READY, DONE, TIMEOUT, DEBUG };
enum class TimeoutType { NONE=0, CONNECT_TIMEOUT, REQUEST_TIMEOUT, RESPONSE_TIMEOUT, TOTAL_TIMEOUT };
enum class OverflowStrategy { DROP_OLDEST=0, DROP_NEWEST, FAIL_FAST };
enum class DisconnectBehavior { QUEUE, FAIL_FAST };
enum class LoadBalancingStrategy { RANDOM=0, ROUND_ROBIN, LEAST_CONNECTIONS, LEAST_LATENCY };
enum class ServerConnStatus { CONNECTED, CLOSED };
```

Most have a `*_to_string` free function: `connection_state_to_string`,
`circuit_state_to_string`, `shutdown_phase_to_string`, `timeout_type_to_string`,
`overflow_strategy_to_string`, `load_balancing_strategy_to_string`,
`channel_error_to_string`, `frame_decode_status_to_string`. Three of the nine above have
none — `EventStatus`, `DisconnectBehavior` and `ServerConnStatus` — so there is no
`event_status_to_string` to call.

### Error codes

Two unrelated numbering schemes live side by side. What `Future::get_error_code()` and the
generated proxies return are plain `errno` values, exported from `rpc/client.rs` as the
`CLIENT_ERR_*` constants below; server-side codes are module-private there, so only the
number reaches you. Chapters 7 and 18 explain what each code means in context and what to do
about it.

| Value | Constant | |
| --- | --- | --- |
| 0 | — | success |
| 2 | *(server-side `ENOENT`)* | no handler registered for that rpc id |
| 5 | `CLIENT_ERR_IO` | send failed at the channel layer |
| 11 | `CLIENT_ERR_AGAIN` / `CLIENT_ERR_WOULD_BLOCK` / `CLIENT_REQUEST_QUEUE_REJECTED_ERROR` | the last is the offline queue refusing an incoming request or evicting an older one; it is 11 on Linux and 35 only on macOS (`#[cfg(target_os = "macos")]`) |
| 16 | `CLIENT_ERR_BUSY` | circuit breaker open, or async slot occupied |
| 17 | *(server-side `EEXIST`)* | returned by `reg_rpc` for a duplicate id |
| 22 | `CLIENT_ERR_INVALID_ARGUMENT` | also what the server replies for a truncated request frame |
| 32 | `CLIENT_ERR_BROKEN_PIPE` | |
| 101 | `CLIENT_ERR_NETWORK_UNREACHABLE` | |
| 103 | `CLIENT_ERR_CONNECTION_ABORTED` | |
| 104 | `CLIENT_ERR_CONNECTION_RESET` | |
| 107 | `CLIENT_ERR_NOT_CONNECTED` | no connection, or the channel is closed |
| 110 | `CLIENT_ERR_TIMED_OUT` | the one-second future cap, or a request timeout |
| 111 | `CLIENT_ERR_CONNECTION_REFUSED` | |
| 113 | `CLIENT_ERR_HOST_UNREACHABLE` | |
| 125 | `CLIENT_ERR_CANCELED` | |

**`RpcError`** in `srpc.errors` is a separate, categorized enumeration used by the
connection-error callback (`add_on_error`) and by `clientconn_map_system_error`. It does not
appear in a future's error code.

```cpp srpc-no-compile
enum class RpcError {
    OK = 0,
    // 100–199 connection
    NOT_CONNECTED = 100, CONNECTION_REFUSED, CONNECTION_RESET,
    NETWORK_UNREACHABLE, HOST_UNREACHABLE, CONNECTION_CLOSED, CIRCUIT_OPEN,
    // 200–299 protocol
    INVALID_MESSAGE = 200, UNKNOWN_RPC_ID, MARSHALLING_ERROR,
    VERSION_MISMATCH, CHECKSUM_ERROR,
    // 300–399 application
    RPC_FAILED = 300, SERVICE_UNAVAILABLE, PERMISSION_DENIED,
    INVALID_ARGUMENT, NOT_FOUND, ALREADY_EXISTS,
    // 400–499 timeout
    CONNECT_TIMEOUT = 400, REQUEST_TIMEOUT, RESPONSE_TIMEOUT,
    IDLE_TIMEOUT, HEARTBEAT_TIMEOUT,
    // 500+ internal
    UNKNOWN_ERROR = 500, OUT_OF_MEMORY, INVALID_STATE, INTERNAL_ERROR,
};

enum class RpcErrorCategory { NONE=0, CONNECTION, PROTOCOL, APPLICATION, TIMEOUT, INTERNAL };

const char* rpc_error_to_string(RpcError err);
const char* rpc_error_category_to_string(RpcErrorCategory cat);
RpcErrorCategory get_error_category(RpcError err);
bool is_connection_error(RpcError err); // 100–199
bool is_timeout_error(RpcError err);    // 400–499
bool is_retryable_error(RpcError err);
```

`is_retryable_error` is an explicit list, not a range: `CONNECTION_RESET`,
`NETWORK_UNREACHABLE`, `HOST_UNREACHABLE`, `CONNECT_TIMEOUT`, `REQUEST_TIMEOUT`,
`RESPONSE_TIMEOUT` and `SERVICE_UNAVAILABLE`. Nothing in the request path consults it — it
is there for callers to use.

---

## 17. Pitfalls and Best Practices

Almost none of the traps in this chapter are compile errors. They are in
three places: the client's timeout model, which is far stricter than it looks;
the server's dispatch modes, two of which put your code on the poll thread; and
the reactor's event rules, which are enforced by assertions that kill the
process rather than by the type system.

### The blocking call gives up after one second

`Future` is constructed with `timeout_` fixed at 1,000,000 microseconds, and
nothing in the library ever changes that field. Every blocking entry point —
`wait()`, `get_error_code()`, `get_reply()` — routes through the same timed wait,
so one second is the deadline for the generated proxy's synchronous call, for
`resolve()` on a typed future, and for a bare `wait()`.

Worse, the deadline latches. When it fires, the future sets `timed_out` and
stores error 110 (`ETIMEDOUT`); when the real reply arrives afterwards, the
notify path refuses to mark a timed-out future ready, so the payload is decoded,
counted in the connection's metrics, and thrown away. A server that answers in
1.2 seconds looks exactly like a server that never answers.

```cpp srpc-no-compile
// The 1s cap applies here — `resolve()` calls get_error_code().
auto result = demo.sum(req);
if (result.is_err() && result.unwrap_err() == 110) {
    // Could be a dead server. Could be a healthy server that took 1.05s.
}
```

The escape is `request_with_options`, and it needs one non-obvious extra step:
the coordinator future that `request_with_options` hands back is created with
`timeout_ms = 0` (the internal attempts own the per-attempt timeout), and
`wait_with_options()` treats a zero budget as "fall back to `wait()`" — which is
the 1s cap again. Give the coordinator its own budget first:

```cpp srpc-no-compile
auto opts = RequestOptions::defaults();
opts.timeout_ms = 500;

auto fu = cl->request_with_options(DemoService::SUM, opts,
    [&](BinaryWriteArchive& m) { srpc::Serialize_::serialize(a, m); });

auto f = fu.unwrap();
auto wait_opts = opts;
wait_opts.timeout_ms = 5000;   // must cover the whole attempt chain
f->set_options(wait_opts);     // takes a const RequestOptions&, not a pointer
f->wait_with_options();
```

### `fast` handlers run on the poll thread, and so does the head of an `async` one

`fast` and `prefix` (they are the same attribute) register through
`reg_fast_rpc`, and the dispatcher calls them inline from the frame callback —
on the poll thread, with no fiber underneath. There is nothing to yield to.
Blocking there does not stall one request; it stalls the epoll loop, so every
connection that thread owns stops reading, stops writing replies, and stops
noticing that its peers went away.

`async` registers through `reg_fast_rpc` too. The spawn helper polls the task
once, inline, before it registers a poller — so everything in the coroutine up
to the first suspension point also runs on the poll thread. Only what comes
after a suspension is resumed by the reactor.

```cpp srpc-no-compile
// BAD — `fast` handler that blocks the poll thread.
rusty::Result<RpcLookupResponse, i32> lookup(const RpcLookupRequest& req) override {
    auto row = db_.blocking_query(req.key);        // stalls every connection
    return rusty::Result<RpcLookupResponse, i32>::Ok(make_resp(row));
}

// GOOD — drop the `fast` keyword in the .rpc file. The default dispatch
// spawns a fiber per request, which may block, sleep, or make nested calls.
```

Use `fast` for handlers that are pure computation over the decoded request and
return immediately. Everything else belongs on the default (fiber) path, or on
`defer` if the answer arrives from somewhere else later.

### Configure the client before `connect` — except buffering

Four settings are staged on the `Client` and pushed into the connection object
that `connect` builds: keepalive, heartbeat, circuit breaker, and reconnect
policy. So is `set_channel_factory` — install the in-memory transport before
`connect` or you get TCP, which `connect` auto-installs when nothing is bound.
Calling the four after `connect` reaches the live connection as well, and that
connection object survives auto-reconnect, so nothing is lost there; what the
staged copies decide is how the *next* `Client::connect` builds its connection.

`set_buffering_config` is the exception in the other direction. It has no staging
slot: it forwards to the connection if one exists and silently does nothing if
one does not. Called before `connect`, it is a no-op, and the disconnect queue
keeps its defaults — which are *enabled*, queueing up to 1000 requests with a
30-second TTL. So this is the setting to get right in both directions: turning
buffering off with `BufferingConfig::disabled()` before `connect` leaves it on.

```cpp srpc-no-compile
auto cl = Client::create(poll);

cl->set_reconnect_policy(ReconnectPolicy::conservative());
cl->set_heartbeat(HeartbeatConfig::defaults());
cl->set_circuit_breaker(CircuitBreakerConfig::defaults());

cl->connect(reinterpret_cast<const int8_t*>(addr), true);

cl->set_buffering_config(BufferingConfig::defaults());   // AFTER, or it is lost
```

### Retries are opt-in twice

`RequestOptions::can_retry` is `idempotent && retry_count < max_retries`. Setting
`max_retries` alone does nothing — and it does not merely fail to retry: the
request path clamps `max_retries` to zero when `idempotent` is false, so there is
no diagnostic either. `defaults()` leaves `idempotent` false; `with_retry`,
`idempotent_retry`, `fast` and `patient` all set it true.

```cpp srpc-no-compile
auto opts = RequestOptions::defaults();
opts.max_retries = 3;
opts.idempotent  = true;    // without this line, max_retries is silently zeroed
```

The retry chain runs on a detached thread of its own, and the serialized argument
bytes are replayed verbatim on every attempt — so an attempt that the server did
process but whose reply was lost will be processed again. That is what
`idempotent` is asserting.

### Do not subclass a non-`abstract` generated service

For an `abstract` service (or a method with a trailing `= 0` in the IDL) the
generator emits pure virtuals and subclassing works normally. Without `abstract`
it emits `virtual R m(const Req&);` — declared, never defined, anywhere. The
class therefore has no key function, its vtable is never emitted, and a subclass
fails at *link* time with undefined references even though every method is
overridden.

Two ways out: mark the service `abstract` in the `.rpc` file, or keep it
non-abstract and define the generated virtuals out of line in a `.cc`, the way
`tests/benchmark_service.cc` does. Do not try to fix it by adding overrides.

### Events belong to one thread, one fiber, and one wait

`wait()` on any event opens with two thread assertions: a reactor must exist on
this thread, and it must be *this* reactor's thread. A third assertion — that a
fiber is running — fires only on the path that actually suspends, so `wait()` on
an event that is already ready, or on one that has already completed, returns
without ever checking. All three are `verify` calls — they print a stack trace
and terminate the process, they do not return an error.

A waiting event stores exactly one weak fiber handle, and the source says so:
*"for now only one fiber can wait on an event."* A second waiter overwrites the
first, and the first is never resumed.

An event is also single-use. Once it has completed, its status is `DONE`, and
`wait()` on a `DONE` event returns immediately without blocking — the code path
is literally commented "second use of the event". Create a fresh event per
round.

Setting an event from another thread is a data race, not merely bad style. The
value, the status and the wait bookkeeping are plain `Cell`s, and `set()` runs
the readiness test inline, which touches a non-atomic `Rc` refcount — the source
guards exactly that step with an owner-thread check and spells out the
consequence of skipping it, a corrupted count. And even when nothing is
corrupted, no wakeup crosses the thread boundary: the waiting fiber resumes only
the next time that reactor's own loop runs, which under a poll thread is within a
millisecond and in a hand-driven reactor is never.

```cpp srpc-no-compile
auto reactor = Reactor::get_reactor();
auto ev = create_sp_int_event(1);          // ready when value >= 1

reactor->create_run_fiber([ev]() {
    ev->wait();                            // or ev->wait_timeout(500 * 1000)
    // ...
});

ev->set(1);                                // same thread as the reactor
reactor->run_loop(false, true);            // drains ready work, then returns
```

The event factories are named free functions — `create_sp_int_event`,
`create_sp_timeout_event`, `create_sp_never_event`, `create_sp_waitany`,
`create_sp_waitall`, `create_sp_waitall_from`, `create_sp_box_event<T>` and — in
global `::janus`, not `srpc` — `janus::create_sp_quorum_event` — not a
`Reactor::create_sp_event<T>` template, and the methods are lowercase (`wait`,
`wait_timeout`, `set`, `test`). Timeouts are in
microseconds, and `wait_timeout` only fires if the loop that is draining events
was asked to check timeouts (`run_loop(..., true)`).

### Know who is running the loop

In an RPC program you never call `run_loop` yourself: the poll worker calls
`run_loop(false, true)` on every epoll iteration, and starting a fiber drains
events once on the way out. Standalone fiber code is different.
`Fiber::create_run` *runs the body immediately*, on the calling thread, up to its
first suspension point. What needs a loop is the resumption. A fiber that is never resumed is a fiber whose event
nobody drained.

`run_loop(true, ...)` is not the answer for a program that wants to stop. The
`infinite` flag is latched at entry and nothing ever clears it, so that call
never returns, and it is a busy spin rather than a blocking wait.

```cpp srpc-no-compile
// The body runs now, up to the first wait; the rest needs a drain.
reactor->create_run_fiber([ev]() { ev->wait(); finish(); });
ev->set(1);
reactor->run_loop(false, true);       // finish() runs here
```

### Sleep and lock the fiber way

`this_fiber::sleep_ms` / `sleep_us` / `sleep_s` suspend the fiber by waiting on a
timeout event; `Time::sleep` and `std::this_thread::sleep_for` stop the whole
thread and every fiber on it. Two sharp edges: `this_fiber::sleep_us(0)` returns
immediately without yielding (use `this_fiber::yield()` to give up the CPU), and
a non-zero sleep called outside a fiber hits the "can't wait outside a fiber"
assertion and aborts the process.

srpc's own lock is `SpinLock`, and it ships no RAII guard to go with it: the
interface is bare `lock()` / `unlock()`. Its slow path sleeps the *thread* for 50
microseconds per round, so a fiber that contends for one blocks every other fiber
on that thread. Keep the critical section to a few instructions, never hold it
across a yield, a sleep, or an RPC call, and wrap it in your own scope guard if
you want exception safety.

### Application error codes can trip the circuit breaker

The breaker counts a reply's error code as a transport failure when it is one of
107, 111, 104, 103, 110, 113, 101 or 32. Those are just integers on the wire, so
a handler that returns `Err(110)` to mean "your deadline, not mine" contributes
to opening the client's breaker — after five of them the client rejects
everything locally with 16 (`EBUSY`) for 30 seconds. Choose application error
codes outside that set.

The inverse is worth knowing too: a *client-side* wait timeout is never recorded,
because the timeout latches on the future without going through the breaker. A
server that is alive but slower than the wait cap will never open the breaker, no
matter how many calls time out.

### `request_async` has no timeout and a fixed slot table

The callback form parks its callback in a table of 16,384 slots indexed by
`xid % 16384`. If the slot is taken the call fails immediately with 16
(`EBUSY`). A slot is released by the matching reply, by a dispatch error, or by a
disconnect drain — nothing else, and there is no timer. A request the server
never answers holds its slot for the life of the connection, and the delivery
path does not re-check the xid, so a very late reply can be handed to whichever
callback occupies its slot by then. Use the future-based path when you need a
deadline.

### Checklist

Do:

- Give any call that may exceed a second an explicit budget via
  `request_with_options` plus `set_options` on the returned future.
- Keep `fast`, `prefix` and the pre-suspension part of `async` handlers
  non-blocking; put anything else on the default fiber path.
- Stage client configuration before `connect`, and `set_buffering_config` after.
- Set `idempotent` whenever you set `max_retries`.
- Create a fresh event per wait, and wait on it from the fiber that owns it.
- Use `this_fiber::sleep_*` and `this_fiber::yield()` inside fibers.
- Register services with `reg_service_typed(rusty::make_box<T>())`; the generated
  class has no base to inherit from.
- Hand ownership across the API in `rusty::Box` / `rusty::Arc` — that is what
  `reg_service_typed`, `Client::create` and the channel factories take and
  return.

Don't:

- Don't touch an event from a thread other than its reactor's, and don't expect
  a set from elsewhere to wake anyone.
- Don't put two waiters on one event, and don't reuse one after it completes.
- Don't call `run_loop(true, ...)` unless you intend never to return.
- Don't call `std::this_thread::sleep_for` or `Time::sleep` inside a fiber.
- Don't hold a `SpinLock` across a suspension point.
- Don't subclass a generated service that is not `abstract`.
- Don't read `Client::metrics()` — it is a permanently zeroed stub. Go through
  `client->connection()`, which returns an `Option`, and read
  `metrics()` off the connection.

---

## 18. Troubleshooting

SRPC has exactly two ways of telling you that something went wrong: a log line
on stdout, and a `verify` failure that prints a stack trace and kills the
process. Neither is quiet by default, and neither is configurable from the
environment. Start by reading them.

### Reading the log

The logger is a process-wide level plus a synchronous sink. `LOG_LEVEL_S`
starts at `Log::DEBUG` (4), which means *everything* is enabled out of the box;
the only knob is `Log::set_level`, and there is no environment variable that
touches it.

```cpp srpc-no-compile
Log::set_level(Log::ERROR);      // FATAL 0, ERROR 1, WARN 2, INFO 3, DEBUG 4
```

Lines go to `std::cout`, one flush each, shaped like

```
W [<unknown>:0] 2026-08-29 14:03:11.482 | srpc::ServerConnection: no handler for rpc_id = 271861483
```

The `<unknown>:0` is not a bug: library code calls `log_line(level, 0, nullptr,
...)` and only fills in a file and line when a caller supplies one. Every line
you see with that prefix came from inside srpc.

The variadic `Log_debug` / `Log_info` / `Log_warn` / `Log_error` / `Log_fatal`
wrappers are **not** part of the library — a parameter pack cannot cross the
Rust-to-C++ boundary, so they live on the consumer side in `tests/srpc_log.h`.
Copy that header into your own tree, or call `log_line` directly with
`std::format`.

### What the error codes mean

Every failure surfaces as an errno-shaped `srpc::i32`. These are the ones the
library actually produces:

| Code | Name | Where it comes from |
|---|---|---|
| 2 | `ENOENT` | The server has no handler registered for that rpc id. It replies with this code and logs a warning once per unknown id. |
| 5 | `EIO` | The channel refused the outbound frame — the peer is gone, the frame is over the 64 MiB bound, or the connection's 4 MiB outbound buffer is full. The pending future is removed before the error returns. |
| 11 | `EAGAIN` | A disconnect-buffered request was evicted or refused: the queue is full — under the default `DROP_OLDEST` this code goes to the *oldest* entry, not the new one — or the queue itself is disabled. |
| 16 | `EBUSY` | The circuit breaker is open — or, on `request_async`, the callback slot for this xid is already occupied. |
| 22 | `EINVAL` | The address does not parse, the host/network is unreachable, `connect` was called from a state that forbids it, or the server got a request frame too short to hold an rpc id. |
| 32, 101, 103, 104, 111, 113 | `EPIPE`, `ENETUNREACH`, `ECONNABORTED`, `ECONNRESET`, `ECONNREFUSED`, `EHOSTUNREACH` | Transport failures, reported as-is. |
| 107 | `ENOTCONN` | There is no live connection: `connect` never succeeded, the client was closed, the channel is dead, or a disconnect invalidated the in-flight futures. |
| 110 | `ETIMEDOUT` | The one-second future cap fired — or a buffered request outlived its TTL. |
| 125 | `ECANCELED` | `reconnect()` was aborted. |

Note that 110 has two very different meanings and no way to tell them apart from
the code alone. See "the blocking call gives up after one second" in chapter 17.

### Client symptoms

| Symptom | Cause | What to do |
|---|---|---|
| Every call returns 110 after about a second | The future's built-in 1s deadline, which latches even if the reply arrives later | `request_with_options`, then `set_options` a real budget on the returned future before `wait_with_options()` |
| The very first call returns 107 | `connect` failed, so the `Client` stored no connection at all | Read the `ERROR` log line — it names the address and the reason |
| Calls start returning 16 and keep doing so | The breaker opened after 5 transport failures and stays open for 30s | `client->connection()`, then `circuit_breaker_state()`; it needs 3 successes after half-opening to close |
| A call made while the link is down returns `Ok`, then times out | Disconnect buffering is on by default (queue, 1000 entries, 30s TTL), so the request was parked rather than sent, and the waiter then hit the 1s cap | Check `connected()` first, or install `BufferingConfig::disabled()` *after* connect to get 107 immediately |
| Requests vanish across a reconnect | Buffered requests are parked with a TTL and expired, never replayed | Treat a disconnect as a failure and re-issue at your layer |
| The connection dies and nothing reconnects | Auto-reconnect only fires when a reconnect address was recorded and the policy allows it | Check for `auto-reconnect triggered after connection failure` at `INFO` |
| Heartbeats are configured but never sent | Nothing ticks the client-side heartbeat timer; the protocol is complete but unwired | Do not rely on heartbeats to detect a dead peer |
| Every metric reads zero | `Client::metrics()` returns a permanently empty stub | Unwrap `client->connection()` — it is an `Option` — and read `metrics()` off the connection |
| `LEAST_CONNECTIONS` / `LEAST_LATENCY` pooling never differentiates | Both strategies, and the pool's health check, read that same stub | Use `ROUND_ROBIN` or `RANDOM` |

### Server symptoms

| Symptom | Cause | What to do |
|---|---|---|
| `no handler for rpc_id = N` at `WARN`, client sees 2 | Client and server disagree about method ids | Regenerate *on top of* the old header — ids are random and stabilized only by scraping the previous one |
| Same warning, but only for some methods | The service was never registered, or was registered after `start()` | `reg_service_typed(rusty::make_box<T>())` before `start()`; registrations are frozen into an immutable context by `start` |
| `empty channel-mode request frame, dropping` | A frame arrived with no body, so there is no xid to reply against | Look upstream — this is a framing problem, not an application one |
| `DeferredReply::reply() called multiple times, ignoring` | A `defer` handle replied twice | Both `reply()` and `reply_error()` fire at most once, in total |
| The whole server stops responding under load | A `fast`, `prefix`, or pre-suspension `async` handler blocked the poll thread | Move the work to the default (fiber) dispatch |
| One connection stops, the rest are fine | The peer's stream desynchronized and the frame bound closed it | See below |

### A reply that matches nothing is dropped in silence

The client resolves an inbound reply in two steps: the async callback slot at
`xid % 16384` first, then the pending-future map. If neither has an entry the
payload is simply discarded. There is no log line and no error. The only trace
is that the connection's `bytes_received` counter moved, because inbound
accounting happens before the lookup.

A future that merely timed out is a different case, and a noisier one: nothing
removes it from the pending map, so the late reply still matches. It is decoded
into the future, counted as a completion or a failure, and fed to the circuit
breaker — and only then dropped, because `notify_ready` refuses to mark a
timed-out future ready (chapter 17). The reply that truly matches nothing is one
whose future was already released: `request_with_options` calls `handle_free` on
every attempt it gives up on, and each of those attempts can still be answered
later.

So "the server says it replied and the client never saw it" is usually not a lost
packet; it is a reply that arrived after the client stopped caring.

The slot path has a second sharp edge: it takes whatever callback occupies the
slot without re-checking the xid (the future path does check). A reply that
arrives after 16,384 further async requests can therefore be handed to the wrong
callback.

### A desynchronized stream is caught by the frame bound

The frame header is 4 bytes, **native-endian**: bit 31 is the extended-header
flag, bits 0-30 are the payload size. Two consequences.

First, peers must agree on byte order. There is no network-order conversion
anywhere in the framing path, so a big-endian peer is not merely slow or
mismatched — its first header is nonsense.

Second, `kMaxFramePayloadSize` (64 MiB) exists to make nonsense *visible*.
Without it, a desynchronized stream reads payload bytes as a header, gets a
garbage length, and waits forever for bytes that never come: no error, no log, no
close, no reconnect. With it, the decoder returns `Malformed`, the transport
raises `malformed frame on inbound stream`, closes the connection, and the client
side runs its normal disconnect fan-out — in-flight futures fail with 107 and
auto-reconnect starts. A connection that dies with that message is telling you
the stream was corrupt, not that the peer went away.

The bound is also the only real message-size limit, and the send path checks the
same constant, so an oversized message never reaches the wire: the request fails
locally with 5 (`EIO`).

### Connect and bind failures

`Client::connect` returns 0 or an error code, and logs the reason at `ERROR`
before returning:

```
E [<unknown>:0] ... | srpc::ClientConnection: factory connect failed: ConnectionRefused (addr=127.0.0.1:8848)
```

- **22 (`EINVAL`)** — the address did not parse, or the route was refused at a
  level that maps to "address invalid": host unreachable, network unreachable,
  address not available. Addresses are parsed as an IPv4 socket address, i.e.
  literally `a.b.c.d:port`. `localhost:8848` does not resolve here, and neither
  does an IPv6 address.
- **111 (`ECONNREFUSED`)** — nothing is listening.
- **107 (`ENOTCONN`)** — everything else, including a timed-out connect.

When `connect` fails, the `Client` keeps no connection object at all, so
`connection()` returns `None` and every later request fails with 107 until a
`connect` succeeds.

On the server side, `start()` returns -1 and logs

```
E [<unknown>:0] ... | srpc::Server::start: channel listener failed to bind 127.0.0.1:8848: AddressInUse
```

`AddressInUse` after a previous run is the usual TIME_WAIT story. For tests, bind
to port `0` and ask the server which port it got:

```cpp srpc-no-compile
svr.start(reinterpret_cast<const int8_t*>("127.0.0.1:0"));
int port = svr.get_bound_port();       // -1 if the listener is not up
```

### The process aborted with "verify failed"

`verify` is srpc's assertion. On failure it prints a full stack trace to stderr,
then panics with `verify failed at <file>, line <n>`. For the reactor, `base/`
and `misc/` assertions the file is the *generated* module, since that is where
the call site lives. `rpc/client.rs` is the exception: it routes its four
assertions through a local `client_verify` that hard-codes the canonical Rust
path, so those always read `verify failed at rpc/client.rs, line 0`. The process
does not continue.

The reactor's assertions are the ones you are most likely to hit:

- waiting on an event outside a fiber (`this_fiber::sleep_*` counts — it waits on
  a timeout event);
- waiting on an event, running the loop, spawning a stackless task, or destroying
  a reactor from a thread that is not that reactor's thread;
- waiting on an event from a fiber the scheduler has already finished or
  recycled.

They all say the same thing in different words: reactor state was touched from a
context that does not own it — the wrong thread, or no fiber at all. There is no
mode that downgrades these to errors; the assertion is the diagnostic.

### Shutdown hangs

`graceful_shutdown(ms)` is four steps: `stop_accepting()`, `drain(ms)`, run the
shutdown hooks, `do_shutdown()` (which releases everyone blocked in
`wait_for_shutdown()`). The middle two are where it hangs.

`drain` busy-waits on the in-flight request counter, sleeping a millisecond per
round, until it reaches zero or the timeout expires. That counter includes the
request you are serving — so a handler that calls `graceful_shutdown` on itself
always burns the entire drain timeout before continuing. Shut down from a signal
handler or another thread, not from inside a handler.

The hooks run while the hook list's mutex is held, which is deliberate and
documented. A hook that tries to register another hook deadlocks.

`PollThread::shutdown()` is idempotent and logs its way through every step at
`DEBUG` — `Sending CmdShutdown`, `Acquiring join_handle lock...`,
`Calling thread.join()...`, `Complete`. If a process hangs at exit, the last of
those lines tells you exactly where. Called from the poll thread itself it skips
the join rather than deadlocking, which means the worker outlives the call.

The teardown order that works: close the clients, destroy the `Server` (that is
what closes already-accepted connections), then shut down the poll thread.

### Tools that exist in this repository

The build is CMake and Ninja; there is no makefile and no debug/release
environment switch:

```sh
# Rust lane — no C++ toolchain, no submodules, seconds not minutes.
cargo test --locked --workspace --all-targets

# C++ lane.
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 4
ctest --test-dir build -L srpc --output-on-failure

# One suite, then one case inside it.
ctest --test-dir build -R '^test_fiber$' --output-on-failure
./build/test_fiber --gtest_filter='FiberTest.SleepUsZero'

# Sanitizers are a whole-configuration switch: give them their own tree.
cmake -S . -B build-asan -G Ninja -DSRPC_SANITIZER=address   # none|address|thread|undefined
```

srpc always compiles with `-O2 -g -fno-omit-frame-pointer`, regardless of build
type, so `gdb ./build/test_fiber` gives usable frames without a special build.
Those flags are exported `PUBLIC`, which is also why your own warnings disappear
once you link srpc: the `-w` in that same list comes along.

Three caveats about the tests themselves. Filter with `-L srpc`: the vendored
rusty-cpp subdirectory registers about 69 tests whose binaries are not in `ALL`,
so a bare `ctest` reports them as "Not Run" and exits non-zero. `ctest -L srpc`
selects the 14 this project owns, of which 8 are the runtime battery — and if the
googletest submodule is missing, CMake only warns and registers 5, so a green run
is not proof the battery ran. And of the 76 `.cc` files in `tests/`, CMake builds
exactly 9; the rest are not compiled
by anything and several no longer build at all. Do not use them as a reference
for current API.

---
