#!/usr/bin/env python3
"""Build and test a copied Cargo tree with only Rust sources, the native C kernels
and the pinned Lion crates.

The production dependency policy is an exact allowlist (docs/dev/lion-runtime-plan.md,
D5 and S1). srpc's normal dependencies are exactly `lion-reactor`, `lion-executor` and
`lion-net` (docs/dev/lion-tcp-plan.md, L3), as Cargo git dependencies on LION_GIT_URL,
each `{ git = LION_GIT_URL, rev = "<40-hex commit>", default-features = false }`, all at
one `rev` (docs/dev/lion-git-dep-plan.md, G3). That `rev` in Cargo.toml is the pin of
record; Cargo.lock must resolve every Lion package to exactly that commit.
Their resolved closure may contain only:

* the Lion crates named in LION_CRATES, each from `git+LION_GIT_URL?rev=<rev>#<rev>`, at
  its directory in Lion's repository, with no features;
* the erased Verus library (vstd and verus_builtin) at exactly VERUS_SOURCE;
* the Verus proc-macros and their build-time (host-only) closure: registry crates that
  are reached only through a proc-macro, plus Verus's own parser crates at VERUS_SOURCE.

Nothing from a registry may be linked into srpc, and Lion's runtime crates (FORBIDDEN)
must not appear anywhere in `cargo tree -e normal`. Cargo compiles Lion from its own
checkout of the pinned commit, and nothing stops a process from editing that checkout,
so the Lion crates must all come from one checkout whose HEAD is the rev and whose files
are exactly the pinned tree's, byte for byte, apart from Cargo's own root marker. The
isolated copy contains the Cargo sources, tests and native kernels; it resolves Lion
from the same Cargo git cache and builds and tests offline.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent.parent

# Lion's repository, exactly as Cargo.toml's git dependencies spell it. CMakeLists.txt
# (SRPC_LION_GIT_URL) and scripts/check_srpc_crate_mode.py (LION_GIT_URL) name the same
# URL to the transpiler's --crate-graph-git; scripts/tests/test_goal0_contracts.py
# requires the three to agree.
LION_GIT_URL = 'https://github.com/stonysystems/lion'
# A pinned commit: a full lowercase SHA-1, never a branch, tag or abbreviation.
FULL_COMMIT = re.compile(r'[0-9a-f]{40}')
# srpc's direct normal dependencies: exactly these, as git dependencies at one rev.
LION_DIRECT = ('lion-executor', 'lion-net', 'lion-reactor')
# Every Lion package the closure may contain, and its directory in Lion's repository
# (relative to the checkout root). A Lion pin bump that adds, removes or moves a crate
# fails here and needs review.
LION_CRATES = {
    'lion-executor': 'lion-executor',
    'lion-executor-spec': 'lion-executor-spec',
    'lion-framework-spec': 'lion-framework-spec',
    'lion-net': 'lion-net',
    'lion-reactor': 'lion-reactor',
    'lion-reactor-spec': 'lion-reactor-spec',
    'lion-slab': 'lion-slab',
    'lion-timer-wheel': 'lion-timer-wheel',
    'lion-utility-spec': 'lion-utility/lion-utility-spec',
}
# The Verus revision Lion's manifests name (`rev = "db81a74"`), as Cargo.lock records
# it. A Lion pin bump that moves Verus changes this source and must update it here,
# together with the verify-lion lane's Verus version (scripts/verify_lion.sh).
VERUS_SOURCE = ('git+https://github.com/verus-lang/verus?rev=db81a74'
                '#db81a7496bfffeef3da8b30c306600ea51d2b0fa')
VERUS_LINKED = frozenset({'vstd', 'verus_builtin'})
VERUS_PROC_MACROS = frozenset({'verus_builtin_macros', 'verus_state_machines_macros'})
VERUS_HOST = frozenset({'verus_syn', 'verus_prettyplease'})
CRATES_IO = 'registry+https://github.com/rust-lang/crates.io-index'
# Lion's runtime crates, which its `mio` feature and lion-utility bring in (D5).
FORBIDDEN = ('mio', 'flume', 'tokio', 'socket2', 'futures-task', 'pin-project-lite')
RETIRED_FACADES = ('rusty-rustc', 'rusty-cpp-markers')
# The file Cargo writes at the root of a finished git checkout, and the only path in
# Lion's checkout that may differ from the pinned commit (check_lion_checkout).
CARGO_CHECKOUT_MARKER = '.cargo-ok'
# How many local changes a failed checkout check names before it summarises.
CHANGES_SHOWN = 20
# The git tree modes with bytes to compare: a file, an executable file, a symbolic link.
FILE_MODES = frozenset({'100644', '100755'})
SYMLINK_MODE = '120000'


def lion_source(rev: str) -> str:
    """The Cargo source id of a Lion package pinned at `rev`."""
    return f'git+{LION_GIT_URL}?rev={rev}#{rev}'


def is_lion_repository(source: str | None) -> bool:
    """Whether a source id names LION_GIT_URL itself, at any reference."""
    return source is not None and (source.startswith(f'git+{LION_GIT_URL}?')
                                   or source.startswith(f'git+{LION_GIT_URL}#'))


def production_dependencies(manifest: dict) -> list[tuple[str, str, object]]:
    """(scope, name, spec) for every non-dev dependency in every scope."""
    # Cargo accepts the legacy underscore spelling in this crate's Rust edition.
    keys = ('dependencies', 'build-dependencies', 'build_dependencies')
    scopes = [('', manifest)]
    scopes += [(f'target.{name}.', scope) for name, scope in manifest.get('target', {}).items()]
    return [(f'{prefix}{key}', name, spec)
            for prefix, scope in scopes for key in keys
            for name, spec in scope.get(key, {}).items()]


def validate_manifest(repo: Path) -> str:
    """Check Cargo.toml against the allowlist; return the one Lion rev it pins."""
    manifest = tomllib.loads((repo / 'Cargo.toml').read_text())
    direct: dict[str, object] = {}
    for scope, name, spec in production_dependencies(manifest):
        if scope != 'dependencies':
            raise ValueError(f'canonical Rust takes no {scope}: {name}')
        direct[name] = spec
    if sorted(direct) != sorted(LION_DIRECT):
        raise ValueError('production dependencies must be exactly '
                         f'{sorted(LION_DIRECT)}; dependencies: {sorted(direct)}')
    shape = f'{{ git = "{LION_GIT_URL}", rev = "<40-hex commit>", default-features = false }}'
    for name, spec in sorted(direct.items()):
        if isinstance(spec, dict):
            for key in ('branch', 'tag', 'path'):
                if key in spec:
                    raise ValueError(f'dependency {name} must pin Lion by a full commit '
                                     f'`rev`, not `{key}`: {shape}')
        if not isinstance(spec, dict) or set(spec) != {'git', 'rev', 'default-features'}:
            raise ValueError(f'dependency {name} must be exactly {shape}')
        if spec['git'] != LION_GIT_URL:
            raise ValueError(f'dependency {name} must come from {LION_GIT_URL}, '
                             f'not {spec["git"]}')
        if not isinstance(spec['rev'], str) or not FULL_COMMIT.fullmatch(spec['rev']):
            raise ValueError(f'dependency {name} must pin a full 40-character lowercase '
                             f'commit hash, not rev = {spec["rev"]!r}')
        if spec['default-features'] is not False:
            raise ValueError(f'dependency {name} must set default-features = false '
                             '(Lion\'s default `mio` feature brings mio, socket2 and tokio)')
    revs = sorted({spec['rev'] for spec in direct.values()})
    if len(revs) != 1:
        raise ValueError(f'the Lion dependencies must pin one rev; Cargo.toml names {revs}')
    for table in ('patch', 'replace'):
        if table in manifest:
            raise ValueError(f'[{table}] would redirect a pinned dependency; it is forbidden')
    if manifest.get('workspace', {}).get('members', []):
        raise ValueError('canonical Cargo workspace must contain only the root package')
    for name in RETIRED_FACADES:
        if (repo / name).exists():
            raise ValueError(f'retired facade package still exists: {name}')
    return revs[0]


def validate_lockfile(repo: Path, rev: str) -> None:
    """Cargo.lock must resolve every Lion package to `rev`, and nothing else to Lion.

    `cargo metadata --locked` also refuses a lock that disagrees with Cargo.toml; this
    reads the file itself, so the pin it records is checked even before Cargo runs.
    """
    lock = tomllib.loads((repo / 'Cargo.lock').read_text())
    expected = lion_source(rev)
    found: dict[str, int] = {}
    for package in lock.get('package', []):
        name, source = package.get('name'), package.get('source')
        if name in LION_CRATES:
            if source != expected:
                raise ValueError(f'Cargo.lock resolves {name} to {source or "a path"}, but '
                                 f'Cargo.toml pins {expected}')
            found[name] = found.get(name, 0) + 1
        elif is_lion_repository(source):
            raise ValueError(f'Cargo.lock takes {name} from Lion\'s repository, but it is not '
                             'one of the allowlisted Lion crates')
        elif source is not None and source.startswith('git+') and source != VERUS_SOURCE:
            raise ValueError(f'Cargo.lock has a git package outside the allowlist: '
                             f'{name} ({source})')
    duplicated = sorted(name for name, count in found.items() if count > 1)
    if duplicated:
        raise ValueError(f'Cargo.lock resolves {duplicated} more than once')
    missing = sorted(set(LION_DIRECT) - set(found))
    if missing:
        raise ValueError(f'Cargo.lock does not resolve {missing}')


def ownership_exception(cwd: Path) -> list[str]:
    """`-c safe.directory=...` flags vouching for the worktree that holds `cwd`.

    git refuses a repository whose worktree belongs to another uid ("detected
    dubious ownership"). That is the normal shape of a container CI job, such
    as Mako's, which runs this gate as root over a checkout owned by the runner
    user, with a CARGO_HOME that may belong to someone else too. Naming the one
    worktree being read on the command line keeps the check working without
    depending on, or changing, global git config. The worktree is the nearest
    directory with a `.git`: this repository's root, or Mako's when SRPC is
    vendored at src/srpc, or Cargo's checkout of Lion. This waives git's
    ownership heuristic and nothing else.
    """
    top = next((d for d in (cwd, *cwd.parents) if (d / '.git').exists()), cwd)
    # git matches safe.directory against the worktree path it computed, which
    # has symlinks resolved; cover the path both as found and as resolved.
    directories = sorted({str(top), str(top.resolve())})
    return [argument for directory in directories
            for argument in ('-c', f'safe.directory={directory}')]


def git_output(args: list[str], cwd: Path) -> str:
    return subprocess.run(['git', *ownership_exception(cwd), *args], cwd=cwd, check=True,
                          text=True, stdout=subprocess.PIPE).stdout


def lion_checkout_root(manifests: dict[str, str]) -> Path:
    """The one checkout every Lion package's manifest lies in, at its LION_CRATES dir."""
    roots = set()
    for name, manifest in sorted(manifests.items()):
        path = Path(manifest).resolve()
        relative = Path(LION_CRATES[name]) / 'Cargo.toml'
        if path.parts[-len(relative.parts):] != relative.parts:
            raise ValueError(f'{name} must be built from {relative.as_posix()} in Lion\'s '
                             f'repository, not {manifest}')
        roots.add(Path(*path.parts[:-len(relative.parts)]))
    if len(roots) != 1:
        raise ValueError(f'the Lion crates must come from one checkout; found '
                         f'{sorted(str(root) for root in roots)}')
    return roots.pop()


def summarise(entries: list[str]) -> str:
    """The first CHANGES_SHOWN entries, one per line, then a count of the rest."""
    shown = '\n'.join(entries[:CHANGES_SHOWN])
    if len(entries) > CHANGES_SHOWN:
        shown += f'\n... and {len(entries) - CHANGES_SHOWN} more'
    return shown


def git_blob_id(data: bytes) -> str:
    """The object id git gives a blob holding `data`: SHA-1, like every pinned rev."""
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def pinned_tree(root: Path, rev: str, git=git_output) -> dict[str, tuple[str, str]]:
    """Every path in `rev`'s tree, mapped to its (mode, blob id)."""
    listing = git(['--no-replace-objects', 'ls-tree', '-r', '-z', '--full-tree', rev], root)
    tree = {}
    for entry in filter(None, listing.split('\0')):
        header, path = entry.split('\t', 1)
        mode, _, object_id = header.split(' ')
        if mode not in FILE_MODES and mode != SYMLINK_MODE:
            raise ValueError(f'{rev} has {path} with git mode {mode}; the Lion checkout '
                             'check compares files and symbolic links only')
        tree[path] = (mode, object_id)
    return tree


def checkout_files(root: Path) -> dict[str, tuple[str, str | None]]:
    """Every path under `root` that is not a directory, mapped to its kind and blob id.

    The kind is 'file', 'symlink' (whose blob is its target, as git stores it) or
    'other' (a FIFO, socket or device), which has no id. The root's own `.git` is
    skipped and nothing else is.
    """
    def fail(error: OSError) -> None:
        # Without this, os.walk skips a directory it cannot list.
        raise error

    found: dict[str, tuple[str, str | None]] = {}
    for directory, subdirectories, names in os.walk(root, onerror=fail):
        here = Path(directory)
        if here == root:
            subdirectories[:] = [name for name in subdirectories if name != '.git']
            names = [name for name in names if name != '.git']
        # os.walk lists a symbolic link to a directory with the directories and does
        # not follow it; Lion's tree has four.
        links = [name for name in subdirectories if (here / name).is_symlink()]
        for name in [*names, *links]:
            path = here / name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                entry = ('symlink', git_blob_id(os.fsencode(os.readlink(path))))
            elif stat.S_ISREG(mode):
                entry = ('file', git_blob_id(path.read_bytes()))
            else:
                entry = ('other', None)
            found[path.relative_to(root).as_posix()] = entry
    return found


def check_lion_files(root: Path, rev: str, git=git_output) -> None:
    """The checkout's files must be `rev`'s tree, byte for byte (check_lion_checkout).

    The executable bit is not compared: rustc reads a file's bytes, not its mode.
    """
    tree = pinned_tree(root, rev, git)
    found = checkout_files(root)
    differences = []
    for path in sorted(tree.keys() | found.keys()):
        if path not in found:
            differences.append(f'missing {path}')
        elif path not in tree:
            if path != CARGO_CHECKOUT_MARKER:
                differences.append(f'added {path}')
        else:
            mode, object_id = tree[path]
            kind, found_id = found[path]
            expected = 'symlink' if mode == SYMLINK_MODE else 'file'
            if kind != expected:
                differences.append(f'retyped {path} ({expected} in the tree, {kind} here)')
            elif found_id != object_id:
                differences.append(f'changed {path}')
    if differences:
        raise ValueError(f'the Lion checkout {root} differs from {rev}:\n'
                         f'{summarise(differences)}\n(compared byte for byte with the '
                         f'pinned tree; only the root {CARGO_CHECKOUT_MARKER} may be added)')


def check_lion_checkout(root: Path, rev: str, git=git_output) -> None:
    """The checkout Cargo compiles Lion from must be the pinned commit, unmodified.

    Cargo checks out a git dependency into CARGO_HOME, with its own `.git`; the
    directory is shared by every project on the machine and writable. Cargo's own
    freshness check compares only HEAD with the locked commit, and resets a moved
    checkout before this runs (measured with Cargo 1.97.1), so the HEAD check here is
    defense in depth. Cargo does not notice anything else. An edited tracked file
    compiles silently. So can an added one: Cargo's target discovery and rustc's
    module lookup read the directory, not git's index, and a stray `build.rs` in a
    crate root is a build script Cargo runs.

    The load-bearing check is check_lion_files: every file and symbolic link on disk
    must be the pinned tree's, byte for byte, and only CARGO_CHECKOUT_MARKER may be
    added, at the root. It reads the tree from git's objects and the files from the
    directory, and consults no index, ignore rule or config. `git status` consults
    all three, so it runs first only as a second opinion that names a change in
    git's terms, and also reports a changed executable bit. On its own it misses a
    change to a file marked `assume-unchanged` or `skip-worktree` in the checkout's
    index, and a FIFO (measured with git 2.47.3).

    The status lists ignored files too (`--ignored`). Ignore rules decide what git
    reports, not what Cargo compiles, and they are not all part of the pinned commit:
    an untracked `.gitignore` can hide itself and its neighbours, and the checkout's
    `.git/info/exclude`, its own config and the user's `core.excludesFile` can hide
    anything. Lion's tracked rules already hide `lion*/Cargo.lock`, `*.log` and
    `**/target/`. A checkout as Cargo leaves it holds no ignored file (measured with
    Cargo 1.97.1), so nothing legitimate is lost.

    Every git command here passes --no-replace-objects. A `refs/replace/` entry for
    the rev makes git read another commit in its place, and then `status` and
    `ls-tree` both agree with an edited checkout whose HEAD is still the rev
    (measured). Neither check survives a rewrite of the checkout's git objects
    themselves; that is deliberate tampering, and whoever can do it can as easily
    change the toolchain. If a future Cargo stops leaving a `.git` here, this fails
    closed.
    """
    if not (root / '.git').exists():
        raise ValueError(f'the Lion checkout {root} has no .git; cannot prove it is {rev}')
    head = git(['--no-replace-objects', 'rev-parse', 'HEAD'], root).strip()
    if head != rev:
        raise ValueError(f'the Lion checkout {root} is at {head}, but Cargo.toml pins {rev}')
    # --no-optional-locks: status must not rewrite the index of a checkout that is
    # shared with every other project using this CARGO_HOME. --untracked-files=all
    # lists each file in an untracked directory, and with it --ignored lists each
    # ignored file. -z keeps every path unquoted, one NUL-terminated entry each.
    status = git(['--no-replace-objects', '--no-optional-locks', 'status', '--porcelain', '-z',
                  '--untracked-files=all', '--ignored=traditional'], root)
    # The marker is untracked, or ignored if some exclude rule happens to match it.
    marker = (f'?? {CARGO_CHECKOUT_MARKER}', f'!! {CARGO_CHECKOUT_MARKER}')
    changed = [entry for entry in status.split('\0') if entry and entry not in marker]
    if changed:
        raise ValueError(f'the Lion checkout {root} has local changes:\n{summarise(changed)}\n'
                         f'(?? untracked, !! ignored; only the root {CARGO_CHECKOUT_MARKER} '
                         f'may differ from {rev})')
    check_lion_files(root, rev, git)


def is_proc_macro(package: dict) -> bool:
    return any('proc-macro' in target['kind'] for target in package['targets'])


def edges(node: dict, kinds: tuple) -> list[str]:
    return [dep['pkg'] for dep in node['deps']
            if any(kind['kind'] in kinds for kind in dep['dep_kinds'])]


def validate_graph(metadata: dict, rev: str) -> dict[str, str]:
    """Check the resolved graph against the allowlist.

    Returns {Lion package name: manifest_path} for the Lion crates linked into srpc.
    """
    packages = {package['id']: package for package in metadata['packages']}
    nodes = {node['id']: node for node in metadata['resolve']['nodes']}
    root = metadata['resolve']['root']
    if packages[root]['name'] != 'srpc':
        raise ValueError(f'resolve root is {packages[root]["name"]}, not srpc')
    members = metadata['workspace_members']
    if members != [root]:
        names = sorted(packages[member]['name'] for member in members)
        raise ValueError(f'canonical Cargo workspace must contain only the root package; '
                         f'members: {names}')
    if edges(nodes[root], ('build',)):
        raise ValueError('srpc takes no build-dependencies')
    direct = sorted(packages[dep]['name'] for dep in edges(nodes[root], (None,)))
    if direct != sorted(LION_DIRECT):
        raise ValueError(f'srpc must depend on exactly {sorted(LION_DIRECT)}; resolved: {direct}')
    expected_lion = lion_source(rev)

    def describe(package: dict) -> str:
        return f'{package["name"]} {package["version"]} ({package["source"] or "path"})'

    # Linked closure: what srpc's code links against. Proc-macros end it; they run
    # on the host at build time and are checked with their own closure below.
    linked: set[str] = set()
    macros: set[str] = set()
    lion: dict[str, str] = {}
    pending = edges(nodes[root], (None,))
    while pending:
        current = pending.pop()
        if current in linked or current in macros:
            continue
        package = packages[current]
        name, source = package['name'], package['source']
        if is_proc_macro(package):
            if source != VERUS_SOURCE or name not in VERUS_PROC_MACROS:
                raise ValueError(f'proc-macro outside the allowlist: {describe(package)}')
            macros.add(current)
            continue
        if source is None:
            raise ValueError(f'path dependency outside the allowlist: {describe(package)} '
                             f'(Lion comes only from {LION_GIT_URL})')
        if source == expected_lion:
            if name not in LION_CRATES:
                raise ValueError(f'Lion package outside the allowlist: {describe(package)}')
            features = nodes[current]['features']
            if features:
                raise ValueError(f'{name} must build with no features (mio stays off); '
                                 f'resolved features: {features}')
            lion[name] = package['manifest_path']
        elif source.startswith('git+'):
            if name in LION_CRATES:
                raise ValueError(f'{name} must come from {expected_lion}, not {source}')
            if name not in VERUS_LINKED:
                raise ValueError(f'git dependency outside the allowlist: {describe(package)}')
            if source != VERUS_SOURCE:
                raise ValueError(f'{name} must come from {VERUS_SOURCE}, not {source}')
        else:
            raise ValueError(f'{describe(package)} would be linked into srpc; only Lion '
                             'and the erased vstd may be')
        if edges(nodes[current], ('build',)):
            raise ValueError(f'{name} gained build-dependencies; review them')
        linked.add(current)
        pending.extend(edges(nodes[current], (None,)))

    # Host closure: everything the proc-macros need to run, normal and build edges.
    host: set[str] = set()
    pending = [dep for macro in macros for dep in edges(nodes[macro], (None, 'build'))]
    while pending:
        current = pending.pop()
        if current in host:
            continue
        package = packages[current]
        name, source = package['name'], package['source']
        if source == VERUS_SOURCE:
            if name not in VERUS_HOST | VERUS_PROC_MACROS:
                raise ValueError(f'unexpected Verus crate in the proc-macro closure: '
                                 f'{describe(package)}')
        elif source != CRATES_IO:
            raise ValueError(f'proc-macro closure may use only crates.io and {VERUS_SOURCE}: '
                             f'{describe(package)}')
        host.add(current)
        pending.extend(edges(nodes[current], (None, 'build')))

    for package_id in linked | macros | host:
        if packages[package_id]['name'] in FORBIDDEN:
            raise ValueError(f'{describe(packages[package_id])} must not enter the graph (D5)')
    return lion


def check_normal_tree(tree_output: str) -> None:
    """Reject Lion's runtime crates in `cargo tree -e normal --prefix none` output."""
    names = {line.split()[0] for line in tree_output.splitlines() if line.strip()}
    found = sorted(names & set(FORBIDDEN))
    if found:
        raise ValueError(f'cargo tree -e normal contains {found}; they must stay out (D5)')


def copy_cargo_tree(repo: Path, dest: Path) -> None:
    paths = {Path(name) for name in ('Cargo.toml', 'Cargo.lock', 'build.rs', 'src/lib.rs',
                                    'scripts/native-kernel-sources.txt')}
    modules = tomllib.loads((repo / 'rust-modules.toml').read_text())['module']
    for module in modules:
        source = Path(module['source'])
        if source.suffix != '.rs':
            raise ValueError(f'canonical Cargo source must be Rust: {source}')
        paths.add(source)
    paths.update(path.relative_to(repo) for path in (repo / 'tests').rglob('*.rs'))
    native = []
    for line in (repo / 'scripts/native-kernel-sources.txt').read_text().splitlines():
        selector, name = line.split()
        if selector not in ('all', 'x86_64', 'aarch64') or Path(name).suffix not in ('.c', '.S'):
            raise ValueError(f'invalid C/assembly kernel record: {line}')
        native.append(Path(name))
    # Follow only local native headers. No C++ adapter or vendored runtime is copied.
    while native:
        path = native.pop()
        if path in paths:
            continue
        paths.add(path)
        for name in re.findall(r'^\s*#\s*include\s*"([^"]+)"', (repo / path).read_text(), re.M):
            candidates = (path.parent / name, Path(name))
            included = next((p for p in candidates if (repo / p).is_file()), None)
            if included is None or included.is_absolute() or included.suffix != '.h' or '..' in included.parts:
                raise ValueError(f'unsupported local kernel include: {path}: {name}')
            native.append(included)
    for relative in sorted(paths):
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError(f'Cargo source must stay inside the repository: {relative}')
        source = repo / relative
        if source.is_symlink():
            raise ValueError(f'Cargo source symlink is forbidden: {relative}')
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def cargo_json(cargo: list[str], cwd: Path, env: dict | None = None) -> dict:
    output = subprocess.run([*cargo, 'metadata', '--format-version', '1', '--locked', '--offline'],
                            cwd=cwd, env=env, check=True, text=True, stdout=subprocess.PIPE).stdout
    return json.loads(output)


def cargo_normal_tree(cargo: list[str], cwd: Path, env: dict | None = None) -> str:
    return subprocess.run([*cargo, 'tree', '-e', 'normal', '--locked', '--offline',
                           '--prefix', 'none', '--format', '{p}'],
                          cwd=cwd, env=env, check=True, text=True, stdout=subprocess.PIPE).stdout


def run(repo: Path) -> None:
    rev = validate_manifest(repo)
    validate_lockfile(repo, rev)
    lion = validate_graph(cargo_json(['cargo'], repo), rev)
    check_normal_tree(cargo_normal_tree(['cargo'], repo))
    checkout = lion_checkout_root(lion)
    check_lion_checkout(checkout, rev)
    scratch = repo / 'target' / 'rust-independence'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cargo-only-', dir=scratch) as directory:
        dest = Path(directory) / 'source'
        dest.mkdir()
        copy_cargo_tree(repo, dest)
        tool_dir = Path(directory) / 'tools'
        tool_dir.mkdir()
        # Use real toolchain binaries, bypassing rustup proxies in the restricted PATH.
        # Without rustup (a toolchain installed from the static tarball, as in the
        # CI image), the binaries on PATH are already the real ones.
        rustup = shutil.which('rustup')
        for name in ('cargo', 'rustc', 'rustdoc'):
            if rustup is not None:
                path = subprocess.check_output([rustup, 'which', name], text=True).strip()
            else:
                path = shutil.which(name)
                if path is None:
                    raise ValueError(f'required C/Rust build tool is missing: {name}')
            (tool_dir / name).symlink_to(path)
        for name in ('cc', 'ar', 'as', 'ld', 'sh'):
            path = shutil.which(name)
            if path is None:
                raise ValueError(f'required C/Rust build tool is missing: {name}')
            (tool_dir / name).symlink_to(path)
        # CARGO_HOME (or HOME, which locates it) stays inherited: the copy resolves
        # Lion and vstd from the same git cache. The restricted PATH has no `git`,
        # and needs none: with --offline Cargo never fetches, and it reads its git
        # database and checkouts with its built-in git support, not the git CLI.
        environment = os.environ.copy()
        environment.update(PATH=str(tool_dir), CC=str(tool_dir / 'cc'), AR=str(tool_dir / 'ar'),
                           CXX='/bin/false', RUSTC=str(tool_dir / 'rustc'),
                           RUSTDOC=str(tool_dir / 'rustdoc'), RUSTFLAGS='-Dwarnings',
                           CARGO_INCREMENTAL='0', CARGO_TARGET_DIR=str(Path(directory) / 'target'))
        cargo = [str(tool_dir / 'cargo')]
        copied = validate_graph(cargo_json(cargo, dest, environment), rev)
        if copied != lion:
            raise ValueError(f'the copy resolved the Lion crates to {copied}, '
                             f'expected the checked checkout {lion}')
        check_normal_tree(cargo_normal_tree(cargo, dest, environment))
        # The copied tree has no C++ source, facade, marker crate or other third-party code.
        for args in (['test', '--workspace', '--all-targets'], ['test', '--workspace', '--doc']):
            subprocess.run([*cargo, *args, '--locked', '--offline'],
                           cwd=dest, env=environment, check=True)
    print(f'Cargo independence passed: std + C/assembly kernel + Lion {rev[:7]} '
          f'({len(lion)} git crates from {LION_GIT_URL}, checkout {checkout} identical to the rev) '
          f'+ erased vstd at {VERUS_SOURCE.rsplit("#", 1)[1][:7]}; '
          f'no {", ".join(FORBIDDEN)}; no facade or C++ tools/runtime')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=ROOT)
    args = parser.parse_args()
    run(args.repo.resolve())


if __name__ == '__main__':
    main()
