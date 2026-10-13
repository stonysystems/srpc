#!/usr/bin/env python3
"""Negative controls for the Cargo-only copy, the manifest and lock checks, the D5
allowlist, and the identity of Cargo's Lion checkout."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_rust_independence as gate

URL = gate.LION_GIT_URL
# A fixture pin: any full commit hash works, since every check takes the rev that
# Cargo.toml names rather than a literal of its own.
REV = 'd697b779bb05314e7425b1746b5f89441e8e6712'
OTHER_REV = '3496113ab87175b5c10513419979d40b8d2b1916'
SPEC = f'{{ git = "{URL}", rev = "{REV}", default-features = false }}'
LION_DEPENDENCIES = (
    '[dependencies]\n'
    f'lion-reactor = {SPEC}\n'
    f'lion-executor = {SPEC}\n'
    f'lion-net = {SPEC}\n'
)
MANIFEST = '[package]\nname="fixture"\nversion="0.0.0"\n' + LION_DEPENDENCIES


def lock_text(sources: dict | None = None, extra: str = '') -> str:
    """A Cargo.lock with the nine Lion packages at REV, srpc, vstd and a registry crate.

    `sources` overrides a Lion package's source line (None drops it: a path package).
    """
    sources = sources or {}
    entries = ['version = 4\n',
               '[[package]]\nname = "srpc"\nversion = "0.0.0"\n',
               '[[package]]\nname = "vstd"\nversion = "0.0.0"\n'
               f'source = "{gate.VERUS_SOURCE}"\n',
               '[[package]]\nname = "proptest"\nversion = "1.0.0"\n'
               f'source = "{gate.CRATES_IO}"\n']
    for name in sorted(gate.LION_CRATES):
        source = sources.get(name, gate.lion_source(REV))
        line = f'source = "{source}"\n' if source is not None else ''
        entries.append(f'[[package]]\nname = "{name}"\nversion = "0.1.0"\n{line}')
    return '\n'.join(entries) + extra


class RustIndependenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name) / 'source'
        self.dest = Path(temporary.name) / 'copy'
        self.repo.mkdir()
        self.dest.mkdir()
        for folder in ('scripts', 'src', 'tests', 'native'):
            (self.repo / folder).mkdir()
        (self.repo / 'Cargo.toml').write_text(MANIFEST)
        (self.repo / 'Cargo.lock').write_text(lock_text())
        (self.repo / 'build.rs').write_text('fn main() {}')
        (self.repo / 'src/lib.rs').write_text('pub fn value() -> i32 { 1 }')
        (self.repo / 'rust-modules.toml').write_text('[[module]]\nsource="canonical.rs"\n')
        (self.repo / 'canonical.rs').write_text('pub fn value() -> i32 { 2 }')
        (self.repo / 'tests/probe.rs').write_text('#[test] fn check() {}')
        (self.repo / 'scripts/native-kernel-sources.txt').write_text('all native/kernel.c\n')
        (self.repo / 'native/kernel.c').write_text('#include "kernel.h"\n')
        (self.repo / 'native/kernel.h').write_text('int native_value(void);\n')

    def write_manifest(self, text):
        (self.repo / 'Cargo.toml').write_text(text)

    def test_only_cargo_and_native_inputs_are_copied(self):
        (self.repo / 'runtime.cpp').write_text('int hidden_runtime() { return 0; }')
        # A stale submodule directory is not a Cargo input any more.
        (self.repo / 'third-party/lion/lion-slab').mkdir(parents=True)
        (self.repo / 'third-party/lion/lion-slab/Cargo.toml').write_text('\n')
        self.assertEqual(gate.validate_manifest(self.repo), REV)
        gate.copy_cargo_tree(self.repo, self.dest)
        self.assertTrue((self.dest / 'canonical.rs').is_file())
        self.assertTrue((self.dest / 'tests/probe.rs').is_file())
        self.assertTrue((self.dest / 'native/kernel.h').is_file())
        self.assertTrue((self.dest / 'Cargo.lock').is_file())
        self.assertFalse((self.dest / 'runtime.cpp').exists())
        self.assertFalse((self.dest / 'third-party').exists())

    def test_production_dependencies_are_rejected_in_every_scope(self):
        for table in ('build-dependencies', 'build_dependencies',
                      'target.cfg(unix).dependencies', 'target.cfg(unix).build-dependencies',
                      'target.cfg(unix).build_dependencies'):
            with self.subTest(table=table):
                table = table.replace('cfg(unix)', '\"cfg(unix)\"')
                self.write_manifest(f'{MANIFEST}[{table}]\nshadow="1"\n')
                with self.assertRaisesRegex(ValueError, 'dependencies'):
                    gate.validate_manifest(self.repo)

    def test_extra_registry_dependency_is_rejected(self):
        self.write_manifest(MANIFEST + 'serde = "1"\n')
        with self.assertRaisesRegex(ValueError, "exactly .*'serde'"):
            gate.validate_manifest(self.repo)

    def test_lion_dependencies_are_required(self):
        self.write_manifest('[package]\nname="fixture"\nversion="0.0.0"\n')
        with self.assertRaisesRegex(ValueError, 'exactly'):
            gate.validate_manifest(self.repo)

    def test_each_lion_dependency_is_required(self):
        # lion-net joined the two runtime crates in L3 (docs/dev/lion-tcp-plan.md):
        # the manifest must name all three, so dropping any one is rejected.
        for name in gate.LION_DIRECT:
            with self.subTest(dependency=name):
                line = f'{name} = {SPEC}\n'
                self.assertIn(line, MANIFEST)
                self.write_manifest(MANIFEST.replace(line, ''))
                with self.assertRaisesRegex(ValueError, 'exactly'):
                    gate.validate_manifest(self.repo)

    def test_test_only_dependencies_are_permitted(self):
        self.write_manifest(MANIFEST + '[dev-dependencies]\nproptest="1"\n')
        self.assertEqual(gate.validate_manifest(self.repo), REV)

    def replace_reactor(self, spec):
        manifest = MANIFEST.replace(f'lion-reactor = {SPEC}', f'lion-reactor = {spec}')
        self.assertNotEqual(manifest, MANIFEST)
        self.write_manifest(manifest)

    def test_mio_enabling_dependency_features_are_rejected(self):
        for spec, message in (
                (f'{{ git = "{URL}", rev = "{REV}" }}', 'exactly'),
                (f'{{ git = "{URL}", rev = "{REV}", default-features = true }}',
                 'default-features = false'),
                (f'{{ git = "{URL}", rev = "{REV}", default-features = false, '
                 'features = ["mio"] }', 'exactly'),
                (f'{{ git = "{URL}", rev = "{REV}", default_features = false }}', 'exactly')):
            with self.subTest(spec=spec):
                self.replace_reactor(spec)
                with self.assertRaisesRegex(ValueError, message):
                    gate.validate_manifest(self.repo)

    def test_lion_must_be_pinned_by_a_full_rev(self):
        for spec, message in (
                # A branch or tag moves; only a commit pins.
                (f'{{ git = "{URL}", branch = "srpc-lion", default-features = false }}',
                 'not `branch`'),
                (f'{{ git = "{URL}", tag = "v1", default-features = false }}', 'not `tag`'),
                (f'{{ git = "{URL}", rev = "{REV}", branch = "srpc-lion", '
                 'default-features = false }', 'not `branch`'),
                # No reference at all is the default branch.
                (f'{{ git = "{URL}", default-features = false }}', 'exactly'),
                # Abbreviated and non-canonical spellings of the commit.
                (f'{{ git = "{URL}", rev = "{REV[:7]}", default-features = false }}',
                 'full 40-character'),
                (f'{{ git = "{URL}", rev = "{REV.upper()}", default-features = false }}',
                 'full 40-character'),
                (f'{{ git = "{URL}", rev = "refs/heads/srpc-lion", '
                 'default-features = false }', 'full 40-character')):
            with self.subTest(spec=spec):
                self.replace_reactor(spec)
                with self.assertRaisesRegex(ValueError, message):
                    gate.validate_manifest(self.repo)

    def test_lion_must_come_from_its_repository(self):
        for spec, message in (
                ('{ path = "third-party/lion/lion-reactor", default-features = false }',
                 'not `path`'),
                (f'{{ git = "{URL}", rev = "{REV}", path = "lion-reactor", '
                 'default-features = false }', 'not `path`'),
                (f'{{ git = "{URL}.git", rev = "{REV}", default-features = false }}',
                 f'must come from {URL}, not {URL}.git'),
                (f'{{ git = "https://github.com/someone/lion", rev = "{REV}", '
                 'default-features = false }', 'must come from'),
                (f'{{ git = "{URL}", rev = "{REV}", default-features = false, '
                 'package = "lion-executor" }', 'exactly')):
            with self.subTest(spec=spec):
                self.replace_reactor(spec)
                with self.assertRaisesRegex(ValueError, message):
                    gate.validate_manifest(self.repo)

    def test_lion_dependencies_must_share_one_rev(self):
        self.replace_reactor(SPEC.replace(REV, OTHER_REV))
        with self.assertRaisesRegex(ValueError, 'must pin one rev'):
            gate.validate_manifest(self.repo)

    def test_extra_path_dependency_is_rejected(self):
        self.write_manifest(MANIFEST + 'shadow = { path = "../shadow" }\n')
        with self.assertRaisesRegex(ValueError, "exactly .*'shadow'"):
            gate.validate_manifest(self.repo)

    def test_patch_and_replace_tables_are_rejected(self):
        for table in ('[patch."https://github.com/verus-lang/verus"]\nvstd = { path = "vstd" }\n',
                      f'[patch."{URL}"]\nlion-slab = {{ path = "../lion/lion-slab" }}\n',
                      '[replace]\n"vstd:0.0.0" = { path = "vstd" }\n'):
            with self.subTest(table=table):
                self.write_manifest(MANIFEST + table)
                with self.assertRaisesRegex(ValueError, 'forbidden'):
                    gate.validate_manifest(self.repo)

    def test_extra_workspace_member_is_rejected(self):
        self.write_manifest(MANIFEST + '[workspace]\nmembers=["shadow"]\n')
        with self.assertRaisesRegex(ValueError, 'root package'):
            gate.validate_manifest(self.repo)

    def test_retired_packages_are_rejected(self):
        for name in ('rusty-rustc', 'rusty-cpp-markers'):
            with self.subTest(package=name):
                (self.repo / name).mkdir()
                with self.assertRaisesRegex(ValueError, 'retired facade package'):
                    gate.validate_manifest(self.repo)
                (self.repo / name).rmdir()

    def test_cpp_native_source_is_rejected(self):
        (self.repo / 'scripts/native-kernel-sources.txt').write_text('all native/kernel.cpp\n')
        with self.assertRaisesRegex(ValueError, 'invalid C/assembly'):
            gate.copy_cargo_tree(self.repo, self.dest)

    def test_cpp_local_header_is_rejected(self):
        (self.repo / 'native/kernel.c').write_text('#include "runtime.hpp"\n')
        (self.repo / 'native/runtime.hpp').write_text('class Hidden {};')
        with self.assertRaisesRegex(ValueError, 'unsupported local kernel include'):
            gate.copy_cargo_tree(self.repo, self.dest)

    def test_absolute_local_header_is_rejected(self):
        (self.repo / 'native/kernel.c').write_text(f'#include "{self.repo}/native/kernel.h"\n')
        with self.assertRaisesRegex(ValueError, 'unsupported local kernel include'):
            gate.copy_cargo_tree(self.repo, self.dest)

    def test_source_symlink_is_rejected(self):
        (self.repo / 'canonical.rs').unlink()
        (self.repo / 'canonical.rs').symlink_to(self.repo / 'src/lib.rs')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            gate.copy_cargo_tree(self.repo, self.dest)

    def test_source_escape_is_rejected(self):
        (self.repo / 'rust-modules.toml').write_text('[[module]]\nsource="../outside.rs"\n')
        with self.assertRaisesRegex(ValueError, 'inside the repository'):
            gate.copy_cargo_tree(self.repo, self.dest)


class LockfileTests(unittest.TestCase):
    """Cargo.lock must resolve every Lion package to the rev Cargo.toml pins."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        (self.repo / 'Cargo.toml').write_text(MANIFEST)

    def check(self, text):
        (self.repo / 'Cargo.lock').write_text(text)
        gate.validate_lockfile(self.repo, gate.validate_manifest(self.repo))

    def assertRejected(self, text, message):
        with self.assertRaisesRegex(ValueError, message):
            self.check(text)

    def test_pinned_lock_is_accepted(self):
        self.check(lock_text())

    def test_wrong_rev_in_cargo_toml_is_rejected(self):
        # Cargo.toml moved to another commit, but the lock was not re-resolved.
        (self.repo / 'Cargo.toml').write_text(MANIFEST.replace(REV, OTHER_REV))
        self.assertRejected(lock_text(), f'Cargo.toml pins .*{OTHER_REV}')

    def test_lock_at_another_rev_is_rejected(self):
        for name in ('lion-reactor', 'lion-slab', 'lion-utility-spec'):
            with self.subTest(name=name):
                self.assertRejected(lock_text({name: gate.lion_source(OTHER_REV)}),
                                    f'resolves {name} to .*{OTHER_REV}')

    def test_lock_with_another_precise_commit_is_rejected(self):
        source = f'git+{URL}?rev={REV}#{OTHER_REV}'
        self.assertRejected(lock_text({'lion-net': source}), 'resolves lion-net to')

    def test_lock_on_a_branch_is_rejected(self):
        source = f'git+{URL}?branch=srpc-lion#{REV}'
        self.assertRejected(lock_text({'lion-executor': source}), 'resolves lion-executor to')

    def test_lock_from_another_repository_is_rejected(self):
        for url in ('https://github.com/someone/lion', f'{URL}.git'):
            with self.subTest(url=url):
                source = f'git+{url}?rev={REV}#{REV}'
                self.assertRejected(lock_text({'lion-timer-wheel': source}),
                                    'resolves lion-timer-wheel to')

    def test_lock_with_a_path_lion_package_is_rejected(self):
        self.assertRejected(lock_text({'lion-slab': None}), 'resolves lion-slab to a path')

    def test_lock_with_another_lion_package_is_rejected(self):
        extra = (f'\n[[package]]\nname = "lion-utility"\nversion = "0.1.0"\n'
                 f'source = "{gate.lion_source(REV)}"\n')
        self.assertRejected(lock_text(extra=extra), 'not one of the allowlisted Lion crates')

    def test_lock_with_another_git_package_is_rejected(self):
        extra = ('\n[[package]]\nname = "shadow"\nversion = "0.1.0"\n'
                 f'source = "git+https://example.com/shadow?rev={REV}#{REV}"\n')
        self.assertRejected(lock_text(extra=extra), 'git package outside the allowlist')

    def test_lock_resolving_a_lion_crate_twice_is_rejected(self):
        extra = (f'\n[[package]]\nname = "lion-slab"\nversion = "0.2.0"\n'
                 f'source = "{gate.lion_source(REV)}"\n')
        self.assertRejected(lock_text(extra=extra), 'more than once')


def git(args, cwd):
    return subprocess.run(['git', '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.com',
                           *args], cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE).stdout


class LionCheckoutTests(unittest.TestCase):
    """Cargo's checkout must be one tree, at the rev, with no tracked change.

    The fixture is a real git repository laid out like Lion's, checked by the real
    `git` and the gate's own functions.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name) / 'checkouts/lion-e399824530cb448a/d697b77'
        for relative in gate.LION_CRATES.values():
            crate = self.checkout / relative
            (crate / 'src').mkdir(parents=True)
            (crate / 'Cargo.toml').write_text(f'# {relative}\n')
            (crate / 'src/lib.rs').write_text(f'// {relative}\n')
        (self.checkout / 'ci.sh').write_text('#!/bin/sh\n')
        git(['init', '-q'], self.checkout)
        git(['add', '.'], self.checkout)
        git(['commit', '-q', '-m', 'fixture'], self.checkout)
        self.rev = git(['rev-parse', 'HEAD'], self.checkout).strip()
        # Cargo marks a finished checkout with an untracked file at its root.
        (self.checkout / '.cargo-ok').write_text('{"v":1}')
        self.manifests = {name: str(self.checkout / relative / 'Cargo.toml')
                          for name, relative in gate.LION_CRATES.items()}

    def test_one_clean_checkout_at_the_rev_is_accepted(self):
        root = gate.lion_checkout_root(self.manifests)
        self.assertEqual(root, self.checkout.resolve())
        gate.check_lion_checkout(root, self.rev)

    def test_modified_tracked_file_is_rejected(self):
        (self.checkout / 'lion-slab/src/lib.rs').write_text('// patched\n')
        with self.assertRaisesRegex(ValueError, 'local changes:\n M lion-slab/src/lib.rs'):
            gate.check_lion_checkout(self.checkout, self.rev)

    def test_staged_or_deleted_tracked_file_is_rejected(self):
        (self.checkout / 'lion-net/src/lib.rs').unlink()
        with self.assertRaisesRegex(ValueError, 'local changes'):
            gate.check_lion_checkout(self.checkout, self.rev)

    def test_checkout_at_another_commit_is_rejected(self):
        # The checkout is clean, but Cargo.toml pins a different commit.
        with self.assertRaisesRegex(ValueError, f'is at {self.rev}, but Cargo.toml pins'):
            gate.check_lion_checkout(self.checkout, OTHER_REV)
        # And a clean checkout moved past the pinned commit.
        (self.checkout / 'lion-slab/src/lib.rs').write_text('// next\n')
        git(['commit', '-q', '-am', 'next'], self.checkout)
        with self.assertRaisesRegex(ValueError, 'but Cargo.toml pins'):
            gate.check_lion_checkout(self.checkout, self.rev)

    def test_checkout_without_git_is_rejected(self):
        bare = self.checkout.parent / 'copied'
        (bare / 'lion-slab').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'has no .git'):
            gate.check_lion_checkout(bare, self.rev)

    def test_crates_from_two_checkouts_are_rejected(self):
        manifests = dict(self.manifests)
        manifests['lion-slab'] = str(self.checkout.parent / 'aa5bebe/lion-slab/Cargo.toml')
        with self.assertRaisesRegex(ValueError, 'one checkout'):
            gate.lion_checkout_root(manifests)

    def test_crate_at_the_wrong_directory_is_rejected(self):
        manifests = dict(self.manifests)
        manifests['lion-utility-spec'] = str(self.checkout / 'lion-utility-spec/Cargo.toml')
        with self.assertRaisesRegex(ValueError, 'lion-utility/lion-utility-spec/Cargo.toml'):
            gate.lion_checkout_root(manifests)


CHECKOUT = Path('/cargo/git/checkouts/lion-e399824530cb448a/d697b77')
REGISTRY = gate.CRATES_IO


def package(name, source, manifest=None, kinds=('lib',)):
    return {'id': f'{name}-id', 'name': name, 'version': '0.1.0', 'source': source,
            'manifest_path': str(manifest or f'/registry/{name}/Cargo.toml'),
            'targets': [{'kind': [kind]} for kind in kinds]}


def lion(name, source=None):
    return package(name, source or gate.lion_source(REV),
                   CHECKOUT / gate.LION_CRATES[name] / 'Cargo.toml')


def verus(name, kinds=('lib',)):
    return package(name, gate.VERUS_SOURCE, kinds=kinds)


def dep(name, kind=None):
    return {'pkg': f'{name}-id', 'dep_kinds': [{'kind': kind, 'target': None}]}


LINKED_LION = ('lion-executor', 'lion-net', 'lion-reactor', 'lion-slab', 'lion-utility-spec')


def fixture_metadata():
    """A small graph shaped like the real one: srpc -> Lion -> vstd -> proc-macros."""
    packages = [
        package('srpc', None, Path('/repo/Cargo.toml')),
        *(lion(name) for name in LINKED_LION),
        verus('vstd'), verus('verus_builtin'),
        verus('verus_builtin_macros', kinds=('proc-macro',)),
        verus('verus_syn'),
        package('proc-macro2', REGISTRY), package('indexmap', REGISTRY),
        package('autocfg', REGISTRY), package('proptest', REGISTRY),
    ]
    graph = {
        'srpc': [dep('lion-executor'), dep('lion-net'), dep('lion-reactor'),
                 dep('proptest', 'dev')],
        'lion-executor': [dep('lion-reactor'), dep('lion-slab'), dep('vstd')],
        'lion-net': [dep('lion-reactor'), dep('lion-utility-spec'), dep('vstd')],
        'lion-reactor': [dep('lion-slab'), dep('lion-utility-spec'), dep('vstd')],
        'lion-slab': [dep('vstd')],
        'lion-utility-spec': [dep('vstd')],
        'vstd': [dep('verus_builtin'), dep('verus_builtin_macros')],
        'verus_builtin': [],
        'verus_builtin_macros': [dep('proc-macro2'), dep('verus_syn'), dep('indexmap')],
        'verus_syn': [dep('proc-macro2')],
        'proc-macro2': [],
        'indexmap': [dep('autocfg', 'build')],
        'autocfg': [],
        'proptest': [],
    }
    nodes = [{'id': f'{name}-id', 'deps': deps, 'features': []} for name, deps in graph.items()]
    return {'packages': packages, 'workspace_members': ['srpc-id'],
            'resolve': {'root': 'srpc-id', 'nodes': nodes}}


def add(metadata, new_package, parent, kind=None, features=()):
    metadata['packages'].append(new_package)
    metadata['resolve']['nodes'].append(
        {'id': new_package['id'], 'deps': [], 'features': list(features)})
    node(metadata, parent)['deps'].append(dep(new_package['name'], kind))


def node(metadata, name):
    return next(n for n in metadata['resolve']['nodes'] if n['id'] == f'{name}-id')


def replace(metadata, new_package):
    metadata['packages'] = [p for p in metadata['packages'] if p['id'] != new_package['id']]
    metadata['packages'].append(new_package)


class LionGraphTests(unittest.TestCase):
    """The resolved graph against the D5 allowlist, on synthetic `cargo metadata`."""

    def assertRejected(self, metadata, message, rev=REV):
        with self.assertRaisesRegex(ValueError, message):
            gate.validate_graph(metadata, rev)

    def test_allowlisted_graph_is_accepted(self):
        lion_manifests = gate.validate_graph(fixture_metadata(), REV)
        self.assertEqual(lion_manifests, {
            name: str(CHECKOUT / gate.LION_CRATES[name] / 'Cargo.toml') for name in LINKED_LION})
        self.assertEqual(gate.lion_checkout_root(lion_manifests), CHECKOUT)

    def test_graph_at_another_rev_is_rejected(self):
        # Cargo.toml (and so the rev the gate checks) names OTHER_REV, the graph REV.
        self.assertRejected(fixture_metadata(), 'must come from .*' + OTHER_REV, rev=OTHER_REV)

    def test_lion_crate_from_another_source_is_rejected(self):
        for source in (gate.lion_source(OTHER_REV),
                       f'git+{URL}?branch=srpc-lion#{REV}',
                       f'git+https://github.com/someone/lion?rev={REV}#{REV}'):
            with self.subTest(source=source):
                metadata = fixture_metadata()
                replace(metadata, lion('lion-slab', source))
                self.assertRejected(metadata,
                                    f'lion-slab must come from .*, not {re.escape(source)}')

    def test_lion_crate_as_path_dependency_is_rejected(self):
        metadata = fixture_metadata()
        replace(metadata, package('lion-slab', None, Path('/repo/third-party/lion/lion-slab/Cargo.toml')))
        self.assertRejected(metadata, 'path dependency outside the allowlist: lion-slab')
        metadata = fixture_metadata()
        add(metadata, package('shadow', None, Path('/repo/shadow/Cargo.toml')), 'lion-reactor')
        self.assertRejected(metadata, 'path dependency outside the allowlist: shadow')

    def test_unlisted_package_from_lion_is_rejected(self):
        metadata = fixture_metadata()
        add(metadata, lion('lion-reactor-spec') | {'id': 'lion-utility-id', 'name': 'lion-utility'},
            'lion-executor')
        self.assertRejected(metadata, 'Lion package outside the allowlist: lion-utility')

    def test_extra_registry_dependency_is_rejected(self):
        metadata = fixture_metadata()
        add(metadata, package('serde', REGISTRY), 'srpc')
        self.assertRejected(metadata, 'exactly')

    def test_registry_crate_linked_through_lion_is_rejected(self):
        metadata = fixture_metadata()
        add(metadata, package('libc', REGISTRY), 'lion-reactor')
        self.assertRejected(metadata, 'libc .* would be linked into srpc')

    def test_mio_enabling_feature_is_rejected(self):
        metadata = fixture_metadata()
        node(metadata, 'lion-reactor')['features'] = ['default', 'mio']
        add(metadata, package('mio', REGISTRY), 'lion-reactor')
        self.assertRejected(metadata, 'no features')
        # And without the feature record, mio itself is still a registry crate
        # linked into srpc.
        node(metadata, 'lion-reactor')['features'] = []
        self.assertRejected(metadata, 'mio .* would be linked into srpc')

    def test_forbidden_crate_in_the_proc_macro_closure_is_rejected(self):
        for name in gate.FORBIDDEN:
            with self.subTest(name=name):
                metadata = fixture_metadata()
                add(metadata, package(name, REGISTRY), 'verus_builtin_macros')
                self.assertRejected(metadata, 'must not enter the graph')

    def test_wrong_vstd_rev_is_rejected(self):
        metadata = fixture_metadata()
        wrong = gate.VERUS_SOURCE.replace('rev=db81a74#db81a7496bfffeef3da8b30c306600ea51d2b0fa',
                                          'rev=0123456#0123456789abcdef0123456789abcdef01234567')
        self.assertNotEqual(wrong, gate.VERUS_SOURCE)
        replace(metadata, package('vstd', wrong))
        self.assertRejected(metadata, 'vstd must come from')

    def test_other_git_dependency_is_rejected(self):
        metadata = fixture_metadata()
        add(metadata, package('shadow', f'git+https://example.com/shadow?rev={REV}#{REV}'),
            'lion-executor')
        self.assertRejected(metadata, 'git dependency outside the allowlist')

    def test_proc_macro_outside_verus_is_rejected(self):
        metadata = fixture_metadata()
        add(metadata, package('serde_derive', REGISTRY, kinds=('proc-macro',)), 'lion-reactor')
        self.assertRejected(metadata, 'proc-macro outside the allowlist')

    def test_proc_macro_closure_outside_registry_is_rejected(self):
        metadata = fixture_metadata()
        add(metadata, package('host-shadow', None, Path('/repo/host/Cargo.toml')), 'verus_syn')
        self.assertRejected(metadata, 'proc-macro closure may use only')

    def test_lion_crate_as_workspace_member_is_rejected(self):
        metadata = fixture_metadata()
        metadata['workspace_members'].append('lion-reactor-id')
        self.assertRejected(metadata, 'only the root package')

    def test_build_dependency_on_linked_crate_is_rejected(self):
        metadata = fixture_metadata()
        add(metadata, package('cc', REGISTRY), 'lion-slab', kind='build')
        self.assertRejected(metadata, 'lion-slab gained build-dependencies')
        metadata = fixture_metadata()
        add(metadata, package('cc', REGISTRY), 'srpc', kind='build')
        self.assertRejected(metadata, 'srpc takes no build-dependencies')

    def test_dev_dependencies_are_not_part_of_the_linked_graph(self):
        metadata = fixture_metadata()
        add(metadata, package('tokio', REGISTRY), 'srpc', kind='dev')
        gate.validate_graph(metadata, REV)

    def test_normal_tree_rejects_forbidden_crates(self):
        gate.check_normal_tree('srpc v0.0.0 (/repo)\nvstd v0.0.0 (git)\n')
        for name in gate.FORBIDDEN:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, name):
                    gate.check_normal_tree(f'srpc v0.0.0 (/repo)\n{name} v1.0.0\n')


if __name__ == '__main__':
    unittest.main()
