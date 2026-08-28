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
#    srpc  1 verified, 0 errors
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

**One property**, on `AvgStat::sample` in `misc/stat.rs`:

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
signatures are trusted, their bodies not checked. The `2043 verified` figure in
a run is **vstd's own** library proofs, not srpc's.

**Honest scope.** This pins the *first-sample seeding* invariant — exactly the
bug that shipped — **not** the general "min_/max_ are the true extrema over an
arbitrary stream." The full-stream property needs a loop/recursive spec and is
not done.

## Bugs this effort fixed

| Commit | Bug | Under proof? |
| --- | --- | --- |
| `0e51bce` | `AvgStat` seeded `max_`/`min_` from zero-init fields, so an all-positive stream left `min_` stuck at 0 and an all-negative stream left `max_` stuck at 0 — neither the true extremum. | ✅ guarded by the `sample` contract |
| `b8be721` | `frame_codec` did not bound the frame size, so a desynchronised stream (short write, mid-frame reconnect, upstream bug) was read as a garbage-length header and the connection wedged silently instead of erroring. | ❌ fixed by hand; the `internal_protocol` round-trip below is the proof that would cover its core assumption |
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

## Recommended next property

**The header codec round-trip in `rpc/internal_protocol.rs`.** These three
functions are pure `i32`/`u32` bit manipulation with no dependencies, so the
module is a leaf and slots into the harness with one `#[path]` line:

```rust
pub fn response_has_extended_header(encoded_size: i32) -> bool
pub fn response_payload_size(encoded_size: i32) -> i32
pub fn encode_response_size(payload_size: i32, extended_header: bool) -> i32
```

Two contracts, in order of dependency:

1. **Decoded size is always non-negative and bounded** (the lemma
   `frame_codec_peek_header` silently relies on — its "payload_size is never
   negative" comment becomes a theorem):

   > for all `e: i32`, `0 <= response_payload_size(e) <= kResponseSizeMask`

2. **Encode/decode is a lossless bijection on the valid domain** (the property
   that actually makes the wire format trustworthy — what one peer writes, the
   other reads back unchanged):

   > for all `payload_size >= 0` and `flag: bool`,
   > `response_payload_size(encode_response_size(payload_size, flag)) == payload_size`
   > **and** `response_has_extended_header(encode_response_size(payload_size, flag)) == flag`

Both need Verus's bit-vector reasoning (`assert(...) by (bit_vector)` or
`#[verifier::bit_vector]`) because the masks are `& 0x7fffffff` and
`| 0x80000000`. The domain restriction `payload_size >= 0` is real and must be a
`requires`: `encode_response_size` masks the top bit off, so a negative
`payload_size` cannot round-trip — this is a genuine boundary the proof will
document.

Once these hold, they lift cheaply to the layer above: the
`frame_codec_write_header` → `frame_codec_peek_header` round-trip, and
`FrameHeader::total_frame_size()`'s no-overflow / stays-positive property (its
`saturating_add` cannot saturate because a peeked header is bounded by
`kMaxFramePayloadSize <= i32::MAX - kFrameHeaderSize`). That chain turns the
hand-fixed `b8be721` stream-integrity bound into a proven one.

Why this next, over alternatives:

- **It is a leaf.** Zero new harness machinery, unlike anything touching
  `FrameStreamReader` (slices, cursor, raw-pointer `append`).
- **It is protocol correctness, not just absence-of-crash.** A lossless
  round-trip is the fundamental thing you want to know about a serializer.
- **It retires an assumption already load-bearing in shipped code** — the
  `response_payload_size` non-negativity that `frame_codec` treats as given.
