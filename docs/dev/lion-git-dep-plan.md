# Plan: Lion as a Cargo git dependency

Status legend: `[ ]` not started · `[~]` deferred with reason · `[x]` done.

**Status: DONE and published (2026-10-13).** The owner asked (2026-10-13) that SRPC
take Lion as a Cargo dependency instead of a submodule, pinned by `rev`, and
that this plan be carried out self-paced. At the owner's direction, rusty-cpp
`e0b6578d` is merged into rusty-cpp `main` as `6753edf5`, and this branch
into SRPC `main` (G5). Follow-up F1 (§3), a byte-for-byte check of Cargo's
Lion checkout, is done on a local branch.

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
- [x] **G1. rusty-cpp.** Done (2026-10-13) as rusty-cpp `e0b6578d` on local
  branch `lion/git-crate-graph`, whose parent is `3535f2aa`; see *Result*
  below. On a branch from `3535f2aa`:
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
  - **Result.**
    - **Size:** +931/−33 lines. Most of it is in `transpiler/src/metadata.rs`
      (+591/−32, about 283 of them tests) and `transpiler/tests/crate_graph.rs`
      (+295).
    - **Semantics:**
      - `--crate-graph-git <URL>` is repeatable and requires `--crate-graph`.
        It reaches all three graph resolutions: the driver, the root
        preflight and `--expand`.
      - A package joins the graph when it has no source, or when its source
        is `git+<URL>[?ref]#<commit>` and the URL matches a named one. Matching
        follows Cargo's `CanonicalUrl`: scheme and host ignore case, a
        trailing `/` and then `.git` are dropped, and `github.com` paths are
        lowercased.
      - **Errors:**
        - These values are rejected before any output: an empty value, a
          `git+` prefix, a `?` or `#`, or no scheme.
        - A named URL that contributes no package is an error. The message
          lists the git repositories the graph does reach.
        - So is a name shared by two graph packages.
      - **Matching `cargo tree` output:** the transpiler matches a git
        package to Cargo's own rendering, which form-urlencodes the reference
        and cuts the commit to 8 hex digits. G0's prototype skipped the
        encoding, and it worked only because Lion's rev is plain hex.
      - With a repository named, crate mode always resolves the exact graph,
        so its marker preflight reaches git packages.
      - With nothing named, behaviour is the old `source.is_none()` rule.
    - **Tests:** 7 new unit tests and 4 integration tests. The integration
      tests use `file://` repositories, a `feat/x+y` branch and a private
      `CARGO_HOME`. Negative controls turned them red. `crate_graph` 7/7,
      `lion_parity` 1/1, and the focused metadata and codegen tests 88/88.
    - **Full transpiler suite, same worktree:**

      | Rev | Passed | Failed | Ignored |
      |---|---|---|---|
      | `3535f2aa` | 2,853 | 9 | 23 |
      | `e0b6578d` | 2,864 | 9 | 23 |

      The 11 extra passes are exactly the new tests. The same nine tests fail
      at both revs:
      - `cpp_name_crate_mode_rejects_opaque_expansion_before_output`;
      - `crate_mode_uses_one_cargo_selected_target_dependency_graph_atomically`,
        whose `RUSTC=missing-rustc` fixture breaks `cargo metadata` first;
      - five `test_either_parity_harness_*` and two `test_parity_matrix_*`
        tests.

      The suite deletes the tracked `run_parity_harness.sh` it needs. This
      settles L2's open question: the 9 failures at `3535f2aa` are
      pre-existing, not caused by the Option fix.
    - **End to end, with CMake's exact command:**
      - **Lion as a git dependency, URL named:** exit 0, nine dependency
        crates, and 65 files byte-identical to `main`'s `goal0-crate-cpp`.
      - **Lion as the submodule, option absent:** 65 identical files.
      - **A misspelled URL:** exit 1 and no output.
      - **Lion as a git dependency, option absent:** the transpiler still
        fails open, emitting 41 files with no Lion modules. SRPC must always
        pass the URL, and its dual-compile gate would reject the 41-file
        output.
      - `--verus-build-info` is unchanged.
- [x] **G2. SRPC: transpiler pin.** Done (2026-10-13) as `f90af7f`
  (`build: bump rusty-cpp 3535f2aa -> e0b6578d`): the four edits, in their
  own commit.
  - It does not pass `--crate-graph-git`, because with Lion still a path
    dependency, naming the URL is an error.
  - Gate: `RUSTFLAGS=-Dwarnings cargo test` passed 400, failed 0 and ignored
    1. Configure and build exited 0.
  - SRPC 2107 and Lion 407 strong symbols, 0 hand slots, ctest 51/51.
  - `goal0-crate-cpp` is byte-identical to `main`'s, 65 files. ABI is
    unchanged.
- [x] **G3. SRPC: Lion as a git dependency.** Done (2026-10-13) as `5e3a91f`
  (`build: take lion as a cargo git dependency pinned by rev`, everything
  gated as one tree) and `4bf0ce3` (a prose and docstring correction; see
  *Result*).
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
  - **Result.**
    - **Tree:** `Cargo.toml` names `{ git = LION_GIT_URL, rev = "d697b779…",
      default-features = false }` three times. `Cargo.lock` gains the nine
      git sources. `third-party/lion` and its `.gitmodules` stanza are gone.
    - **CMake:** passes `--crate-graph-git ${SRPC_LION_GIT_URL}` and drops
      the Lion source glob. Crate generation already depends on `Cargo.lock`,
      and a pinned checkout never changes.
    - **New helper:** `scripts/lion_source.sh` extracts Lion at a commit with
      `git archive`. It is shared by `verify_lion.sh` and both `--compare`
      scripts, which now build submodule-era refs too.
    - **The independence gate now checks:**
      - **`Cargo.toml`:** exactly the three crates, each `{ git, rev = <40
        lowercase hex>, default-features = false }`, all with one rev. The
        rev in `Cargo.toml` is the pin of record, with no second copy in the
        script.
      - **`Cargo.lock`:** each Lion package resolves once to
        `git+URL?rev=R#R`. Nothing else comes from Lion's URL, and no git
        source but Verus's is allowed.
      - **The resolved graph:** each Lion crate comes from that source with
        no features, and any path package is rejected.
      - **Cargo's checkout:** all nine crates lie in one checkout, at their
        expected directories, with HEAD at the rev and no tracked change.
        Every git call keeps `safe.directory`.
      - **The offline copy:** it resolves Lion from the same `CARGO_HOME`.
    - **`test_rust_independence.py`** has 53 tests, and ten mutations of the
      gate each turned them red.
    - **The real script, on mutated trees with a private `CARGO_HOME`,**
      exits 1 for each of these, while the untouched tree passes:
      - a wrong rev;
      - `branch =`;
      - a path dependency;
      - a short rev;
      - revs that differ across the three dependencies;
      - a lockfile that disagrees;
      - a `.git`-suffixed URL;
      - a lock entry from another URL;
      - an edited tracked file in the checkout.
    - **`test_goal0_contracts.py`** goes red when CMake drops the flag, or
      when CMake and the gates name different URLs.
    - **Cargo resets a moved HEAD:** a checkout whose HEAD was moved is reset
      by Cargo during `cargo metadata`, before the gate looks. So that control
      passes through the script and fails only when `check_lion_checkout` is
      called directly. `4bf0ce3` corrects the prose that said Cargo never
      re-checks its checkout. Cargo does not notice an edited file, and the
      gate's status check is what catches it.
    - **Gate at `5e3a91f`,** in a clone with no `third-party/lion`:
      - `cargo test` passed 400, failed 0 and ignored 1. Doctests 2/2, and
        clippy is clean.
      - The source gate's Python suites have 0 skips, and its Cargo runs pass
        804, fail 0 and ignore 2.
      - The transpiler reports "Crate graph (9 dependency crate(s))".
      - SRPC 2107 and Lion 407 strong symbols, 0 hand slots, ctest 51/51.
      - `goal0-crate-cpp` is byte-identical to `main`'s, 65 files.
      - `4bf0ce3`'s own checks: independence tests 53 OK, standalone tests
        7 OK, the docs lint, and the real script exits 0.
    - **`verify_lion.sh --dry-run`** resolves `d697b779` (tree `f0cce113`,
      2,091 files, equal to Cargo's checkout). It works from Cargo's
      database, and from GitHub with an empty `CARGO_HOME`.
    - **`--compare` smoke runs** built and ran every side:
      - `run_rpc_echo_bench.sh --compare 3312dda 21f2320 <G3>`;
      - `run_microbench.sh --compare 21f2320 <G3>`.
    - **Deferred to G4:**
      - a cold clone with an empty `CARGO_HOME`;
      - a full `verify_lion.sh` run;
      - an untracked file added inside a Lion crate in Cargo's checkout is
        not yet detected. The submodule check had the same gap.
- [x] **G4. Acceptance.** Done (2026-10-13) as `b7ea803` and `06e43f8`; see *Result* below.
  - The full gate on a cold clone, which no longer fetches a Lion submodule.
  - Generated C++ byte-identical to `main`'s.
  - ABI 2107 + 407.
  - ctest 51/51.
  - Each new gate check goes red under a negative control: a wrong `rev`, a
    `branch` dependency, a path dependency, and a lockfile that disagrees.
  - `verify_lion.sh` 10/10.
  - A `--compare` smoke run between `21f2320` and the new head (done in G3).
  - Mako builds and passes its tests in a scratch copy. This is moot per
    G0: Mako's SRPC copy predates Lion, so nothing in Mako changes.
  - Close G3's untracked-file gap: the checkout check rejects any untracked
    file except Cargo's root `.cargo-ok`, with a negative control.
  - The moved-HEAD control, run against `check_lion_checkout` directly.
  - **Result.**
    - **`b7ea803` (gate):** `check_lion_checkout` runs `git status
      --porcelain -z --untracked-files=all --ignored=traditional`, with
      `safe.directory`. It fails on any entry except Cargo's root
      `.cargo-ok`.
      - **Ignored files count too:** ignore rules decide what git shows, not
        what Cargo compiles. Lion's own rules hide `**/target/` and `*.log`,
        and a self-hiding `.gitignore` or the checkout's `info/exclude` can
        hide anything.
      - **The gap was real:** an untracked `lion-net/build.rs` became a
        build-script target that `cargo build` ran, and G3's check passed it.
      - **Tests:** `test_rust_independence.py` has 61 tests. Seven mutations
        of the check all turned it red, among them reverting to
        `--untracked-files=no` (9 failures).
      - **Real script:** it exits 1 on each of these, and the clean checkout
        passes:
        - an untracked `build.rs`;
        - an ignored `x.log`;
        - a `build.rs` hidden by `info/exclude`;
        - a self-hiding `.gitignore`;
        - a new source directory.
      - **Moved HEAD:** `check_lion_checkout` rejects it when called
        directly.
      - **Build footprint:** four checkouts, including one after a full CMake
        build, list only `.cargo-ok`, so the build writes nothing there.
      - **Not covered:** a tracked file marked `assume-unchanged` or
        `skip-worktree` in the checkout's index. F1 closes this.
    - **`06e43f8` (CLAUDE.md):** a cold machine needs four `cargo fetch
      --locked` runs:
      - the root;
      - `third-party/rusty-cpp`;
      - `scripts/rust_source_audit/`;
      - `bench/`, which the old list missed: `rpc_echo` failed offline on
        `unicode-ident` 1.0.26.

      The gate needs only the first three.
    - **Cold cache:** a fresh clone with an empty `CARGO_HOME`. The first
      three fetches came to 436 MB. Every gate step then ran with
      `CARGO_NET_OFFLINE=true`, and the cache did not grow. Results at
      `b7ea803`:
      - `RUSTFLAGS=-Dwarnings cargo test` passed 400, failed 0 and ignored
        1. Doctests 2/2, and clippy is clean.
      - Configure and build exited 0. The source gate's Python suites had 0
        skips, and its Cargo runs passed 804, failed 0 and ignored 2.
      - "Crate graph (9 dependency crate(s))". Lion 6 providers and 407
        symbols; SRPC 2107 symbols and 0 hand slots.
      - ctest 51/51.
      - `goal0-crate-cpp` is byte-identical to `main`'s, 65 files.
      - Until G5 publishes rusty-cpp `e0b6578d`, a fresh clone's `git
        submodule update` fails with "not our ref".
    - **`verify_lion.sh`:** Lion `d697b779` with Verus `0.2025.11.15.db81a74`
      printed "All checks passed" in 588 s, with 0 errors. Verified counts:
      net 10, utility 80, reactor 208, executor 99, liveness 556,
      timer-wheel 117, reactor-spec 57, executor-spec 12, framework-spec 8,
      slab 5.
    - **Mako:** needs nothing. No Mako file names Lion, and its
      `src/srpc/Cargo.toml` predates Lion.
    - **Sanitizers:** not rerun. The generated C++ and the C kernels are
      unchanged from `main`, so its 35/35 results stand.
    - **One new subtest** sets git's excludes file through `GIT_CONFIG_*`
      environment variables, which needs git 2.31 or later. This host has
      2.47.3. F1 replaces them with a scratch `HOME`.
- [x] **G5. Publish (owner's OK).** Done (2026-10-13) at the owner's
  direction. Merge the rusty-cpp branch into rusty-cpp `main`, and this branch
  into SRPC `main`.
  - **Result.**
    - **rusty-cpp:** `e0b6578d` is merged into `main` `4c0878f1` as
      `6753edf5`, and pushed.
      - Both sides touch `transpile.rs` and `tests/crate_graph.rs`. git
        merged them without conflict, and each side's diff reached the merge
        unchanged apart from line offsets.
      - On the merged tree: `crate_graph` 7 passed, `lion_parity` 1 passed,
        and the metadata and crate-graph unit tests 88 passed, as on
        `e0b6578d`.
      - Full release suite: `main` 2879 passed, 9 failed, 23 ignored; the
        merge 2890, 9, 23. The 11 extra are G1's new tests, and the nine
        failures are the same ones that fail at `3535f2aa`.
      - `e0b6578d` now fetches from the URL in SRPC's `.gitmodules`, so the
        "not our ref" failure above is gone.
      - SRPC's pin stays `e0b6578d`. Moving it to `6753edf5` would pull in
        `main`'s `rusty_ext` changes, which alter emitted C++, so that is a
        separate bump.
    - **Lion:** nothing to push. `d697b77` is already the head of
      `srpc-lion`.
    - **SRPC:** `main` `21f2320` is an ancestor of this branch, so the merge's
      tree is this branch's. The gate on the merge, in a fresh clone:
      - `RUSTFLAGS=-Dwarnings cargo test` passed 400, failed 0, ignored 1;
        doctests 2/2; clippy is clean.
      - Configure and build exited 0. The source gate's Python suites had 0
        skips, and its Cargo runs passed 804, failed 0 and ignored 2.
      - "Crate graph (9 dependency crate(s))". Lion 6 providers and 407
        symbols; SRPC 2107 symbols and 0 hand slots.
      - ctest 51/51.
      - `goal0-crate-cpp` is byte-identical to the build of `main`
        `21f2320`, 65 files.

## 3. Follow-ups

- [x] **F1. Byte-compare Cargo's Lion checkout.** Done (2026-10-13) on local
  branch `lion-checkout-content`. After G5 the owner asked to close two of the
  gaps G4 left open: the `assume-unchanged`/`skip-worktree` hole in the
  checkout check, and the subtest that needs git 2.31. The third, rusty-cpp
  exiting 0 without `--crate-graph-git`, is for the owner to decide.
  - **The gap:** the check trusted `git status`, which reads the checkout's
    index, ignore rules and config. Measured with git 2.47.3, status shows
    nothing for any of these:
    - an edit to a file marked `assume-unchanged`;
    - an edit or a deletion of a file marked `skip-worktree`;
    - a FIFO;
    - an edited file committed and registered with `git replace` for the
      rev, with HEAD moved back to the rev. HEAD still names the rev, and
      both `status` and `ls-tree` read the replacement commit.
  - **The check:** `check_lion_files` now carries it.
    - It lists the rev's tree with `git --no-replace-objects ls-tree -r`.
    - It walks the checkout directory, skipping only the root `.git`. It
      follows no link, takes a link to a directory as an entry (Lion's tree
      has four), and fails on any directory it cannot list.
    - It hashes each file and link as a git blob, and requires the tree's
      paths, kinds and blob ids, with only the root `.cargo-ok` added.
    - A tree entry that is neither a file nor a link, such as a submodule,
      is refused. The executable bit is not compared.
    - `git status` still runs first, now with `--no-replace-objects` (as do
      `rev-parse` and `ls-tree`), as a second opinion.
  - **Cost:** the whole checkout check took 0.35 s on a local-disk
    `CARGO_HOME` and 5.2 s on the NFS-backed `~/.cargo` (16.8 s with a cold
    cache), for 2,091 tree entries: 2,087 files and 4 links, about 57 MB.
  - **Tests:** `test_rust_independence.py` has 70 tests, 9 of them new. The
    fixture gains an executable script and a link to a directory.
    - Each of 12 mutations of the check turned at least one test red, among
      them dropping `--no-replace-objects` from `ls-tree` or `status`,
      dropping links to directories from the walk, and walking without
      `onerror`.
    - The unreadable directory is injected through `os.scandir`, because
      `chmod` does not stop root, and Mako's CI runs this gate as root.
  - **Real script:** against a copy of a Cargo cache, with plain `git status`
    blind to each case:
    - an `assume-unchanged` edit, a `skip-worktree` edit and a FIFO each exit
      1 in the byte comparison;
    - the replaced commit exits 1 in `status`, which now reads the real
      objects;
    - a `skip-worktree` deletion of a crate's `lib.rs` exits 1 earlier, in
      `cargo metadata`;
    - the untouched checkout passes.
  - **The git 2.31 subtest:** the user's `core.excludesFile` now comes from
    a scratch `HOME`'s `.gitconfig`, which every git version reads. The
    subtest sets `XDG_CONFIG_HOME` to an empty directory and
    `GIT_CONFIG_NOSYSTEM=1`, and removes `GIT_CONFIG_GLOBAL` and
    `GIT_CONFIG_COUNT`.
    - Writing the config elsewhere, or keeping a caller's
      `GIT_CONFIG_GLOBAL`, turns it red.
    - It has not been run on a git older than 2.31; none is installed here.
  - **Gate:** on the branch's tree, in a warm local-disk clone:
    - `RUSTFLAGS=-Dwarnings cargo test` passed 400, failed 0, ignored 1;
      doctests 2/2; clippy is clean.
    - Configure and build exited 0. The 8 Python suites passed with 0
      skips, and the source gate's Cargo runs passed 804, failed 0 and
      ignored 2. The independence check reported `~/.cargo`'s checkout
      identical to the rev.
    - Lion 6 providers and 407 symbols; SRPC 2107 symbols and 0 hand slots.
    - ctest 51/51.
    - `goal0-crate-cpp` is byte-identical to the build of `main` `21f2320`,
      65 files.
  - **Not covered:**
    - a rewrite of the checkout's git objects themselves. That is
      deliberate tampering, and whoever can do it can as easily change the
      toolchain;
    - a change made after the check and before Cargo compiles.
