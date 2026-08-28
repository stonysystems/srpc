//! Verus verification entry point for srpc.
//!
//! Each `#[path]` below points at a REAL srpc source file (no copy). This crate
//! forces `--cfg verus` (see ../.cargo/config.toml), so under `cargo verus
//! verify` the in-place `#[cfg(verus)]` specs those files carry are activated
//! and checked. Add a line here as more leaf modules gain specs.

#[allow(dead_code, unused_imports)]
#[path = "../../misc/stat.rs"]
mod stat;

fn main() {}
