# Plan: Lion as a Cargo git dependency

Status legend: `[ ]` not started · `[~]` deferred with reason · `[x]` done.

**Status: IN PROGRESS (2026-10-13).** The owner asked (2026-10-13) that SRPC
take Lion as a Cargo dependency instead of a submodule, pinned by `rev`, and
that this plan be carried out self-paced. Nothing is pushed without the
owner's OK.

## 0. The short version

Today the `third-party/lion` submodule (gitlink `d697b77`, Lion branch
`srpc-lion`) holds Lion's source, and `Cargo.toml` reaches it through path
dependencies:

```toml
lion-reactor = { path = "third-party/lion/lion-reactor", default-features = false }
```

The target is a git dependency at the same commit, with the submodule gone:

```toml
lion-reactor = { git = "https://github.com/stonysystems/lion", rev = "d697b779bb05314e7425b1746b5f89441e8e6712", default-features = false }
```

and the same for `lion-executor` and `lion-net`. rusty-cpp and googletest stay
submodules: they are build tools, not Cargo dependencies of `srpc`.

What this buys:

- One pin: the `rev` in `Cargo.toml`, which `Cargo.lock` must agree with,
  instead of a gitlink plus the gate's submodule checks.
- No submodule to initialize and no `[workspace] exclude` for Lion.
- Cargo caps lints for non-path dependencies, so a new warning in Lion no
  longer fails SRPC's `-Dwarnings` gate.
- Vendoring SRPC into Mako no longer carries a nested Lion submodule.
- No new network requirement: Verus's `vstd` already arrives through Lion as
  a Cargo git dependency.

The blocker is in rusty-cpp. `--crate-graph` transpiles only packages with
`source.is_none() && path.is_some()` (`transpiler/src/main.rs:1824` and
`:2229` at `3535f2aa`). A git package has a source, so Lion's crates would be
skipped like `vstd`, and all six Lion providers would vanish from the C++
lane.

## 1. Fixed constraints

- **Lion's identity claim is unchanged.** SRPC compiles Lion@`rev`
  unmodified. Every Lion package in `Cargo.lock` must resolve to
  `git+https://github.com/stonysystems/lion?rev=<sha>#<sha>`, and the gate
  checks this.
- **D5 is unchanged in substance.** `[dependencies]` is exactly the three
  Lion crates, now `{ git = LION_URL, rev = LION_REV, default-features =
  false }`. The resolved graph is the same nine `LION_CRATES` with no
  features, `vstd`/`verus_builtin` at `VERUS_SOURCE`, and no mio, flume,
  tokio, socket2, futures-task or pin-project-lite anywhere.
- **Generated C++ is unchanged.** The 37 SRPC and six Lion providers should
  be byte-identical to `main` (`21f2320`), with ABI 2107 + 407. Any
  difference is explained before acceptance.
- **rusty-cpp's default is unchanged.** A git package is transpiled only when
  named explicitly, so `vstd` stays excluded. The change branches from SRPC's
  pin `3535f2aa`, not from rusty-cpp `main`, whose newer commits change
  emitted C++.
- **Older refs stay benchmarkable.** `run_rpc_echo_bench.sh --compare` and
  `run_microbench.sh --compare` must build both submodule-era refs and
  git-dependency refs.
- **Mako:** never write to the live checkout. Any Mako check runs in a
  scratch copy.
- **No push** to SRPC, rusty-cpp or Lion without the owner's OK.

## 2. Phases

- [ ] **G0. Spike.** In a scratch clone of `main`:
  - switch `Cargo.toml` to git dependencies, and re-lock only the Lion
    packages;
  - check `cargo tree -e normal`, the `Cargo.lock` sources, and any warnings
    Cargo prints while scanning Lion's repository (it has 74 manifests and no
    root workspace, and pingora's two copies share package names);
  - `RUSTFLAGS=-Dwarnings cargo test --locked --workspace --all-targets`;
  - confirm that rusty-cpp skips Lion, and list every place in rusty-cpp that
    assumes a local package (`is_local`, `path`, output layout,
    `crate-graph.json` fields);
  - list every SRPC file that names `third-party/lion`;
  - read (never write) Mako for references to SRPC's Lion submodule.
- [ ] **G1. rusty-cpp.** On a branch from `3535f2aa`:
  - let `--crate-graph` transpile git packages named on the command line,
    reading each from the manifest path `cargo metadata` reports;
  - keep the default unchanged and `vstd` excluded;
  - add tests;
  - run rusty-cpp's crate-graph tests and `lion_parity`.
- [ ] **G2. SRPC: transpiler pin.** `build: bump rusty-cpp 3535f2aa -> <new>`,
  the four edits, in its own commit. ABI is unchanged.
- [ ] **G3. SRPC: Lion as a git dependency.**
  - `Cargo.toml` and `Cargo.lock`;
  - remove the submodule: the gitlink, the `.gitmodules` entry and the
    workspace exclude;
  - CMake: pass rusty-cpp the Lion packages, and track Lion through
    `Cargo.lock` instead of globbing `third-party/lion` sources;
  - `check_rust_independence.py` and its tests: an exact git source and
    `rev`, `Cargo.lock` agreeing, the checkout unmodified, and the offline
    copy built from Cargo's git cache;
  - `check_srpc_crate_mode.py`;
  - `verify_lion.sh`: take the commit from `Cargo.lock`;
  - both `--compare` benchmark scripts, for both eras;
  - CLAUDE.md (cold machine, the Lion pin-bump procedure, D5) and the docs
    that name the submodule.
- [ ] **G4. Acceptance.**
  - The full gate on a cold clone, which no longer fetches a Lion submodule.
  - Generated C++ byte-identical to `main`'s.
  - ABI 2107 + 407.
  - ctest 51/51.
  - Each new gate check goes red under a negative control: a wrong `rev`, a
    `branch` dependency, a path dependency, and a lockfile that disagrees.
  - `verify_lion.sh` 10/10.
  - A `--compare` smoke run between `21f2320` and the new head.
  - Mako builds and passes its tests in a scratch copy.
- [ ] **G5. Publish (owner's OK).** Merge the rusty-cpp branch into rusty-cpp
  `main`, and this branch into SRPC `main`.
