#!/usr/bin/env python3
"""Gate: no facade function may SILENTLY do nothing.

The facade (`rusty-rustc/`) is omitted from generated C++ by package identity,
so it is allowed to stand in for the C++ runtime. What it must not do is stand
in *quietly*: a body of `{}` or `0` still ANSWERS, so a canonical caller that
reaches it gets a plausible wrong result and every Rust-lane test over that path
proves nothing. Two such bodies have already cost real bugs --
`RandomGenerator::rand` returning `min` (ClientPool always selected index 0) and
an inert `Serialize_` pair that serialized nothing while the loud stub's own
error message recommended them.

The rule this enforces: a facade function that cannot do the real thing must say
so loudly (`unimplemented!` / `panic!`), not return a plausible value. Bodies
that are silent on purpose live in ALLOWED_STUBS with a reason.

This is the companion to check_facade_shadow.py: that gate asks "does a facade
item shadow a canonical one", this one asks "does a facade item lie".
"""

import os
import re
import sys
from pathlib import Path

REPO = Path(os.environ.get("SRPC_REPO", Path(__file__).resolve().parent.parent))
FACADE_SRC = REPO / "rusty-rustc" / "src"

# Silent bodies that are correct, keyed "<ImplType>::<fn>" ("::<fn>" when free).
# A value here is a claim that the constant IS the truth, not a placeholder.
ALLOWED_STUBS = {
    "Box::is_valid": (
        "`rusty::Box`/`Arc` model C++ runtime handles whose C++ `is_valid()` "
        "checks for null. A Rust `Box`/`Arc` cannot be null -- the type system "
        "guarantees it -- so `true` is the correct answer here, not a stand-in. "
        "Canonical sources still spell the predicate so the generated C++ keeps "
        "checking handles that reach it from C++ callers."
    ),
    "Arc::is_valid": (
        "See Box::is_valid: a Rust `Arc` is never null, so `true` is the fact."
    ),
    "Box::is_empty": (
        "Models `rusty::Function::is_empty()`, which in C++ asks whether the "
        "callable slot was ever assigned. A Rust `Box<dyn Fn>` cannot be empty "
        "-- there is no unassigned state -- so `false` is the fact."
    ),
    "SerializableBase::save": (
        "Models the C++ `SerializableBase` base-class hook, whose own body is "
        "empty: derived types override it, and the base contributes no bytes. "
        "The real per-type behaviour is in the Serialize/Deserialize impls. "
        "SerializableBase is wired as a foreign symbol in cpp-module-index.toml, "
        "so the C++ lane uses the real class; this models only the base."
    ),
    "SerializableBase::load": ("Read-side twin of SerializableBase::save."),
    "SerializableBase::kind": (
        "The C++ base returns a sentinel kind that derived payloads override; 0 "
        "is that sentinel, not a placeholder."
    ),
}

# A body matching one of these IS a loud refusal, which is always acceptable.
LOUD = re.compile(r"\b(unimplemented!|todo!|panic!|unreachable!)\s*\(")

# A body that is exactly one of these is a constant answer -- the function
# cannot be doing anything, whatever its name promises.
#
# `Self::default()` is deliberately NOT here. It is a CALL that delegates to a
# real `impl Default`, not a constant, and `fn new() -> Self { Self::default() }`
# is ordinary Rust. If that Default impl is itself a lie, it is a `fn default()`
# with a body and gets scanned on its own.
CONST_BODY = re.compile(r"^(?:true|false|-?\d+(?:\.\d+)?|None|\"\")$")

FN = re.compile(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>]*>)?\s*\(", re.M)
IMPL = re.compile(r"^\s*impl\b[^{]*?(?:for\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>]*>)?\s*\{", re.M)


def block_at(text: str, open_idx: int):
    """(body, end_index) for the brace block at/after open_idx."""
    i = text.find("{", open_idx)
    if i < 0:
        return None, open_idx
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j], j
    return None, open_idx


def test_spans(text: str):
    """Byte ranges of `#[cfg(test)] mod .. { .. }` -- test code is not surface."""
    spans = []
    for m in re.finditer(r"#\[cfg\(test\)\]", text):
        _, end = block_at(text, m.end())
        if end > m.start():
            spans.append((m.start(), end))
    return spans


def impl_target_at(text: str, idx: int) -> str:
    """Nearest enclosing `impl X`/`impl .. for X` before idx, else '' (free fn)."""
    target = ""
    for m in IMPL.finditer(text):
        if m.start() > idx:
            break
        _, end = block_at(text, m.end() - 1)
        if end >= idx:
            target = m.group(1)
    return target


def main() -> int:
    findings = []
    checked = 0
    for path in sorted(FACADE_SRC.rglob("*.rs")):
        text = path.read_text(encoding="utf-8")
        skip = test_spans(text)
        for m in FN.finditer(text):
            if any(lo <= m.start() <= hi for lo, hi in skip):
                continue
            close = text.find(")", m.end())
            brace = text.find("{", close)
            semi = text.find(";", close)
            # A signature ending in `;` is a TRAIT DECLARATION with no body.
            if semi != -1 and (brace == -1 or semi < brace):
                continue
            body, _ = block_at(text, close)
            if body is None:
                continue
            checked += 1
            stripped = "\n".join(
                line for line in body.splitlines()
                if line.strip() and not line.strip().startswith("//")
            ).strip()
            if LOUD.search(stripped):
                continue
            silent = (not stripped) or bool(CONST_BODY.match(stripped))
            if not silent:
                continue
            name = m.group(1)
            key = f"{impl_target_at(text, m.start())}::{name}"
            if key in ALLOWED_STUBS:
                continue
            line_no = text[: m.start()].count("\n") + 1
            findings.append((path, line_no, key, stripped or "<empty>"))

    if findings:
        print("facade stub check FAILED: silent bodies with no recorded reason\n")
        for path, line_no, key, body in findings:
            rel = path.relative_to(REPO)
            print(f"  {rel}:{line_no}  {key}")
            print(f"      body: {body[:70]}")
        print(
            "\nA facade function that cannot do the real thing must say so LOUDLY\n"
            "(unimplemented!/panic!), so a caller finds out instead of receiving a\n"
            "plausible wrong answer. If the constant IS the truth (a Rust Arc is\n"
            "never null, say), add it to ALLOWED_STUBS with the reason."
        )
        return 1

    print(
        f"ok: no facade function answers silently "
        f"({checked} bodies checked, {len(ALLOWED_STUBS)} recorded exceptions)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
