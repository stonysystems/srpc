#!/usr/bin/env python3
# Gate: a rustc-only facade stub must never shadow a canonical Rust implementation.
#
# `rusty-rustc/src/lib.rs` (Cargo package `rusty`, spelled `cpp` in sources) is a
# rustc-only facade the transpiler omits from generated C++ by package identity.
# It exists to model symbols that genuinely live in *foreign* C++ modules --
# `std::` primitives and the `srpc_*` ADL intrinsics.
#
# It was also, historically, a mock runtime: back when `reactor`, `logging` and
# friends were still hand-written C++, the facade carried test doubles for them so
# `tests/*_rust.rs` could run. Those modules have since been promoted to canonical
# Rust, but the doubles were never retired -- and nothing noticed, because both
# spellings (`cpp_reactor::f` and `crate::reactor::f`) emit the *same* C++, and
# rustc type-checks a mock exactly as happily as the real thing. The stubs then
# drifted from the functions they shadow with no gate to catch it:
# `fiber_create_run_impl` took `<F: FnMut()>, *const i8` in the facade against
# `FiberFn, &'static str` in `reactor/reactor.rs`, and `log_line`'s body was `{}`,
# silently discarding every log line in the Rust lane.
#
# This gate makes that class of rot impossible: if a facade item has a canonical
# counterpart of the same name, the call sites must use `crate::<module>::<item>`
# and the stub must go. Deliberate exceptions live in ALLOWED_SHADOWS with a reason.

import os
import re
import sys
from pathlib import Path

# `SRPC_REPO` lets the gate be exercised from outside the tree; scripts/ is the norm.
REPO = Path(os.environ.get("SRPC_REPO", Path(__file__).resolve().parent.parent))
FACADE = REPO / "rusty-rustc" / "src" / "lib.rs"
CANONICAL_DIRS = ("base", "misc", "rpc", "reactor")

# Facade items that legitimately share a name with a canonical item.
# Key is "<module>::<item>"; value is why the stub must stay.
#
# Every entry below was established by running the pinned transpiler over a
# patched tree, not by inspection: each names the failure the rewire produces.
# The Rust lane hides all of them -- `cargo test` and `cargo clippy -D warnings`
# stay green on trees whose C++ output does not build -- so do not drop an entry
# because rustc accepts the change.
ALLOWED_SHADOWS = {
    "debugging::verify": (
        "Canonical `verify<Expr>(expr, location) where Expr: Copy + Into<bool>` is "
        "generic. The transpiler's flat-import contract requires a route-(a) "
        "`use crate::<module>::<leaf>` target to be a non-generic free function with no "
        "where-clause, so the rewire dies as `cpp_import_namespace leaf "
        "`crate::debugging::verify` must be an unconditional, ordinary, non-generic free "
        "function`, emitting no .cppm at all. The second parameter is also a "
        "`cpp_default_argument(source_location)` slot that exists so *C++* fills in the "
        "caller's location; a Rust caller filling it explicitly emits "
        "`rusty::SourceLocation::current()`, and no such C++ type exists."
    ),
    "rand::RandomGenerator": (
        "`misc/rand.rs` carries `cpp_abi` markers, making it an adapted sibling "
        "declaration. A `crate::rand::RandomGenerator::rand` reference fails the crate "
        "preflight -- `cpp_abi crate preflight found a sibling-file reference` -- and the "
        "whole-crate transpile aborts before emitting any module."
    ),
    "reactor::Fiber": (
        "`use crate::reactor::Fiber;` makes `Fiber` a flat-sibling leaf name crate-wide, "
        "which then rejects the PRE-EXISTING, unmodified `pub type Fiber = "
        "cpp::ReactorFiber;` in rpc/client.rs: `cpp_import_namespace crate preflight "
        "rejects a namespace-emitted type alias whose name collides with a flat sibling "
        "leaf`. Adding the import anywhere is enough to break an unrelated file."
    ),
    "reactor::PollThread": (
        "Retargeting the alias to `crate::reactor::PollThread` stops the emitter writing "
        "direct method calls; it emits `__rusty_alias_PollThread_*` free functions "
        "instead, and the alias table is crate-global, so unrelated receivers are "
        "rewritten too -- an ordinary `pending_fu_` `HashMap::remove(xid)` in "
        "rpc/client.rs becomes `__rusty_alias_PollThread_remove(guard, xid)`, whose "
        "declaration takes `void*`. Silent miscompilation, not a build error."
    ),
    "reactor::IntEvent": (
        "Coupled to `create_sp_int_event`: the value is stored as "
        "`Arc<rusty::ReactorIntEvent>` in `FiberChannel::pending_recv_event_`, so `set` "
        "and `wait` can only move with the factory. Canonical `IntEvent::wait` forwards "
        "to `event_wait_impl`, which asserts a live per-thread reactor AND a current "
        "fiber -- neither of which the Rust lane has."
    ),
    "reactor::create_sp_int_event": (
        "Canonical returns `Arc<crate::reactor::IntEvent>` against the facade's "
        "`Arc<rusty::ReactorIntEvent>`, forcing the `IntEvent` rewire above and its "
        "reactor/fiber preconditions with it. Blocking cross-thread wakeup is genuinely "
        "unavailable under rustc."
    ),
    "reactor::create_sp_box_event": (
        "Canonical routes through `Reactor::get_reactor()`. Under rustc the nine "
        "`#[cfg_attr(any(), thread_local)]` statics in reactor/reactor.rs are ordinary "
        "process-global `static mut`, so tests sharing one non-atomic `Rc<Reactor>` race: "
        "measured 7 failures / 600 runs of tests/future_rust.rs at the default thread "
        "count, 3 of them SIGABRT. Green at --test-threads=1, which is why a single run "
        "looks fine."
    ),
    "reactor::fiber_sleep": (
        "Canonical `fiber_sleep` is not a no-op: it builds a timeout event and calls "
        "`TimeoutEvent::wait` -> `event_wait_impl`, which verifies a current fiber "
        "exists. rustc has no fiber runtime, so every `this_fiber::sleep_*` helper "
        "aborts. The facade's recording stub is what `take_test_sleep_calls` observes."
    ),
    "serializable::Serialize_": (
        "misc/any_message.rs serializes `type_name_: LegacyStdString`. Canonical "
        "`Serialize_::serialize<T: Serialize>` is bounded and `String` does not implement "
        "canonical `Serialize` (C++ finds it by ADL instead), and `AnyMessage::save` "
        "takes the facade's `BinaryWriteArchive` in its public signature."
    ),
    "serializable::Deserialize_": (
        "Read-side twin of `Serialize_`: `String` does not implement canonical "
        "`Deserialize`, and `AnyMessage::load` takes the facade's `BinaryReadArchive`."
    ),
}

ITEM_RE = re.compile(
    r"^[ \t]*pub[ \t]+(?:unsafe[ \t]+)?"
    r"(fn|struct|enum|trait|type|const|static|mod)[ \t]+"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.M,
)


def block_at(text, open_brace_index):
    # Return the source between the brace at `open_brace_index` and its match.
    depth = 0
    for i in range(open_brace_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace_index + 1 : i]
    raise SystemExit(f"{FACADE}: unbalanced braces from offset {open_brace_index}")


def facade_modules(text):
    # The facade's foreign-module surface is everything under `pub mod srpc`.
    m = re.search(r"^pub[ \t]+mod[ \t]+srpc[ \t]*\{", text, re.M)
    if not m:
        raise SystemExit(f"{FACADE}: no `pub mod srpc` block found")
    body = block_at(text, m.end() - 1)
    out = {}
    for sub in re.finditer(r"^[ \t]*pub[ \t]+mod[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\{", body, re.M):
        out[sub.group(1)] = block_at(body, sub.end() - 1)
    return out


def public_items(body):
    # Only top-level items of the block, not those nested inside an inner module.
    items = {}
    depth = 0
    for line in body.splitlines():
        if depth == 0:
            m = ITEM_RE.match(line)
            if m and m.group(1) != "mod":
                items.setdefault(m.group(2), line.strip())
            elif m:
                items.setdefault(m.group(2), line.strip())
        depth += line.count("{") - line.count("}")
        depth = max(depth, 0)
    return items


def canonical_path(module):
    for d in CANONICAL_DIRS:
        p = REPO / d / f"{module}.rs"
        if p.exists():
            return p
    return None


def main():
    text = FACADE.read_text(encoding="utf-8")
    violations = []
    stale = set(ALLOWED_SHADOWS)
    checked = 0

    for module, body in sorted(facade_modules(text).items()):
        path = canonical_path(module)
        if path is None:
            continue  # no canonical module of that name; nothing can be shadowed
        canon = path.read_text(encoding="utf-8")
        canon_items = {m.group(2): m.group(0).strip() for m in ITEM_RE.finditer(canon)}
        for item, sig in sorted(public_items(body).items()):
            checked += 1
            if item not in canon_items:
                continue
            key = f"{module}::{item}"
            stale.discard(key)
            if key in ALLOWED_SHADOWS:
                continue
            line = canon[: canon.index(canon_items[item])].count("\n") + 1
            violations.append(
                f"  facade `rusty::srpc::{module}::{item}` shadows canonical "
                f"{path.relative_to(REPO)}:{line}\n"
                f"      facade   : {sig}\n"
                f"      canonical: {canon_items[item]}\n"
                f"      fix: point call sites at `crate::{module}::{item}` and delete the stub,\n"
                f"           or add \"{key}\" to ALLOWED_SHADOWS with a reason."
            )

    if stale:
        violations.append(
            "  ALLOWED_SHADOWS has entries that no longer shadow anything "
            "(delete them): " + ", ".join(sorted(stale))
        )

    if violations:
        print(
            f"FAIL: rustc-only facade stubs shadow canonical Rust "
            f"({len(violations)} finding(s), {checked} facade item(s) checked)\n",
            file=sys.stderr,
        )
        print("\n".join(violations), file=sys.stderr)
        return 1

    print(f"ok: no facade stub shadows a canonical implementation ({checked} facade items checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
