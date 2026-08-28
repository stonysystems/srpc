#!/usr/bin/env bash
#
# Verify srpc's in-place Verus specs.
#
# Leaf modules carry `#[cfg(verus)]`-gated Verus annotations (see misc/stat.rs).
# They are invisible to `cargo build`/`cargo test` and to the rusty-cpp C++
# transpile, and are activated only here, by running the standalone Verus
# driver with `--cfg verus`. This is the "driver over the module" route: the
# prebuilt Verus dist ships libvstd.rlib / libverus_builtin.rlib that the
# driver links, so no cargo dependency on Verus is needed (and `cargo verus
# verify` -- which would rebuild the macro crates from source -- is not used).
#
# Usage:  VERUS=/path/to/verus  scripts/verify_srpc.sh
#
# Each verified module must be dep-free (a leaf): the driver compiles the one
# file as a standalone crate. `use crate::...` modules need the whole-crate
# driver invocation and are out of scope for this script.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERUS="${VERUS:-verus}"

if ! command -v "$VERUS" >/dev/null 2>&1 && [ ! -x "$VERUS" ]; then
    echo "verify_srpc: Verus driver not found (set VERUS=/path/to/verus)" >&2
    exit 2
fi

# Leaf modules with in-place specs. Extend as more leaves are annotated.
LEAVES=(
    "misc/stat.rs"
)

rc=0
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for rel in "${LEAVES[@]}"; do
    src="$HERE/$rel"
    # The driver needs the module as a crate root. Prepend nothing; the gated
    # `use vstd::prelude::*;` inside the file supplies the prelude under
    # --cfg verus. A trailing `fn main(){}` lets it compile as an executable
    # crate without requiring --crate-type.
    unit="$tmp/$(echo "$rel" | tr '/' '_')"
    { cat "$src"; printf '\n#[cfg(verus)]\nfn main() {}\n'; } > "$unit"
    echo "== verifying $rel =="
    if "$VERUS" --cfg verus "$unit"; then
        echo "   OK: $rel"
    else
        echo "   FAIL: $rel" >&2
        rc=1
    fi
done

exit $rc
