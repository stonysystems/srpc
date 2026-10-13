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

- [x] **G0. Spike.** Done (2026-10-13); see *Result* below. In a scratch clone of `main`:
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
  - **Result.**
    - **Cargo:** re-locking adds exactly nine `source =
      "git+https://github.com/stonysystems/lion?rev=d697b77…#d697b77…"`
      lines, one per Lion package: executor, executor-spec, framework-spec,
      net, reactor, reactor-spec, slab, timer-wheel and utility-spec. The
      package count stays 58, and the Verus entries are unchanged.
      - Cargo prints no warnings. Lion's 22 duplicate package names are all
        under `lion-benchmark/`, outside the graph.
      - No forbidden crate appears, even in `cargo tree -e all --target all`.
      - The checkout (`~/.cargo/git/checkouts/lion-<hash>/d697b77/`) has a
        full `.git` whose HEAD is the rev, and it is clean.
      - Lion and `vstd` now build with `--cap-lints allow`.
    - **Rust lane:** `RUSTFLAGS=-Dwarnings cargo test` passes 400, fails 0
      and ignores 1, the same as `main`, with Lion coming from the git URL.
    - **rusty-cpp `3535f2aa` fails open.** It exits 0 with "Crate graph (0
      dependency crate(s))" and emits no Lion provider. It also silently
      mis-generates three SRPC providers:
      - `srpc.epoll_wrapper` loses `export import lion_reactor;` and gains a
        self-recursive `deregister`;
      - `srpc.reactor` turns `os_backend` into `void*`;
      - `srpc.tcp_channel` loses its Lion re-exports.

      SRPC's dual-compile gate would reject this, because it requires the
      six providers. Still, G3 must not land before G1 and G2.
    - **Correction to §0:** `main.rs:1824` and `:2229` handle
      workspace-inherited dependencies, not the crate graph. The graph skips
      git packages in `metadata.rs`:
      - `:1834`, where only path packages get a `cargo tree` rendering;
      - `:2120`, where the walk does not descend into sourced packages;
      - `:2166`, where sourced dependencies get no edge.

      The graph is resolved at three call sites (`main.rs:4200`, `:4684`,
      `:2701`). Output layout, `crate-graph.json` and the verus-erase helper
      carry no paths.
    - **Prototype:** an environment-variable opt-in in `metadata.rs` only
      (+36/−5 lines). With Lion's nine packages opted in, all 65 output files
      are byte-identical to `main`'s `goal0-crate-cpp`. With none opted in,
      the output is identical to the stock transpiler's.
    - **SRPC files to change:**
      - `check_rust_independence.py`: `:207-211` reject any git source but
        `vstd`, and the manifest check, the graph check and
        `check_lion_checkout` change;
      - `test_rust_independence.py`;
      - `CMakeLists.txt`: drop the `:518-521` glob, since the transpile
        already depends on `Cargo.lock`, and add the flag near `:895`;
      - `check_srpc_crate_mode.py`: `LION_CRATE_MODE_ARGUMENTS`, `:1083`;
      - `test_goal0_contracts.py`: `:232`;
      - `verify_lion.sh`;
      - the two `--compare` scripts. Submodule-era refs need Lion fetched at
        their gitlink commit. All three historical pins (`aa5bebe`,
        `3496113`, `d697b77`) are ancestors of `srpc-lion`.
      - `bench/Cargo.lock` already lacks `lion-net` (pre-existing).
    - **Mako:** no change today. Its `src/srpc` is a copied subtree of SRPC
      `683c506`, from before Lion. Its next SRPC sync needs network or a
      warm `CARGO_HOME`, and its own rusty-cpp pin must include G1.
- [ ] **G1. rusty-cpp.** On a branch from `3535f2aa`:
  - Add a repeatable `--crate-graph-git <URL>` option. Each git package whose
    source repository equals a named URL joins the crate graph as if local,
    read from `cargo metadata`'s manifest path.
  - Opting in by repository rather than by package name means SRPC names one
    URL, not Lion's crate list, and a partial list cannot occur.
  - A named URL that matches no package is an error, raised before any
    output is written.
  - Keep the default unchanged and `vstd` excluded (it comes from another
    repository).
  - Add tests;
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
