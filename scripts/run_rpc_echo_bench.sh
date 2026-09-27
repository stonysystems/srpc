#!/usr/bin/env bash
# Run the Rust-lane RPC echo benchmark (bench/src/bin/rpc_echo.rs) on the
# current tree, or A/B it across two commits.
#
#   scripts/run_rpc_echo_bench.sh                  # current tree
#   scripts/run_rpc_echo_bench.sh --compare A B    # two commits, same sitting
#
# Each run starts a fresh server and client (two PollThreads, loopback TCP)
# and prints one `RPC_ECHO qps=... p50_us=... p99_us=...` line: throughput with
# RPC_ECHO_WINDOW requests in flight, and the round-trip latency distribution
# with one in flight. Compare mode builds each commit from `git archive` (plus
# the Lion submodule at the commit that ref records, when it has one) with
# today's bench/ copied in, so the harness is held constant, then alternates
# RPC_ECHO_TRIALS runs of the two builds and prints the spread per build. Like
# rpcbench, read the spread, not the best run: on a shared host an effect
# smaller than the trial-to-trial range is not an effect.
#
# Builds are offline: the Cargo registry and the Verus git checkout must be
# warm (see CLAUDE.md). bench/ is workspace-excluded, so none of this touches
# the source gate.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TRIALS="${RPC_ECHO_TRIALS:-5}"

summarize() {
    # stdin: "<label> <qps> <p50> <p99>" lines; prints min/median/max per label.
    awk '
    { qps[$1] = qps[$1] " " $2; p50[$1] = p50[$1] " " $3; p99[$1] = p99[$1] " " $4; seen[$1] = 1 }
    function stats(list,   n, a, i, j, t) {
        n = split(list, a, " ")
        for (i = 1; i <= n; i++) for (j = i + 1; j <= n; j++) if (a[j] + 0 < a[i] + 0) { t = a[i]; a[i] = a[j]; a[j] = t }
        return sprintf("min %s  median %s  max %s", a[1], a[int((n + 1) / 2)], a[n])
    }
    END { for (l in seen) {
        printf "%s\n  qps      %s\n  p50_us   %s\n  p99_us   %s\n", l, stats(qps[l]), stats(p50[l]), stats(p99[l])
    } }'
}

field() {
    # field <name> <RPC_ECHO line>
    tr ' ' '\n' <<<"$2" | sed -n "s/^$1=//p"
}

if [[ $# -eq 0 ]]; then
    cargo build --release --quiet --offline --manifest-path "$REPO/bench/Cargo.toml" --bin rpc_echo
    for ((trial = 1; trial <= TRIALS; trial++)); do
        "$REPO/bench/target/release/rpc_echo" 2>/dev/null | grep '^RPC_ECHO'
    done
    exit 0
fi

if [[ "${1:-}" != "--compare" || $# -ne 3 ]]; then
    echo "usage: $0 [--compare <ref-a> <ref-b>]" >&2
    exit 2
fi

REF_A="$2"
REF_B="$3"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/srpc-rpc-echo-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT INT TERM

declare -A BIN
for ref in "$REF_A" "$REF_B"; do
    dir="$WORK/$(git -C "$REPO" rev-parse --short "$ref")"
    echo "### building $ref in $dir" >&2
    mkdir -p "$dir"
    git -C "$REPO" archive "$ref" | tar -x -C "$dir"
    if lion="$(git -C "$REPO" rev-parse --verify --quiet "$ref:third-party/lion")"; then
        mkdir -p "$dir/third-party/lion"
        git -C "$REPO/third-party/lion" archive "$lion" | tar -x -C "$dir/third-party/lion"
    fi
    # Today's harness into that tree.
    rm -rf "$dir/bench"
    mkdir -p "$dir/bench"
    cp -r "$REPO/bench/Cargo.toml" "$REPO/bench/Cargo.lock" "$REPO/bench/src" "$dir/bench/"
    cargo build --release --quiet --offline --manifest-path "$dir/bench/Cargo.toml" --bin rpc_echo >&2
    BIN[$ref]="$dir/bench/target/release/rpc_echo"
done

RESULTS="$WORK/results"
: >"$RESULTS"
for ((trial = 1; trial <= TRIALS; trial++)); do
    for ref in "$REF_A" "$REF_B"; do
        line="$("${BIN[$ref]}" 2>/dev/null | grep '^RPC_ECHO')"
        echo "trial $trial  $ref  $line"
        echo "$ref $(field qps "$line") $(field p50_us "$line") $(field p99_us "$line")" >>"$RESULTS"
    done
done
echo
summarize <"$RESULTS"
