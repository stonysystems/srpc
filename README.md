# SRPC

SRPC is a Simple RPC framework for C++23. You describe a service in a small `.rpc`
file, run the code generator, and get a typed server base class and a typed client
proxy. The runtime underneath is an epoll reactor with stackful fibers, a pluggable
transport, and the usual production plumbing — timeouts, retries, reconnect,
circuit breaking, connection pooling.

It descends from [simple-rpc](https://github.com/santazhang/simple-rpc), and the IDL
will look familiar. The C++ it generates will not: handlers take a typed request
struct and return `rusty::Result`, and everything lives in namespace `srpc::`. See
[What changed since simple-rpc](#what-changed-since-simple-rpc).

## Getting the code

Clone the repo and pull the submodules:

```sh
git clone https://github.com/stonysystems/srpc
cd srpc
git submodule update --init --recursive
```

`third-party/rusty-cpp` is the pinned transpiler; CMake refuses to configure without it.

For the full C++ build you need **Clang 22 or newer with libc++**, CMake 3.30+,
Ninja, Cargo (with clippy), and Python 3.11+. The Rust-only lane needs none of the
C++ toolchain.

## Writing the .rpc file

First, write your `.rpc` file. Declare a namespace, any structs you need, and one or
more services:

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

`abstract` makes every generated handler pure virtual. Use it whenever you plan to
subclass the generated service — see [Implementing the server
side](#implementing-the-server-side) for why.

The `|` separates input parameters from output parameters, and it is optional — a
method may have inputs only, outputs only, or neither. Integers must be written with
an explicit size: `i8`, `i16`, `i32`, `i64`, or the varint-encoded `v32` and `v64`.
Writing `int`, `long`, `bool` or `unsigned` is an error, on purpose. Note that `v32`
and `v64` fields come out as the `srpc::v32` / `srpc::v64` wrapper structs rather than
arithmetic types — read and write them with `.get()` and `.set()`.

The STL types `string`, `pair`, `vector`, `list`, `set`, `map`, `unordered_set` and
`unordered_map` are recognized and get a `std::` prefix for you. Templates nest, so
`map<i32, vector<string>>` is fine. Any other name is passed through untouched, which
is how `point3` above works and how you reach your own types — except that a name
*beginning* with a reserved word (`int`, `bool`, `long`, `unsigned`, `struct`,
`service`, `namespace`, or a dispatch keyword) tokenizes as that keyword and is
rejected or misparsed.

Comments run from `//` to end of line, but they must not be empty — a line containing
exactly `//` is a syntax error. Semicolons are ignored, so use them or don't.

The keyword in front of a method — `fast` and `defer` above — tells the server how to
run your handler. Leave it off for now; [Choosing how each method is
dispatched](#choosing-how-each-method-is-dispatched) covers all of them.

If you need to splice raw C++ into the generated header, the file needs **two** lines
containing exactly `%%`: text above the first lands near the top of the header, the IDL
goes between the two, and text below the second lands at the bottom. A file with only
one `%%` gets no header section — the text above it is parsed as IDL instead.

## Generating the code stubs

Run the generator against the `.rpc` file you wrote:

```sh
PYTHONPATH=/path/to/srpc/pylib python3 -c \
  "from simplerpcgen.rpcgen import rpcgen; rpcgen('demo.rpc', ['cpp'])"
```

That writes `demo.h` next to `demo.rpc`. Pass `['cpp', 'python']` if you also want the
Python stub.

**Regenerate on top of the old header — never into a clean directory.** Each method
gets a random id from the range `0x10000000`–`0x70000000`, and the generator keeps
those ids stable only by reading them back out of the header it is about to overwrite.
Lose the old header and every id changes, silently breaking the wire for anything built
against it. Renaming a method reassigns its id for the same reason.

Generation is not part of the build. Run it by hand and commit the output, the way
`tests/benchmark_service.h` is committed here.

For each service the generated header contains a `<Svc>Service` class and a
`<Svc>Proxy` class. Everything else is a *member* of `<Svc>Service`: one
`Rpc<Method>Request` and one `Rpc<Method>Response` struct per method, an `enum` of
method ids, and the registration and dispatch glue. `<Method>` is the method name split
on `_` with each part capitalized, so `dot_prod` gives `RpcDotProdRequest` and
`slow_echo` gives `RpcSlowEchoResponse`.

The structs are nested, so from outside the class they are spelled
`DemoService::RpcSumRequest` — `demo::RpcSumRequest` does not exist. `DemoProxy`
re-exports each one with a `using`, which is why the client code below says
`DemoProxy::RpcSumRequest`. For `sum` above you get, inside `class DemoService`:

```cpp
    struct RpcSumRequest {
        srpc::i32 a;
        srpc::i32 b;
        srpc::i32 c;
    };
    struct RpcSumResponse {
        srpc::i32 result;
    };

    // typed service signatures
    virtual rusty::Result<RpcSumResponse, srpc::i32> sum(const RpcSumRequest& req);
```

One thing to fix up: the generated header opens with `#include "srpc/srpc.hpp"`, which
assumes SRPC sits in a directory named `srpc` on your include path. For a header
generated into `tests/`, change that line to `#include "../srpc.hpp"`. The generator
rewrites the line on every run, so re-apply the edit after each regeneration. The
committed `tests/benchmark_service.h` still carries the unedited form — nothing in the
build compiles it, so nothing catches it there.

## Implementing the server side

Those functions are to be implemented by you. Inherit from the generated service and
override them:

```cpp
#include "demo.h"

using namespace srpc;
using namespace demo;

class MyDemoService : public DemoService {
public:
    rusty::Result<RpcSayhiResponse, i32> sayhi(const RpcSayhiRequest& req) override {
        printf("%s\n", req.hi.c_str());
        return rusty::Result<RpcSayhiResponse, i32>::Ok(RpcSayhiResponse{});
    }

    rusty::Result<RpcSumResponse, i32> sum(const RpcSumRequest& req) override {
        RpcSumResponse resp{};
        resp.result = req.a + req.b + req.c;
        return rusty::Result<RpcSumResponse, i32>::Ok(resp);
    }

    rusty::Result<RpcDotProdResponse, i32> dot_prod(const RpcDotProdRequest& req) override {
        RpcDotProdResponse resp{};
        resp.v = req.p1.x * req.p2.x + req.p1.y * req.p2.y + req.p1.z * req.p2.z;
        return rusty::Result<RpcDotProdResponse, i32>::Ok(resp);
    }

    void slow_echo(const RpcSlowEchoRequest& req, RpcSlowEchoResponse& resp,
                   DeferredReply defer) override {
        resp.echoed = req.msg;
        defer.reply();
    }
};
```

Return `::Ok(resp)` with the response filled in, or `::Err(code)` with an error code of
your choosing. The generated wrapper does all the (de)serialization and sends the reply
for you.

Override every method — because the service is `abstract`, the generated virtuals are
pure and the compiler will not let you forget one. (You can also make a single method
pure with a trailing `= 0` in the IDL instead of marking the whole service.)

If you leave the service non-`abstract`, do not subclass it. Its virtuals are then
declared but never defined, so the class has no key function and its vtable is never
emitted — subclassing fails at link time even when you override everything. For a
non-`abstract` service, define the generated virtuals out of line in a `.cc` instead,
the way `tests/benchmark_service.cc` does.

## Starting the RPC server

Make a poll thread, make a server on it, register the service, and start listening:

```cpp
#include "demo.h"

using namespace srpc;
using namespace demo;

int main() {
    auto poll = PollThread::create();
    auto svr = Server::new_(rusty::Some(poll));

    svr.reg_service_typed(rusty::make_box<MyDemoService>());

    const char* addr = "127.0.0.1:8848";
    if (svr.start(reinterpret_cast<const int8_t*>(addr)) != 0) {
        return 1;
    }

    // Blocks until something calls svr.graceful_shutdown(...) — a signal handler,
    // say, or another thread holding a reference to `svr`. With nothing to call it,
    // this runs until the process is killed.
    svr.wait_for_shutdown();
    return 0;
}
```

`start()` returns 0 on success and -1 on failure. It takes `const int8_t*`, so the
`reinterpret_cast` is required. TCP is installed automatically; you only have to touch
the transport if you want a different one.

Bind to port `0` and call `svr.get_bound_port()` if you want the OS to pick a port.

To stop, call `graceful_shutdown` with a drain timeout in milliseconds. It stops
accepting, waits for in-flight requests, runs any hooks you registered, and releases
anyone blocked in `wait_for_shutdown()`:

```cpp
svr.add_shutdown_hook([]() { /* runs during shutdown */ });
svr.graceful_shutdown(30000);
```

Destroying the `Server` is what closes connections that were already accepted, so the
usual teardown order is: close the clients, destroy the server, shut down the poll
thread.

## Writing a client

The generated proxy makes an RPC call very easy to use. Build a poll thread and a
client, connect, and wrap the client in the proxy:

```cpp
#include "demo.h"

using namespace srpc;
using namespace demo;

int main() {
    auto poll = PollThread::create();
    auto cl = Client::create(poll);

    const char* addr = "127.0.0.1:8848";
    if (cl->connect(reinterpret_cast<const int8_t*>(addr), true) != 0) {
        return 1;
    }

    DemoProxy demo(const_cast<Client*>(cl.get()));

    DemoProxy::RpcSumRequest req;
    req.a = 1; req.b = 2; req.c = 3;

    auto result = demo.sum(req);
    if (result.is_ok()) {
        printf("1 + 2 + 3 = %d\n", result.unwrap().result);
    }

    cl->close();
    poll->shutdown();
    return 0;
}
```

The `const_cast` is needed because `rusty::Arc<T>::get()` hands back a `const T*` while
the proxy constructor wants a mutable one.

The blocking form waits **one second**, then returns `Err(110)` (`ETIMEDOUT`) — even if
the server answers later. That deadline is baked into the future, so the async form
does not escape it either: the sync call is `async_sum(req)`, unwrapped, then
`.resolve()` — and `resolve()`, `wait()` and `get_error_code()` all funnel through the
same cap. For a longer budget, see [Setting timeouts and
retries](#setting-timeouts-and-retries).

Error codes are plain `errno`-style integers: 107 `ENOTCONN` if the client is not
connected, 110 `ETIMEDOUT` on timeout, 16 `EBUSY` when the circuit breaker is open,
2 `ENOENT` if the server has no handler for that method id.

## Building your program

Be warned that this is the least finished part of SRPC. There is no `install()` and no
CMake package config, so the only route is `add_subdirectory` — and srpc's CMakeLists
still assumes it is the top-level project. A parent project has to repeat srpc's
directory-scoped toolchain settings, because they do not propagate through the target:

```cmake
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_EXTENSIONS ON)
set(CMAKE_CXX_MODULE_STD ON)
add_compile_options(-stdlib=libc++)
add_link_options(-stdlib=libc++ -lc++abi)

add_subdirectory(srpc)

add_executable(demo_server demo_server.cc)
target_link_libraries(demo_server srpc)
```

Only the include paths and `-march=native` come across on their own. Two more things to
expect: srpc's gate targets are in `ALL` and build paths off `CMAKE_BINARY_DIR`, so a
downstream build will run them and they will look in the wrong directory; and a pure
module consumer may need `CXX_SCAN_FOR_MODULES OFF`, `CXX_MODULE_STD OFF` and an
explicit module map, which is what the in-tree test executables do — see
`scripts/emit_module_map.py`. This path is not exercised by anything in the repo.

Two rules for your own translation units. Put `import std;` after all your textual
`#include`s — libc++ rejects `import std; ... #include <vector>`. And keep
`-march=native` on: it is a module-compatibility requirement rather than a performance
knob, which also means a build tree cannot be copied to a machine with a different CPU.

Run the server in one terminal and the client in another. You should see `1 + 2 + 3 = 6`.

## Calling without blocking

Every method also gets an `async_` form. It returns a typed future immediately, so you
can fire off a batch of requests and collect the answers afterwards:

```cpp
DemoProxy::RpcSumRequest req;
req.a = 1; req.b = 2; req.c = 3;

auto fu = demo.async_sum(req);          // rusty::Result<sumTypedFuture, srpc::i32>
if (fu.is_err()) { return; }

auto typed = fu.unwrap();
// ... issue more calls, do other work ...

auto result = typed.resolve();          // waits, then decodes the response
if (result.is_ok()) {
    printf("result = %d\n", result.unwrap().result);
}
```

The typed future also gives you `ready()`, `wait()`, `get_error_code()` and
`raw_future()`. If you would rather be notified than poll, pass a `FutureAttr` carrying
a callback as the second argument to `async_sum`.

## Setting timeouts and retries

The generated proxy always uses the default request settings. For per-call control,
drop to the client's own entry point with the method id from the service enum:

```cpp
import srpc.request_options;    // not in the srpc.hpp umbrella

auto opts = RequestOptions::defaults();
opts.timeout_ms = 500;
opts.max_retries = 3;
opts.idempotent = true;      // without this, retries are silently disabled

auto fu = cl->request_with_options(DemoService::SUM, opts,
    [&](BinaryWriteArchive& m) {
        srpc::Serialize_::serialize(req.a, m);
        srpc::Serialize_::serialize(req.b, m);
        srpc::Serialize_::serialize(req.c, m);
    });

if (fu.is_ok()) {
    auto f = fu.unwrap();

    // The coordinator future is created with timeout_ms = 0, and
    // wait_with_options() then falls back to a hard-coded 1s cap. Give it
    // a budget that covers the whole retry chain.
    auto wait_opts = opts;
    wait_opts.timeout_ms = 5000;
    f->set_options(wait_opts);

    f->wait_with_options();
    printf("retries: %d\n", f->get_retry_count());
}
```

Retries are opt-in twice over: setting `max_retries` does nothing unless `idempotent`
is also true. The presets `with_retry(max_retries, timeout_ms)`,
`idempotent_retry(max_retries)`, `fast()` and `patient()` set it for you; `defaults()`
does not. The arguments are serialized once and the identical bytes are replayed on
every attempt.

This is request-level idempotency, and it is unrelated to `rpc/idempotency.rs`, which
is a standalone deduplication utility not wired into the request path.

There is also `request_async(rpc_id, write_fn, on_reply)`, which skips the future
entirely and hands the reply to a callback — the cheapest path when you never intend to
wait.

## Keeping the client connected

A client can reconnect on its own, watch the connection with heartbeats, break the
circuit after repeated failures, and park requests while the link is down. Configure
these before you connect:

```cpp
import srpc.heartbeat;          // these three are not in the srpc.hpp umbrella
import srpc.circuit_breaker;
import srpc.reconnect_policy;

auto cl = Client::create(poll);

auto policy = ReconnectPolicy::conservative();   // 5 retries, 1s -> 30s, x2, jitter
cl->set_reconnect_policy(policy);

// Both defaults() presets are ENABLED. You get heartbeats every 10s with a 5s
// timeout, and a breaker that opens after 5 failures for 30s. Leave these calls
// out entirely and both features stay off.
cl->set_heartbeat(HeartbeatConfig::defaults());
cl->set_circuit_breaker(CircuitBreakerConfig::defaults());

cl->add_on_disconnected([]() { /* ... */ });
cl->add_on_reconnected([](bool ok) { /* ... */ });

cl->connect(reinterpret_cast<const int8_t*>(addr), true);

// This one is different: buffering is applied to the live connection,
// so it must be set AFTER connect.
cl->set_buffering_config(BufferingConfig::defaults());
```

Auto-reconnect is on out of the box. Heartbeats and the circuit breaker are off until
you ask for them, which is what the two `set_*` calls above are doing.

For talking to several servers, `ClientPool` keeps a health-checked pool per address
and picks a connection with a `LoadBalancingStrategy` — `RANDOM`, `ROUND_ROBIN`,
`LEAST_CONNECTIONS` or `LEAST_LATENCY`:

```cpp
import srpc.load_balancer;      // also outside the umbrella

auto config = PoolConfig::defaults();
config.load_balancing = LoadBalancingStrategy::ROUND_ROBIN;

auto pool = ClientPool::new_(rusty::Some(poll), config);

auto client_opt = pool.get_client("127.0.0.1:8848");   // Option<Arc<Client>>
if (client_opt.is_some()) {
    auto client = client_opt.unwrap();
    DemoProxy demo(const_cast<Client*>(client.get()));
}
```

## Choosing how each method is dispatched

The keyword before a method name decides how the server runs your handler.

| Attribute | Handler signature | Runs |
| --- | --- | --- |
| *(none)* | `rusty::Result<Resp, i32> m(const Req&)` | in a fresh fiber — may block or make nested RPC calls |
| `fast` / `prefix` | same | inline on the poll thread, no fiber |
| `defer` | `void m(const Req&, Resp& resp, srpc::DeferredReply defer)` | in a fiber; you reply whenever you like |
| `async` | `rusty::Task<rusty::Result<Resp, i32>> m(const Req&)` | entered inline on the poll thread, then resumed as a stackless coroutine |
| `raw` | `void m(rusty::Box<srpc::Request>, srpc::WeakServerConnection)` | you decode and reply yourself |

`fast` and `prefix` are the same thing; `fast` is just an alias. A `fast` handler is
the cheapest option, but it runs on the poll thread with no fiber to yield from, so it
must not block — if it does, every connection on that thread stalls. The same applies
to an `async` handler up to its first suspension point, because those register through
the fast path too.

A `defer` handler fills in `resp` and then calls `defer.reply()` (or
`defer.reply_error(code)`) whenever the answer is ready. Both fire at most once, and
simply dropping the handle without replying is safe — the caller just never hears back.

An `async` handler is a coroutine; finish it with `co_return`.

There is also a `fiber` attribute, but the code it generates names `Fiber` unqualified,
so it needs `using namespace srpc;` spliced into your `%%` header section to compile.
Prefer the default, which already runs in a fiber.

## Testing without a network

There is a second transport that runs entirely in-process, with no sockets and no
ports. Install it on both ends before starting, and everything else stays the same:

```cpp
import srpc.inmemory_channel;   // not in the srpc.hpp umbrella

auto switchboard = rusty::Arc<InMemorySwitchboard>::new_(InMemorySwitchboard::new_());

auto make_factory = [&]() {
    auto f = rusty::Arc<InMemoryFactory>::new_(InMemoryFactory::new_(switchboard.clone()));
    return make_inmemory_factory_proxy(std::move(f));
};

svr.set_channel_factory(make_factory());
svr.start(reinterpret_cast<const int8_t*>("inmemory://demo"));

cl->set_channel_factory(make_factory());
cl->connect(reinterpret_cast<const int8_t*>("inmemory://demo"), true);
```

The address is just a key — nothing parses a scheme. Delivery is synchronous and
unframed, which makes tests deterministic but also means the in-memory channel cannot
reproduce a framing or desync bug. It ships fault injection for the failures you do
want to test: `inmemory_channel_inject_drop_next_sends`,
`inmemory_channel_inject_send_error`, `inmemory_channel_clear_fault_injection`.

## Building and testing SRPC itself

The Rust lane is the fast one. It needs no C++ toolchain and no submodules:

```sh
cargo test --locked --workspace --all-targets
```

The full C++ lane builds the pinned transpiler, regenerates all 37 module providers,
builds the archive, and compares the generated and shipped ABI:

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 4 --target srpc_goal0_dual_compile
ctest --test-dir build --output-on-failure
```

Budget minutes, not seconds, for a cold build. Building `srpc` — and therefore anything
that links it — runs the whole Rust suite and `clippy -D warnings` first, so a Rust
warning breaks the C++ build.

`third-party/googletest` is only needed for the test battery; without it CMake warns
and builds fewer tests. Of the 76 C++ test files in `tests/`, the build compiles 8; the
rest are not currently wired into CMake.

Sanitizers are a whole-configuration switch, so give them their own build directory:

```sh
cmake -S . -B build-asan -G Ninja -DSRPC_SANITIZER=address    # none|address|thread|undefined
```

There is no CI. That sequence is the safety net.

## Why the sources are Rust

SRPC is a C++ library whose production modules are not hand-written C++. All 37 of them
are canonical **Rust** files living at their historical C++ paths in `base/`, `misc/`,
`reactor/` and `rpc/`, and the pinned rusty-cpp transpiler generates a `srpc.<name>.cppm`
C++23 module from each. Two toolchains read the exact same bytes: rustc, through the
generated crate index `src/lib.rs`, and rusty-cpp, through a single whole-crate
invocation. Hand-written non-Rust does remain, but only as seam:
`reactor/epoll_platform_linux.cc` — the one C++ implementation unit, supplying the Linux
epoll layer — plus eight plain-C syscall kernels (`*/srpc_*.c`) and the two fiber
context-switch assembly files.

That is what `srpc_goal0_dual_compile` checks. It recompiles every generated module on
its own, links one importer program twice — once with those fresh objects placed ahead
of `libsrpc.a`, once against `libsrpc.a` alone — runs both, and then compares `nm`
symbol sets per module: the
archive must carry exactly what the freshly compiled objects carry, plus the five
symbols of that one platform implementation unit. So a change to a `.rs` file is
simultaneously a Rust change and a C++ ABI change.

Two modules also carry machine-checked contracts. `misc/stat.rs` and
`rpc/internal_protocol.rs` have Verus specifications written inline, behind
`#[cfg(verus)]`, and verified against the real sources rather than a copy:

```sh
VERUS_HOME=/path/to/verus-dist scripts/verify_srpc.sh
```

`docs/verification.md` explains the scope, which is deliberately narrow — the
response-header codec round-trip in `rpc/internal_protocol.rs`, and a first-sample
invariant that pins a bug that actually shipped. Lifting the round trip up into
`rpc/frame_codec.rs` is listed there as the next property, not a proven one.

## What changed since simple-rpc

The IDL is nearly unchanged. The generated C++ is not.

- The namespace is `srpc::`, not `rrr::`. The only alias shipped is
  `namespace base = srpc;`.
- Handlers and proxies use typed structs. `sum(a, b, c, &result)` is now
  `sum(const RpcSumRequest&)` returning `rusty::Result<RpcSumResponse, srpc::i32>`.
  Hand-decoded methods survive only as `raw`, which takes a `rusty::Box<srpc::Request>`
  and does its own decoding — and whose *proxy* still keeps the old pointer-out-parameter
  shape. Among handler signatures, the only one that still writes through an
  out-parameter is `defer`, and it takes a reference, not a pointer.
- There is no `bin/rpcgen`. Drive the generator from Python as shown above.
- A client and a server each need a `PollThread`, and addresses are passed as
  `const int8_t*`.
- Registration is `reg_service_typed(rusty::make_box<T>())` — the generated class has
  no base class to inherit from.
- The build is CMake + Ninja rather than waf, and needs Clang 22 with libc++.

## Rough edges

Worth knowing before you rely on them:

- `Client::metrics()` always returns zeros. Read metrics off the connection instead —
  and add `import srpc.connection_metrics;`, which is also outside the umbrella:

  ```cpp
  auto conn = cl->connection();
  if (conn.is_some()) { printf("%lu\n", conn.unwrap()->metrics().requests_sent()); }
  ```

  The `LEAST_CONNECTIONS` and `LEAST_LATENCY` pool strategies read the same stub, so
  they do not currently differentiate, and `ClientPool` health checks read it too — a
  connected client is always judged healthy and nothing is ever evicted.
- Buffered requests are parked with a TTL and expired, not replayed after reconnect.
- The heartbeat protocol is implemented end to end, but nothing currently ticks the
  client-side timer.
- `set_keepalive()` stores its configuration but does not yet push it to the socket.

## Not documented yet

These work but are not covered above: `raw` and `async` handlers in depth, and the
Python stub. `tests/benchmark_service.{rpc,h,cc}` is the fullest worked example — IDL,
generated header, and all twelve handlers defined out of line on the generated class.
Nothing in the build compiles it, and its committed header still carries the unedited
`#include "srpc/srpc.hpp"`, so fix that line before you try.
