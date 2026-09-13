#!/usr/bin/env python3
"""Audit every facade declaration against reviewed standard/ABI adapters.

The facade is excluded from C++ lowering. It may adapt standard-library types,
C layouts, or trait calls; SRPC algorithms belong in canonical Rust. The syn
scanner follows the module graph, including private and cfg declarations. The
inventory pins normalized AST tokens, so renaming a shadow, hiding it in a method,
or replacing an adapter body cannot bypass review. There is no automatic inventory
updater and no exemption for a loud missing-runtime panic.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

SOURCE_ROOT = Path(__file__).resolve().parent.parent
CATEGORIES = {
    "standard": "Rust standard-library value, container, callable, synchronization, or OS adapter; no SRPC policy.",
    "c-layout": "C ABI type or machine register layout shared with a native leaf; no SRPC policy.",
    "trait-dispatch": "Trait contract or forwarding call into canonical Rust implementation; no algorithm body.",
    "future": "Rust Future, Waker, and Poll representation for the C++ coroutine ABI; canonical executor owns scheduling.",
    "import": "Standard-library re-export, type alias, module declaration, or lint configuration.",
}

# These facts are guaranteed by Rust types, not stand-ins for missing behavior.
# Each declaration's exact AST hash also pins its returned boolean.
CONSTANT_FACTS = {
    "impl:Box < T > as RustyHandleIsValid < T : ? Sized >": {
        "body": "Box < T > as RustyHandleIsValid < T : ? Sized >::is_valid",
        "reason": "A Rust Box always owns a non-null allocation.",
    },
    "impl::: std :: sync :: Arc < T > as RustyHandleIsValid < T : ? Sized >": {
        "body": ":: std :: sync :: Arc < T > as RustyHandleIsValid < T : ? Sized >::is_valid",
        "reason": "A Rust Arc always owns a non-null allocation.",
    },
}


def scanner_binary() -> Path:
    """Build the standalone parser with the repository's pinned lockfile."""
    target = SOURCE_ROOT / "target" / "rust-source-audit"
    scratch = target / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(CARGO_TARGET_DIR=str(target), TMPDIR=str(scratch))
    subprocess.run(
        ["cargo", "build", "--quiet", "--locked", "--offline", "--manifest-path",
         str(SOURCE_ROOT / "scripts" / "rust_source_audit" / "Cargo.toml")],
        env=environment, check=True,
    )
    return target / "debug" / "srpc-rust-source-audit"


def parse(binary: Path, mode: str, source: Path, namespace: str = "") -> dict:
    result = subprocess.run([str(binary), mode, str(source), namespace],
                            text=True, capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"AST parser failed for {source}")
    return json.loads(result.stdout)


def declaration_key(record: dict) -> str:
    key = f"{record['kind']}:{record['path']}"
    if record.get("presence"):
        key += " [" + record["presence"] + "]"
    return key


def digest(record: dict) -> str:
    return hashlib.sha256(record["tokens"].encode()).hexdigest()


def audit(repo: Path, binary: Path) -> list[str]:
    facade_dir = repo / "rusty-rustc" / "src"
    result = parse(binary, "facade", facade_dir / "lib.rs")
    findings = [f"{row['file']}:{row['line']}: {row['message']} ({row['path']})"
                for row in result["findings"]]
    physical_files = set(result["files"])
    for source in sorted(facade_dir.rglob("*.rs")):
        if source.is_symlink():
            findings.append(f"facade source symlink is forbidden: {source}")
        if str(source.resolve()) not in physical_files:
            findings.append(f"untracked facade Rust file outside its module graph: {source}")

    inventory = json.loads((repo / "scripts" / "facade-adapters.json").read_text())
    if set(inventory) != {"schema_version", "categories", "constant_facts", "declarations"}:
        findings.append("invalid facade adapter inventory fields")
    if inventory.get("schema_version") != 1 or inventory.get("categories") != CATEGORIES:
        findings.append("invalid facade adapter inventory categories/schema")
    if inventory.get("constant_facts") != CONSTANT_FACTS:
        findings.append("stale or unreviewed constant-body exception in facade inventory")
    expected = inventory.get("declarations", {})
    if not isinstance(expected, dict):
        raise ValueError("facade inventory declarations must be an object")

    canonical_names: dict[str, list[str]] = {}
    constants = json.loads((repo / "scripts" / "canonical-constant-functions.json").read_text())
    if set(constants) != {"schema_version", "functions"} or constants["schema_version"] != 1:
        raise ValueError("invalid canonical constant-function inventory schema")
    expected_constants = constants["functions"]
    if not isinstance(expected_constants, dict):
        raise ValueError("canonical constant-function inventory must be an object")
    seen_constants: set[str] = set()
    manifest = tomllib.loads((repo / "rust-modules.toml").read_text())
    for module in manifest["module"]:
        canonical = parse(binary, "canonical", repo / module["source"], module["cpp_module"])
        for declaration in canonical["declarations"]:
            if declaration["kind"] in {"struct", "enum", "trait", "function"}:
                canonical_names.setdefault(declaration["name"], []).append(declaration["path"])
            for missing in declaration["missing_macros"]:
                findings.append(f"{module['source']}:{missing['line']}: missing canonical runtime behavior: {missing['name']}!")
            for function in declaration["constant_functions"]:
                key = f"{module['source']}:{declaration['path']}::{function['name']}"
                where = f"{module['source']}:{function['line']}"
                if key in seen_constants:
                    findings.append(f"{where}: duplicate canonical constant function {key}")
                seen_constants.add(key)
                entry = expected_constants.get(key)
                if (not isinstance(entry, dict) or set(entry) != {"sha256", "reason"}
                        or not isinstance(entry["reason"], str) or not entry["reason"].strip()):
                    findings.append(f"{where}: unreviewed constant/empty canonical runtime function {key}")
                elif entry["sha256"] != digest(function):
                    findings.append(f"{where}: changed canonical constant function {key}; review actual behavior")
    for missing in sorted(set(expected_constants) - seen_constants):
        findings.append(f"stale canonical constant-function inventory entry {missing}")

    seen: set[str] = set()
    seen_facts: set[str] = set()
    for record in result["declarations"]:
        key = declaration_key(record)
        where = f"{Path(record['file']).relative_to(repo.resolve())}:{record['line']}"
        if key in seen:
            findings.append(f"{where}: duplicate facade declaration {key}")
        seen.add(key)
        if "srpc" in record["path"].split("::"):
            findings.append(f"{where}: forbidden SRPC implementation namespace {key}")
        constant_bodies = record["constant_bodies"]
        fact = CONSTANT_FACTS.get(key)
        if fact and constant_bodies == [fact["body"]]:
            seen_facts.add(key)
        elif constant_bodies:
            findings.append(f"{where}: missing/constant runtime body in {key}: {constant_bodies}")
        entry = expected.get(key)
        if entry is None:
            collision = canonical_names.get(record["name"], [])
            suffix = f"; canonical owners: {', '.join(collision)}" if collision else ""
            findings.append(f"{where}: unreviewed facade declaration {key}{suffix}")
            continue
        if (not isinstance(entry, dict) or set(entry) != {"category", "sha256"}
                or entry["category"] not in CATEGORIES):
            findings.append(f"{where}: invalid adapter inventory entry {key}")
        elif entry["sha256"] != digest(record):
            findings.append(f"{where}: changed facade adapter body/declaration {key}; review canonical ownership")
    for missing in sorted(set(expected) - seen):
        findings.append(f"stale facade adapter inventory entry {missing}")
    for missing in sorted(set(CONSTANT_FACTS) - seen_facts):
        findings.append(f"stale constant-body exception {missing}")
    return findings


def main() -> int:
    repo = Path(os.environ.get("SRPC_REPO", SOURCE_ROOT)).resolve()
    try:
        findings = audit(repo, scanner_binary())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"facade AST audit FAILED: {error}", file=sys.stderr)
        return 1
    if findings:
        print("facade AST audit FAILED:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print("facade AST audit passed: reviewed standard/C ABI adapters and canonical constant functions; zero SRPC runtime shadows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
