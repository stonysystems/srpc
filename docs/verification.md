# Formal verification of srpc with Verus

srpc's canonical sources are Rust (rustc compiles them; rusty-cpp translates the
same bytes into the production C++). Because the source of truth is Rust, we can
run [Verus](https://github.com/verus-lang/verus) — an SMT-backed verifier for
Rust — directly on the real modules and machine-check functional contracts.

This document is the standing reference for that work: how the harness is wired,
how to run it, what is proven today, and how to prove the next thing.

## TL;DR

```sh
# with a Verus dist (ships both `cargo-verus` and `verus`) on PATH, or:
VERUS_HOME=/path/to/verus-x86-linux scripts/verify_srpc.sh
# => vstd  2043 verified, 0 errors
#    srpc  11 verified, 0 errors
```

## The route: `cargo verus verify` over an excluded `verify/` harness

An earlier attempt concluded `cargo verus verify` was "unusable" with the
prebuilt dist. **That was wrong about the cause.** cargo-verus does *not* build
the dist's `builtin_macros/` (the crate with unresolved `{ workspace = true }`
deps). It sets `RUSTC_WRAPPER=verus` plus a set of `__VERUS_DRIVER_*` env vars
and runs a plain `cargo check`, resolving `vstd`, `verus_builtin`, `verus_syn`,
`verus_prettyplease`, `verus_state_machines_macros` as ordinary **crates.io
registry** crates (their versions match the dist's `version.txt`). The only real
blocker was that srpc was not shaped as a Verus package.

The `verify/` package supplies that shape without perturbing production:

- **Workspace-excluded** (`exclude = ["verify"]` in the root `Cargo.toml`). It
  never enters the production `cargo build` or the rusty-cpp transpile, and the
  srpc crate itself keeps **no `vstd` dependency**. `verify/` has an empty
  `[workspace]` table so it is its own single-package root.
- **Verifies the real sources in place.** `verify/src/main.rs` pulls each
  specced module with `#[path = "../../<module>.rs"] mod ...;` — the same bytes
  rustc compiles and rusty-cpp translates, not an extracted copy.
- **Gate discipline that keeps production byte-identical.** Specs live behind
  `#[cfg(verus)]` / `#[cfg_attr(verus, ...)]`. `verus` is the *same* cfg the
  rusty-cpp transpiler strips, so production never sees the annotations or the
  `use vstd::prelude::*;`. cargo-verus only turns on `verus_only`, so
  `verify/.cargo/config.toml` forces `--cfg verus` **locally** to activate them:

  ```toml
  [build]
  rustflags = ["--cfg", "verus"]
  ```

  The extra cfg is inert for the vstd / verus_* dependency crates (built with
  `--cap-lints allow`).

> ⚠️ **Do not rename the in-file gate to `verus_only`.** The pinned transpiler
> (`third-party/rusty-cpp`) special-cases only the ident `verus`; a
> `#[cfg(verus_only)]` in a canonical module breaks the whole-crate transpile
> (`--crate Cargo.toml` → build failure). Keep `verus` in the module; force it
> in `verify/`. This is also why the harness needs no transpiler change and no
> gitlink pin bump.

### Why not the standalone driver?

The dist also ships prebuilt `libvstd.rlib` / `libverus_builtin.rlib`, and the
bare `verus <file> --cfg verus` driver links them with no cargo at all. That was
the first unblock, and it still works as a fallback, but only for **dependency-
free** leaf files. The cargo-verus route resolves real dependencies and is the
supported path, so it is what `scripts/verify_srpc.sh` uses.

## What is proven today

Two modules carry in-place, machine-checked contracts. `scripts/verify_srpc.sh`
reports `11 verified, 0 errors` for the srpc crate (plus vstd's own `2043
verified`, which are the standard library's proofs, not srpc's).

### `misc/stat.rs` — `AvgStat::sample` first-sample seeding

> For a fresh stat (`n_stat_ == 0 && sum_ == 0`) and any `s` with
> `i64::MIN < s < i64::MAX`, after `sample(s)`: `min_ == s && max_ == s`.

```rust
#[cfg_attr(verus, verus_spec(
    requires old(self).n_stat_ == 0, old(self).sum_ == 0,
             s > i64::MIN, s < i64::MAX,
    ensures  final(self).min_ == s, final(self).max_ == s,
))]
pub fn sample(&mut self, s: i64) { ... }
```

The other `AvgStat` methods are `#[verus_verify(external_body)]`: their
signatures are trusted, their bodies not checked. **Honest scope:** this pins the
*first-sample seeding* invariant — exactly the bug that shipped — **not** the
general "min_/max_ are the true extrema over an arbitrary stream," which needs a
loop/recursive spec and is not done.

### `rpc/internal_protocol.rs` — header-codec round trip

The wire header packs a 31-bit payload size and a 1-bit extended-header flag into
one `i32`. Both directions are proven, with Verus bit-vector reasoning:

1. **Decoded size is always non-negative and in range** — `response_payload_size`
   returns a value in `[0, kResponseSizeMask]` for *every* input. This is exactly
   the fact `frame_codec::peek_header` relied on by comment ("payload_size is
   never negative"); it is now a checked theorem.

2. **Encode/decode is a lossless round trip on the valid domain** — for any
   `payload_size >= 0` and any `flag`:
   `response_payload_size(encode_response_size(payload_size, flag)) == payload_size`
   **and** `response_has_extended_header(encode_response_size(...)) == flag`.
   The `payload_size >= 0` precondition is real: encode masks the top bit off, so
   a negative size genuinely cannot round-trip, and the spec says so rather than
   silently truncating.

Where the proof lives is deliberate. `internal_protocol.rs` carries only
**definitional** contracts on each function (`r == <the exact wire-bit
expression>`), which need no in-body proof. The bit-vector reasoning for the two
theorems above lives in `verify/src/internal_protocol_proofs.rs`, which calls the
real functions and reasons from their contracts. It sits in the harness, not the
srpc crate, because the C++ transpiler's preflight rejects any opaque macro
(including Verus's `proof!`) inside a body it must translate — so in-body
`proof! { assert(..) by (bit_vector) }` is confined to files that are never
transpiled. Definitional contracts are the transpile-safe surface a verified
module exposes; the proofs that consume them live in verify/.

## Bugs this effort fixed

| Commit | Bug | Under proof? |
| --- | --- | --- |
| `0e51bce` | `AvgStat` seeded `max_`/`min_` from zero-init fields, so an all-positive stream left `min_` stuck at 0 and an all-negative stream left `max_` stuck at 0 — neither the true extremum. | ✅ guarded by the `sample` contract |
| `b8be721` | `frame_codec` did not bound the frame size, so a desynchronised stream (short write, mid-frame reconnect, upstream bug) was read as a garbage-length header and the connection wedged silently instead of erroring. | ⚠️ fixed by hand; the assumption it leans on (`response_payload_size` is never negative) is now proven — see `internal_protocol` above. The `peek_header` size *bound* itself is not yet proven. |
| `e376fd6` | The client did not drain its disconnect buffer on teardown. | ❌ fixed by hand |

The `0e51bce` bug is instructive: it had been pinned as *correct* in six
implementation-derived oracles (a Rust test and a C++ runtime contract both
asserted `max == 0`). Every oracle derived from the buggy code blessed the bug.
The Verus spec was the only artifact that could not be satisfied by the wrong
implementation — which is exactly why it surfaced it. The negative control
confirms the spec bites: reintroduce the seed bug and `cargo verus verify` fails
with `postcondition not satisfied`.

## Adding the next leaf

1. Put the specs in the real module, gated on `#[cfg(verus)]` /
   `#[cfg_attr(verus, verus_spec(...))]`. Mark every sibling method the verifier
   should trust rather than check with `#[cfg_attr(verus, verus_verify(external_body))]`.
   For `&mut self`, use `old(self)` in `requires` and `final(self)` in `ensures`.
2. Add one line to `verify/src/main.rs`:
   `#[path = "../../<module>.rs"] mod <name>;`
3. If the module needs `vstd` lemmas, keep `#[cfg(verus)] use vstd::prelude::*;`
   at its top — stripped in production, active under the harness.
4. `scripts/verify_srpc.sh` and confirm `N verified, 0 errors`.
5. **Always run a negative control**: perturb the body so the spec should fail,
   confirm it goes red, then revert. A green that never went red proves nothing.

Only files that compile as leaves (no `crate::` / `rusty::` items the verifier
can't model) drop straight into the harness. Deeper modules need those types
given Verus models or marked external first.

**In-body proof vs. the transpiler.** A canonical srpc module is transpiled to
C++, and the transpiler's preflight rejects any opaque macro — including Verus's
`proof!` — inside a body it must translate. So a spec that needs bit-vector or
other in-body proof steps cannot keep them in the module. The pattern (see
`internal_protocol.rs` + `verify/src/internal_protocol_proofs.rs`): give the
module function a **definitional** contract (`r == <its exact expression>`, which
proves with no in-body steps and transpiles cleanly), then prove the real theorem
in a `verify/src/*_proofs.rs` driver that calls the function and does the
`proof! { assert(..) by (bit_vector) }` there. Files under `verify/src/` are never
transpiled, so they may use any Verus proof construct. A spec whose contract
proves without in-body steps (like `stat.rs`'s `sample`) can stay entirely in the
module.

## Recommended next property

**Lift the round trip one layer up, into `rpc/frame_codec.rs`.** The
`internal_protocol` proofs are the foundation; the next step is to make the frame
header itself — the thing actually on the wire — carry the same guarantees:

1. **`frame_codec_write_header` → `frame_codec_peek_header` round trip.** A header
   written for a `payload_size` in `[0, kMaxFramePayloadSize]` peeks back as
   `Complete` with the same `payload_size` and `extended_header_flag`. This builds
   directly on the `internal_protocol` round trip (write_header calls
   `encode_response_size`, peek_header calls `response_payload_size` /
   `response_has_extended_header`).

2. **`FrameHeader::total_frame_size()` never overflows or goes negative** for a
   peeked header. `peek_header` guarantees `payload_size <= kMaxFramePayloadSize`
   and `kMaxFramePayloadSize <= i32::MAX - kFrameHeaderSize`, so the
   `saturating_add` provably never saturates and the result stays positive —
   which is what makes the `as usize` casts in `next_frame` / `consume_frame`
   safe. This is the property that turns the hand-fixed `b8be721` stream-integrity
   bound into a proven one.

Both are still leaf-friendly: `peek_header` and `write_header` operate on plain
`&[u8]` / `&mut [u8]` slices, no `FrameStreamReader` cursor or raw-pointer
`append`. Property 2 needs the `kMaxFramePayloadSize` bound stated as a fact.

Beyond that, the harder tier is `FrameStreamReader` itself (buffer/cursor
invariants, the raw-pointer `append` and `consume_frame` compaction) — valuable
but it needs Verus models for the cursor and `unsafe` pointer reasoning, so it is
a project rather than a leaf.
