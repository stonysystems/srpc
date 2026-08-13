# SRPC

SRPC is the standalone Repeatable Research Runtime extracted from Mako with
its path history intact. Canonical Rust implementations intentionally keep
their historical `.cpp` paths. The matching `src/*.rs` entries are symlinks
used for Cargo and rusty-cpp crate discovery; CMake treats the historical
paths as transpiler inputs rather than native C++ sources.

## Dependencies

The repository pins rusty-cpp as a Git submodule. Initialize it exactly as
recorded by the SRPC commit:

```sh
git submodule update --init --recursive
```

The full C++ module build requires CMake 3.30+, Ninja, Cargo, Python 3.11+,
and Clang 22 with libc++. The Cargo-only lane does not require the C++
toolchain.

## Build and test

```sh
cargo test --locked --workspace --all-targets
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 4 --target rrr_goal0_dual_compile
ctest --test-dir build --output-on-failure
```

The `rrr_goal0_dual_compile` target builds the pinned transpiler, validates
the remaining inline Rust DSL, generates the canonical Rust providers, builds
the production SRPC archive, and compares generated and production ABI/runtime
behavior.
