#!/usr/bin/env bash
#
# Re-verify the pinned Lion crates by running Lion's own CI (`./ci.sh`).
#
# SRPC's claim about Lion is identity with Lion@pin (docs/dev/lion-runtime-plan.md,
# D2): the Rust lane compiles, unmodified, the Lion commit that Cargo.toml's git
# dependencies pin by `rev` and Cargo.lock resolves, and Lion's CI is the proof of
# record. This lane re-runs that CI on exactly those bytes. It is optional and
# non-gating, like scripts/verify_srpc.sh: nothing in CMake, ctest or the source
# gate calls it.
#
# It verifies the commit Cargo.lock records for every Lion package (they must all
# agree), not any working tree: the commit is fetched into a bare clone of Lion's
# repository at $VERIFY_LION_DIR/lion.git (from Cargo's own git database when
# Cargo has fetched Lion before, otherwise from the URL; scripts/lion_source.sh)
# and extracted with `git archive` into $VERIFY_LION_DIR/<sha>/, so neither a
# modified Cargo checkout nor anything else on disk can change what is verified.
# Lion's ci.sh then writes its build output next to that copy. VERIFY_LION_DIR
# defaults to ${XDG_CACHE_HOME:-~/.cache}/srpc/verify-lion and must lie outside
# this checkout: Cargo looks upward for a workspace, and a Lion crate found under
# SRPC's root (target/ included) is rejected as a package that "believes it's in a
# workspace when it's not". The extraction is kept, so a second run reuses Lion's
# build output.
#
# Requirements (Lion's REQUIREMENTS.md pins both; Verus refuses other versions):
#   - a Verus 0.2025.11.15.db81a74 dist (bundles Z3 4.12.5; the directory that
#     holds `cargo-verus`, `verus` and `version.txt`), given as VERUS_PATH
#     (Lion's name) or VERUS_HOME (verify_srpc.sh's name);
#   - the Rust 1.91.0 toolchain installed through rustup (Verus runs it itself;
#     SRPC's own toolchain is unaffected);
#   - the pinned commit in Cargo's git database, or network access to fetch it;
#   - Cargo able to resolve Lion's full dependency set: ci.sh also verifies
#     lion-utility and lion-liveness, which pull mio, tokio, socket2 and more
#     from crates.io (SRPC's own graph excludes them). Run once with network
#     access, or with a warm Cargo registry.
#
# A cold run takes about nine minutes.
#
# `--dry-run` resolves the commit and its bytes and stops: it prints the commit,
# its tree and the archive's file count, and needs no Verus, toolchain or
# extraction.
#
# Usage:  VERUS_PATH=/path/to/verus-x86-linux [VERIFY_LION_DIR=/scratch] scripts/verify_lion.sh
#         [VERIFY_LION_DIR=/scratch] scripts/verify_lion.sh --dry-run
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lion_source.sh
. "$HERE/scripts/lion_source.sh"
EXPECTED_VERUS_VERSION="0.2025.11.15.db81a74"
EXPECTED_RUST_TOOLCHAIN="1.91.0"

dry_run=0
case "${1:-}" in
    "") ;;
    --dry-run) dry_run=1 ;;
    *)
        echo "usage: $0 [--dry-run]" >&2
        exit 2
        ;;
esac

# The Verus dist and toolchain Lion@pin needs (a dry run needs neither).
check_verus() {
    verus_path="${VERUS_PATH:-${VERUS_HOME:-}}"
    if [ -z "$verus_path" ]; then
        echo "verify_lion: set VERUS_PATH (or VERUS_HOME) to a Verus $EXPECTED_VERUS_VERSION dist" >&2
        exit 2
    fi
    verus_path="$(cd "$verus_path" && pwd)"
    if [ ! -x "$verus_path/cargo-verus" ]; then
        echo "verify_lion: no cargo-verus in $verus_path" >&2
        exit 2
    fi
    verus_version="$(tr -d '[:space:]' < "$verus_path/version.txt" 2>/dev/null || true)"
    if [ "$verus_version" != "$EXPECTED_VERUS_VERSION" ]; then
        echo "verify_lion: Verus at $verus_path is '${verus_version:-unknown}'," \
             "Lion@pin needs $EXPECTED_VERUS_VERSION" >&2
        exit 2
    fi
    if ! rustup toolchain list | grep -q "^${EXPECTED_RUST_TOOLCHAIN}-"; then
        echo "verify_lion: Verus $EXPECTED_VERUS_VERSION needs the Rust $EXPECTED_RUST_TOOLCHAIN" \
             "toolchain (rustup toolchain install $EXPECTED_RUST_TOOLCHAIN)" >&2
        exit 2
    fi
}
if [ "$dry_run" = 0 ]; then
    check_verus
fi

# The commit SRPC pins: the one Cargo.lock resolves every Lion package to.
if ! sha="$(lion_locked_commit "$HERE")"; then
    echo "verify_lion: cannot take the Lion commit from $HERE/Cargo.lock" >&2
    exit 2
fi

work="${VERIFY_LION_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/srpc/verify-lion}"
mkdir -p "$work"
work="$(cd "$work" && pwd)"
case "$work/" in
    "$HERE/"*)
        echo "verify_lion: VERIFY_LION_DIR ($work) must lie outside $HERE" >&2
        exit 2
        ;;
esac
cache="$work/lion.git"
if ! lion_fetch "$cache" "$sha"; then
    echo "verify_lion: Lion commit $sha is neither in Cargo's git database nor" \
         "fetchable from $LION_GIT_URL" >&2
    exit 2
fi
if [ "$dry_run" = 1 ]; then
    files="$(git -C "$cache" archive --format=tar "$sha" | tar -t | grep -vc '/$')"
    echo "verify_lion: dry run: Lion $sha (tree $(git -C "$cache" rev-parse "$sha^{tree}")," \
         "$files files) from $cache; would extract to $work/$sha"
    exit 0
fi
tree="$work/$sha"
if [ ! -f "$tree/.extracted" ]; then
    rm -rf "$tree" "$tree.partial"
    mkdir -p "$tree.partial"
    git -C "$cache" archive --format=tar "$sha" | tar -x -C "$tree.partial"
    touch "$tree.partial/.extracted"
    mv "$tree.partial" "$tree"
fi
printf 'VERUS_PATH=%s\n' "$verus_path" > "$tree/verus.config"

echo "verify_lion: Lion $sha, Verus $verus_version, in $tree"
start=$(date +%s)
status=0
(cd "$tree" && ./ci.sh) || status=$?
echo "verify_lion: ci.sh exited $status after $(( $(date +%s) - start ))s"
exit "$status"
