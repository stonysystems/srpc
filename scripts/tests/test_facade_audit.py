#!/usr/bin/env python3
"""Negative controls for the AST facade ownership gate."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import facade_audit
import check_native_kernels


class FacadeAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = facade_audit.scanner_binary()
        cls.source = facade_audit.SOURCE_ROOT
        cls.scratch = cls.source / "target" / "rust-source-audit" / "tests"
        cls.scratch.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=self.scratch)
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        shutil.copytree(self.source / "rusty-rustc" / "src", self.repo / "rusty-rustc" / "src")
        (self.repo / "scripts").mkdir()
        shutil.copy(self.source / "scripts" / "facade-adapters.json", self.repo / "scripts")
        (self.repo / "scripts" / "canonical-constant-functions.json").write_text(
            json.dumps({"schema_version": 1, "functions": {}}))
        # The real entrypoint scans the full canonical manifest. A small canonical
        # owner here lets negative controls isolate duplicate and renamed cases.
        (self.repo / "rust-modules.toml").write_text(
            '[[module]]\ncpp_module = "srpc.reactor"\nsource = "canonical.rs"\n')
        (self.repo / "canonical.rs").write_text("pub struct Fiber; pub struct RandomGenerator;\n")
        self.lib = self.repo / "rusty-rustc" / "src" / "lib.rs"

    def audit(self):
        return facade_audit.audit(self.repo, self.binary)

    def append(self, source):
        self.lib.write_text(self.lib.read_text() + "\n" + source + "\n")

    def rejected(self, source, expected):
        self.append(source)
        self.assertIn(expected, "\n".join(self.audit()))

    def test_reviewed_baseline(self):
        self.assertEqual(self.audit(), [])

    def test_canonical_constant_and_empty_runtime_are_rejected(self):
        (self.repo / "canonical.rs").write_text(
            "pub struct Worker; impl Worker { pub fn remove_count(&self) -> i32 { 0 } }\n"
            "pub fn run_scheduler() {}\n")
        findings = "\n".join(self.audit())
        self.assertIn("unreviewed constant/empty canonical runtime function", findings)
        self.assertIn("remove_count", findings)
        self.assertIn("run_scheduler", findings)

    def test_canonical_test_helpers_are_not_production_defaults(self):
        (self.repo / "canonical.rs").write_text(
            "pub struct Worker; impl Worker { #[cfg(test)] fn helper(&self) {} }\n"
            "#[cfg(test)] mod tests { fn helper() -> i32 { 0 } }\n")
        self.assertEqual(self.audit(), [])

    def test_canonical_conditional_unimplemented_is_rejected(self):
        (self.repo / "canonical.rs").write_text(
            "pub fn run(condition: bool) { if condition { unimplemented!() } }\n")
        self.assertIn("missing canonical runtime behavior: unimplemented!", "\n".join(self.audit()))

    def test_comments_and_documentation_are_not_code(self):
        self.append('// pub fn fake() -> i32 { 0 }\n/* } { r#" } fn fake(){} "# */')
        self.lib.write_text(self.lib.read_text().replace(
            "pub struct PthreadSpinlock", '/// braces { } and fake fn names\npub struct PthreadSpinlock', 1))
        self.assertEqual(self.audit(), [])

    def test_root_canonical_shadow(self):
        self.rejected("pub struct Fiber;", "canonical owners: srpc.reactor::Fiber")

    def test_renamed_real_runtime_is_unreviewed(self):
        self.rejected("fn alternate_scheduler() { ::std::thread::yield_now(); }", "unreviewed facade declaration")

    def test_private_renamed_stub(self):
        self.rejected("fn renamed_runtime() -> i32 { 0 }", "missing/constant runtime body")

    def test_nested_private_stub(self):
        self.rejected("mod hidden { mod deeper { fn renamed() -> Option<()> { None } } }", "hidden::deeper::renamed")

    def test_srpc_namespace_is_forbidden_even_when_empty(self):
        self.rejected("mod srpc {}", "forbidden SRPC implementation namespace")

    def test_srpc_nested_namespace_is_forbidden(self):
        self.rejected("mod hidden { mod srpc {} }", "forbidden SRPC implementation namespace")

    def test_test_cfg_cannot_hide_runtime(self):
        self.rejected("#[cfg(test)] fn test_only_runtime() {}", "missing/constant runtime body")

    def test_inactive_cfg_cannot_hide_runtime(self):
        self.rejected("#[cfg(any())] fn platform_runtime() {}", "missing/constant runtime body")

    def test_loud_panic_is_missing_behavior(self):
        self.rejected('fn missing() { panic!("not implemented") }', "missing/constant runtime body")

    def test_conditional_unimplemented_is_forbidden(self):
        self.rejected('fn missing(value: bool) { if value { unimplemented!() } }', "missing runtime behavior")

    def test_opaque_macro_definition_and_invocation(self):
        self.rejected("macro_rules! runtime { () => { fn fake() {} } } runtime!();", "opaque macro")

    def test_unknown_expression_macro(self):
        self.rejected("fn runtime() { hidden_runtime!(); }", "opaque macro hidden_runtime")

    def test_opaque_attribute(self):
        self.rejected("#[hidden_runtime] fn adapter() { ::std::thread::yield_now(); }", "opaque or exported-runtime attribute")

    def test_cfg_attr_cannot_hide_attribute_macro(self):
        self.rejected("#[cfg_attr(any(), hidden_runtime)] fn adapter() {}", "opaque or exported-runtime attribute cfg_attr")

    def test_exported_native_stub(self):
        self.rejected('#[no_mangle] extern "C" fn srpc_gettimeofday_us() -> i64 { 0 }', "exported-runtime attribute")

    def test_changed_adapter_body_is_rejected(self):
        path = self.repo / "rusty-rustc" / "src" / "task.rs"
        source = path.read_text()
        needle = "panic!(\"facade Task polled after completion\")"
        self.assertIn(needle, source)
        path.write_text(source.replace(needle, "unimplemented!()", 1))
        findings = "\n".join(self.audit())
        self.assertIn("missing runtime behavior", findings)
        self.assertIn("changed facade adapter body", findings)

    def test_nested_function_inside_adapter_is_rejected(self):
        self.lib.write_text(self.lib.read_text().replace(
            "pub fn make_box", "fn nested_container() { fn fake() {} }\npub fn make_box", 1))
        self.assertIn("missing/constant runtime body", "\n".join(self.audit()))

    def test_external_module_is_scanned(self):
        self.append("mod hidden;")
        self.lib.with_name("hidden.rs").write_text("fn hidden_runtime() {}")
        self.assertIn("hidden::hidden_runtime", "\n".join(self.audit()))

    def test_orphan_source_is_rejected(self):
        self.lib.with_name("unlisted.rs").write_text("fn fake() {}")
        self.assertIn("outside its module graph", "\n".join(self.audit()))

    def test_explicit_module_path_is_rejected(self):
        self.append('#[path = "../../canonical.rs"] mod hidden;')
        with self.assertRaisesRegex(ValueError, "explicit module path"):
            self.audit()

    def test_symlink_escape_is_rejected(self):
        self.append("mod hidden;")
        self.lib.with_name("hidden.rs").symlink_to(self.repo / "canonical.rs")
        with self.assertRaisesRegex(ValueError, "escapes its source directory"):
            self.audit()

    def test_malformed_rust_fails_closed(self):
        self.append("fn broken( {")
        with self.assertRaises(ValueError):
            self.audit()

    def test_stale_inventory_is_rejected(self):
        path = self.repo / "scripts" / "facade-adapters.json"
        data = json.loads(path.read_text())
        data["declarations"]["struct:DeletedModel"] = {"category": "standard", "sha256": "0" * 64}
        path.write_text(json.dumps(data))
        self.assertIn("stale facade adapter inventory", "\n".join(self.audit()))

    def test_stale_constant_exception_is_rejected(self):
        path = self.repo / "scripts" / "facade-adapters.json"
        data = json.loads(path.read_text())
        data["constant_facts"]["SerializableBase::save"] = {"body": "save", "reason": "legacy"}
        path.write_text(json.dumps(data))
        self.assertIn("stale or unreviewed constant-body exception", "\n".join(self.audit()))


class NativeKernelAuditTests(unittest.TestCase):
    def setUp(self):
        source = facade_audit.SOURCE_ROOT
        scratch = source / "target" / "rust-source-audit" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        for path in check_native_kernels.native_paths(source):
            destination = self.repo / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, destination)
        (self.repo / "scripts").mkdir()
        for name in ("native-kernels.json", "native-kernel-sources.txt"):
            shutil.copy(source / "scripts" / name, self.repo / "scripts")
        for name in ("build.rs", "CMakeLists.txt"):
            shutil.copy(source / name, self.repo)

    def findings(self):
        return "\n".join(check_native_kernels.audit(self.repo))

    def test_reviewed_native_baseline(self):
        self.assertEqual(self.findings(), "")

    def test_new_cpp_carrier_is_rejected(self):
        (self.repo / "rpc" / "hidden.cpp").write_text("int runtime() { return 0; }\n")
        self.assertIn("handwritten C++ implementation carrier", self.findings())

    def test_new_c_carrier_is_rejected(self):
        (self.repo / "rpc" / "hidden.c").write_text("int runtime() { return 0; }\n")
        self.assertIn("unreviewed native source/header", self.findings())
        self.assertIn("manifest must list every", self.findings())

    def test_policy_added_to_kernel_is_rejected(self):
        path = self.repo / "misc" / "srpc_io.c"
        path.write_text(path.read_text() + "\nint retry_forever(void) { for (;;) {} }\n")
        self.assertIn("changed native kernel/header", self.findings())

    def test_policy_added_to_adapter_header_is_rejected(self):
        path = self.repo / "misc" / "serializable_support.hpp"
        path.write_text(path.read_text() + "\ninline int serialize_fake() { return 0; }\n")
        self.assertIn("changed native kernel/header", self.findings())

    def test_policy_added_to_root_import_headers_is_rejected(self):
        for name in ("srpc.hpp", "std_compat.hpp"):
            with self.subTest(header=name):
                path = self.repo / name
                path.write_text(path.read_text() +
                    "\nnamespace srpc { inline int hidden_policy(int value) { return value + 1; } }\n")
                self.assertIn(f"changed native kernel/header: {name};", self.findings())

    def test_new_root_carriers_are_rejected(self):
        for suffix in (".c", ".cpp", ".hpp", ".inc"):
            with self.subTest(suffix=suffix):
                path = self.repo / f"hidden{suffix}"
                path.write_text("int hidden_runtime() { return 0; }\n")
                findings = self.findings()
                self.assertIn(f"unreviewed native source/header: {path.name}", findings)
                if suffix == ".cpp":
                    self.assertIn("handwritten C++ implementation carrier", findings)
                if suffix == ".c":
                    self.assertIn("manifest must list every", findings)
                path.unlink()

    def test_missing_root_header_is_rejected(self):
        (self.repo / "srpc.hpp").unlink()
        self.assertIn("stale native kernel inventory entry: srpc.hpp", self.findings())

    def test_native_manifest_missing_source_is_rejected(self):
        path = self.repo / "scripts" / "native-kernel-sources.txt"
        path.write_text(path.read_text().replace("all misc/srpc_io.c\n", ""))
        self.assertIn("manifest differs", self.findings())

    def test_native_manifest_duplicate_source_is_rejected(self):
        path = self.repo / "scripts" / "native-kernel-sources.txt"
        path.write_text(path.read_text() + "all misc/srpc_io.c\n")
        self.assertIn("duplicate native source manifest", self.findings())

    def test_rust_native_linkage_drift_is_rejected(self):
        path = self.repo / "build.rs"
        path.write_text(path.read_text().replace('sources.push(source);', 'sources.push("misc/srpc_io.c");'))
        self.assertIn("native manifest consumer changed", self.findings())

    def test_cpp_native_linkage_drift_is_rejected(self):
        path = self.repo / "CMakeLists.txt"
        path.write_text(path.read_text().replace(
            "target_sources(srpc PRIVATE ${SRPC_NATIVE_SOURCES})",
            "target_sources(srpc PRIVATE misc/srpc_io.c)"))
        with self.assertRaisesRegex(ValueError, "production native sources"):
            self.findings()

    def test_stale_native_inventory_is_rejected(self):
        (self.repo / "misc" / "srpc_io.c").unlink()
        self.assertIn("stale native kernel inventory", self.findings())


if __name__ == "__main__":
    unittest.main()
