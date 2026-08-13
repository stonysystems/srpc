#!/usr/bin/env python3
"""Fast structural checks for the standalone Goal-0 repository."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUSTY_CPP_PIN = "29418811b7dc530bd3fe3936fe20ebc16aeb9a16"


class StandaloneGoal0Tests(unittest.TestCase):
    def test_manifest_sources_keep_cpp_history_and_rust_discovery_shims(self) -> None:
        with (ROOT / "rust-modules.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        self.assertEqual(manifest["schema_version"], 2)
        modules = manifest["module"]
        self.assertGreater(len(modules), 0)
        self.assertEqual(
            len({entry["cpp_module"] for entry in modules}), len(modules)
        )
        for entry in modules:
            rust_name = entry["cpp_module"].removeprefix("rrr.")
            source = ROOT / entry["source"]
            shim = ROOT / "src" / f"{rust_name}.rs"
            self.assertIn(source.suffix, {".cpp", ".cc"})
            self.assertEqual(source.stem, rust_name)
            self.assertTrue(source.is_file(), source)
            self.assertTrue(shim.is_symlink(), shim)
            self.assertEqual(shim.resolve(), source.resolve())

        cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        inventory = re.search(
            r"set\(RRR_GOAL0_CANONICAL_MODULES\s+(.*?)\n\)",
            cmake,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(inventory)
        assert inventory is not None
        self.assertEqual(
            set(inventory.group(1).split()),
            {
                entry["cpp_module"].removeprefix("rrr.")
                for entry in modules
            },
        )
        for entry in modules:
            rust_name = entry["cpp_module"].removeprefix("rrr.")
            self.assertIn(
                f"set(RRR_GOAL0_SOURCE_{rust_name} "
                f"${{CMAKE_CURRENT_SOURCE_DIR}}/{entry['source']})",
                cmake,
            )

    def test_rusty_cpp_is_an_exact_gitlink_dependency(self) -> None:
        fields = subprocess.check_output(
            ["git", "ls-files", "--stage", "--", "third-party/rusty-cpp"],
            cwd=ROOT,
            text=True,
        ).split()
        self.assertGreaterEqual(len(fields), 3)
        self.assertEqual(fields[0], "160000")
        self.assertEqual(fields[1], RUSTY_CPP_PIN)
        with (ROOT / ".gitmodules").open("rb") as stream:
            text = stream.read().decode()
        self.assertIn("https://github.com/shuaimu/rusty-cpp.git", text)

    def test_cmake_and_goal0_scripts_are_repository_relative(self) -> None:
        cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn("project(srpc LANGUAGES C CXX ASM)", cmake)
        self.assertIn("SRPC_RUSTY_CPP_DIR", cmake)
        self.assertIn("--dependency-module-root", cmake)
        self.assertIn("--configured-module-map-root", cmake)
        self.assertIn('"${CMAKE_BINARY_DIR}/CMakeFiles"', cmake)
        self.assertNotIn("${CMAKE_SOURCE_DIR}/src/rrr", cmake)
        self.assertNotIn("${CMAKE_SOURCE_DIR}/scripts", cmake)
        self.assertNotIn("MAKO_BREW_LIBCXX_FLAGS", cmake)
        for relative in (
            "scripts/rrr_dsl_check.sh",
            "scripts/extract_rrr_rust.py",
            "scripts/check_rrr_crate_mode.py",
            "scripts/update_file_fingerprint.cmake",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_no_absolute_mako_checkout_dependency(self) -> None:
        candidates = [ROOT / "CMakeLists.txt", *sorted((ROOT / "scripts").rglob("*"))]
        for candidate in candidates:
            if not candidate.is_file() or "__pycache__" in candidate.parts:
                continue
            data = candidate.read_bytes()
            self.assertNotIn(b"/home/users/" + b"shuai/mako", data, candidate)
            self.assertNotIn(b"/var/tmp/" + b"mako-", data, candidate)


if __name__ == "__main__":
    unittest.main()
