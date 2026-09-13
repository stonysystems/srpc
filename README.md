# SRPC

SRPC is an RPC framework written in Rust, with a native Cargo library and a
C++23 library generated from the same sources. It supports TCP and in-memory
transports, an epoll reactor, stackful fibers, async tasks, timeouts, retries,
reconnection and connection pooling.

The Rust library runs on Rust std and a small C/assembly kernel. The C++ lane
also provides a `.rpc` service generator for typed handlers and client proxies.
SRPC descends from [simple-rpc](https://github.com/santazhang/simple-rpc).

**[Read the SRPC book](docs/srpc-book.md)** for service examples, API details,
architecture, configuration and troubleshooting.

## Build and test

SRPC targets Linux on x86_64 and aarch64. Start with a checkout:

```sh
git clone https://github.com/stonysystems/srpc
cd srpc
```

For Rust, install a stable Rust toolchain, a C compiler and an archiver. No
submodules or C++ toolchain are needed:

```sh
cargo test --locked --workspace --all-targets
cargo test --locked --workspace --doc
```

For C++, also install Clang 22+ with libc++, CMake 3.30+, Ninja, Cargo with
clippy, Python 3.11+ and ripgrep. Initialize the submodules before building:

```sh
git submodule update --init --recursive
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 4
ctest --test-dir build -L srpc --output-on-failure
```

See the book for [setup and build details](docs/srpc-book.md#building-and-testing-srpc)
and [testing and sanitizer commands](docs/srpc-book.md#tools-that-exist-in-this-repository).

## Using SRPC

- [Rust service example](docs/srpc-book.md#the-shape-of-a-service)
- [C++ service and client walkthrough](docs/srpc-book.md#20-consuming-srpc-from-c)
- [Service definition and code generation](docs/srpc-book.md#12-service-definition-and-code-generation-c-lane)
- [Timeouts, retries and reconnection](docs/srpc-book.md#11-reliability-features)
- [Running benchmarks](docs/srpc-book.md#reproducing-benchmarks)

The book also covers [Rust and C++ runtime ownership](docs/srpc-book.md#19-the-c-lane-one-source-two-compilers)
and [Verus verification](docs/srpc-book.md#checking-rust-contracts).
