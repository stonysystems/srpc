#!/usr/bin/env python3
"""Check rusty-cpp crate output against exact generated and production ABIs."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import tempfile

import extract_rrr_rust as extraction


DEFAULT_TRANSPILER = (
    "third-party/rusty-cpp/target/release/rusty-cpp-transpiler"
)
RUSTY_CPP_SUBMODULE = "third-party/rusty-cpp"
REQUIRED_RUSTY_CPP_COMMIT = "c19ca924eccfb41d65f9e3c0596e6765917f54d4"
EXTRACTION_DRIVER = "scripts/extract_rrr_rust.py"
EXTRACTION_MANIFEST = "rust-modules.toml"
MODULE_PREAMBLE = "module-preambles.toml"
TYPE_MAP = "rust-type-map.toml"
CPP_MODULE_INDEX = "cpp-module-index.toml"
NM_LINE = re.compile(r"^[0-9A-Fa-f]+\s+([A-Za-z])\s+(.+)$")
PLACEHOLDER = re.compile(r"\b(?:TODO|UNSUPPORTED|skipped)\b", re.IGNORECASE)

# A compiler diagnostic comment is not an unimplemented user lowering.
# rusty-cpp emits this exact informational marker when it breaks a by-value
# type cycle while ordering emitted declarations; the affected types are still
# fully defined and the module still compiles. rrr.tcp_channel is the live
# example: it carries this marker for TcpListener, defines TcpListener and all
# of its methods, and builds to a complete object with the ratcheted ABI.
# Upstream rusty-cpp main emits the identical text, so this one fixed form is
# allowlisted rather than treated as an unimplemented slot. The strict
# TODO/UNSUPPORTED/skipped ratchet still applies to every other spelling,
# including hand-attention slots such as TODO(interface_traits).
BENIGN_GENERATED_DIAGNOSTIC = re.compile(
    r"^// UNSUPPORTED: unsupported by-value circular type dependency "
    r"in scope [^:\n]+: \[[^\]\n]*\](?:; cycle path: [^\n]*)?$",
    re.MULTILINE,
)
EXPECTED_TOTAL_PROVIDER_SYMBOLS = 1254

# These maps are intentionally exhaustive. Adding a canonical manifest module
# without its dependency and generated-output ratchets is a gate error rather
# than an implicitly accepted, unreviewed provider.
#
# POLICY (repealed byte-identity): the project's acceptance criterion is that
# srpc builds, its tests pass, and the public function surface is equivalent.
# Byte-identical generated C++ is explicitly NO LONGER REQUIRED, so
# EXPECTED_GENERATED_MODULE_SHA256 is ADVISORY ONLY (see require_cpp_surfaces).
# ABI_SPECS, EXPECTED_IMPORTS and IMPORTER_USE_MARKERS remain HARD gates:
# those measure the real semantics -- symbol surface, module graph, and
# importer coverage -- which is what "equivalent public surface" means.
EXPECTED_IMPORTS = {
    "rrr.basetypes": [],
    "rrr.callback_wrapper": [],
    "rrr.internal_protocol": [],
    "rrr.stat": [],
    "rrr.errors": [],
    "rrr.connection_metrics": [],
    "rrr.completion_tracker": ["rusty"],
    "rrr.rand": ["rusty"],
    "rrr.request_options": ["rrr.rand"],
    "rrr.reconnect_policy": ["rrr.rand"],
    "rrr.circuit_breaker": [],
    "rrr.connection_state": [],
    "rrr.heartbeat": ["rrr.circuit_breaker"],
    "rrr.request_queue": ["rusty", "rrr.circuit_breaker"],
    "rrr.load_balancer": [],
    "rrr.utils": ["rrr.logging"],
    "rrr.frame_codec": ["rrr.internal_protocol"],
    "rrr.serializable": [
        "rrr.basetypes",
        "rrr.debugging",
        "rusty",
        "std",
    ],
    "rrr.serializable_envelope": [
        "rrr.basetypes",
        "rrr.debugging",
        "rrr.serializable",
    ],
    "rrr.future": ["rrr.reactor", "std"],
    "rrr.logging": ["rrr.debugging", "std"],
    "rrr.idempotency": ["rusty", "rrr.serializable"],
    "rrr.fiber": ["rusty", "rrr.basetypes", "rrr.reactor"],
    "rrr.misc": [],
    "rrr.channel": ["rrr.callback_wrapper"],
    "rrr.epoll_wrapper": ["rusty"],
    "rrr.pollable_proxy": [],
    "rrr.callbacks": ["rusty", "rrr.errors"],
    "rrr.inmemory_channel": ["rusty", "rrr.channel"],
    # Same dependency set as before; the emitter now orders the rrr.* imports
    # alphabetically, matching every other entry in this map.
    "rrr.fiber_channel": ["rusty", "rrr.channel", "rrr.reactor"],
    "rrr.threading": ["rrr.debugging"],
    "rrr.debugging": ["rusty"],
    "rrr.any_message": ["rusty", "rrr.debugging", "rrr.serializable"],
    "rrr.tcp_channel": [
        "rrr.channel",
        "rrr.frame_codec",
        "rrr.pollable_proxy",
        "rrr.reactor",
    ],
}

EXPECTED_GENERATED_MODULE_SHA256 = {
    "rrr.basetypes": "712d949cee2025b9e2441a13cdadd6ec2ebb3396a9561ec4a5aadc536b19cf7d",
    "rrr.callback_wrapper": "b645833262c8cf8fd4ea2306f50d6ddf018610fe85cb8bcb5b3b195dc0503341",
    "rrr.internal_protocol": "6d6c3107651d323ba54bbf2a40b8cbe454e7d7caff86e4b7b064e5f517d75eb4",
    "rrr.stat": "6bb3860679d151d047c65c7392d6126dc7e2d03c07589e97683cccb5383a9962",
    "rrr.errors": "89a1d07ee64721fb2a0de981028c617f0c1a14f6bdd9aec72d6ae8f88f2b16ac",
    "rrr.connection_metrics": "a1cb3a899b81d01faaacd9f4d75e2582d1017b120b499fce6b30f631db2f7c1b",
    "rrr.completion_tracker": "299a98e7155a0e31836e8f9b4dca13adaeb1ac89f03ff9d0a4fb07bc2378f74e",
    "rrr.rand": "0a62c12d6787e03503b6a0222fd530ed077c6e87eb392d4eab32b0e6c055fd27",
    "rrr.request_options": "0ab14f407358088c737bd09c8eb43c3988b5a97ccf49439254a7b484975cd7c3",
    "rrr.reconnect_policy": "a4a59e6f6b7cf38cab31a838f8a1bcd83a3a6383e588e62e011dd73eaf2b2c3e",
    "rrr.circuit_breaker": "3a8fe6f4550f8ff69f9358c58ea9751ae1ecb0cbd2fda12b9dd478d05e92ff23",
    "rrr.connection_state": "7a3b5edf774ef448575c2761c9e02e4c935eab2c95f36b23e2003e639a7b6baa",
    "rrr.heartbeat": "c076399ae3bc25c845162276e4a4ac93b25b8b9f6af05e02ffe5f3f9a1f14dfa",
    "rrr.request_queue": "1e6a70e795647ba28b75fffbac57000566f51072bf9bd3d16c76d689caf8923d",
    "rrr.load_balancer": "8e19a04224e7f760bcaf72838e69fe4e56b2329d07a1c6438a634cde2a6ad062",
    "rrr.utils": "492005cf6e7153ebb69e551eaf782eaaab3cbad925ef3ce8631977ab4409e5fd",
    "rrr.frame_codec": "84db9800b41406f78fdcc1071103650f1d950af7a17cfad2fe2059390bad03eb",
    "rrr.serializable": "8759dc392050eebfebecd4d0a7d7649ab5877a6521bebbbe6dbf9f5649496599",
    "rrr.serializable_envelope": "10e741356898a59ead60f6f3b69f4c007f18f037d897f4fcacfd009168813f52",
    "rrr.future": "f2dfa65121cb1d8d5423eeb9ab546c82502d7851370086cdd6764e10e485aabb",
    "rrr.logging": "ab48d535bc9ed3fa7bc59c7150dabf70fa1a148fe84fd6a8471a06a60bac2816",
    "rrr.idempotency": "477296e6dea8f20becf8df619176641ec52bba55aa6d3f4bde52a556813bd722",
    "rrr.fiber": "c1f62c52feffc2d2efc9f8bf73bbcad61b77b32f1f41c1b61bc79ae54bf65dbf",
    "rrr.misc": "6607b359a539723a887172124c77888169c09dba2ad0c14e860d3718c73262db",
    "rrr.channel": "62a35ac1c01f67fd45876564af7aed3fc740306fea7fab77e530d01183490988",
    "rrr.epoll_wrapper": "cfc9e8a76f01f56ff3fe1691aa8a8e771231887b5d76cd73968294701abcc36c",
    "rrr.pollable_proxy": "002b3adac68f5350e0ddbf6b8114b9b6ea7424313a3b9d664b073617a21a2bfc",
    "rrr.callbacks": "2b5121d95b6cac9594ab2e4eab9c6d8e6c6e48b005c334ad2975d7dcbce77a55",
    "rrr.inmemory_channel": "e1ed9325814c60815990035079fd4c36bfbf7356330f27c0cb78dcff6f9e19e4",
    "rrr.fiber_channel": "419ee69fe99e24e22c2fdb5da07edcfa1300e8e77b37e2928961a0b2e1250516",
    "rrr.threading": "91f4a45f99886d4a83b7242d7afa511afc96f485f3e0ecc6c52263b49671fdd7",
    "rrr.debugging": "7c346ba032661233a6ef8dec2a95e5c3e77873d96bb18549faf0279488428514",
    "rrr.any_message": "30bbb8483d830747ab4a52d48380ffca8835216010660505ab6b4cf7ace27384",
    "rrr.tcp_channel": "a00b6f7b25682b1be842e0a24828df8ac532ab0b2f867eadb90e94ed9c85a5b2",
}

IMPORTER_USE_MARKERS = {
    "rrr.basetypes": "rrr::SparseInt",
    "rrr.callback_wrapper": "rrr::detail::CallbackWrapper",
    "rrr.internal_protocol": "rrr::encode_response_size",
    "rrr.stat": "rrr::AvgStat",
    "rrr.errors": "rrr::RpcError",
    "rrr.connection_metrics": "rrr::ConnectionMetrics",
    "rrr.completion_tracker": "rrr::CompletionTracker",
    "rrr.rand": "rrr::RandomGenerator",
    "rrr.request_options": "rrr::RequestOptions",
    "rrr.reconnect_policy": "rrr::ReconnectPolicy",
    "rrr.circuit_breaker": "rrr::CircuitBreaker",
    "rrr.connection_state": "rrr::ConnectionStateMachine",
    "rrr.heartbeat": "rrr::HeartbeatManager",
    "rrr.request_queue": "rrr::RequestQueue",
    "rrr.load_balancer": "rrr::LoadBalancer",
    "rrr.utils": "rrr::AddrInfo",
    "rrr.frame_codec": "rrr::FrameStreamReader",
    "rrr.serializable_envelope": "rrr::SerializableEnvelope",
    "rrr.future": "rrr::FiberFuture",
    "rrr.logging": "rrr::log_level_tag",
    "rrr.idempotency": "rrr::IdempotencyCache",
    "rrr.fiber": "rrr::this_fiber::get_id",
    "rrr.misc": "rrr::OneTimeJob",
    "rrr.channel": "rrr::ChannelFrame",
    "rrr.epoll_wrapper": "rrr::Epoll",
    "rrr.pollable_proxy": "rrr::PollableProxy",
    "rrr.callbacks": "rrr::CallbackManager",
    "rrr.inmemory_channel": "rrr::InMemorySwitchboard",
    "rrr.fiber_channel": "rrr::FiberChannel",
    "rrr.threading": "rrr::SpinLock",
    "rrr.debugging": "rrr::likely",
    "rrr.any_message": "rrr::AnyMessage",
    "rrr.serializable": "rrr::BinaryWriteArchive",
    "rrr.tcp_channel": "rrr::kTcpConnectionOutboundHighWaterDefault",
}


@dataclass(frozen=True)
class AbiSpec:
    """Checked C++ surface and exact symbols for one canonical Rust module."""

    surface: frozenset[str]
    symbols: frozenset[tuple[str, str]]


ABI_SPECS = {
    "rrr.callback_wrapper": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.callback_wrapper;",
                "namespace rrr {",
                "namespace detail {",
                "export template<typename F>",
                "struct CallbackWrapper",
                "rusty::Option<rusty::Arc<F>> inner;",
                "static CallbackWrapper<F> from_callable(F callable) {",
                "rusty::Arc<F>::new_(std::move(callable))",
                "bool has_value() const {",
                "const F& callable() const {",
                "CallbackWrapper<F> clone() const {",
                "static CallbackWrapper<F> default_() {",
                "static constexpr bool is_send",
                "static constexpr bool is_sync",
            }
        ),
        # CallbackWrapper is an exported class template. Its concrete weak
        # instantiations belong to importers, so module provider objects must
        # not acquire an out-of-line specialization ABI.
        symbols=frozenset(),
    ),
    "rrr.internal_protocol": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.internal_protocol;",
                "namespace rrr {",
                "export constexpr int32_t kInternalHeartbeatRpcId",
                "export constexpr uint32_t kResponseHeaderExtFlag",
                "export constexpr uint32_t kResponseSizeMask",
                "export bool response_has_extended_header(int32_t encoded_size);",
                "export int32_t response_payload_size(int32_t encoded_size);",
                "export int32_t encode_response_size(int32_t payload_size, bool extended_header);",
            }
        ),
        symbols=frozenset(
            {
                ("R", "rrr::kInternalHeartbeatRpcId@rrr.internal_protocol"),
                ("R", "rrr::kResponseHeaderExtFlag@rrr.internal_protocol"),
                ("R", "rrr::kResponseSizeMask@rrr.internal_protocol"),
                (
                    "T",
                    "rrr::encode_response_size@rrr.internal_protocol(int, bool)",
                ),
                (
                    "T",
                    "rrr::response_has_extended_header@rrr.internal_protocol(int)",
                ),
                (
                    "T",
                    "rrr::response_payload_size@rrr.internal_protocol(int)",
                ),
            }
        ),
    ),
    "rrr.stat": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.stat;",
                "namespace rrr {",
                "export struct AvgStat",
                "int64_t n_stat_;",
                "int64_t sum_;",
                "int64_t avg_;",
                "int64_t max_;",
                "int64_t min_;",
                "static AvgStat new_();",
                "void sample(int64_t s);",
                "void clear();",
                "AvgStat reset();",
                "AvgStat peek() const;",
                "int64_t avg() const;",
            }
        ),
        symbols=frozenset(
            {
                ("T", "rrr::AvgStat@rrr.stat::new_()"),
                ("T", "rrr::AvgStat@rrr.stat::sample(long)"),
                ("T", "rrr::AvgStat@rrr.stat::clear()"),
                ("T", "rrr::AvgStat@rrr.stat::reset()"),
                ("T", "rrr::AvgStat@rrr.stat::peek() const"),
                ("T", "rrr::AvgStat@rrr.stat::avg() const"),
            }
        ),
    ),
    "rrr.errors": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.errors;",
                "namespace rrr {",
                "export enum class RpcErrorCategory",
                "export enum class RpcError",
                "export std::string_view rpc_error_category_to_string(RpcErrorCategory cat);",
                "export std::string_view rpc_error_to_string(RpcError err);",
                "export RpcErrorCategory get_error_category(RpcError err);",
                "export bool is_connection_error(RpcError err);",
                "export bool is_timeout_error(RpcError err);",
                "export bool is_retryable_error(RpcError err);",
            }
        ),
        symbols=frozenset(
            {
                (
                    "T",
                    "rrr::get_error_category@rrr.errors(rrr::RpcError@rrr.errors)",
                ),
                (
                    "T",
                    "rrr::is_connection_error@rrr.errors(rrr::RpcError@rrr.errors)",
                ),
                (
                    "T",
                    "rrr::is_retryable_error@rrr.errors(rrr::RpcError@rrr.errors)",
                ),
                (
                    "T",
                    "rrr::is_timeout_error@rrr.errors(rrr::RpcError@rrr.errors)",
                ),
                (
                    "T",
                    "rrr::rpc_error_category_to_string@rrr.errors(rrr::RpcErrorCategory@rrr.errors)",
                ),
                (
                    "T",
                    "rrr::rpc_error_to_string@rrr.errors(rrr::RpcError@rrr.errors)",
                ),
            }
        ),
    ),
    "rrr.connection_metrics": AbiSpec(
        surface=frozenset(
            {
                "#include <rusty/sync/atomic.hpp>",
                "export module rrr.connection_metrics;",
                "namespace rrr {",
                "export struct ConnectionMetrics",
                "using rusty::sync::atomic::AtomicU64;",
                "using rusty::sync::atomic::Ordering;",
                "rusty::sync::atomic::AtomicU64 requests_sent_field;",
                "rusty::sync::atomic::AtomicU64 requests_completed_field;",
                "rusty::sync::atomic::AtomicU64 requests_failed_field;",
                "rusty::sync::atomic::AtomicU64 requests_timed_out_field;",
                "rusty::sync::atomic::AtomicU64 in_flight_requests_field;",
                "rusty::sync::atomic::AtomicU64 bytes_sent_field;",
                "rusty::sync::atomic::AtomicU64 bytes_received_field;",
                "rusty::sync::atomic::AtomicU64 reconnect_count_field;",
                "rusty::sync::atomic::AtomicU64 retry_attempts_field;",
                "rusty::sync::atomic::AtomicU64 queue_dropped_requests_field;",
                "rusty::sync::atomic::AtomicU64 circuit_open_rejections_field;",
                "rusty::sync::atomic::AtomicU64 circuit_open_transitions_field;",
                "rusty::sync::atomic::AtomicU64 circuit_half_open_transitions_field;",
                "rusty::sync::atomic::AtomicU64 circuit_closed_transitions_field;",
                "rusty::sync::atomic::AtomicU64 connect_time_ms_field;",
                "rusty::sync::atomic::AtomicU64 total_latency_us_field;",
                "rusty::sync::atomic::AtomicU64 min_latency_us_field;",
                "rusty::sync::atomic::AtomicU64 max_latency_us_field;",
                "static ConnectionMetrics new_();",
                "uint64_t requests_sent() const;",
                "uint64_t requests_completed() const;",
                "uint64_t requests_failed() const;",
                "uint64_t requests_timed_out() const;",
                "uint64_t in_flight_requests() const;",
                "uint64_t bytes_sent() const;",
                "uint64_t bytes_received() const;",
                "uint64_t reconnect_count() const;",
                "uint64_t retry_attempts() const;",
                "uint64_t queue_dropped_requests() const;",
                "uint64_t circuit_open_rejections() const;",
                "uint64_t circuit_open_transitions() const;",
                "uint64_t circuit_half_open_transitions() const;",
                "uint64_t circuit_closed_transitions() const;",
                "uint64_t connect_time_ms() const;",
                "uint64_t min_latency_us() const;",
                "uint64_t max_latency_us() const;",
                "uint64_t success_rate_percent() const;",
                "uint64_t avg_latency_us() const;",
                "uint64_t uptime_ms(uint64_t current_time_ms) const;",
                "void record_request_sent() const;",
                "void record_request_completed_with_latency(uint64_t latency_us) const;",
                "void record_request_completed() const;",
                "void record_request_failed() const;",
                "void record_request_timeout() const;",
                "void record_request_dropped() const;",
                "void record_bytes_sent(uint64_t bytes) const;",
                "void record_bytes_received(uint64_t bytes) const;",
                "void record_reconnect() const;",
                "void record_retry_attempt() const;",
                "void record_queue_drop() const;",
                "void record_circuit_open_rejection() const;",
                "void record_circuit_open_transition() const;",
                "void record_circuit_half_open_transition() const;",
                "void record_circuit_closed_transition() const;",
                "void record_connect(uint64_t current_time_ms) const;",
                "void reset() const;",
                "void decrement_in_flight() const;",
                "static constexpr bool is_send = true;",
                "static constexpr bool is_sync = true;",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::ConnectionMetrics@rrr.connection_metrics::new_()",
                "rrr::ConnectionMetrics@rrr.connection_metrics::requests_sent() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::requests_completed() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::requests_failed() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::requests_timed_out() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::in_flight_requests() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::bytes_sent() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::bytes_received() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::reconnect_count() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::retry_attempts() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::queue_dropped_requests() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::circuit_open_rejections() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::circuit_open_transitions() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::circuit_half_open_transitions() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::circuit_closed_transitions() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::connect_time_ms() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::min_latency_us() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::max_latency_us() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::success_rate_percent() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::avg_latency_us() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::uptime_ms(unsigned long) const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_request_sent() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_request_completed_with_latency(unsigned long) const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_request_completed() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_request_failed() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_request_timeout() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_request_dropped() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_bytes_sent(unsigned long) const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_bytes_received(unsigned long) const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_reconnect() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_retry_attempt() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_queue_drop() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_circuit_open_rejection() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_circuit_open_transition() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_circuit_half_open_transition() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_circuit_closed_transition() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::record_connect(unsigned long) const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::reset() const",
                "rrr::ConnectionMetrics@rrr.connection_metrics::decrement_in_flight() const",
            }
        ),
    ),
    "rrr.completion_tracker": AbiSpec(
        surface=frozenset(
            {
                "#include <rusty/sync/atomic.hpp>",
                "export module rrr.completion_tracker;",
                "import rusty;",
                "export enum class CompletionStatus",
                "export struct CompletionTrackerConfig",
                "export struct CompletedEntry",
                "export struct CompletionTracker",
                "export struct CompletionQueryResult",
                "using rusty::HashSet;",
                "using rusty::VecDeque;",
                "using rusty::sync::atomic::AtomicU64;",
                "using rusty::sync::atomic::Ordering;",
                "using rusty::Mutex;",
                "rusty::Mutex<CompletionTrackerConfig> config_;",
                "rusty::Mutex<rusty::VecDeque<CompletedEntry>> lru_list_;",
                "rusty::Mutex<rusty::HashSet<int64_t>> completed_set_;",
                "rusty::sync::atomic::AtomicU64 total_tracked_;",
                "rusty::sync::atomic::AtomicU64 queries_;",
                "rusty::sync::atomic::AtomicU64 query_hits_;",
                "rusty::sync::atomic::AtomicU64 evictions_;",
                "CompletionTracker();",
                "CompletionTracker(CompletionTrackerConfig config);",
                "bool enabled() const;",
                "CompletionTrackerConfig config() const;",
                "void set_config(CompletionTrackerConfig config);",
                "void mark_completed(int64_t xid, uint64_t current_time_ms);",
                "bool is_completed(int64_t xid, uint64_t current_time_ms);",
                "bool remove(int64_t xid);",
                "void clear();",
                "size_t size() const;",
                "uint64_t total_tracked() const;",
                "uint64_t queries() const;",
                "uint64_t query_hits() const;",
                "double hit_rate() const;",
                "uint64_t evictions() const;",
                "void reset_stats();",
                "size_t evict_expired(uint64_t current_time_ms);",
                "CompletionStatus status;",
                "int32_t error_code;",
                "bool has_cached_response;",
                "export std::string_view completion_status_to_string(CompletionStatus status);",
                "rusty::wrapping_add(this->timestamp_ms",
                "static constexpr bool is_send = true;",
                "static constexpr bool is_sync = true;",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::CompletionTrackerConfig@rrr.completion_tracker::new_()",
                "rrr::CompletionTrackerConfig@rrr.completion_tracker::defaults()",
                "rrr::CompletionTrackerConfig@rrr.completion_tracker::small()",
                "rrr::CompletionTrackerConfig@rrr.completion_tracker::large()",
                "rrr::CompletionTrackerConfig@rrr.completion_tracker::disabled()",
                "rrr::CompletedEntry@rrr.completion_tracker::new_(long, unsigned long)",
                "rrr::CompletedEntry@rrr.completion_tracker::is_expired(unsigned long, unsigned long) const",
                "rrr::CompletionTracker@rrr.completion_tracker::CompletionTracker()",
                "rrr::CompletionTracker@rrr.completion_tracker::CompletionTracker(rrr::CompletionTrackerConfig@rrr.completion_tracker)",
                "rrr::CompletionTracker@rrr.completion_tracker::enabled() const",
                "rrr::CompletionTracker@rrr.completion_tracker::config() const",
                "rrr::CompletionTracker@rrr.completion_tracker::set_config(rrr::CompletionTrackerConfig@rrr.completion_tracker)",
                "rrr::CompletionTracker@rrr.completion_tracker::mark_completed(long, unsigned long)",
                "rrr::CompletionTracker@rrr.completion_tracker::is_completed(long, unsigned long)",
                "rrr::CompletionTracker@rrr.completion_tracker::remove(long)",
                "rrr::CompletionTracker@rrr.completion_tracker::clear()",
                "rrr::CompletionTracker@rrr.completion_tracker::size() const",
                "rrr::CompletionTracker@rrr.completion_tracker::total_tracked() const",
                "rrr::CompletionTracker@rrr.completion_tracker::queries() const",
                "rrr::CompletionTracker@rrr.completion_tracker::query_hits() const",
                "rrr::CompletionTracker@rrr.completion_tracker::hit_rate() const",
                "rrr::CompletionTracker@rrr.completion_tracker::evictions() const",
                "rrr::CompletionTracker@rrr.completion_tracker::reset_stats()",
                "rrr::CompletionTracker@rrr.completion_tracker::evict_expired(unsigned long)",
                "rrr::CompletionQueryResult@rrr.completion_tracker::new_()",
                "rrr::CompletionQueryResult@rrr.completion_tracker::not_found()",
                "rrr::CompletionQueryResult@rrr.completion_tracker::completed(int, bool)",
                "rrr::CompletionQueryResult@rrr.completion_tracker::expired()",
                "rrr::CompletionQueryResult@rrr.completion_tracker::is_completed() const",
                "rrr::completion_status_to_string@rrr.completion_tracker(rrr::CompletionStatus@rrr.completion_tracker)",
            }
        ),
    ),
    "rrr.rand": AbiSpec(
        surface=frozenset(
            {
                '#include "misc/srpc_rand.h"',
                "export module rrr.rand;",
                "import rusty;",
                "namespace rusty_cpp_abi_detail {",
                "bytes_from_std_string(const std::string& input)",
                "std_string_from_bytes(rusty::Vec<uint8_t> input)",
                "f64_span_from_std_vector(const std::vector<double>& input)",
                "export using RandWeightVec = std::vector<double>;",
                "export struct RandomGenerator",
                "export double randgen_rand_max();",
                "export std::string randgen_zero_pad(std::string s, int32_t length);",
                "export int32_t randgen_rand_raw();",
                "export int32_t randgen_nu_constant_now();",
                "export void randgen_destroy();",
                "static int32_t rand(int32_t min, int32_t max);",
                "static double rand_double(double min, double max);",
                "static std::string int2str_n(int32_t i, int32_t length);",
                "static bool percentage_true(int32_t p);",
                "static int32_t nu_rand(int32_t a, int32_t x, int32_t y);",
                "static uint32_t weighted_select(const RandWeightVec& weight_vector);",
                "static void destroy();",
                "rusty::wrapping_sub(max",
                "rusty::wrapping_add((rusty::detail::deref_if_pointer_like(r)",
                "rusty::wrapping_sub(((static_cast<uint32_t>(k)))",
                "static constexpr bool is_send = true;",
                "static constexpr bool is_sync = true;",
            }
        ),
        symbols=frozenset(
            {
                ("T", "rrr::randgen_rand_max@rrr.rand()"),
                (
                    "T",
                    "rrr::randgen_zero_pad@rrr.rand(std::__1::basic_string<char, "
                    "std::__1::char_traits<char>, std::__1::allocator<char>>, int)",
                ),
                ("T", "rrr::randgen_rand_raw@rrr.rand()"),
                ("T", "rrr::randgen_nu_constant_now@rrr.rand()"),
                ("T", "rrr::randgen_destroy@rrr.rand()"),
                ("T", "rrr::RandomGenerator@rrr.rand::rand(int, int)"),
                (
                    "T",
                    "rrr::RandomGenerator@rrr.rand::rand_double(double, double)",
                ),
                (
                    "T",
                    "rrr::RandomGenerator@rrr.rand::int2str_n(int, int)",
                ),
                (
                    "T",
                    "rrr::RandomGenerator@rrr.rand::percentage_true(int)",
                ),
                (
                    "T",
                    "rrr::RandomGenerator@rrr.rand::nu_rand(int, int, int)",
                ),
                (
                    "T",
                    "rrr::RandomGenerator@rrr.rand::weighted_select("
                    "std::__1::vector<double, std::__1::allocator<double>> const&)",
                ),
                ("T", "rrr::RandomGenerator@rrr.rand::destroy()"),
            }
        ),
    ),
    "rrr.request_options": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.request_options;",
                "import rrr.rand;",
                "export enum class TimeoutType",
                "export constexpr TimeoutType TimeoutType_NONE();",
                "export constexpr TimeoutType TimeoutType_CONNECT_TIMEOUT();",
                "export constexpr TimeoutType TimeoutType_REQUEST_TIMEOUT();",
                "export constexpr TimeoutType TimeoutType_RESPONSE_TIMEOUT();",
                "export constexpr TimeoutType TimeoutType_TOTAL_TIMEOUT();",
                "inline constexpr TimeoutType TimeoutType_NONE()",
                "inline constexpr TimeoutType TimeoutType_CONNECT_TIMEOUT()",
                "inline constexpr TimeoutType TimeoutType_REQUEST_TIMEOUT()",
                "inline constexpr TimeoutType TimeoutType_RESPONSE_TIMEOUT()",
                "inline constexpr TimeoutType TimeoutType_TOTAL_TIMEOUT()",
                "export struct RequestOptions",
                "uint64_t timeout_ms;",
                "uint64_t total_timeout_ms;",
                "uint16_t max_retries;",
                "uint16_t base_delay_ms;",
                "uint16_t max_delay_ms;",
                "float jitter_factor;",
                "bool idempotent;",
                "static RequestOptions new_();",
                "static RequestOptions defaults();",
                "static RequestOptions with_retry(uint16_t max_retries, uint64_t timeout_ms);",
                "static RequestOptions idempotent_retry(uint16_t max_retries);",
                "static RequestOptions no_timeout();",
                "static RequestOptions fast();",
                "static RequestOptions patient();",
                "bool can_retry(uint16_t current_retry_count) const;",
                "uint64_t calculate_delay_ms(uint16_t attempt) const;",
                "bool is_total_timeout_exceeded(uint64_t elapsed_ms) const;",
                "uint64_t remaining_time_ms(uint64_t elapsed_ms) const;",
                "static constexpr bool is_send = true;",
                "static constexpr bool is_sync = true;",
                "export std::string_view timeout_type_to_string(TimeoutType ty);",
                "static_cast<double>(randgen_rand_raw())",
                "randgen_rand_max()",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::RequestOptions@rrr.request_options::new_()",
                "rrr::RequestOptions@rrr.request_options::defaults()",
                "rrr::RequestOptions@rrr.request_options::with_retry(unsigned short, unsigned long)",
                "rrr::RequestOptions@rrr.request_options::idempotent_retry(unsigned short)",
                "rrr::RequestOptions@rrr.request_options::no_timeout()",
                "rrr::RequestOptions@rrr.request_options::fast()",
                "rrr::RequestOptions@rrr.request_options::patient()",
                "rrr::RequestOptions@rrr.request_options::can_retry(unsigned short) const",
                "rrr::RequestOptions@rrr.request_options::calculate_delay_ms(unsigned short) const",
                "rrr::RequestOptions@rrr.request_options::is_total_timeout_exceeded(unsigned long) const",
                "rrr::RequestOptions@rrr.request_options::remaining_time_ms(unsigned long) const",
                "rrr::timeout_type_to_string@rrr.request_options(rrr::TimeoutType@rrr.request_options)",
            }
        ),
    ),
    "rrr.reconnect_policy": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.reconnect_policy;",
                "import rrr.rand;",
                "export struct ReconnectPolicy",
                "bool auto_reconnect;",
                "uint32_t max_retries;",
                "uint32_t initial_delay_ms;",
                "uint32_t max_delay_ms;",
                "double backoff_multiplier;",
                "bool jitter_enabled;",
                "static ReconnectPolicy new_();",
                "static ReconnectPolicy aggressive();",
                "static ReconnectPolicy conservative();",
                "static ReconnectPolicy no_retry();",
                "static constexpr bool is_send = true;",
                "static constexpr bool is_sync = true;",
                "export struct ReconnectCalculator",
                "const ReconnectPolicy& policy;",
                "rusty::Cell<uint32_t> retries;",
                "static ReconnectCalculator new_(const ReconnectPolicy& policy);",
                "bool should_retry() const;",
                "uint32_t next_delay_ms() const;",
                "uint32_t peek_delay_ms() const;",
                "void reset() const;",
                "uint32_t retry_count() const;",
                "bool retries_exhausted() const;",
                "rusty::wrapping_add(count",
                "static_cast<double>(randgen_rand_raw())",
                "randgen_rand_max()",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::ReconnectPolicy@rrr.reconnect_policy::new_()",
                "rrr::ReconnectPolicy@rrr.reconnect_policy::aggressive()",
                "rrr::ReconnectPolicy@rrr.reconnect_policy::conservative()",
                "rrr::ReconnectPolicy@rrr.reconnect_policy::no_retry()",
                "rrr::ReconnectCalculator@rrr.reconnect_policy::new_(rrr::ReconnectPolicy@rrr.reconnect_policy const&)",
                "rrr::ReconnectCalculator@rrr.reconnect_policy::should_retry() const",
                "rrr::ReconnectCalculator@rrr.reconnect_policy::next_delay_ms() const",
                "rrr::ReconnectCalculator@rrr.reconnect_policy::peek_delay_ms() const",
                "rrr::ReconnectCalculator@rrr.reconnect_policy::reset() const",
                "rrr::ReconnectCalculator@rrr.reconnect_policy::retry_count() const",
                "rrr::ReconnectCalculator@rrr.reconnect_policy::retries_exhausted() const",
            }
        ),
    ),
    "rrr.circuit_breaker": AbiSpec(
        surface=frozenset(
            {
                '#include "misc/srpc_timing.h"',
                "export module rrr.circuit_breaker;",
                "export enum class CircuitState",
                "export struct CircuitBreakerConfig",
                "uint32_t failure_threshold;",
                "uint32_t success_threshold;",
                "uint32_t timeout_ms;",
                "bool enabled;",
                "static CircuitBreakerConfig new_();",
                "static CircuitBreakerConfig defaults();",
                "static CircuitBreakerConfig sensitive();",
                "static CircuitBreakerConfig relaxed();",
                "static CircuitBreakerConfig disabled();",
                "export struct CircuitBreaker",
                "rusty::Cell<CircuitBreakerConfig> config_field;",
                "rusty::Cell<CircuitState> state_field;",
                "rusty::Cell<uint32_t> failure_count_field;",
                "rusty::Cell<uint32_t> success_count_field;",
                "rusty::Cell<uint64_t> last_failure_time;",
                "rusty::Cell<bool> probe_in_progress;",
                "static CircuitBreaker new_(CircuitBreakerConfig config);",
                "void set_config(CircuitBreakerConfig config) const;",
                "bool allow_request() const;",
                "void record_success() const;",
                "void record_failure() const;",
                "CircuitState state() const;",
                "bool is_open() const;",
                "bool is_closed() const;",
                "bool is_half_open() const;",
                "void reset() const;",
                "uint32_t failure_count() const;",
                "uint32_t success_count() const;",
                "CircuitBreakerConfig config() const;",
                "export uint64_t current_time_us();",
                "export std::string_view circuit_state_to_string(CircuitState state);",
                "rusty::wrapping_sub(now",
                "rusty::wrapping_add(this->failure_count_field.get()",
                "rusty::wrapping_add(this->success_count_field.get()",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::current_time_us@rrr.circuit_breaker()",
                "rrr::circuit_state_to_string@rrr.circuit_breaker(rrr::CircuitState@rrr.circuit_breaker)",
                "rrr::CircuitBreakerConfig@rrr.circuit_breaker::new_()",
                "rrr::CircuitBreakerConfig@rrr.circuit_breaker::defaults()",
                "rrr::CircuitBreakerConfig@rrr.circuit_breaker::sensitive()",
                "rrr::CircuitBreakerConfig@rrr.circuit_breaker::relaxed()",
                "rrr::CircuitBreakerConfig@rrr.circuit_breaker::disabled()",
                "rrr::CircuitBreaker@rrr.circuit_breaker::new_(rrr::CircuitBreakerConfig@rrr.circuit_breaker)",
                "rrr::CircuitBreaker@rrr.circuit_breaker::set_config(rrr::CircuitBreakerConfig@rrr.circuit_breaker) const",
                "rrr::CircuitBreaker@rrr.circuit_breaker::allow_request() const",
                "rrr::CircuitBreaker@rrr.circuit_breaker::record_success() const",
                "rrr::CircuitBreaker@rrr.circuit_breaker::record_failure() const",
                "rrr::CircuitBreaker@rrr.circuit_breaker::state() const",
                "rrr::CircuitBreaker@rrr.circuit_breaker::is_open() const",
                "rrr::CircuitBreaker@rrr.circuit_breaker::is_closed() const",
                "rrr::CircuitBreaker@rrr.circuit_breaker::is_half_open() const",
                "rrr::CircuitBreaker@rrr.circuit_breaker::reset() const",
                "rrr::CircuitBreaker@rrr.circuit_breaker::failure_count() const",
                "rrr::CircuitBreaker@rrr.circuit_breaker::success_count() const",
                "rrr::CircuitBreaker@rrr.circuit_breaker::config() const",
            }
        ),
    ),
    "rrr.connection_state": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.connection_state;",
                "export enum class ConnectionState",
                "export struct ConnectionStateMachine",
                "export using StateChangeCallback = rusty::Function<void(ConnectionState, ConnectionState) const>;",
                "rusty::Cell<ConnectionState> state_field;",
                "StateChangeCallback on_state_change;",
                "static ConnectionStateMachine new_();",
                "ConnectionState state() const;",
                "bool can_transition_to(ConnectionState new_state) const;",
                "bool transition_to(ConnectionState new_state) const;",
                "void force_state(ConnectionState new_state) const;",
                "void set_on_state_change(StateChangeCallback callback);",
                "bool is_connected() const;",
                "bool is_failed() const;",
                "bool is_terminal() const;",
                "bool can_connect() const;",
                "bool is_usable() const;",
                "static bool is_valid_transition(ConnectionState from, ConnectionState to);",
                "export std::string_view connection_state_to_string(ConnectionState state);",
                ".on_state_change = rusty::default_like<StateChangeCallback>()",
                "rusty::is_empty(this->on_state_change)",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::connection_state_to_string@rrr.connection_state(rrr::ConnectionState@rrr.connection_state)",
                "rrr::ConnectionStateMachine@rrr.connection_state::new_()",
                "rrr::ConnectionStateMachine@rrr.connection_state::state() const",
                "rrr::ConnectionStateMachine@rrr.connection_state::can_transition_to(rrr::ConnectionState@rrr.connection_state) const",
                "rrr::ConnectionStateMachine@rrr.connection_state::transition_to(rrr::ConnectionState@rrr.connection_state) const",
                "rrr::ConnectionStateMachine@rrr.connection_state::force_state(rrr::ConnectionState@rrr.connection_state) const",
                "rrr::ConnectionStateMachine@rrr.connection_state::set_on_state_change(rusty::Function<void (rrr::ConnectionState@rrr.connection_state, rrr::ConnectionState@rrr.connection_state) const>)",
                "rrr::ConnectionStateMachine@rrr.connection_state::is_connected() const",
                "rrr::ConnectionStateMachine@rrr.connection_state::is_failed() const",
                "rrr::ConnectionStateMachine@rrr.connection_state::is_terminal() const",
                "rrr::ConnectionStateMachine@rrr.connection_state::can_connect() const",
                "rrr::ConnectionStateMachine@rrr.connection_state::is_usable() const",
                "rrr::ConnectionStateMachine@rrr.connection_state::is_valid_transition(rrr::ConnectionState@rrr.connection_state, rrr::ConnectionState@rrr.connection_state)",
            }
        ),
    ),
    "rrr.heartbeat": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.heartbeat;",
                "export using HeartbeatTimeoutCallback = rusty::Function<void()>;",
                "export uint64_t heartbeat_time_us();",
                "export struct HeartbeatConfig",
                "bool enabled;",
                "uint32_t interval_ms;",
                "uint32_t timeout_ms;",
                "uint32_t max_missed;",
                "static HeartbeatConfig new_();",
                "static HeartbeatConfig defaults();",
                "static HeartbeatConfig aggressive();",
                "static HeartbeatConfig relaxed();",
                "static HeartbeatConfig disabled();",
                "export struct HeartbeatManager",
                "rusty::Cell<HeartbeatConfig> config_field;",
                "rusty::Cell<uint64_t> last_send_time;",
                "rusty::Cell<uint64_t> last_recv_time;",
                "rusty::Cell<uint32_t> missed_count_field;",
                "rusty::Cell<bool> pending_pong;",
                "rusty::Cell<bool> timed_out;",
                "rusty::RefCell<HeartbeatTimeoutCallback> on_timeout;",
                "static HeartbeatManager new_(const HeartbeatConfig& config);",
                "void set_config(const HeartbeatConfig& config) const;",
                "void set_on_timeout(HeartbeatTimeoutCallback callback) const;",
                "bool should_send_heartbeat() const;",
                "void on_heartbeat_sent() const;",
                "void on_pong_received() const;",
                "bool check_timeout() const;",
                "uint32_t time_until_next_heartbeat_ms() const;",
                "bool is_timed_out() const;",
                "uint32_t missed_count() const;",
                "bool is_pending_pong() const;",
                "void reset() const;",
                "HeartbeatConfig config() const;",
                "return current_time_us();",
                ".on_timeout = rusty::RefCell<HeartbeatTimeoutCallback>::new_(rusty::default_like<HeartbeatTimeoutCallback>())",
                "rusty::is_empty(((*callback)))",
                "rusty::wrapping_sub(now",
                "rusty::wrapping_add(this->missed_count_field.get()",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::heartbeat_time_us@rrr.heartbeat()",
                "rrr::HeartbeatConfig@rrr.heartbeat::new_()",
                "rrr::HeartbeatConfig@rrr.heartbeat::defaults()",
                "rrr::HeartbeatConfig@rrr.heartbeat::aggressive()",
                "rrr::HeartbeatConfig@rrr.heartbeat::relaxed()",
                "rrr::HeartbeatConfig@rrr.heartbeat::disabled()",
                "rrr::HeartbeatManager@rrr.heartbeat::new_(rrr::HeartbeatConfig@rrr.heartbeat const&)",
                "rrr::HeartbeatManager@rrr.heartbeat::set_config(rrr::HeartbeatConfig@rrr.heartbeat const&) const",
                "rrr::HeartbeatManager@rrr.heartbeat::set_on_timeout(rusty::Function<void ()>) const",
                "rrr::HeartbeatManager@rrr.heartbeat::should_send_heartbeat() const",
                "rrr::HeartbeatManager@rrr.heartbeat::on_heartbeat_sent() const",
                "rrr::HeartbeatManager@rrr.heartbeat::on_pong_received() const",
                "rrr::HeartbeatManager@rrr.heartbeat::check_timeout() const",
                "rrr::HeartbeatManager@rrr.heartbeat::time_until_next_heartbeat_ms() const",
                "rrr::HeartbeatManager@rrr.heartbeat::is_timed_out() const",
                "rrr::HeartbeatManager@rrr.heartbeat::missed_count() const",
                "rrr::HeartbeatManager@rrr.heartbeat::is_pending_pong() const",
                "rrr::HeartbeatManager@rrr.heartbeat::reset() const",
                "rrr::HeartbeatManager@rrr.heartbeat::config() const",
            }
        ),
    ),
    "rrr.load_balancer": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.load_balancer;",
                "export enum class LoadBalancingStrategy",
                "RANDOM = 0,",
                "ROUND_ROBIN = 1,",
                "LEAST_CONNECTIONS = 2,",
                "LEAST_LATENCY = 3",
                "// Rust-only trait import marker: using _ = rusty::LoadBalancerClient;",
                "// Rust-only trait import marker: using _ = rusty::LoadBalancerMetrics;",
                "export struct LoadBalancerState",
                "rusty::Cell<size_t> round_robin_index_field;",
                "static LoadBalancerState new_();",
                "size_t next_round_robin_index(size_t pool_size) const;",
                "void reset() const;",
                "export struct LoadBalancer",
                "template<typename ClientVec>",
                "static size_t select(LoadBalancingStrategy strategy, const ClientVec& clients, const LoadBalancerState& state, size_t rand_value);",
                "static size_t select_random(size_t pool_size, size_t rand_value);",
                "static size_t select_round_robin(size_t pool_size, const LoadBalancerState& state);",
                "export std::string_view load_balancing_strategy_to_string(LoadBalancingStrategy strategy);",
                "size_t lb_pool_size(const ClientVec& clients);",
                "size_t lb_select_least_connections(const ClientVec& clients);",
                "size_t lb_select_least_latency(const ClientVec& clients);",
                "rusty::wrapping_add(current",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::load_balancing_strategy_to_string@rrr.load_balancer(rrr::LoadBalancingStrategy@rrr.load_balancer)",
                "rrr::LoadBalancerState@rrr.load_balancer::new_()",
                "rrr::LoadBalancerState@rrr.load_balancer::next_round_robin_index(unsigned long) const",
                "rrr::LoadBalancerState@rrr.load_balancer::reset() const",
                "rrr::LoadBalancer@rrr.load_balancer::select_random(unsigned long, unsigned long)",
                "rrr::LoadBalancer@rrr.load_balancer::select_round_robin(unsigned long, rrr::LoadBalancerState@rrr.load_balancer const&)",
            }
        ),
    ),
    "rrr.frame_codec": AbiSpec(
        surface=frozenset(
            {
                "#include <vector>",
                "#include <rusty/io.hpp>",
                "export module rrr.frame_codec;",
                "import rrr.internal_protocol;",
                "export enum class FrameDecodeStatus",
                "NeedMoreBytes = 0,",
                "Complete = 1,",
                "Malformed = 2",
                "using FrameBytes = std::vector<uint8_t>;",
                "export using FrameCursor = rusty::io::Cursor<FrameBytes>;",
                "export constexpr size_t kFrameHeaderSize",
                "export constexpr int32_t kMaxFramePayloadSize",
                "export struct FrameHeader",
                "int32_t payload_size;",
                "bool extended_header_flag;",
                "int32_t total_frame_size() const;",
                "export struct FrameView",
                "FrameHeader header;",
                "const uint8_t* payload;",
                "size_t payload_size;",
                "export struct FrameStreamReader",
                "FrameCursor cursor_;",
                "rusty::Cell<bool> noncopy_;",
                "static FrameStreamReader new_();",
                "void append(const uint8_t* data, size_t size);",
                "FrameDecodeStatus next_frame(FrameView& out_view) const;",
                "void consume_frame();",
                "void reset();",
                "size_t buffered_bytes() const;",
                "bool empty() const;",
                "export std::string_view frame_decode_status_to_string(FrameDecodeStatus status);",
                "export bool frame_codec_write_header(std::span<uint8_t> out_buf, int32_t payload_size, bool extended_header_flag);",
                "export FrameDecodeStatus frame_codec_peek_header(std::span<const uint8_t> buf, FrameHeader& out_header);",
                "export FrameCursor make_frame_cursor();",
                "export bool frame_codec_encode_into(FrameBytes& out, const uint8_t* payload, int32_t payload_size, bool extended_header_flag);",
                "export void fsr_append(FrameStreamReader& reader, const uint8_t* data, size_t size);",
                "export void fsr_consume_frame(FrameStreamReader& reader);",
                "rusty::wrapping_add(this->payload_size",
                "static constexpr bool is_send = true;",
                "static constexpr bool is_sync = true;",
            }
        ),
        symbols=frozenset(
            {
                ("R", "rrr::kFrameHeaderSize@rrr.frame_codec"),
                ("R", "rrr::kMaxFramePayloadSize@rrr.frame_codec"),
                (
                    "T",
                    "rrr::FrameHeader@rrr.frame_codec::total_frame_size() const",
                ),
                (
                    "T",
                    "rrr::FrameStreamReader@rrr.frame_codec::append(unsigned char const*, unsigned long)",
                ),
                (
                    "T",
                    "rrr::FrameStreamReader@rrr.frame_codec::buffered_bytes() const",
                ),
                (
                    "T",
                    "rrr::FrameStreamReader@rrr.frame_codec::consume_frame()",
                ),
                ("T", "rrr::FrameStreamReader@rrr.frame_codec::empty() const"),
                ("T", "rrr::FrameStreamReader@rrr.frame_codec::new_()"),
                (
                    "T",
                    "rrr::FrameStreamReader@rrr.frame_codec::next_frame(rrr::FrameView@rrr.frame_codec&) const",
                ),
                ("T", "rrr::FrameStreamReader@rrr.frame_codec::reset()"),
                (
                    "T",
                    "rrr::frame_codec_encode_into@rrr.frame_codec(std::__1::vector<unsigned char, std::__1::allocator<unsigned char>>&, unsigned char const*, int, bool)",
                ),
                (
                    "T",
                    "rrr::frame_codec_peek_header@rrr.frame_codec(std::__1::span<unsigned char const, 18446744073709551615ul>, rrr::FrameHeader@rrr.frame_codec&)",
                ),
                (
                    "T",
                    "rrr::frame_codec_write_header@rrr.frame_codec(std::__1::span<unsigned char, 18446744073709551615ul>, int, bool)",
                ),
                (
                    "T",
                    "rrr::frame_decode_status_to_string@rrr.frame_codec(rrr::FrameDecodeStatus@rrr.frame_codec)",
                ),
                (
                    "T",
                    "rrr::fsr_append@rrr.frame_codec(rrr::FrameStreamReader@rrr.frame_codec&, unsigned char const*, unsigned long)",
                ),
                (
                    "T",
                    "rrr::fsr_consume_frame@rrr.frame_codec(rrr::FrameStreamReader@rrr.frame_codec&)",
                ),
                ("T", "rrr::make_frame_cursor@rrr.frame_codec()"),
            }
        ),
    ),
    "rrr.utils": AbiSpec(
        surface=frozenset(
            {
                "#include <netdb.h>",
                "export module rrr.utils;",
                "import rrr.logging;",
                "export struct AddrInfo",
                "addrinfo* info_;",
                "rusty::Cell<bool> owned_;",
                "AddrInfo(AddrInfo&& other) noexcept",
                "AddrInfo& operator=(AddrInfo&& other) noexcept",
                "AddrInfo();",
                "AddrInfo(addrinfo* info);",
                "addrinfo* get() const;",
                "bool valid() const;",
                "~AddrInfo() noexcept(false);",
                "export int32_t find_open_port();",
                "export std::string get_host_name();",
                "rrr::log_line(3, 0, rusty::ptr::null(), message);",
                "rrr::log_line(1, 0, rusty::ptr::null(), message);",
                "rusty::sys::env::hostname();",
                "utils_ffi::srpc_find_open_port();",
                "utils_ffi::freeaddrinfo(this->info_);",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::AddrInfo@rrr.utils::AddrInfo()",
                "rrr::AddrInfo@rrr.utils::AddrInfo(addrinfo*)",
                "rrr::AddrInfo@rrr.utils::AddrInfo(addrinfo*, rusty::Cell<bool>)",
                "rrr::AddrInfo@rrr.utils::AddrInfo(rrr::AddrInfo@rrr.utils&&)",
                "rrr::AddrInfo@rrr.utils::get() const",
                "rrr::AddrInfo@rrr.utils::operator=(rrr::AddrInfo@rrr.utils&&)",
                "rrr::AddrInfo@rrr.utils::rusty_mark_forgotten() const",
                "rrr::AddrInfo@rrr.utils::valid() const",
                "rrr::AddrInfo@rrr.utils::~AddrInfo()",
                "rrr::find_open_port@rrr.utils()",
                "rrr::get_host_name@rrr.utils()",
            }
        ),
    ),
    "rrr.basetypes": AbiSpec(
        surface=frozenset(
            {
                '#include "misc/srpc_timing.h"',
                "#include <rusty/sync/atomic.hpp>",
                "export module rrr.basetypes;",
                "export using i8 = int8_t;",
                "export using i16 = int16_t;",
                "export using i32 = int32_t;",
                "export using i64 = int64_t;",
                "export using rusty::sync::atomic::AtomicI64;",
                "export using rusty::sync::atomic::Ordering;",
                "export constexpr uint64_t RRR_USEC_PER_SEC",
                "export struct SparseInt",
                "static size_t buf_size(uint8_t byte0);",
                "static size_t dump32(int32_t val, uint8_t* buf);",
                "static size_t dump64(int64_t val, uint8_t* buf);",
                "static int32_t load32(const uint8_t* buf);",
                "static int64_t load64(const uint8_t* buf);",
                "static size_t val_size(int64_t val);",
                "export struct v32",
                "int32_t val_field;",
                "static v32 new_(int32_t v);",
                "export struct v64",
                "int64_t val_field;",
                "static v64 new_(int64_t v);",
                "export struct Counter",
                "rusty::sync::atomic::AtomicI64 next_field;",
                "static Counter new_(int64_t start);",
                "int64_t peek_next() const;",
                "int64_t next(int64_t step) const;",
                "void reset(int64_t start) const;",
                "export struct Time",
                "static uint64_t now(bool accurate);",
                "static void sleep(uint64_t t);",
                "export struct Timer",
                "uint64_t begin_us;",
                "uint64_t end_us;",
                "static Timer new_();",
                "void start();",
                "void stop();",
                "void reset();",
                "double elapsed() const;",
                "export void abort_if_false(bool cond);",
                "std::abort();",
                "export uint64_t time_now_us(bool accurate);",
                "srpc_clock_monotonic_us();",
                "srpc_clock_realtime_coarse_us();",
                "srpc_gettimeofday_us();",
                "srpc_sleep_us(uint64_t microseconds);",
                "rusty::wrapping_sub(end,",
            }
        ),
        symbols=frozenset(
            {
                ("R", "rrr::RRR_USEC_PER_SEC@rrr.basetypes"),
                ("T", "rrr::abort_if_false@rrr.basetypes(bool)"),
                ("T", "rrr::time_now_us@rrr.basetypes(bool)"),
                ("T", "rrr::SparseInt@rrr.basetypes::buf_size(unsigned char)"),
                ("T", "rrr::SparseInt@rrr.basetypes::dump32(int, unsigned char*)"),
                ("T", "rrr::SparseInt@rrr.basetypes::dump64(long, unsigned char*)"),
                ("T", "rrr::SparseInt@rrr.basetypes::load32(unsigned char const*)"),
                ("T", "rrr::SparseInt@rrr.basetypes::load64(unsigned char const*)"),
                ("T", "rrr::SparseInt@rrr.basetypes::val_size(long)"),
                ("T", "rrr::v32@rrr.basetypes::new_(int)"),
                ("T", "rrr::v32@rrr.basetypes::set(int)"),
                ("T", "rrr::v32@rrr.basetypes::get() const"),
                ("T", "rrr::v32@rrr.basetypes::val_size() const"),
                ("T", "rrr::v64@rrr.basetypes::new_(long)"),
                ("T", "rrr::v64@rrr.basetypes::set(long)"),
                ("T", "rrr::v64@rrr.basetypes::get() const"),
                ("T", "rrr::v64@rrr.basetypes::val_size() const"),
                ("T", "rrr::Counter@rrr.basetypes::new_(long)"),
                ("T", "rrr::Counter@rrr.basetypes::peek_next() const"),
                ("T", "rrr::Counter@rrr.basetypes::next(long) const"),
                ("T", "rrr::Counter@rrr.basetypes::reset(long) const"),
                ("T", "rrr::Time@rrr.basetypes::now(bool)"),
                ("T", "rrr::Time@rrr.basetypes::sleep(unsigned long)"),
                ("T", "rrr::Timer@rrr.basetypes::new_()"),
                ("T", "rrr::Timer@rrr.basetypes::start()"),
                ("T", "rrr::Timer@rrr.basetypes::stop()"),
                ("T", "rrr::Timer@rrr.basetypes::reset()"),
                ("T", "rrr::Timer@rrr.basetypes::elapsed() const"),
            }
        ),
    ),
    "rrr.request_queue": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.request_queue;",
                "import rusty;",
                "import rrr.circuit_breaker;",
                "export enum class OverflowStrategy",
                "export constexpr OverflowStrategy OverflowStrategy_DROP_OLDEST();",
                "export constexpr OverflowStrategy OverflowStrategy_DROP_NEWEST();",
                "export constexpr OverflowStrategy OverflowStrategy_FAIL_FAST();",
                "export using QueuedRequestCallback = rusty::Function<void(int32_t)>;",
                "export constexpr int32_t kRequestQueueRejectedError = static_cast<int32_t>(35);",
                "export constexpr int32_t kRequestQueueRejectedError = static_cast<int32_t>(11);",
                "export constexpr int32_t kRequestQueueExpiredError = static_cast<int32_t>(60);",
                "export constexpr int32_t kRequestQueueExpiredError = static_cast<int32_t>(110);",
                "export std::string_view overflow_strategy_to_string(OverflowStrategy strategy);",
                "export uint64_t queued_request_time_us();",
                "export void rq_invoke_callback_safely(QueuedRequestCallback callback, int32_t error);",
                "export struct QueuedRequest",
                "int64_t xid;",
                "int32_t rpc_id;",
                "uint64_t timestamp_us;",
                "uint32_t retry_count;",
                "QueuedRequestCallback callback;",
                "uint32_t ttl_ms;",
                "static QueuedRequest new_();",
                "bool is_expired() const;",
                "uint32_t age_ms() const;",
                "export struct RequestQueueConfig",
                "size_t max_size;",
                "uint32_t default_ttl_ms;",
                "OverflowStrategy overflow_strategy;",
                "bool enabled;",
                "static RequestQueueConfig new_();",
                "static RequestQueueConfig defaults();",
                "static RequestQueueConfig small();",
                "static RequestQueueConfig large();",
                "static RequestQueueConfig disabled();",
                "export struct RequestQueue",
                "rusty::Cell<RequestQueueConfig> config_;",
                "rusty::Mutex<rusty::VecDeque<QueuedRequest>> queue_;",
                "RequestQueue();",
                "RequestQueue(RequestQueueConfig config);",
                "bool enqueue(QueuedRequest request) const;",
                "rusty::Option<QueuedRequest> dequeue();",
                "size_t expire_stale() const;",
                "size_t size() const;",
                "bool empty() const;",
                "bool full();",
                "size_t remaining_capacity();",
                "void clear_all(int32_t error_code) const;",
                "RequestQueueConfig config() const;",
                "bool enabled() const;",
                "size_t max_size() const;",
                "void update_config(RequestQueueConfig config) const;",
                "return current_time_us();",
                "rusty::wrapping_sub(::rrr::queued_request_time_us()",
                "catch_unwind(AssertUnwindSafe(",
            }
        ),
        symbols=frozenset(
            {
                ("R", "rrr::kRequestQueueRejectedError@rrr.request_queue"),
                ("R", "rrr::kRequestQueueExpiredError@rrr.request_queue"),
                *(
                    ("T", symbol)
                    for symbol in {
                        "rrr::overflow_strategy_to_string@rrr.request_queue(rrr::OverflowStrategy@rrr.request_queue)",
                        "rrr::queued_request_time_us@rrr.request_queue()",
                        "rrr::rq_invoke_callback_safely@rrr.request_queue(rusty::Function<void (int)>, int)",
                        "rrr::QueuedRequest@rrr.request_queue::new_()",
                        "rrr::QueuedRequest@rrr.request_queue::is_expired() const",
                        "rrr::QueuedRequest@rrr.request_queue::age_ms() const",
                        "rrr::RequestQueueConfig@rrr.request_queue::new_()",
                        "rrr::RequestQueueConfig@rrr.request_queue::defaults()",
                        "rrr::RequestQueueConfig@rrr.request_queue::small()",
                        "rrr::RequestQueueConfig@rrr.request_queue::large()",
                        "rrr::RequestQueueConfig@rrr.request_queue::disabled()",
                        "rrr::RequestQueue@rrr.request_queue::RequestQueue()",
                        "rrr::RequestQueue@rrr.request_queue::RequestQueue(rrr::RequestQueueConfig@rrr.request_queue)",
                        "rrr::RequestQueue@rrr.request_queue::enqueue(rrr::QueuedRequest@rrr.request_queue) const",
                        "rrr::RequestQueue@rrr.request_queue::dequeue()",
                        "rrr::RequestQueue@rrr.request_queue::expire_stale() const",
                        "rrr::RequestQueue@rrr.request_queue::size() const",
                        "rrr::RequestQueue@rrr.request_queue::empty() const",
                        "rrr::RequestQueue@rrr.request_queue::full()",
                        "rrr::RequestQueue@rrr.request_queue::remaining_capacity()",
                        "rrr::RequestQueue@rrr.request_queue::clear_all(int) const",
                        "rrr::RequestQueue@rrr.request_queue::config() const",
                        "rrr::RequestQueue@rrr.request_queue::enabled() const",
                        "rrr::RequestQueue@rrr.request_queue::max_size() const",
                        "rrr::RequestQueue@rrr.request_queue::update_config(rrr::RequestQueueConfig@rrr.request_queue) const",
                    }
                ),
            }
        ),
    ),
    "rrr.serializable": AbiSpec(
        surface=frozenset(
            {
                'export module rrr.serializable;',
                'export struct BufferSink;',
                'export struct BufferSource;',
                'export struct FdSink;',
                'export struct FdSource;',
                'export struct BinaryWriteArchive;',
                'export struct BinaryReadArchive;',
                'export struct SerializableRegistry;',
                'export SinkProxy make_sink_proxy(BufferSink* sink);',
                'export SourceProxy make_source_proxy(BufferSource* source);',
                'export SinkProxy make_sink_proxy(FdSink* sink);',
                'export SourceProxy make_source_proxy(FdSource* source);',
                'export void serializable_registry_register_factory(int32_t kind, rusty::Function<SerializableProxy()> factory);',
                'export bool serializable_registry_is_registered_impl(int32_t kind);',
                'export void serializable_registry_clear_impl();',
            }
        ),
        symbols=frozenset(
            {
                ('D', 'typeinfo for rrr::Deserialize@rrr.serializable'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<double>'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<int>'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<long>'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<short>'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<signed char>'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<unsigned char>'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<unsigned int>'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<unsigned long>'),
                ('D', 'typeinfo for rrr::DeserializeAdapter@rrr.serializable<unsigned short>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<double>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<int>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<long>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<short>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<signed char>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<unsigned char>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<unsigned int>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<unsigned long>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRef@rrr.serializable<unsigned short>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<double>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<int>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<long>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<short>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<signed char>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned char>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned int>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned long>'),
                ('D', 'typeinfo for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned short>'),
                ('D', 'typeinfo for rrr::SerializableBase@rrr.serializable'),
                ('D', 'typeinfo for rrr::Serialize@rrr.serializable'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<double>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<int>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<long>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<short>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<signed char>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<unsigned char>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<unsigned int>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<unsigned long>'),
                ('D', 'typeinfo for rrr::SerializeAdapter@rrr.serializable<unsigned short>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<double>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<int>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<long>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<short>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<signed char>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<unsigned char>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<unsigned int>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<unsigned long>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRef@rrr.serializable<unsigned short>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<double>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<int>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<long>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<short>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<signed char>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned char>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned int>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned long>'),
                ('D', 'typeinfo for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned short>'),
                ('D', 'typeinfo for rrr::SinkBase@rrr.serializable'),
                ('D', 'typeinfo for rrr::SinkBaseAdapter@rrr.serializable<rrr::BufferSink@rrr.serializable>'),
                ('D', 'typeinfo for rrr::SinkBaseAdapter@rrr.serializable<rrr::FdSink@rrr.serializable>'),
                ('D', 'typeinfo for rrr::SinkBaseAdapterRef@rrr.serializable<rrr::BufferSink@rrr.serializable>'),
                ('D', 'typeinfo for rrr::SinkBaseAdapterRef@rrr.serializable<rrr::FdSink@rrr.serializable>'),
                ('D', 'typeinfo for rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::BufferSink@rrr.serializable>'),
                ('D', 'typeinfo for rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::FdSink@rrr.serializable>'),
                ('D', 'typeinfo for rrr::SourceBase@rrr.serializable'),
                ('D', 'typeinfo for rrr::SourceBaseAdapter@rrr.serializable<rrr::BufferSource@rrr.serializable>'),
                ('D', 'typeinfo for rrr::SourceBaseAdapter@rrr.serializable<rrr::FdSource@rrr.serializable>'),
                ('D', 'typeinfo for rrr::SourceBaseAdapterRef@rrr.serializable<rrr::BufferSource@rrr.serializable>'),
                ('D', 'typeinfo for rrr::SourceBaseAdapterRef@rrr.serializable<rrr::FdSource@rrr.serializable>'),
                ('D', 'typeinfo for rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::BufferSource@rrr.serializable>'),
                ('D', 'typeinfo for rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::FdSource@rrr.serializable>'),
                ('D', 'vtable for rrr::Deserialize@rrr.serializable'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<double>'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<int>'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<long>'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<short>'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<signed char>'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<unsigned char>'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<unsigned int>'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<unsigned long>'),
                ('D', 'vtable for rrr::DeserializeAdapter@rrr.serializable<unsigned short>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<double>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<int>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<long>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<short>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<signed char>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<unsigned char>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<unsigned int>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<unsigned long>'),
                ('D', 'vtable for rrr::DeserializeAdapterRef@rrr.serializable<unsigned short>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<double>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<int>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<long>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<short>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<signed char>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned char>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned int>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned long>'),
                ('D', 'vtable for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned short>'),
                ('D', 'vtable for rrr::SerializableBase@rrr.serializable'),
                ('D', 'vtable for rrr::Serialize@rrr.serializable'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<double>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<int>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<long>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<short>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<signed char>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<unsigned char>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<unsigned int>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<unsigned long>'),
                ('D', 'vtable for rrr::SerializeAdapter@rrr.serializable<unsigned short>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<double>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<int>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<long>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<short>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<signed char>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<unsigned char>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<unsigned int>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<unsigned long>'),
                ('D', 'vtable for rrr::SerializeAdapterRef@rrr.serializable<unsigned short>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<double>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<int>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<long>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<short>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<signed char>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned char>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned int>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned long>'),
                ('D', 'vtable for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned short>'),
                ('D', 'vtable for rrr::SinkBase@rrr.serializable'),
                ('D', 'vtable for rrr::SinkBaseAdapter@rrr.serializable<rrr::BufferSink@rrr.serializable>'),
                ('D', 'vtable for rrr::SinkBaseAdapter@rrr.serializable<rrr::FdSink@rrr.serializable>'),
                ('D', 'vtable for rrr::SinkBaseAdapterRef@rrr.serializable<rrr::BufferSink@rrr.serializable>'),
                ('D', 'vtable for rrr::SinkBaseAdapterRef@rrr.serializable<rrr::FdSink@rrr.serializable>'),
                ('D', 'vtable for rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::BufferSink@rrr.serializable>'),
                ('D', 'vtable for rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::FdSink@rrr.serializable>'),
                ('D', 'vtable for rrr::SourceBase@rrr.serializable'),
                ('D', 'vtable for rrr::SourceBaseAdapter@rrr.serializable<rrr::BufferSource@rrr.serializable>'),
                ('D', 'vtable for rrr::SourceBaseAdapter@rrr.serializable<rrr::FdSource@rrr.serializable>'),
                ('D', 'vtable for rrr::SourceBaseAdapterRef@rrr.serializable<rrr::BufferSource@rrr.serializable>'),
                ('D', 'vtable for rrr::SourceBaseAdapterRef@rrr.serializable<rrr::FdSource@rrr.serializable>'),
                ('D', 'vtable for rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::BufferSource@rrr.serializable>'),
                ('D', 'vtable for rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::FdSource@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::Deserialize@rrr.serializable'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<double>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<int>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<long>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<short>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<signed char>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<unsigned char>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<unsigned int>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<unsigned long>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapter@rrr.serializable<unsigned short>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<double>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<int>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<long>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<short>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<signed char>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<unsigned char>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<unsigned int>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<unsigned long>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRef@rrr.serializable<unsigned short>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<double>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<int>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<long>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<short>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<signed char>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned char>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned int>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned long>'),
                ('R', 'typeinfo name for rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned short>'),
                ('R', 'typeinfo name for rrr::SerializableBase@rrr.serializable'),
                ('R', 'typeinfo name for rrr::Serialize@rrr.serializable'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<double>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<int>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<long>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<short>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<signed char>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<unsigned char>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<unsigned int>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<unsigned long>'),
                ('R', 'typeinfo name for rrr::SerializeAdapter@rrr.serializable<unsigned short>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<double>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<int>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<long>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<short>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<signed char>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<unsigned char>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<unsigned int>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<unsigned long>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRef@rrr.serializable<unsigned short>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<double>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<int>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<long>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<short>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<signed char>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned char>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned int>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned long>'),
                ('R', 'typeinfo name for rrr::SerializeAdapterRefMut@rrr.serializable<unsigned short>'),
                ('R', 'typeinfo name for rrr::SinkBase@rrr.serializable'),
                ('R', 'typeinfo name for rrr::SinkBaseAdapter@rrr.serializable<rrr::BufferSink@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::SinkBaseAdapter@rrr.serializable<rrr::FdSink@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::SinkBaseAdapterRef@rrr.serializable<rrr::BufferSink@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::SinkBaseAdapterRef@rrr.serializable<rrr::FdSink@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::BufferSink@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::FdSink@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::SourceBase@rrr.serializable'),
                ('R', 'typeinfo name for rrr::SourceBaseAdapter@rrr.serializable<rrr::BufferSource@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::SourceBaseAdapter@rrr.serializable<rrr::FdSource@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::SourceBaseAdapterRef@rrr.serializable<rrr::BufferSource@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::SourceBaseAdapterRef@rrr.serializable<rrr::FdSource@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::BufferSource@rrr.serializable>'),
                ('R', 'typeinfo name for rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::FdSource@rrr.serializable>'),
                ('T', 'rrr::BinaryReadArchive@rrr.serializable::read_exact(unsigned char*, unsigned long)'),
                ('T', 'rrr::BinaryReadArchive@rrr.serializable::read_or_abort(unsigned char*, unsigned long)'),
                ('T', 'rrr::BinaryWriteArchive@rrr.serializable::write_bytes(unsigned char const*, unsigned long)'),
                ('T', 'rrr::BufferSink@rrr.serializable::write_bytes(unsigned char const*, unsigned long)'),
                ('T', 'rrr::BufferSource@rrr.serializable::eof() const'),
                ('T', 'rrr::BufferSource@rrr.serializable::new_(unsigned char const*, unsigned long)'),
                ('T', 'rrr::BufferSource@rrr.serializable::pos() const'),
                ('T', 'rrr::BufferSource@rrr.serializable::read_bytes(unsigned char*, unsigned long)'),
                ('T', 'rrr::BufferSource@rrr.serializable::remaining() const'),
                ('T', 'rrr::Deserialize@rrr.serializable::~Deserialize()'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<double>::DeserializeAdapter(double)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<double>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<double>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<double>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<int>::DeserializeAdapter(int)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<int>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<int>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<int>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<long>::DeserializeAdapter(long)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<long>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<long>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<long>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>::DeserializeAdapter(rrr::v32@rrr.basetypes)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>::DeserializeAdapter(rrr::v64@rrr.basetypes)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<short>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<short>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<short>::DeserializeAdapter(short)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<short>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<signed char>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<signed char>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<signed char>::DeserializeAdapter(signed char)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<signed char>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::DeserializeAdapter(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned char>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<unsigned char>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned char>::DeserializeAdapter(unsigned char)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned char>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned int>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<unsigned int>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned int>::DeserializeAdapter(unsigned int)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned int>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned long>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<unsigned long>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned long>::DeserializeAdapter(unsigned long)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned long>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned short>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<unsigned short>&&)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned short>::DeserializeAdapter(unsigned short)'),
                ('T', 'rrr::DeserializeAdapter@rrr.serializable<unsigned short>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<double>::DeserializeAdapterRef(double const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<double>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<int>::DeserializeAdapterRef(int const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<int>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<long>::DeserializeAdapterRef(long const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<long>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>::DeserializeAdapterRef(rrr::v32@rrr.basetypes const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>::DeserializeAdapterRef(rrr::v64@rrr.basetypes const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<short>::DeserializeAdapterRef(short const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<short>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<signed char>::DeserializeAdapterRef(signed char const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<signed char>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::DeserializeAdapterRef(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<unsigned char>::DeserializeAdapterRef(unsigned char const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<unsigned char>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<unsigned int>::DeserializeAdapterRef(unsigned int const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<unsigned int>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<unsigned long>::DeserializeAdapterRef(unsigned long const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<unsigned long>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<unsigned short>::DeserializeAdapterRef(unsigned short const&)'),
                ('T', 'rrr::DeserializeAdapterRef@rrr.serializable<unsigned short>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<double>::DeserializeAdapterRefMut(double&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<double>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<int>::DeserializeAdapterRefMut(int&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<int>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<long>::DeserializeAdapterRefMut(long&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<long>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>::DeserializeAdapterRefMut(rrr::v32@rrr.basetypes&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>::DeserializeAdapterRefMut(rrr::v64@rrr.basetypes&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<short>::DeserializeAdapterRefMut(short&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<short>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<signed char>::DeserializeAdapterRefMut(signed char&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<signed char>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::DeserializeAdapterRefMut(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned char>::DeserializeAdapterRefMut(unsigned char&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned char>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned int>::DeserializeAdapterRefMut(unsigned int&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned int>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned long>::DeserializeAdapterRefMut(unsigned long&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned long>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned short>::DeserializeAdapterRefMut(unsigned short&)'),
                ('T', 'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned short>::deserialize(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(double&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(int&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(long&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(rrr::v32@rrr.basetypes&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(rrr::v64@rrr.basetypes&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(short&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(signed char&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(unsigned char&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(unsigned int&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(unsigned long&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::Deserialize_::deserialize@rrr.serializable(unsigned short&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::FdSink@rrr.serializable::fd() const'),
                ('T', 'rrr::FdSink@rrr.serializable::new_(int)'),
                ('T', 'rrr::FdSink@rrr.serializable::write_bytes(unsigned char const*, unsigned long)'),
                ('T', 'rrr::FdSource@rrr.serializable::fd() const'),
                ('T', 'rrr::FdSource@rrr.serializable::new_(int)'),
                ('T', 'rrr::FdSource@rrr.serializable::read_bytes(unsigned char*, unsigned long)'),
                ('T', 'rrr::SerializableBase@rrr.serializable::~SerializableBase()'),
                ('T', 'rrr::SerializableRegistry@rrr.serializable::clear_for_testing()'),
                ('T', 'rrr::SerializableRegistry@rrr.serializable::create(int)'),
                ('T', 'rrr::SerializableRegistry@rrr.serializable::is_registered(int)'),
                ('T', 'rrr::Serialize@rrr.serializable::~Serialize()'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<double>::SerializeAdapter(double)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<double>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<double>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<double>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<int>::SerializeAdapter(int)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<int>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<int>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<int>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<long>::SerializeAdapter(long)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<long>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<long>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<long>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>::SerializeAdapter(rrr::v32@rrr.basetypes)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>::SerializeAdapter(rrr::v64@rrr.basetypes)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<short>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<short>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<short>::SerializeAdapter(short)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<short>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<signed char>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<signed char>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<signed char>::SerializeAdapter(signed char)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<signed char>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::SerializeAdapter(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>::SerializeAdapter(std::__1::basic_string_view<char, std::__1::char_traits<char>>)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned char>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<unsigned char>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned char>::SerializeAdapter(unsigned char)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned char>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned int>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<unsigned int>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned int>::SerializeAdapter(unsigned int)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned int>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned long>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<unsigned long>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned long>::SerializeAdapter(unsigned long)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned long>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned short>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<unsigned short>&&)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned short>::SerializeAdapter(unsigned short)'),
                ('T', 'rrr::SerializeAdapter@rrr.serializable<unsigned short>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<double>::SerializeAdapterRef(double const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<double>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<int>::SerializeAdapterRef(int const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<int>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<long>::SerializeAdapterRef(long const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<long>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>::SerializeAdapterRef(rrr::v32@rrr.basetypes const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>::SerializeAdapterRef(rrr::v64@rrr.basetypes const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<short>::SerializeAdapterRef(short const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<short>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<signed char>::SerializeAdapterRef(signed char const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<signed char>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::SerializeAdapterRef(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>::SerializeAdapterRef(std::__1::basic_string_view<char, std::__1::char_traits<char>> const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<unsigned char>::SerializeAdapterRef(unsigned char const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<unsigned char>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<unsigned int>::SerializeAdapterRef(unsigned int const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<unsigned int>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<unsigned long>::SerializeAdapterRef(unsigned long const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<unsigned long>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<unsigned short>::SerializeAdapterRef(unsigned short const&)'),
                ('T', 'rrr::SerializeAdapterRef@rrr.serializable<unsigned short>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<double>::SerializeAdapterRefMut(double&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<double>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<int>::SerializeAdapterRefMut(int&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<int>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<long>::SerializeAdapterRefMut(long&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<long>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>::SerializeAdapterRefMut(rrr::v32@rrr.basetypes&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>::SerializeAdapterRefMut(rrr::v64@rrr.basetypes&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<short>::SerializeAdapterRefMut(short&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<short>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<signed char>::SerializeAdapterRefMut(signed char&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<signed char>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::SerializeAdapterRefMut(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>::SerializeAdapterRefMut(std::__1::basic_string_view<char, std::__1::char_traits<char>>&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned char>::SerializeAdapterRefMut(unsigned char&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned char>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned int>::SerializeAdapterRefMut(unsigned int&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned int>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned long>::SerializeAdapterRefMut(unsigned long&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned long>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned short>::SerializeAdapterRefMut(unsigned short&)'),
                ('T', 'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned short>::serialize(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(double const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(int const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(long const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(rrr::v32@rrr.basetypes const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(rrr::v64@rrr.basetypes const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(short const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(signed char const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(std::__1::basic_string_view<char, std::__1::char_traits<char>> const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(unsigned char const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(unsigned int const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(unsigned long const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::Serialize_::serialize@rrr.serializable(unsigned short const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::SinkBase@rrr.serializable::~SinkBase()'),
                ('T', 'rrr::SinkBaseAdapter@rrr.serializable<rrr::BufferSink@rrr.serializable>::SinkBaseAdapter(rrr::BufferSink@rrr.serializable)'),
                ('T', 'rrr::SinkBaseAdapter@rrr.serializable<rrr::BufferSink@rrr.serializable>::SinkBaseAdapter(rrr::SinkBaseAdapter@rrr.serializable<rrr::BufferSink@rrr.serializable>&&)'),
                ('T', 'rrr::SinkBaseAdapter@rrr.serializable<rrr::BufferSink@rrr.serializable>::write_bytes(unsigned char const*, unsigned long)'),
                ('T', 'rrr::SinkBaseAdapter@rrr.serializable<rrr::FdSink@rrr.serializable>::SinkBaseAdapter(rrr::FdSink@rrr.serializable)'),
                ('T', 'rrr::SinkBaseAdapter@rrr.serializable<rrr::FdSink@rrr.serializable>::SinkBaseAdapter(rrr::SinkBaseAdapter@rrr.serializable<rrr::FdSink@rrr.serializable>&&)'),
                ('T', 'rrr::SinkBaseAdapter@rrr.serializable<rrr::FdSink@rrr.serializable>::write_bytes(unsigned char const*, unsigned long)'),
                ('T', 'rrr::SinkBaseAdapterRef@rrr.serializable<rrr::BufferSink@rrr.serializable>::SinkBaseAdapterRef(rrr::BufferSink@rrr.serializable const&)'),
                ('T', 'rrr::SinkBaseAdapterRef@rrr.serializable<rrr::BufferSink@rrr.serializable>::write_bytes(unsigned char const*, unsigned long)'),
                ('T', 'rrr::SinkBaseAdapterRef@rrr.serializable<rrr::FdSink@rrr.serializable>::SinkBaseAdapterRef(rrr::FdSink@rrr.serializable const&)'),
                ('T', 'rrr::SinkBaseAdapterRef@rrr.serializable<rrr::FdSink@rrr.serializable>::write_bytes(unsigned char const*, unsigned long)'),
                ('T', 'rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::BufferSink@rrr.serializable>::SinkBaseAdapterRefMut(rrr::BufferSink@rrr.serializable&)'),
                ('T', 'rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::BufferSink@rrr.serializable>::write_bytes(unsigned char const*, unsigned long)'),
                ('T', 'rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::FdSink@rrr.serializable>::SinkBaseAdapterRefMut(rrr::FdSink@rrr.serializable&)'),
                ('T', 'rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::FdSink@rrr.serializable>::write_bytes(unsigned char const*, unsigned long)'),
                ('T', 'rrr::SourceBase@rrr.serializable::~SourceBase()'),
                ('T', 'rrr::SourceBaseAdapter@rrr.serializable<rrr::BufferSource@rrr.serializable>::SourceBaseAdapter(rrr::BufferSource@rrr.serializable)'),
                ('T', 'rrr::SourceBaseAdapter@rrr.serializable<rrr::BufferSource@rrr.serializable>::SourceBaseAdapter(rrr::SourceBaseAdapter@rrr.serializable<rrr::BufferSource@rrr.serializable>&&)'),
                ('T', 'rrr::SourceBaseAdapter@rrr.serializable<rrr::BufferSource@rrr.serializable>::read_bytes(unsigned char*, unsigned long)'),
                ('T', 'rrr::SourceBaseAdapter@rrr.serializable<rrr::FdSource@rrr.serializable>::SourceBaseAdapter(rrr::FdSource@rrr.serializable)'),
                ('T', 'rrr::SourceBaseAdapter@rrr.serializable<rrr::FdSource@rrr.serializable>::SourceBaseAdapter(rrr::SourceBaseAdapter@rrr.serializable<rrr::FdSource@rrr.serializable>&&)'),
                ('T', 'rrr::SourceBaseAdapter@rrr.serializable<rrr::FdSource@rrr.serializable>::read_bytes(unsigned char*, unsigned long)'),
                ('T', 'rrr::SourceBaseAdapterRef@rrr.serializable<rrr::BufferSource@rrr.serializable>::SourceBaseAdapterRef(rrr::BufferSource@rrr.serializable const&)'),
                ('T', 'rrr::SourceBaseAdapterRef@rrr.serializable<rrr::BufferSource@rrr.serializable>::read_bytes(unsigned char*, unsigned long)'),
                ('T', 'rrr::SourceBaseAdapterRef@rrr.serializable<rrr::FdSource@rrr.serializable>::SourceBaseAdapterRef(rrr::FdSource@rrr.serializable const&)'),
                ('T', 'rrr::SourceBaseAdapterRef@rrr.serializable<rrr::FdSource@rrr.serializable>::read_bytes(unsigned char*, unsigned long)'),
                ('T', 'rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::BufferSource@rrr.serializable>::SourceBaseAdapterRefMut(rrr::BufferSource@rrr.serializable&)'),
                ('T', 'rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::BufferSource@rrr.serializable>::read_bytes(unsigned char*, unsigned long)'),
                ('T', 'rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::FdSource@rrr.serializable>::SourceBaseAdapterRefMut(rrr::FdSource@rrr.serializable&)'),
                ('T', 'rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::FdSource@rrr.serializable>::read_bytes(unsigned char*, unsigned long)'),
                ('T', 'rrr::make_sink_proxy@rrr.serializable(rrr::BufferSink@rrr.serializable*)'),
                ('T', 'rrr::make_sink_proxy@rrr.serializable(rrr::FdSink@rrr.serializable*)'),
                ('T', 'rrr::make_source_proxy@rrr.serializable(rrr::BufferSource@rrr.serializable*)'),
                ('T', 'rrr::make_source_proxy@rrr.serializable(rrr::FdSource@rrr.serializable*)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(double&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(int&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(long&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(rrr::v32@rrr.basetypes&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(rrr::v64@rrr.basetypes&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(short&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(signed char&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(unsigned char&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(unsigned int&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(unsigned long&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::deserialize@rrr.serializable(unsigned short&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(double const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(int const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(long const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(rrr::v32@rrr.basetypes const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(rrr::v64@rrr.basetypes const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(short const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(signed char const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(std::__1::basic_string_view<char, std::__1::char_traits<char>> const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(unsigned char const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(unsigned int const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(unsigned long const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::rusty_ext::serialize@rrr.serializable(unsigned short const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
                ('T', 'rrr::serializable_registry_clear_impl@rrr.serializable()'),
                ('T', 'rrr::serializable_registry_create_impl@rrr.serializable(int)'),
                ('T', 'rrr::serializable_registry_is_registered_impl@rrr.serializable(int)'),
                ('T', 'rrr::serializable_registry_register_factory@rrr.serializable(int, rusty::Function<rusty::Arc<rrr::SerializableBase@rrr.serializable> ()>)'),
            }
        ),
    ),
    "rrr.serializable_envelope": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.serializable_envelope;",
                "export template<typename Set, typename Implementor>",
                "struct PayloadMember",
                "export template<typename PayloadSet>",
                "struct SerializableEnvelope",
                "int32_t kind_;",
                "rusty::Option<rusty::Arc<SerializableBase>> inner_;",
                "const SerializableBase* base_ptr() const",
                "void refresh_kind()",
                "bool has_value() const",
                "int32_t kind() const",
                "static SerializableEnvelope<PayloadSet> pack(const T& value)",
                "static SerializableEnvelope<PayloadSet> pack_aliased(rusty::Arc<T> sp)",
                "std::add_pointer_t<std::add_const_t<T>> unpack() const",
                "rusty::Option<rusty::Arc<T>> unpack_shared() const",
                "bool is_a() const",
                "void save(BinaryWriteArchive& ar) const",
                "void load(BinaryReadArchive& ar)",
                "std::add_pointer_t<T> unpack_mut()",
                "SerializableEnvelope<PayloadSet> clone() const",
                "bool operator==(const SerializableEnvelope<PayloadSet>& other) const",
                "rusty::Option<rusty::Arc<T>> marshallable_cast(const SerializableEnvelope<PayloadSet>& env)",
                "void serialize(const SerializableEnvelope<PayloadSet>& env, BinaryWriteArchive& ar)",
                "void deserialize(SerializableEnvelope<PayloadSet>& env, BinaryReadArchive& ar)",
            }
        ),
        symbols=frozenset(),
    ),
    "rrr.future": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.future;",
                "export template<typename T>",
                "struct FiberFuture",
                "struct FiberPromise",
                "rusty::Option<rusty::Arc<BoxEvent<T>>> state_;",
                "FiberFuture()",
                "T get()",
                "bool wait_for(uint64_t timeout_us)",
                "bool is_ready() const",
                "bool valid() const",
                "FiberPromise()",
                "FiberFuture<T> get_future()",
                "void set_value(const T& value)",
                "FiberFuture<T> fiber_promise_get_future(FiberPromise<T>& self_)",
                "std::pair<FiberPromise<T>, FiberFuture<T>> make_promise()",
                "FiberFuture<T> make_ready_future(T value)",
                "FiberFuture already retrieved from FiberPromise",
                "FiberPromise value already set",
            }
        ),
        symbols=frozenset(),
    ),
    "rrr.logging": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.logging;",
                "export struct Log",
                "static constexpr int32_t FATAL = static_cast<int32_t>(0);",
                "static constexpr int32_t ERROR = static_cast<int32_t>(1);",
                "static constexpr int32_t WARN = static_cast<int32_t>(2);",
                "static constexpr int32_t INFO = static_cast<int32_t>(3);",
                "static constexpr int32_t DEBUG = static_cast<int32_t>(4);",
                "static void set_level(int32_t level);",
                "static int32_t level_now();",
                "export std::string_view log_level_tag(int32_t level);",
                "export void log_line(int32_t level, int32_t line, const int8_t* file, const std::string& msg);",
                "export void log_sink_write(const std::string& line);",
                "export std::string log_basename(const int8_t* fpath);",
                "export std::string log_time_now();",
                "logging_ffi::srpc_path_basename(reinterpret_cast<const std::string::value_type*>(fpath))",
                "logging_ffi::srpc_time_now_str(now.data())",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::Log@rrr.logging::level_now()",
                "rrr::Log@rrr.logging::set_level(int)",
                "rrr::log_basename@rrr.logging(signed char const*)",
                "rrr::log_level_tag@rrr.logging(int)",
                "rrr::log_line@rrr.logging(int, int, signed char const*, std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&)",
                "rrr::log_sink_write@rrr.logging(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&)",
                "rrr::log_time_now@rrr.logging()",
            }
        ),
    ),
    "rrr.idempotency": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.idempotency;",
                "export struct IdempotencyKey",
                "uint64_t client_id;",
                "uint64_t sequence;",
                "static IdempotencyKey new_(uint64_t client_id, uint64_t sequence);",
                "static IdempotencyKey empty();",
                "bool is_valid() const;",
                "bool operator==(const IdempotencyKey& other) const;",
                "export struct IdempotencyKeyHash",
                "uint64_t hash_one(const IdempotencyKey& key) const;",
                "export struct IdempotencyConfig",
                "uint64_t ttl_ms;",
                "size_t max_entries;",
                "bool enabled;",
                "static IdempotencyConfig defaults();",
                "static IdempotencyConfig small();",
                "static IdempotencyConfig large();",
                "static IdempotencyConfig disabled();",
                "export struct CachedResponse",
                "rusty::Vec<uint8_t> response_data;",
                "bool is_expired(uint64_t current_time_ms, uint64_t ttl_ms) const;",
                "export struct IdempotencyKeyGenerator",
                "rusty::Cell<uint64_t> client_id_field;",
                "rusty::Cell<uint64_t> sequence_field;",
                "static IdempotencyKeyGenerator new_(uint64_t client_id);",
                "IdempotencyKey next() const;",
                "void set_client_id(uint64_t id) const;",
                "export struct IdempotencyCache",
                "rusty::Cell<IdempotencyConfig> config_;",
                "rusty::Mutex<rusty::VecDeque<CachedResponse>> cache_;",
                "IdempotencyCache();",
                "IdempotencyCache(IdempotencyConfig config);",
                "bool lookup(const IdempotencyKey& key, uint64_t current_time_ms, int32_t& out_error_code, rusty::Vec<uint8_t>& out_response) const;",
                "void store(const IdempotencyKey& key, int32_t error_code, const rusty::Vec<uint8_t>& response, uint64_t current_time_ms) const;",
                "size_t evict_expired(uint64_t current_time_ms) const;",
                "export void serialize(const IdempotencyKey& key, rrr::BinaryWriteArchive& archive)",
                "export void deserialize(IdempotencyKey& key, rrr::BinaryReadArchive& archive)",
                "rusty::wrapping_add(this->timestamp_ms",
                "rusty::wrapping_add(sequence",
                "rusty::wrapping_add(this->misses_.get()",
                "rusty::wrapping_add(this->hits_.get()",
                "rusty::wrapping_add(this->evictions_.get()",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::serialize@rrr.idempotency(rrr::IdempotencyKey@rrr.idempotency const&, rrr::BinaryWriteArchive@rrr.serializable&)",
                "rrr::deserialize@rrr.idempotency(rrr::IdempotencyKey@rrr.idempotency&, rrr::BinaryReadArchive@rrr.serializable&)",
                "rrr::IdempotencyKey@rrr.idempotency::new_(unsigned long, unsigned long)",
                "rrr::IdempotencyKey@rrr.idempotency::empty()",
                "rrr::IdempotencyKey@rrr.idempotency::is_valid() const",
                "rrr::IdempotencyKey@rrr.idempotency::operator==(rrr::IdempotencyKey@rrr.idempotency const&) const",
                "rrr::IdempotencyKeyHash@rrr.idempotency::hash_one(rrr::IdempotencyKey@rrr.idempotency const&) const",
                "rrr::IdempotencyConfig@rrr.idempotency::new_()",
                "rrr::IdempotencyConfig@rrr.idempotency::defaults()",
                "rrr::IdempotencyConfig@rrr.idempotency::small()",
                "rrr::IdempotencyConfig@rrr.idempotency::large()",
                "rrr::IdempotencyConfig@rrr.idempotency::disabled()",
                "rrr::cached_response_set@rrr.idempotency(rrr::CachedResponse@rrr.idempotency&, rusty::port::vec::Vec@vec_port.vec<unsigned char, rusty::alloc::Global> const&)",
                "rrr::cached_response_get@rrr.idempotency(rrr::CachedResponse@rrr.idempotency const&, rusty::port::vec::Vec@vec_port.vec<unsigned char, rusty::alloc::Global>&)",
                "rrr::CachedResponse@rrr.idempotency::is_expired(unsigned long, unsigned long) const",
                "rrr::IdempotencyKeyGenerator@rrr.idempotency::new_(unsigned long)",
                "rrr::IdempotencyKeyGenerator@rrr.idempotency::next() const",
                "rrr::IdempotencyKeyGenerator@rrr.idempotency::client_id() const",
                "rrr::IdempotencyKeyGenerator@rrr.idempotency::set_client_id(unsigned long) const",
                "rrr::IdempotencyKeyGenerator@rrr.idempotency::current_sequence() const",
                "rrr::IdempotencyCache@rrr.idempotency::IdempotencyCache()",
                "rrr::IdempotencyCache@rrr.idempotency::IdempotencyCache(rrr::IdempotencyConfig@rrr.idempotency)",
                "rrr::IdempotencyCache@rrr.idempotency::enabled() const",
                "rrr::IdempotencyCache@rrr.idempotency::config() const",
                "rrr::IdempotencyCache@rrr.idempotency::set_config(rrr::IdempotencyConfig@rrr.idempotency const&) const",
                "rrr::IdempotencyCache@rrr.idempotency::lookup(rrr::IdempotencyKey@rrr.idempotency const&, unsigned long, int&, rusty::port::vec::Vec@vec_port.vec<unsigned char, rusty::alloc::Global>&) const",
                "rrr::IdempotencyCache@rrr.idempotency::store(rrr::IdempotencyKey@rrr.idempotency const&, int, rusty::port::vec::Vec@vec_port.vec<unsigned char, rusty::alloc::Global> const&, unsigned long) const",
                "rrr::IdempotencyCache@rrr.idempotency::remove(rrr::IdempotencyKey@rrr.idempotency const&) const",
                "rrr::IdempotencyCache@rrr.idempotency::clear() const",
                "rrr::IdempotencyCache@rrr.idempotency::size() const",
                "rrr::IdempotencyCache@rrr.idempotency::hits() const",
                "rrr::IdempotencyCache@rrr.idempotency::misses() const",
                "rrr::IdempotencyCache@rrr.idempotency::evictions() const",
                "rrr::IdempotencyCache@rrr.idempotency::hit_rate() const",
                "rrr::IdempotencyCache@rrr.idempotency::reset_stats() const",
                "rrr::IdempotencyCache@rrr.idempotency::evict_expired(unsigned long) const",
            }
        ),
    ),
    "rrr.fiber": AbiSpec(
        surface=frozenset(
            {
                "export module rrr.fiber;",
                "namespace this_fiber",
                "export uint64_t get_id();",
                "export rusty::Option<rusty::Rc<Fiber>> current();",
                "export bool in_fiber_context();",
                "export void yield();",
                "export void sleep_us(uint64_t microseconds);",
                "export void sleep_ms(uint64_t milliseconds);",
                "export void sleep_s(uint64_t seconds);",
                "export void sleep_until_us(uint64_t abs_time_us);",
                "rusty::wrapping_mul(milliseconds",
                "rusty::wrapping_mul(seconds",
                "rrr::fiber_sleep",
            }
        ),
        symbols=frozenset(
            ("T", symbol)
            for symbol in {
                "rrr::this_fiber::current@rrr.fiber()",
                "rrr::this_fiber::get_id@rrr.fiber()",
                "rrr::this_fiber::in_fiber_context@rrr.fiber()",
                "rrr::this_fiber::sleep_ms@rrr.fiber(unsigned long)",
                "rrr::this_fiber::sleep_s@rrr.fiber(unsigned long)",
                "rrr::this_fiber::sleep_until_us@rrr.fiber(unsigned long)",
                "rrr::this_fiber::sleep_us@rrr.fiber(unsigned long)",
                "rrr::this_fiber::yield@rrr.fiber()",
            }
        ),
    ),
    "rrr.misc": AbiSpec(
        surface=frozenset(
            {
                '#include "base/rustc_markers.hpp"',
                "export module rrr.misc;",
                "export class Job;",
                "export struct OneTimeJob;",
                "export class Job",
                "virtual bool Ready() = 0;",
                "virtual void Work() = 0;",
                "virtual bool Done() = 0;",
                "export struct OneTimeJob : public Job",
                "rusty::Function<void()> func_;",
                "static OneTimeJob new_(rusty::Function<void()> func);",
                "export template<typename T, typename T1, typename T2>",
                "T clamp(const T& value, const T1& lower, const T2& upper)",
                "export int32_t get_ncpu();",
                "export std::string format_thousands(double val);",
                "int32_t srpc_get_ncpu();",
                "int32_t srpc_format_fixed_2(double value, int8_t* output, size_t capacity);",
                "namespace Job_",
                "export bool Ready(OneTimeJob& self_)",
                "export void Work(OneTimeJob& self_)",
                "export bool Done(OneTimeJob& self_)",
            }
        ),
        symbols=frozenset(
            {
                ("D", "typeinfo for rrr::Job@rrr.misc"),
                ("D", "typeinfo for rrr::OneTimeJob@rrr.misc"),
                ("D", "vtable for rrr::Job@rrr.misc"),
                ("D", "vtable for rrr::OneTimeJob@rrr.misc"),
                ("R", "typeinfo name for rrr::Job@rrr.misc"),
                ("R", "typeinfo name for rrr::OneTimeJob@rrr.misc"),
                *(
                    ("T", symbol)
                    for symbol in {
                        "rrr::Job@rrr.misc::~Job()",
                        "rrr::Job_::Done@rrr.misc(rrr::OneTimeJob@rrr.misc&)",
                        "rrr::Job_::Ready@rrr.misc(rrr::OneTimeJob@rrr.misc&)",
                        "rrr::Job_::Work@rrr.misc(rrr::OneTimeJob@rrr.misc&)",
                        "rrr::OneTimeJob@rrr.misc::Done()",
                        "rrr::OneTimeJob@rrr.misc::OneTimeJob(bool, bool, rusty::Function<void ()>)",
                        "rrr::OneTimeJob@rrr.misc::OneTimeJob(rrr::OneTimeJob@rrr.misc&&)",
                        "rrr::OneTimeJob@rrr.misc::Ready()",
                        "rrr::OneTimeJob@rrr.misc::Work()",
                        "rrr::OneTimeJob@rrr.misc::new_(rusty::Function<void ()>)",
                        "rrr::format_thousands@rrr.misc(double)",
                        "rrr::get_ncpu@rrr.misc()",
                    }
                ),
            }
        ),
    ),
    "rrr.channel": AbiSpec(
        surface=frozenset(
            {
                "export enum class ChannelError : int32_t;",
                "export struct ChannelFrame;",
                "export class ChannelFactoryBase;",
                "export class ChannelListenerBase;",
                "export class ChannelConnectionBase;",
                "export using ChannelConnectionProxy = rusty::Box<ChannelConnectionBase>;",
                "export std::string_view channel_error_to_string(ChannelError error);",
            }
        ),
        symbols=frozenset(
            {
                ('D', 'typeinfo for rrr::ChannelConnectionBase@rrr.channel'),
                ('D', 'typeinfo for rrr::ChannelFactoryBase@rrr.channel'),
                ('D', 'typeinfo for rrr::ChannelListenerBase@rrr.channel'),
                ('D', 'vtable for rrr::ChannelConnectionBase@rrr.channel'),
                ('D', 'vtable for rrr::ChannelFactoryBase@rrr.channel'),
                ('D', 'vtable for rrr::ChannelListenerBase@rrr.channel'),
                ('R', 'typeinfo name for rrr::ChannelConnectionBase@rrr.channel'),
                ('R', 'typeinfo name for rrr::ChannelFactoryBase@rrr.channel'),
                ('R', 'typeinfo name for rrr::ChannelListenerBase@rrr.channel'),
                ('T', 'rrr::ChannelConnectionBase@rrr.channel::~ChannelConnectionBase()'),
                ('T', 'rrr::ChannelFactoryBase@rrr.channel::~ChannelFactoryBase()'),
                ('T', 'rrr::ChannelListenerBase@rrr.channel::~ChannelListenerBase()'),
                ('T', 'rrr::channel_error_to_string@rrr.channel(rrr::ChannelError@rrr.channel)'),
            }
        ),
    ),
    "rrr.epoll_wrapper": AbiSpec(
        surface=frozenset(
            {
                "export class Pollable;",
                "export struct Epoll;",
                "export extern rusty::sync::atomic::AtomicI32 epoll_remove_count;",
                "export void epoll_bump_remove_count();",
                "export int32_t epoll_open();",
                "int32_t Add(int32_t fd, int32_t poll_mode);",
                "int32_t Remove(int32_t fd);",
                "int32_t Update(int32_t fd, int32_t new_mode, int32_t old_mode);",
            }
        ),
        symbols=frozenset(
            {
                ('D', 'typeinfo for rrr::Pollable@rrr.epoll_wrapper'),
                ('D', 'vtable for rrr::Pollable@rrr.epoll_wrapper'),
                ('R', 'rrr::LINUX_EPOLLERR@rrr.epoll_wrapper'),
                ('R', 'rrr::LINUX_EPOLLHUP@rrr.epoll_wrapper'),
                ('R', 'rrr::LINUX_EPOLLIN@rrr.epoll_wrapper'),
                ('R', 'rrr::LINUX_EPOLLOUT@rrr.epoll_wrapper'),
                ('R', 'rrr::LINUX_EPOLLRDHUP@rrr.epoll_wrapper'),
                ('R', 'rrr::PollMode::NO_CHANGE@rrr.epoll_wrapper'),
                ('R', 'rrr::PollMode::READ@rrr.epoll_wrapper'),
                ('R', 'rrr::PollMode::WRITE@rrr.epoll_wrapper'),
                ('R', 'rrr::PollReady::ERROR@rrr.epoll_wrapper'),
                ('R', 'rrr::PollReady::READABLE@rrr.epoll_wrapper'),
                ('R', 'rrr::PollReady::WRITABLE@rrr.epoll_wrapper'),
                ('R', 'typeinfo name for rrr::Pollable@rrr.epoll_wrapper'),
                ('T', 'rrr::Epoll@rrr.epoll_wrapper::Add(int, int)'),
                ('T', 'rrr::Epoll@rrr.epoll_wrapper::Epoll()'),
                ('T', 'rrr::Epoll@rrr.epoll_wrapper::Remove(int)'),
                ('T', 'rrr::Epoll@rrr.epoll_wrapper::Update(int, int, int)'),
                ('T', 'rrr::Epoll@rrr.epoll_wrapper::fd() const'),
                ('T', 'rrr::EpollWaitEvent@rrr.epoll_wrapper::default_()'),
                ('T', 'rrr::Pollable@rrr.epoll_wrapper::~Pollable()'),
                ('T', 'rrr::epoll_bump_remove_count@rrr.epoll_wrapper()'),
            }
        ),
    ),
    "rrr.pollable_proxy": AbiSpec(
        surface=frozenset(
            {
                "export class PollableBase;",
                "export using PollableProxy = rusty::Box<PollableBase>;",
                "export template<typename T>",
                "PollableProxy make_pollable_proxy_from_typed_arc(rusty::Arc<T> poll);",
                "virtual int32_t fd() const = 0;",
                "virtual int32_t poll_mode() const = 0;",
                "virtual void close() = 0;",
            }
        ),
        symbols=frozenset(
            {
                ('D', 'typeinfo for rrr::PollableBase@rrr.pollable_proxy'),
                ('D', 'vtable for rrr::PollableBase@rrr.pollable_proxy'),
                ('R', 'typeinfo name for rrr::PollableBase@rrr.pollable_proxy'),
                ('T', 'rrr::PollableBase@rrr.pollable_proxy::~PollableBase()'),
            }
        ),
    ),
    "rrr.callbacks": AbiSpec(
        surface=frozenset(
            {
                "export struct ConnectionCallbacks;",
                "export struct CallbackManager;",
                "export using ConnectionCallback = rusty::Arc<rusty::Function<void() const>>;",
                "static ConnectionCallbacks new_();",
                "static CallbackManager new_();",
                "void invoke_on_error(LegacyRpcError error, const std::string& message) const;",
                "size_t callback_count() const;",
            }
        ),
        symbols=frozenset(
            {
                ('T', 'rrr::CallbackManager@rrr.callbacks::add_on_connected(rusty::Function<void () const>) const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::add_on_disconnected(rusty::Function<void () const>) const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::add_on_error(rusty::Function<void (rrr::RpcError@rrr.errors, std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&) const>) const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::add_on_reconnected(rusty::Function<void (bool) const>) const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::add_on_reconnecting(rusty::Function<void () const>) const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::callback_count() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::clear_all() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::has_callbacks() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::inflight_enter() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::inflight_exit() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::invoke_on_connected() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::invoke_on_disconnected() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::invoke_on_error(rrr::RpcError@rrr.errors, std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&) const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::invoke_on_reconnected(bool) const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::invoke_on_reconnecting() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::new_()'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::on_connected_count() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::on_disconnected_count() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::on_error_count() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::on_reconnected_count() const'),
                ('T', 'rrr::CallbackManager@rrr.callbacks::on_reconnecting_count() const'),
                ('T', 'rrr::ConnectionCallbacks@rrr.callbacks::clear()'),
                ('T', 'rrr::ConnectionCallbacks@rrr.callbacks::new_()'),
                ('T', 'rrr::ConnectionCallbacks@rrr.callbacks::total_count() const'),
                ('T', 'rrr::invoke_callback_safely@rrr.callbacks(rusty::Arc<rusty::Function<void () const>> const&)'),
                ('T', 'rrr::invoke_callback_safely@rrr.callbacks(rusty::Arc<rusty::Function<void (bool) const>> const&, bool)'),
                ('T', 'rrr::invoke_callback_safely@rrr.callbacks(rusty::Arc<rusty::Function<void (rrr::RpcError@rrr.errors, std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&) const>> const&, rrr::RpcError@rrr.errors, std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&)'),
            }
        ),
    ),
    "rrr.inmemory_channel": AbiSpec(
        surface=frozenset(
            {
                '#include "base/rustc_markers.hpp"',
                "export struct InMemoryChannel;",
                "export struct InMemorySwitchboard;",
                "export struct InMemoryListener;",
                "export struct InMemoryFactory;",
                "export struct InMemoryChannelShim;",
                "export struct InMemoryListenerShim;",
                "export struct InMemoryFactoryShim;",
                "::rrr::ChannelError send_frame(const ::rrr::ChannelFrame& frame) const;",
                "static InMemorySwitchboard new_();",
            }
        ),
        symbols=frozenset(
            {
                ('T', 'rrr::channel_error_address_in_use@rrr.inmemory_channel()'),
                ('T', 'rrr::channel_error_connection_reset@rrr.inmemory_channel()'),
                ('T', 'rrr::channel_error_from_code@rrr.inmemory_channel(int)'),
                ('T', 'rrr::channel_error_internal@rrr.inmemory_channel()'),
                ('T', 'rrr::channel_error_none@rrr.inmemory_channel()'),
                ('T', 'rrr::empty_connection_inner@rrr.inmemory_channel()'),
                ('T', 'rrr::empty_listener_inner@rrr.inmemory_channel()'),
                ('T', 'rrr::inmemory_listener_listen_with_weak@rrr.inmemory_channel(rrr::InMemoryListener@rrr.inmemory_channel const&, std::__1::basic_string_view<char, std::__1::char_traits<char>>, rusty::Option<rusty::sync::Weak<rrr::InMemoryListener@rrr.inmemory_channel>>)'),
                ('T', 'rrr::make_connection_state@rrr.inmemory_channel(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>, std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>)'),
                ('D', 'typeinfo for rrr::InMemoryChannelShim@rrr.inmemory_channel'),
                ('D', 'typeinfo for rrr::InMemoryFactoryShim@rrr.inmemory_channel'),
                ('D', 'typeinfo for rrr::InMemoryListenerShim@rrr.inmemory_channel'),
                ('D', 'vtable for rrr::InMemoryChannelShim@rrr.inmemory_channel'),
                ('D', 'vtable for rrr::InMemoryFactoryShim@rrr.inmemory_channel'),
                ('D', 'vtable for rrr::InMemoryListenerShim@rrr.inmemory_channel'),
                ('R', 'typeinfo name for rrr::InMemoryChannelShim@rrr.inmemory_channel'),
                ('R', 'typeinfo name for rrr::InMemoryFactoryShim@rrr.inmemory_channel'),
                ('R', 'typeinfo name for rrr::InMemoryListenerShim@rrr.inmemory_channel'),
                ('T', 'rrr::InMemoryChannel@rrr.inmemory_channel::close() const'),
                ('T', 'rrr::InMemoryChannel@rrr.inmemory_channel::flush() const'),
                ('T', 'rrr::InMemoryChannel@rrr.inmemory_channel::is_closed() const'),
                ('T', 'rrr::InMemoryChannel@rrr.inmemory_channel::new_(rusty::Arc<rrr::InMemoryConnectionState@rrr.inmemory_channel>, bool)'),
                ('T', 'rrr::InMemoryChannel@rrr.inmemory_channel::peer_address() const'),
                ('T', 'rrr::InMemoryChannel@rrr.inmemory_channel::send_frame(rrr::ChannelFrame@rrr.channel const&) const'),
                ('T', 'rrr::InMemoryChannel@rrr.inmemory_channel::set_on_closed(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel) const>>) const'),
                ('T', 'rrr::InMemoryChannel@rrr.inmemory_channel::set_on_error(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel, std::__1::basic_string_view<char, std::__1::char_traits<char>>) const>>) const'),
                ('T', 'rrr::InMemoryChannel@rrr.inmemory_channel::set_on_frame(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelFrame@rrr.channel const&) const>>) const'),
                ('T', 'rrr::InMemoryChannelShim@rrr.inmemory_channel::InMemoryChannelShim(rrr::InMemoryChannelShim@rrr.inmemory_channel&&)'),
                ('T', 'rrr::InMemoryChannelShim@rrr.inmemory_channel::InMemoryChannelShim(rusty::Arc<rrr::InMemoryChannel@rrr.inmemory_channel>)'),
                ('T', 'rrr::InMemoryChannelShim@rrr.inmemory_channel::close()'),
                ('T', 'rrr::InMemoryChannelShim@rrr.inmemory_channel::flush()'),
                ('T', 'rrr::InMemoryChannelShim@rrr.inmemory_channel::is_closed() const'),
                ('T', 'rrr::InMemoryChannelShim@rrr.inmemory_channel::peer_address() const'),
                ('T', 'rrr::InMemoryChannelShim@rrr.inmemory_channel::send_frame(rrr::ChannelFrame@rrr.channel const&)'),
                ('T', 'rrr::InMemoryChannelShim@rrr.inmemory_channel::set_on_closed(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel) const>>)'),
                ('T', 'rrr::InMemoryChannelShim@rrr.inmemory_channel::set_on_error(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel, std::__1::basic_string_view<char, std::__1::char_traits<char>>) const>>)'),
                ('T', 'rrr::InMemoryChannelShim@rrr.inmemory_channel::set_on_frame(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelFrame@rrr.channel const&) const>>)'),
                ('T', 'rrr::InMemoryFactory@rrr.inmemory_channel::backend_name() const'),
                ('T', 'rrr::InMemoryFactory@rrr.inmemory_channel::connect(std::__1::basic_string_view<char, std::__1::char_traits<char>>) const'),
                ('T', 'rrr::InMemoryFactory@rrr.inmemory_channel::make_listener() const'),
                ('T', 'rrr::InMemoryFactory@rrr.inmemory_channel::new_(rusty::Arc<rrr::InMemorySwitchboard@rrr.inmemory_channel>)'),
                ('T', 'rrr::InMemoryFactoryShim@rrr.inmemory_channel::InMemoryFactoryShim(rrr::InMemoryFactoryShim@rrr.inmemory_channel&&)'),
                ('T', 'rrr::InMemoryFactoryShim@rrr.inmemory_channel::InMemoryFactoryShim(rusty::Arc<rrr::InMemoryFactory@rrr.inmemory_channel>)'),
                ('T', 'rrr::InMemoryFactoryShim@rrr.inmemory_channel::backend_name() const'),
                ('T', 'rrr::InMemoryFactoryShim@rrr.inmemory_channel::connect(std::__1::basic_string_view<char, std::__1::char_traits<char>>)'),
                ('T', 'rrr::InMemoryFactoryShim@rrr.inmemory_channel::make_listener()'),
                ('T', 'rrr::InMemoryListener@rrr.inmemory_channel::close() const'),
                ('T', 'rrr::InMemoryListener@rrr.inmemory_channel::is_closed() const'),
                ('T', 'rrr::InMemoryListener@rrr.inmemory_channel::listen(std::__1::basic_string_view<char, std::__1::char_traits<char>>) const'),
                ('T', 'rrr::InMemoryListener@rrr.inmemory_channel::local_address() const'),
                ('T', 'rrr::InMemoryListener@rrr.inmemory_channel::new_(rusty::Arc<rrr::InMemorySwitchboard@rrr.inmemory_channel>)'),
                ('T', 'rrr::InMemoryListener@rrr.inmemory_channel::set_on_accept(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rusty::Box<rrr::ChannelConnectionBase@rrr.channel, rusty::alloc::Global>) const>>) const'),
                ('T', 'rrr::InMemoryListener@rrr.inmemory_channel::set_on_error(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel, std::__1::basic_string_view<char, std::__1::char_traits<char>>) const>>) const'),
                ('T', 'rrr::InMemoryListener@rrr.inmemory_channel::set_self_weak(rusty::sync::Weak<rrr::InMemoryListener@rrr.inmemory_channel>)'),
                ('T', 'rrr::InMemoryListenerShim@rrr.inmemory_channel::InMemoryListenerShim(rrr::InMemoryListenerShim@rrr.inmemory_channel&&)'),
                ('T', 'rrr::InMemoryListenerShim@rrr.inmemory_channel::InMemoryListenerShim(rusty::Arc<rrr::InMemoryListener@rrr.inmemory_channel>)'),
                ('T', 'rrr::InMemoryListenerShim@rrr.inmemory_channel::close()'),
                ('T', 'rrr::InMemoryListenerShim@rrr.inmemory_channel::is_closed() const'),
                ('T', 'rrr::InMemoryListenerShim@rrr.inmemory_channel::listen(std::__1::basic_string_view<char, std::__1::char_traits<char>>)'),
                ('T', 'rrr::InMemoryListenerShim@rrr.inmemory_channel::local_address() const'),
                ('T', 'rrr::InMemoryListenerShim@rrr.inmemory_channel::set_on_accept(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rusty::Box<rrr::ChannelConnectionBase@rrr.channel, rusty::alloc::Global>) const>>)'),
                ('T', 'rrr::InMemoryListenerShim@rrr.inmemory_channel::set_on_error(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel, std::__1::basic_string_view<char, std::__1::char_traits<char>>) const>>)'),
                ('T', 'rrr::InMemorySwitchboard@rrr.inmemory_channel::find_listener(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&) const'),
                ('T', 'rrr::InMemorySwitchboard@rrr.inmemory_channel::new_()'),
                ('T', 'rrr::InMemorySwitchboard@rrr.inmemory_channel::register_listener(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>, rusty::sync::Weak<rrr::InMemoryListener@rrr.inmemory_channel>) const'),
                ('T', 'rrr::InMemorySwitchboard@rrr.inmemory_channel::unregister_listener(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&) const'),
                ('T', 'rrr::inmemory_channel_clear_fault_injection@rrr.inmemory_channel(rrr::InMemoryChannel@rrr.inmemory_channel const&)'),
                ('T', 'rrr::inmemory_channel_inject_drop_next_sends@rrr.inmemory_channel(rrr::InMemoryChannel@rrr.inmemory_channel const&, int)'),
                ('T', 'rrr::inmemory_channel_inject_send_error@rrr.inmemory_channel(rrr::InMemoryChannel@rrr.inmemory_channel const&, rrr::ChannelError@rrr.channel, int)'),
                ('T', 'rrr::inmemory_channel_send_frame@rrr.inmemory_channel(rrr::InMemoryChannel@rrr.inmemory_channel const&, rrr::ChannelFrame@rrr.channel const&)'),
                ('T', 'rrr::inmemory_factory_connect@rrr.inmemory_channel(rrr::InMemoryFactory@rrr.inmemory_channel const&, std::__1::basic_string_view<char, std::__1::char_traits<char>>)'),
                ('T', 'rrr::inmemory_factory_make_listener@rrr.inmemory_channel(rrr::InMemoryFactory@rrr.inmemory_channel const&)'),
                ('T', 'rrr::inmemory_listener_accept_for_connect@rrr.inmemory_channel(rrr::InMemoryListener@rrr.inmemory_channel const&, std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&)'),
                ('T', 'rrr::make_channel_pair_for_testing@rrr.inmemory_channel(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>, std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>)'),
                ('T', 'rrr::make_inmemory_channel_proxy@rrr.inmemory_channel(rusty::Arc<rrr::InMemoryChannel@rrr.inmemory_channel>)'),
                ('T', 'rrr::make_inmemory_factory_proxy@rrr.inmemory_channel(rusty::Arc<rrr::InMemoryFactory@rrr.inmemory_channel>)'),
                ('T', 'rrr::make_inmemory_listener_proxy@rrr.inmemory_channel(rusty::Arc<rrr::InMemoryListener@rrr.inmemory_channel>)'),
            }
        ),
    ),
    "rrr.fiber_channel": AbiSpec(
        surface=frozenset(
            {
                "export struct OwnedFrame;",
                "export struct FiberChannel;",
                "explicit FiberChannel(::rrr::ChannelConnectionProxy ch);",
                "rusty::Option<OwnedFrame> recv_frame();",
                "::rrr::ChannelError send_frame(const ::rrr::ChannelFrame& frame);",
                "bool is_closed() const;",
            }
        ),
        symbols=frozenset(
            {
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::FiberChannel(rusty::Box<rrr::ChannelConnectionBase@rrr.channel, rusty::alloc::Global>)'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::arm_waiter()'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::bind_callbacks()'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::channel_for_test()'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::close()'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::is_closed() const'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::on_inbound_closed()'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::on_inbound_frame(rrr::ChannelFrame@rrr.channel const&)'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::recv_frame()'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::rusty_mark_forgotten() const'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::send_frame(rrr::ChannelFrame@rrr.channel const&)'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::signal_pending_recv()'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::try_pop()'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::wait_for_signal()'),
                ('T', 'rrr::FiberChannel@rrr.fiber_channel::~FiberChannel()'),
                ('T', 'rrr::OwnedFrame@rrr.fiber_channel::default_()'),
                ('T', 'rrr::fiberchannel_owned_copy@rrr.fiber_channel(rrr::ChannelFrame@rrr.channel const&)'),
            }
        ),
    ),
    "rrr.threading": AbiSpec(
        surface=frozenset(
            {
                "export struct SpinLock;",
                "export using AtomicBool = rusty::sync::atomic::AtomicBool;",
                "export using Ordering = rusty::sync::atomic::Ordering;",
                "export void cpu_pause();",
                "static SpinLock new_();",
                "void lock() const;",
                "void unlock() const;",
            }
        ),
        symbols=frozenset(
            {
                ('T', 'rrr::Pthread_cond_broadcast@rrr.threading(pthread_cond_t*)'),
                ('T', 'rrr::Pthread_cond_destroy@rrr.threading(pthread_cond_t*)'),
                ('T', 'rrr::Pthread_cond_init@rrr.threading(pthread_cond_t*, pthread_condattr_t const*)'),
                ('T', 'rrr::Pthread_cond_signal@rrr.threading(pthread_cond_t*)'),
                ('T', 'rrr::Pthread_cond_wait@rrr.threading(pthread_cond_t*, pthread_mutex_t*)'),
                ('T', 'rrr::Pthread_mutex_destroy@rrr.threading(pthread_mutex_t*)'),
                ('T', 'rrr::Pthread_mutex_init@rrr.threading(pthread_mutex_t*, pthread_mutexattr_t const*)'),
                ('T', 'rrr::Pthread_mutex_lock@rrr.threading(pthread_mutex_t*)'),
                ('T', 'rrr::Pthread_mutex_unlock@rrr.threading(pthread_mutex_t*)'),
                ('T', 'rrr::Pthread_spin_destroy@rrr.threading(int volatile*)'),
                ('T', 'rrr::Pthread_spin_init@rrr.threading(int volatile*, int)'),
                ('T', 'rrr::Pthread_spin_lock@rrr.threading(int volatile*)'),
                ('T', 'rrr::Pthread_spin_unlock@rrr.threading(int volatile*)'),
                ('T', 'rrr::SpinLock@rrr.threading::lock() const'),
                ('T', 'rrr::SpinLock@rrr.threading::new_()'),
                ('T', 'rrr::SpinLock@rrr.threading::unlock() const'),
                ('T', 'rrr::cpu_pause@rrr.threading()'),
            }
        ),
    ),
    "rrr.debugging": AbiSpec(
        surface=frozenset(
            {
                "export bool likely(bool value);",
                "export bool unlikely(bool value);",
                "export void print_stack_trace(FILE* stream = stderr);",
                "export void verify_failed(std::string_view file, uint32_t line);",
                "static BtCapture new_();",
                "export template<typename Expr>",
            }
        ),
        symbols=frozenset(
            {
                ('T', 'rrr::BtCapture@rrr.debugging::new_()'),
                ('T', 'rrr::bt_capture@rrr.debugging()'),
                ('T', 'rrr::bt_empty_string@rrr.debugging()'),
                ('T', 'rrr::bt_index_prefix@rrr.debugging(int)'),
                ('T', 'rrr::bt_render@rrr.debugging(rrr::BtCapture@rrr.debugging const&)'),
                ('T', 'rrr::likely@rrr.debugging(bool)'),
                ('T', 'rrr::print_stack_trace@rrr.debugging(_IO_FILE*)'),
                ('T', 'rrr::unlikely@rrr.debugging(bool)'),
                ('T', 'rrr::verify_failed@rrr.debugging(std::__1::basic_string_view<char, std::__1::char_traits<char>>, unsigned int)'),
            }
        ),
    ),
    "rrr.any_message": AbiSpec(
        surface=frozenset(
            {
                "export struct AnyMessage;",
                "void save(rrr::BinaryWriteArchive& archive) const;",
                "void load(rrr::BinaryReadArchive& archive);",
                "bool is_a() const;",
                "rusty::Option<rusty::Arc<T>> unpack() const;",
                "static AnyMessage pack_as(std::string name, rusty::Arc<T> value);",
                "static AnyMessage pack(rusty::Arc<T> value);",
                "export void serialize(const AnyMessage& message, rrr::BinaryWriteArchive& archive)",
                "export void deserialize(AnyMessage& message, rrr::BinaryReadArchive& archive)",
            }
        ),
        symbols=frozenset(
            {
                ('T', 'rrr::AnyMessage@rrr.any_message::load(rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::AnyMessage@rrr.any_message::save(rrr::BinaryWriteArchive@rrr.serializable&) const'),
                ('T', 'rrr::any_message_registry::clear_for_testing@rrr.any_message()'),
                ('T', 'rrr::any_message_registry::create@rrr.any_message(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&)'),
                ('T', 'rrr::any_message_registry::is_registered_name@rrr.any_message(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&)'),
                ('T', 'rrr::any_message_registry::is_registered_type@rrr.any_message(std::__1::type_index)'),
                ('T', 'rrr::any_message_registry::name_for_type_owned@rrr.any_message(std::__1::type_index)'),
                ('T', 'rrr::any_message_registry::register_type@rrr.any_message(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>, std::__1::type_index, rusty::Function<rusty::Arc<rrr::SerializableBase@rrr.serializable> ()>)'),
                ('T', 'rrr::deserialize@rrr.any_message(rrr::AnyMessage@rrr.any_message&, rrr::BinaryReadArchive@rrr.serializable&)'),
                ('T', 'rrr::serialize@rrr.any_message(rrr::AnyMessage@rrr.any_message const&, rrr::BinaryWriteArchive@rrr.serializable&)'),
            }
        ),
    ),
    "rrr.tcp_channel": AbiSpec(
        surface=frozenset(
            {
                'export module rrr.tcp_channel;',
                'export struct TcpConnection;',
                'export struct TcpListener;',
                'export struct TcpFactory;',
                'export constexpr size_t kTcpConnectionOutboundHighWaterDefault',
                'export ::rrr::ChannelConnectionProxy make_tcp_connection_channel_proxy(rusty::Arc<TcpConnection> conn) {',
                'export ::rrr::ChannelListenerProxy make_tcp_listener_channel_proxy(rusty::Arc<TcpListener> listener) {',
                'export ::rrr::ChannelFactoryProxy make_tcp_factory_proxy(rusty::Arc<TcpFactory> factory) {',
                'export ::rrr::ConnectResult tcp_factory_connect(const TcpFactory& fac, std::string_view addr) {',
                'export rusty::Option<::rrr::ChannelListenerProxy> tcp_factory_make_listener(const TcpFactory& self_) {',
            }
        ),
        symbols=frozenset(
            {
                ('D', 'typeinfo for rrr::TcpChannelShim@rrr.tcp_channel'),
                ('D', 'typeinfo for rrr::TcpFactoryShim@rrr.tcp_channel'),
                ('D', 'typeinfo for rrr::TcpListenerChannelShim@rrr.tcp_channel'),
                ('D', 'typeinfo for rrr::TcpListenerPollableShim@rrr.tcp_channel'),
                ('D', 'typeinfo for rrr::TcpPollableShim@rrr.tcp_channel'),
                ('D', 'vtable for rrr::TcpChannelShim@rrr.tcp_channel'),
                ('D', 'vtable for rrr::TcpFactoryShim@rrr.tcp_channel'),
                ('D', 'vtable for rrr::TcpListenerChannelShim@rrr.tcp_channel'),
                ('D', 'vtable for rrr::TcpListenerPollableShim@rrr.tcp_channel'),
                ('D', 'vtable for rrr::TcpPollableShim@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_ACCES@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_ADDR_IN_USE@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_ADDR_NOT_AVAILABLE@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_AGAIN@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_BROKEN_PIPE@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_CONNECTION_REFUSED@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_CONNECTION_RESET@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_HOST_UNREACHABLE@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_INTERRUPTED@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_NETWORK_UNREACHABLE@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_NOT_CONNECTED@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_OPERATION_NOT_PERMITTED@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_PROCESS_FD_LIMIT@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_SYSTEM_FD_LIMIT@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_TIMED_OUT@rrr.tcp_channel'),
                ('R', 'rrr::TCP_ERR_WOULD_BLOCK@rrr.tcp_channel'),
                ('R', 'rrr::TCP_MAX_FRAME_PAYLOAD_SIZE@rrr.tcp_channel'),
                ('R', 'rrr::TCP_POLL_NO_CHANGE@rrr.tcp_channel'),
                ('R', 'rrr::TCP_POLL_READ@rrr.tcp_channel'),
                ('R', 'rrr::TCP_POLL_WRITE@rrr.tcp_channel'),
                ('R', 'rrr::kRecvScratchBytes@rrr.tcp_channel'),
                ('R', 'rrr::kTcpConnectionOutboundHighWaterDefault@rrr.tcp_channel'),
                ('R', 'typeinfo name for rrr::TcpChannelShim@rrr.tcp_channel'),
                ('R', 'typeinfo name for rrr::TcpFactoryShim@rrr.tcp_channel'),
                ('R', 'typeinfo name for rrr::TcpListenerChannelShim@rrr.tcp_channel'),
                ('R', 'typeinfo name for rrr::TcpListenerPollableShim@rrr.tcp_channel'),
                ('R', 'typeinfo name for rrr::TcpPollableShim@rrr.tcp_channel'),
                ('T', 'rrr::TcpChannelShim@rrr.tcp_channel::TcpChannelShim(rrr::TcpChannelShim@rrr.tcp_channel&&)'),
                ('T', 'rrr::TcpChannelShim@rrr.tcp_channel::TcpChannelShim(rusty::Arc<rrr::TcpConnection@rrr.tcp_channel>)'),
                ('T', 'rrr::TcpChannelShim@rrr.tcp_channel::close()'),
                ('T', 'rrr::TcpChannelShim@rrr.tcp_channel::flush()'),
                ('T', 'rrr::TcpChannelShim@rrr.tcp_channel::is_closed() const'),
                ('T', 'rrr::TcpChannelShim@rrr.tcp_channel::peer_address() const'),
                ('T', 'rrr::TcpChannelShim@rrr.tcp_channel::send_frame(rrr::ChannelFrame@rrr.channel const&)'),
                ('T', 'rrr::TcpChannelShim@rrr.tcp_channel::set_on_closed(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel) const>>)'),
                ('T', 'rrr::TcpChannelShim@rrr.tcp_channel::set_on_error(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel, std::__1::basic_string_view<char, std::__1::char_traits<char>>) const>>)'),
                ('T', 'rrr::TcpChannelShim@rrr.tcp_channel::set_on_frame(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelFrame@rrr.channel const&) const>>)'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::TcpConnection(int, std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>)'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::check_pending_write_update() const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::close() const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::content_size() const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::fd() const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::flush() const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::handle_error() const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::handle_read() const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::handle_write() const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::is_closed() const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::peer_address() const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::poll_mode() const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::send_frame(rrr::ChannelFrame@rrr.channel const&) const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::set_on_closed(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel) const>>) const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::set_on_error(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel, std::__1::basic_string_view<char, std::__1::char_traits<char>>) const>>) const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::set_on_frame(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelFrame@rrr.channel const&) const>>) const'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::set_outbound_high_water(unsigned long)'),
                ('T', 'rrr::TcpConnection@rrr.tcp_channel::set_poll_thread(rusty::Arc<rrr::PollThread@rrr.reactor>)'),
                ('T', 'rrr::TcpFactory@rrr.tcp_channel::backend_name() const'),
                ('T', 'rrr::TcpFactory@rrr.tcp_channel::connect(std::__1::basic_string_view<char, std::__1::char_traits<char>>) const'),
                ('T', 'rrr::TcpFactory@rrr.tcp_channel::make_listener() const'),
                ('T', 'rrr::TcpFactory@rrr.tcp_channel::new_(rusty::Arc<rrr::PollThread@rrr.reactor>)'),
                ('T', 'rrr::TcpFactory@rrr.tcp_channel::set_connect_timeout_ms(int)'),
                ('T', 'rrr::TcpFactoryShim@rrr.tcp_channel::TcpFactoryShim(rrr::TcpFactoryShim@rrr.tcp_channel&&)'),
                ('T', 'rrr::TcpFactoryShim@rrr.tcp_channel::TcpFactoryShim(rusty::Arc<rrr::TcpFactory@rrr.tcp_channel>)'),
                ('T', 'rrr::TcpFactoryShim@rrr.tcp_channel::backend_name() const'),
                ('T', 'rrr::TcpFactoryShim@rrr.tcp_channel::connect(std::__1::basic_string_view<char, std::__1::char_traits<char>>)'),
                ('T', 'rrr::TcpFactoryShim@rrr.tcp_channel::make_listener()'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::TcpListener()'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::check_pending_write_update() const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::close() const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::content_size() const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::fd() const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::handle_error() const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::handle_read() const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::handle_write() const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::is_closed() const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::listen(std::__1::basic_string_view<char, std::__1::char_traits<char>>) const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::local_address() const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::poll_mode() const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::set_on_accept(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rusty::Box<rrr::ChannelConnectionBase@rrr.channel, rusty::alloc::Global>) const>>) const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::set_on_error(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel, std::__1::basic_string_view<char, std::__1::char_traits<char>>) const>>) const'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::set_poll_thread(rusty::Arc<rrr::PollThread@rrr.reactor>)'),
                ('T', 'rrr::TcpListener@rrr.tcp_channel::set_self_weak(rusty::sync::Weak<rrr::TcpListener@rrr.tcp_channel>)'),
                ('T', 'rrr::TcpListenerChannelShim@rrr.tcp_channel::TcpListenerChannelShim(rrr::TcpListenerChannelShim@rrr.tcp_channel&&)'),
                ('T', 'rrr::TcpListenerChannelShim@rrr.tcp_channel::TcpListenerChannelShim(rusty::Arc<rrr::TcpListener@rrr.tcp_channel>)'),
                ('T', 'rrr::TcpListenerChannelShim@rrr.tcp_channel::close()'),
                ('T', 'rrr::TcpListenerChannelShim@rrr.tcp_channel::is_closed() const'),
                ('T', 'rrr::TcpListenerChannelShim@rrr.tcp_channel::listen(std::__1::basic_string_view<char, std::__1::char_traits<char>>)'),
                ('T', 'rrr::TcpListenerChannelShim@rrr.tcp_channel::local_address() const'),
                ('T', 'rrr::TcpListenerChannelShim@rrr.tcp_channel::set_on_accept(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rusty::Box<rrr::ChannelConnectionBase@rrr.channel, rusty::alloc::Global>) const>>)'),
                ('T', 'rrr::TcpListenerChannelShim@rrr.tcp_channel::set_on_error(rrr::detail::CallbackWrapper@rrr.callback_wrapper<rusty::Function<void (rrr::ChannelError@rrr.channel, std::__1::basic_string_view<char, std::__1::char_traits<char>>) const>>)'),
                ('T', 'rrr::TcpListenerHandleReadScope@rrr.tcp_channel::TcpListenerHandleReadScope(rrr::TcpListenerHandleReadScope@rrr.tcp_channel&&)'),
                ('T', 'rrr::TcpListenerHandleReadScope@rrr.tcp_channel::TcpListenerHandleReadScope(rusty::sync::atomic::detail::Atomic<unsigned int> const*, bool)'),
                ('T', 'rrr::TcpListenerHandleReadScope@rrr.tcp_channel::acquired() const'),
                ('T', 'rrr::TcpListenerHandleReadScope@rrr.tcp_channel::new_(rrr::TcpListener@rrr.tcp_channel const&)'),
                ('T', 'rrr::TcpListenerHandleReadScope@rrr.tcp_channel::operator=(rrr::TcpListenerHandleReadScope@rrr.tcp_channel&&)'),
                ('T', 'rrr::TcpListenerHandleReadScope@rrr.tcp_channel::rusty_mark_forgotten() const'),
                ('T', 'rrr::TcpListenerHandleReadScope@rrr.tcp_channel::~TcpListenerHandleReadScope()'),
                ('T', 'rrr::TcpListenerPollableShim@rrr.tcp_channel::TcpListenerPollableShim(rrr::TcpListenerPollableShim@rrr.tcp_channel&&)'),
                ('T', 'rrr::TcpListenerPollableShim@rrr.tcp_channel::TcpListenerPollableShim(rusty::Arc<rrr::TcpListener@rrr.tcp_channel>)'),
                ('T', 'rrr::TcpListenerPollableShim@rrr.tcp_channel::check_pending_write_update() const'),
                ('T', 'rrr::TcpListenerPollableShim@rrr.tcp_channel::close()'),
                ('T', 'rrr::TcpListenerPollableShim@rrr.tcp_channel::content_size()'),
                ('T', 'rrr::TcpListenerPollableShim@rrr.tcp_channel::fd() const'),
                ('T', 'rrr::TcpListenerPollableShim@rrr.tcp_channel::handle_error()'),
                ('T', 'rrr::TcpListenerPollableShim@rrr.tcp_channel::handle_read()'),
                ('T', 'rrr::TcpListenerPollableShim@rrr.tcp_channel::handle_write()'),
                ('T', 'rrr::TcpListenerPollableShim@rrr.tcp_channel::is_closed() const'),
                ('T', 'rrr::TcpListenerPollableShim@rrr.tcp_channel::poll_mode() const'),
                ('T', 'rrr::TcpPollableShim@rrr.tcp_channel::TcpPollableShim(rrr::TcpPollableShim@rrr.tcp_channel&&)'),
                ('T', 'rrr::TcpPollableShim@rrr.tcp_channel::TcpPollableShim(rusty::Arc<rrr::TcpConnection@rrr.tcp_channel>)'),
                ('T', 'rrr::TcpPollableShim@rrr.tcp_channel::check_pending_write_update() const'),
                ('T', 'rrr::TcpPollableShim@rrr.tcp_channel::close()'),
                ('T', 'rrr::TcpPollableShim@rrr.tcp_channel::content_size()'),
                ('T', 'rrr::TcpPollableShim@rrr.tcp_channel::fd() const'),
                ('T', 'rrr::TcpPollableShim@rrr.tcp_channel::handle_error()'),
                ('T', 'rrr::TcpPollableShim@rrr.tcp_channel::handle_read()'),
                ('T', 'rrr::TcpPollableShim@rrr.tcp_channel::handle_write()'),
                ('T', 'rrr::TcpPollableShim@rrr.tcp_channel::is_closed() const'),
                ('T', 'rrr::TcpPollableShim@rrr.tcp_channel::poll_mode() const'),
                ('T', 'rrr::connect_errno_to_channel_error@rrr.tcp_channel(int)'),
                ('T', 'rrr::io_kind_to_channel_error@rrr.tcp_channel(rusty::io::Error::Kind)'),
                ('T', 'rrr::make_tcp_connection_channel_proxy@rrr.tcp_channel(rusty::Arc<rrr::TcpConnection@rrr.tcp_channel>)'),
                ('T', 'rrr::make_tcp_connection_pollable_proxy@rrr.tcp_channel(rusty::Arc<rrr::TcpConnection@rrr.tcp_channel>)'),
                ('T', 'rrr::make_tcp_factory_proxy@rrr.tcp_channel(rusty::Arc<rrr::TcpFactory@rrr.tcp_channel>)'),
                ('T', 'rrr::make_tcp_listener_channel_proxy@rrr.tcp_channel(rusty::Arc<rrr::TcpListener@rrr.tcp_channel>)'),
                ('T', 'rrr::make_tcp_listener_pollable_proxy@rrr.tcp_channel(rusty::Arc<rrr::TcpListener@rrr.tcp_channel>)'),
                ('T', 'rrr::set_nonblocking_fd@rrr.tcp_channel(int)'),
                ('T', 'rrr::tcp_factory_connect@rrr.tcp_channel(rrr::TcpFactory@rrr.tcp_channel const&, std::__1::basic_string_view<char, std::__1::char_traits<char>>)'),
                ('T', 'rrr::tcp_factory_connect_socket@rrr.tcp_channel(rusty::net::SocketAddrV4, int, rrr::ChannelError@rrr.channel&)'),
                ('T', 'rrr::tcp_factory_make_listener@rrr.tcp_channel(rrr::TcpFactory@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcpconn_append_inbound@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&, unsigned long)'),
                ('T', 'rrr::tcpconn_close@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcpconn_consume_inbound@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcpconn_deliver_on_closed_locked@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&, rrr::ChannelError@rrr.channel)'),
                ('T', 'rrr::tcpconn_drain_outbound_locked@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&, std::__1::vector<unsigned char, std::__1::allocator<unsigned char>>&)'),
                ('T', 'rrr::tcpconn_drop_after_error@rrr.tcp_channel(std::__1::vector<unsigned char, std::__1::allocator<unsigned char>>&, unsigned long)'),
                ('T', 'rrr::tcpconn_errno_to_channel_error@rrr.tcp_channel(int)'),
                ('T', 'rrr::tcpconn_flush@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcpconn_handle_error@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcpconn_handle_read@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcpconn_handle_write@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcpconn_last_errno@rrr.tcp_channel()'),
                ('T', 'rrr::tcpconn_next_frame@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&, rrr::FrameView@rrr.frame_codec&)'),
                ('T', 'rrr::tcpconn_recv_bytes@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&, rrr::RecvScratch@rrr.tcp_channel*)'),
                ('T', 'rrr::tcpconn_reset_fd@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcpconn_reset_inbound@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcpconn_scratch@rrr.tcp_channel()'),
                ('T', 'rrr::tcpconn_send_bytes@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&, std::__1::vector<unsigned char, std::__1::allocator<unsigned char>>&, unsigned long)'),
                ('T', 'rrr::tcpconn_send_frame@rrr.tcp_channel(rrr::TcpConnection@rrr.tcp_channel const&, rrr::ChannelFrame@rrr.channel const&)'),
                ('T', 'rrr::tcpconn_trim_sent@rrr.tcp_channel(std::__1::vector<unsigned char, std::__1::allocator<unsigned char>>&, unsigned long)'),
                ('T', 'rrr::tcplistener_accept_step@rrr.tcp_channel(rrr::TcpListener@rrr.tcp_channel const&, rrr::AcceptStep@rrr.tcp_channel*)'),
                ('T', 'rrr::tcplistener_accept_step_new@rrr.tcp_channel()'),
                ('T', 'rrr::tcplistener_close_accepted@rrr.tcp_channel(rrr::AcceptStep@rrr.tcp_channel&)'),
                ('T', 'rrr::tcplistener_handle_error@rrr.tcp_channel(rrr::TcpListener@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcplistener_handle_read@rrr.tcp_channel(rrr::TcpListener@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcplistener_is_bound@rrr.tcp_channel(rrr::TcpListener@rrr.tcp_channel const&)'),
                ('T', 'rrr::tcplistener_take_proxy@rrr.tcp_channel(rrr::AcceptStep@rrr.tcp_channel&)'),
            }
        ),
    ),
}

# Symbols that a module acquires in the production library from a hand-written
# module *implementation unit* that is not part of the generated crate.
#
# rrr.epoll_wrapper follows Rust std's sys-module pattern: the generated
# .cppm is the interface unit, and reactor/epoll_platform_linux.cc is the
# platform implementation unit that CMake compiles into librrr.a (see the
# "Platform implementation units for rrr.epoll_wrapper" block in
# CMakeLists.txt). Those definitions are therefore legitimately absent from
# the independently compiled crate object and present in production.
#
# This is an exhaustive allowlist, not a relaxation: the crate object must
# still match ABI_SPECS exactly, and the production library must match
# ABI_SPECS plus exactly these entries -- no more, no less.
PLATFORM_IMPL_SYMBOLS = {
    "rrr.epoll_wrapper": frozenset(
        {
            ("T", "rrr::epoll_add_impl@rrr.epoll_wrapper(int, int, int)"),
            ("T", "rrr::epoll_event_zeroed@rrr.epoll_wrapper()"),
            ("T", "rrr::epoll_open@rrr.epoll_wrapper()"),
            ("T", "rrr::epoll_remove_impl@rrr.epoll_wrapper(int, int)"),
            (
                "T",
                "rrr::epoll_update_impl@rrr.epoll_wrapper(int, int, int, int)",
            ),
        }
    ),
}
EXPECTED_TOTAL_PLATFORM_SYMBOLS = 5

# Extra raw entries emitted by the C++ ABI for constructor/destructor aliases.
# Each tuple is one additional occurrence beyond the unique strong symbol in
# ABI_SPECS. Every module also has exactly one module initializer.
RAW_ABI_ALIASES = {
    "rrr.serializable": (
        (
            'T',
            'rrr::Deserialize@rrr.serializable::~Deserialize()',
        ),
        (
            'T',
            'rrr::Deserialize@rrr.serializable::~Deserialize()',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<double>::DeserializeAdapter(double)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<double>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<double>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<int>::DeserializeAdapter(int)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<int>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<int>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<long>::DeserializeAdapter(long)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<long>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<long>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>::DeserializeAdapter(rrr::v32@rrr.basetypes)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>::DeserializeAdapter(rrr::v64@rrr.basetypes)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<short>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<short>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<short>::DeserializeAdapter(short)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<signed char>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<signed char>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<signed char>::DeserializeAdapter(signed char)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::DeserializeAdapter(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<unsigned char>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<unsigned char>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<unsigned char>::DeserializeAdapter(unsigned char)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<unsigned int>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<unsigned int>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<unsigned int>::DeserializeAdapter(unsigned int)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<unsigned long>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<unsigned long>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<unsigned long>::DeserializeAdapter(unsigned long)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<unsigned short>::DeserializeAdapter(rrr::DeserializeAdapter@rrr.serializable<unsigned short>&&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapter@rrr.serializable<unsigned short>::DeserializeAdapter(unsigned short)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<double>::DeserializeAdapterRef(double const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<int>::DeserializeAdapterRef(int const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<long>::DeserializeAdapterRef(long const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>::DeserializeAdapterRef(rrr::v32@rrr.basetypes const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>::DeserializeAdapterRef(rrr::v64@rrr.basetypes const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<short>::DeserializeAdapterRef(short const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<signed char>::DeserializeAdapterRef(signed char const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::DeserializeAdapterRef(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<unsigned char>::DeserializeAdapterRef(unsigned char const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<unsigned int>::DeserializeAdapterRef(unsigned int const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<unsigned long>::DeserializeAdapterRef(unsigned long const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRef@rrr.serializable<unsigned short>::DeserializeAdapterRef(unsigned short const&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<double>::DeserializeAdapterRefMut(double&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<int>::DeserializeAdapterRefMut(int&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<long>::DeserializeAdapterRefMut(long&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>::DeserializeAdapterRefMut(rrr::v32@rrr.basetypes&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>::DeserializeAdapterRefMut(rrr::v64@rrr.basetypes&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<short>::DeserializeAdapterRefMut(short&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<signed char>::DeserializeAdapterRefMut(signed char&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::DeserializeAdapterRefMut(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned char>::DeserializeAdapterRefMut(unsigned char&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned int>::DeserializeAdapterRefMut(unsigned int&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned long>::DeserializeAdapterRefMut(unsigned long&)',
        ),
        (
            'T',
            'rrr::DeserializeAdapterRefMut@rrr.serializable<unsigned short>::DeserializeAdapterRefMut(unsigned short&)',
        ),
        (
            'T',
            'rrr::SerializableBase@rrr.serializable::~SerializableBase()',
        ),
        (
            'T',
            'rrr::SerializableBase@rrr.serializable::~SerializableBase()',
        ),
        (
            'T',
            'rrr::Serialize@rrr.serializable::~Serialize()',
        ),
        (
            'T',
            'rrr::Serialize@rrr.serializable::~Serialize()',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<double>::SerializeAdapter(double)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<double>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<double>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<int>::SerializeAdapter(int)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<int>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<int>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<long>::SerializeAdapter(long)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<long>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<long>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<rrr::v32@rrr.basetypes>::SerializeAdapter(rrr::v32@rrr.basetypes)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<rrr::v64@rrr.basetypes>::SerializeAdapter(rrr::v64@rrr.basetypes)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<short>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<short>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<short>::SerializeAdapter(short)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<signed char>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<signed char>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<signed char>::SerializeAdapter(signed char)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::SerializeAdapter(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>::SerializeAdapter(std::__1::basic_string_view<char, std::__1::char_traits<char>>)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<unsigned char>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<unsigned char>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<unsigned char>::SerializeAdapter(unsigned char)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<unsigned int>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<unsigned int>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<unsigned int>::SerializeAdapter(unsigned int)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<unsigned long>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<unsigned long>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<unsigned long>::SerializeAdapter(unsigned long)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<unsigned short>::SerializeAdapter(rrr::SerializeAdapter@rrr.serializable<unsigned short>&&)',
        ),
        (
            'T',
            'rrr::SerializeAdapter@rrr.serializable<unsigned short>::SerializeAdapter(unsigned short)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<double>::SerializeAdapterRef(double const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<int>::SerializeAdapterRef(int const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<long>::SerializeAdapterRef(long const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<rrr::v32@rrr.basetypes>::SerializeAdapterRef(rrr::v32@rrr.basetypes const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<rrr::v64@rrr.basetypes>::SerializeAdapterRef(rrr::v64@rrr.basetypes const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<short>::SerializeAdapterRef(short const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<signed char>::SerializeAdapterRef(signed char const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::SerializeAdapterRef(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>::SerializeAdapterRef(std::__1::basic_string_view<char, std::__1::char_traits<char>> const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<unsigned char>::SerializeAdapterRef(unsigned char const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<unsigned int>::SerializeAdapterRef(unsigned int const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<unsigned long>::SerializeAdapterRef(unsigned long const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRef@rrr.serializable<unsigned short>::SerializeAdapterRef(unsigned short const&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<double>::SerializeAdapterRefMut(double&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<int>::SerializeAdapterRefMut(int&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<long>::SerializeAdapterRefMut(long&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v32@rrr.basetypes>::SerializeAdapterRefMut(rrr::v32@rrr.basetypes&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<rrr::v64@rrr.basetypes>::SerializeAdapterRefMut(rrr::v64@rrr.basetypes&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<short>::SerializeAdapterRefMut(short&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<signed char>::SerializeAdapterRefMut(signed char&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>>::SerializeAdapterRefMut(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<std::__1::basic_string_view<char, std::__1::char_traits<char>>>::SerializeAdapterRefMut(std::__1::basic_string_view<char, std::__1::char_traits<char>>&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned char>::SerializeAdapterRefMut(unsigned char&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned int>::SerializeAdapterRefMut(unsigned int&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned long>::SerializeAdapterRefMut(unsigned long&)',
        ),
        (
            'T',
            'rrr::SerializeAdapterRefMut@rrr.serializable<unsigned short>::SerializeAdapterRefMut(unsigned short&)',
        ),
        (
            'T',
            'rrr::SinkBase@rrr.serializable::~SinkBase()',
        ),
        (
            'T',
            'rrr::SinkBase@rrr.serializable::~SinkBase()',
        ),
        (
            'T',
            'rrr::SinkBaseAdapter@rrr.serializable<rrr::BufferSink@rrr.serializable>::SinkBaseAdapter(rrr::BufferSink@rrr.serializable)',
        ),
        (
            'T',
            'rrr::SinkBaseAdapter@rrr.serializable<rrr::BufferSink@rrr.serializable>::SinkBaseAdapter(rrr::SinkBaseAdapter@rrr.serializable<rrr::BufferSink@rrr.serializable>&&)',
        ),
        (
            'T',
            'rrr::SinkBaseAdapter@rrr.serializable<rrr::FdSink@rrr.serializable>::SinkBaseAdapter(rrr::FdSink@rrr.serializable)',
        ),
        (
            'T',
            'rrr::SinkBaseAdapter@rrr.serializable<rrr::FdSink@rrr.serializable>::SinkBaseAdapter(rrr::SinkBaseAdapter@rrr.serializable<rrr::FdSink@rrr.serializable>&&)',
        ),
        (
            'T',
            'rrr::SinkBaseAdapterRef@rrr.serializable<rrr::BufferSink@rrr.serializable>::SinkBaseAdapterRef(rrr::BufferSink@rrr.serializable const&)',
        ),
        (
            'T',
            'rrr::SinkBaseAdapterRef@rrr.serializable<rrr::FdSink@rrr.serializable>::SinkBaseAdapterRef(rrr::FdSink@rrr.serializable const&)',
        ),
        (
            'T',
            'rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::BufferSink@rrr.serializable>::SinkBaseAdapterRefMut(rrr::BufferSink@rrr.serializable&)',
        ),
        (
            'T',
            'rrr::SinkBaseAdapterRefMut@rrr.serializable<rrr::FdSink@rrr.serializable>::SinkBaseAdapterRefMut(rrr::FdSink@rrr.serializable&)',
        ),
        (
            'T',
            'rrr::SourceBase@rrr.serializable::~SourceBase()',
        ),
        (
            'T',
            'rrr::SourceBase@rrr.serializable::~SourceBase()',
        ),
        (
            'T',
            'rrr::SourceBaseAdapter@rrr.serializable<rrr::BufferSource@rrr.serializable>::SourceBaseAdapter(rrr::BufferSource@rrr.serializable)',
        ),
        (
            'T',
            'rrr::SourceBaseAdapter@rrr.serializable<rrr::BufferSource@rrr.serializable>::SourceBaseAdapter(rrr::SourceBaseAdapter@rrr.serializable<rrr::BufferSource@rrr.serializable>&&)',
        ),
        (
            'T',
            'rrr::SourceBaseAdapter@rrr.serializable<rrr::FdSource@rrr.serializable>::SourceBaseAdapter(rrr::FdSource@rrr.serializable)',
        ),
        (
            'T',
            'rrr::SourceBaseAdapter@rrr.serializable<rrr::FdSource@rrr.serializable>::SourceBaseAdapter(rrr::SourceBaseAdapter@rrr.serializable<rrr::FdSource@rrr.serializable>&&)',
        ),
        (
            'T',
            'rrr::SourceBaseAdapterRef@rrr.serializable<rrr::BufferSource@rrr.serializable>::SourceBaseAdapterRef(rrr::BufferSource@rrr.serializable const&)',
        ),
        (
            'T',
            'rrr::SourceBaseAdapterRef@rrr.serializable<rrr::FdSource@rrr.serializable>::SourceBaseAdapterRef(rrr::FdSource@rrr.serializable const&)',
        ),
        (
            'T',
            'rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::BufferSource@rrr.serializable>::SourceBaseAdapterRefMut(rrr::BufferSource@rrr.serializable&)',
        ),
        (
            'T',
            'rrr::SourceBaseAdapterRefMut@rrr.serializable<rrr::FdSource@rrr.serializable>::SourceBaseAdapterRefMut(rrr::FdSource@rrr.serializable&)',
        ),
    ),
    "rrr.tcp_channel": (
        (
            'T',
            'rrr::TcpChannelShim@rrr.tcp_channel::TcpChannelShim(rrr::TcpChannelShim@rrr.tcp_channel&&)',
        ),
        (
            'T',
            'rrr::TcpChannelShim@rrr.tcp_channel::TcpChannelShim(rusty::Arc<rrr::TcpConnection@rrr.tcp_channel>)',
        ),
        (
            'T',
            'rrr::TcpConnection@rrr.tcp_channel::TcpConnection(int, std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>)',
        ),
        (
            'T',
            'rrr::TcpFactoryShim@rrr.tcp_channel::TcpFactoryShim(rrr::TcpFactoryShim@rrr.tcp_channel&&)',
        ),
        (
            'T',
            'rrr::TcpFactoryShim@rrr.tcp_channel::TcpFactoryShim(rusty::Arc<rrr::TcpFactory@rrr.tcp_channel>)',
        ),
        (
            'T',
            'rrr::TcpListener@rrr.tcp_channel::TcpListener()',
        ),
        (
            'T',
            'rrr::TcpListenerChannelShim@rrr.tcp_channel::TcpListenerChannelShim(rrr::TcpListenerChannelShim@rrr.tcp_channel&&)',
        ),
        (
            'T',
            'rrr::TcpListenerChannelShim@rrr.tcp_channel::TcpListenerChannelShim(rusty::Arc<rrr::TcpListener@rrr.tcp_channel>)',
        ),
        (
            'T',
            'rrr::TcpListenerHandleReadScope@rrr.tcp_channel::TcpListenerHandleReadScope(rrr::TcpListenerHandleReadScope@rrr.tcp_channel&&)',
        ),
        (
            'T',
            'rrr::TcpListenerHandleReadScope@rrr.tcp_channel::TcpListenerHandleReadScope(rusty::sync::atomic::detail::Atomic<unsigned int> const*, bool)',
        ),
        (
            'T',
            'rrr::TcpListenerHandleReadScope@rrr.tcp_channel::~TcpListenerHandleReadScope()',
        ),
        (
            'T',
            'rrr::TcpListenerPollableShim@rrr.tcp_channel::TcpListenerPollableShim(rrr::TcpListenerPollableShim@rrr.tcp_channel&&)',
        ),
        (
            'T',
            'rrr::TcpListenerPollableShim@rrr.tcp_channel::TcpListenerPollableShim(rusty::Arc<rrr::TcpListener@rrr.tcp_channel>)',
        ),
        (
            'T',
            'rrr::TcpPollableShim@rrr.tcp_channel::TcpPollableShim(rrr::TcpPollableShim@rrr.tcp_channel&&)',
        ),
        (
            'T',
            'rrr::TcpPollableShim@rrr.tcp_channel::TcpPollableShim(rusty::Arc<rrr::TcpConnection@rrr.tcp_channel>)',
        ),
    ),
    "rrr.completion_tracker": (
        (
            "T",
            "rrr::CompletionTracker@rrr.completion_tracker::CompletionTracker()",
        ),
        (
            "T",
            "rrr::CompletionTracker@rrr.completion_tracker::CompletionTracker(rrr::CompletionTrackerConfig@rrr.completion_tracker)",
        ),
    ),
    "rrr.request_queue": (
        ("T", "rrr::RequestQueue@rrr.request_queue::RequestQueue()"),
        (
            "T",
            "rrr::RequestQueue@rrr.request_queue::RequestQueue(rrr::RequestQueueConfig@rrr.request_queue)",
        ),
    ),
    "rrr.utils": tuple(
        ("T", symbol)
        for symbol in (
            "rrr::AddrInfo@rrr.utils::AddrInfo()",
            "rrr::AddrInfo@rrr.utils::AddrInfo(addrinfo*)",
            "rrr::AddrInfo@rrr.utils::AddrInfo(addrinfo*, rusty::Cell<bool>)",
            "rrr::AddrInfo@rrr.utils::AddrInfo(rrr::AddrInfo@rrr.utils&&)",
            "rrr::AddrInfo@rrr.utils::~AddrInfo()",
        )
    ),
    "rrr.idempotency": (
        ("T", "rrr::IdempotencyCache@rrr.idempotency::IdempotencyCache()"),
        (
            "T",
            "rrr::IdempotencyCache@rrr.idempotency::IdempotencyCache(rrr::IdempotencyConfig@rrr.idempotency)",
        ),
    ),
    "rrr.misc": (
        ("T", "rrr::Job@rrr.misc::~Job()"),
        ("T", "rrr::Job@rrr.misc::~Job()"),
        (
            "T",
            "rrr::OneTimeJob@rrr.misc::OneTimeJob(bool, bool, rusty::Function<void ()>)",
        ),
        (
            "T",
            "rrr::OneTimeJob@rrr.misc::OneTimeJob(rrr::OneTimeJob@rrr.misc&&)",
        ),
    ),
    "rrr.channel": tuple(
        ("T", symbol)
        for symbol in (
            "rrr::ChannelFactoryBase@rrr.channel::~ChannelFactoryBase()",
            "rrr::ChannelFactoryBase@rrr.channel::~ChannelFactoryBase()",
            "rrr::ChannelListenerBase@rrr.channel::~ChannelListenerBase()",
            "rrr::ChannelListenerBase@rrr.channel::~ChannelListenerBase()",
            "rrr::ChannelConnectionBase@rrr.channel::~ChannelConnectionBase()",
            "rrr::ChannelConnectionBase@rrr.channel::~ChannelConnectionBase()",
        )
    ),
    "rrr.epoll_wrapper": tuple(
        ("T", symbol)
        for symbol in (
            "rrr::Epoll@rrr.epoll_wrapper::Epoll()",
            "rrr::Pollable@rrr.epoll_wrapper::~Pollable()",
            "rrr::Pollable@rrr.epoll_wrapper::~Pollable()",
        )
    ),
    "rrr.pollable_proxy": (
        ("T", "rrr::PollableBase@rrr.pollable_proxy::~PollableBase()"),
        ("T", "rrr::PollableBase@rrr.pollable_proxy::~PollableBase()"),
    ),
    "rrr.inmemory_channel": tuple(
        ("T", symbol)
        for symbol in (
            "rrr::InMemoryChannelShim@rrr.inmemory_channel::InMemoryChannelShim(rusty::Arc<rrr::InMemoryChannel@rrr.inmemory_channel>)",
            "rrr::InMemoryChannelShim@rrr.inmemory_channel::InMemoryChannelShim(rrr::InMemoryChannelShim@rrr.inmemory_channel&&)",
            "rrr::InMemoryFactoryShim@rrr.inmemory_channel::InMemoryFactoryShim(rusty::Arc<rrr::InMemoryFactory@rrr.inmemory_channel>)",
            "rrr::InMemoryFactoryShim@rrr.inmemory_channel::InMemoryFactoryShim(rrr::InMemoryFactoryShim@rrr.inmemory_channel&&)",
            "rrr::InMemoryListenerShim@rrr.inmemory_channel::InMemoryListenerShim(rusty::Arc<rrr::InMemoryListener@rrr.inmemory_channel>)",
            "rrr::InMemoryListenerShim@rrr.inmemory_channel::InMemoryListenerShim(rrr::InMemoryListenerShim@rrr.inmemory_channel&&)",
        )
    ),
    "rrr.fiber_channel": (
        (
            "T",
            "rrr::FiberChannel@rrr.fiber_channel::FiberChannel(rusty::Box<rrr::ChannelConnectionBase@rrr.channel, rusty::alloc::Global>)",
        ),
        ("T", "rrr::FiberChannel@rrr.fiber_channel::~FiberChannel()"),
    ),
}


class GateError(RuntimeError):
    """A crate generation, compilation, import, or ABI-parity failure."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def executable(root: Path, value: str, description: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or "/" in value:
        resolved = candidate if candidate.is_absolute() else root / candidate
    else:
        found = shutil.which(value)
        if found is None:
            raise GateError(f"{description} is unavailable: {value}")
        resolved = Path(found)
    # Preserve the invoked basename. Clang selects C++ driver behavior from
    # argv[0], and resolving a `clang++ -> clang-N` symlink silently drops the
    # implicit C++ standard-library link in the direct gate commands.
    resolved = Path(os.path.abspath(resolved))
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise GateError(f"{description} is unavailable: {resolved}")
    return resolved


def run(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        diagnostic = (completed.stdout + completed.stderr).strip()
        rendered = " ".join(command)
        raise GateError(
            f"command failed with exit {completed.returncode}: {rendered}\n{diagnostic}"
        )
    return completed.stdout


def git_output(cwd: Path, arguments: list[str], description: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise GateError(f"cannot inspect {description}: {exc}") from exc
    if completed.returncode != 0:
        diagnostic = (completed.stdout + completed.stderr).strip()
        raise GateError(f"cannot inspect {description}: {diagnostic}")
    return completed.stdout.strip()


def verify_transpiler_build_info(root: Path, transpiler: Path) -> None:
    try:
        completed = subprocess.run(
            [str(transpiler), "--build-info"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise GateError(
            f"cannot read rusty-cpp transpiler build info: {exc}"
        ) from exc
    if completed.returncode != 0:
        diagnostic = (completed.stdout + completed.stderr).strip()
        raise GateError(
            "rusty-cpp transpiler --build-info failed with exit "
            f"{completed.returncode}: {diagnostic}"
        )
    lines = completed.stdout.splitlines()
    if len(lines) != 1:
        raise GateError(
            "rusty-cpp transpiler --build-info must emit exactly one JSON line"
        )
    try:
        build_info = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise GateError(
            f"rusty-cpp transpiler --build-info emitted invalid JSON: {exc}"
        ) from exc
    if not isinstance(build_info, dict) or set(build_info) != {
        "git_hash",
        "git_dirty",
    }:
        raise GateError(
            "rusty-cpp transpiler --build-info JSON keys must be exactly "
            "git_hash and git_dirty"
        )
    if build_info["git_hash"] != REQUIRED_RUSTY_CPP_COMMIT:
        raise GateError(
            "rusty-cpp transpiler build commit mismatch: "
            f"expected {REQUIRED_RUSTY_CPP_COMMIT}, "
            f"got {build_info['git_hash']!r}"
        )
    if build_info["git_dirty"] is not False:
        raise GateError("rusty-cpp transpiler build must report git_dirty=false")


def verify_pinned_toolchain(root: Path, transpiler: Path) -> None:
    index_entry = git_output(
        root,
        ["ls-files", "--stage", "--", RUSTY_CPP_SUBMODULE],
        "rusty-cpp gitlink",
    ).split()
    if (
        len(index_entry) < 3
        or index_entry[0] != "160000"
        or index_entry[1] != REQUIRED_RUSTY_CPP_COMMIT
    ):
        actual = index_entry[1] if len(index_entry) >= 2 else "missing"
        raise GateError(
            "rusty-cpp gitlink pin mismatch: "
            f"expected {REQUIRED_RUSTY_CPP_COMMIT}, got {actual}"
        )
    submodule = root / RUSTY_CPP_SUBMODULE
    head = git_output(submodule, ["rev-parse", "HEAD"], "rusty-cpp HEAD")
    if head != REQUIRED_RUSTY_CPP_COMMIT:
        raise GateError(
            "rusty-cpp submodule HEAD mismatch: "
            f"expected {REQUIRED_RUSTY_CPP_COMMIT}, got {head}"
        )
    dirty = git_output(
        submodule,
        ["status", "--porcelain", "--untracked-files=no"],
        "rusty-cpp worktree",
    )
    if dirty:
        raise GateError("rusty-cpp submodule has tracked local changes")
    verify_transpiler_build_info(root, transpiler)


def require_extraction_check(root: Path, transpiler: Path) -> None:
    run(
        [
            sys.executable,
            EXTRACTION_DRIVER,
            "--check",
            "--transpiler",
            str(transpiler),
        ],
        root,
    )


def load_owned_modules(root: Path) -> list[extraction.ModuleEntry]:
    try:
        modules = extraction.load_manifest(root, root / EXTRACTION_MANIFEST)
    except extraction.ExtractionError as exc:
        raise GateError(f"cannot load extraction ownership: {exc}") from exc
    actual = {module.cpp_module for module in modules}
    expected = set(ABI_SPECS)
    ratchet_maps = {
        "ABI specification": expected,
        "direct-import specification": set(EXPECTED_IMPORTS),
        "generated-module digest": set(EXPECTED_GENERATED_MODULE_SHA256),
        "combined-importer use marker": set(IMPORTER_USE_MARKERS),
    }
    if any(names != actual for names in ratchet_maps.values()):
        details = ["crate-mode ABI ratchet does not match extraction manifest"]
        for description, names in ratchet_maps.items():
            if names - actual:
                details.append(
                    f"{description} has non-manifest module(s): "
                    + ", ".join(sorted(names - actual))
                )
            if actual - names:
                details.append(
                    f"missing {description}(s): "
                    + ", ".join(sorted(actual - names))
                )
        raise GateError("\n".join(details))
    return modules


def read_generated(path: Path, description: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError(f"missing generated {description} {path}: {exc}") from exc
    # A generated module's global module fragment may contain compiler-owned
    # runtime support.  Some of those helpers use "unsupported" in legitimate
    # diagnostics (for example, an unreachable conversion branch), so it is
    # not sound to classify that fixed support text as an unimplemented user
    # lowering.  Generated Rust declarations and definitions live in the
    # named-module purview; keep the strict placeholder ratchet there.  Files
    # without a named-module declaration (the crate root CMake file included)
    # are still checked in full.
    module_declaration = re.search(r"^export module [^;\n]+;[ \t]*$", text, re.MULTILINE)
    placeholder_region = (
        text[module_declaration.start() :] if module_declaration is not None else text
    )
    # Drop the one allowlisted compiler diagnostic form (see
    # BENIGN_GENERATED_DIAGNOSTIC) before applying the strict token ratchet.
    # Only that exact comment line is removed, so any other TODO/UNSUPPORTED/
    # skipped text -- including a differently worded by-value-cycle marker --
    # still fails the gate.
    placeholder_region = BENIGN_GENERATED_DIAGNOSTIC.sub("", placeholder_region)
    placeholder = PLACEHOLDER.search(placeholder_region)
    if placeholder is not None:
        raise GateError(
            f"generated {description} contains placeholder marker "
            f"{placeholder.group(0)!r}: {path}"
        )
    return text


def require_exact_module_imports(
    text: str, module_name: str, expected: list[str]
) -> None:
    """Require the exact private named-module dependencies of a child."""

    matches = re.findall(
        r"^(export )?import ([^;\n]+);[ \t]*$",
        text,
        flags=re.MULTILINE,
    )
    actual = [imported for _, imported in matches]
    exported = [imported for prefix, imported in matches if prefix]
    if actual != expected or exported:
        raise GateError(
            f"generated {module_name} module private imports must be exactly "
            f"{expected!r}; got {actual!r}, exported={exported!r}"
        )


def require_cpp_surfaces(
    root: Path, output: Path, modules: list[extraction.ModuleEntry]
) -> None:
    expected_files = {f"{module.cpp_module}.cppm" for module in modules}
    expected_files.add("rrr.cppm")
    actual_files = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*.cppm")
        if path.is_file()
    }
    if actual_files != expected_files:
        raise GateError(
            "generated C++ module census mismatch: expected "
            f"{sorted(expected_files)!r}, got {sorted(actual_files)!r}"
        )

    runtime_facade_output = output / "rusty"
    if runtime_facade_output.exists():
        raise GateError(
            "rustc-only rusty runtime facade leaked into generated C++ output"
        )
    generated_cmake = read_generated(output / "CMakeLists.txt", "crate CMake file")
    for forbidden in ("rusty/rusty.cppm", "add_subdirectory(rusty"):
        if forbidden in generated_cmake:
            raise GateError(
                "rustc-only rusty runtime facade leaked into generated CMake: "
                f"{forbidden!r}"
            )

    digest_drift: list[tuple[str, str, str]] = []
    for module in modules:
        path = output / f"{module.cpp_module}.cppm"
        text = read_generated(path, f"child module {module.cpp_module}")
        # ADVISORY ONLY -- deliberately not a gate failure.
        #
        # This map used to enforce byte-identical generated C++. The project's
        # acceptance rule has since been changed by the user: acceptance is
        # "builds + tests pass + equivalent public function surface", and byte
        # drift explicitly does not count. Enforcing the digest turned every
        # compiler improvement into a red gate for output that is semantically
        # identical -- the codegen fixes on the pinned rusty-cpp commit moved
        # 20 of these 32 digests while leaving 30 of 32 module ABIs
        # bit-for-bit equal, which is exactly the failure mode the repeal
        # targets. The map and its computation are retained so real drift is
        # still visible and reviewable; the enforcing checks are the ABI
        # symbol sets, the direct-import graph, and importer coverage below.
        actual_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        expected_digest = EXPECTED_GENERATED_MODULE_SHA256[module.cpp_module]
        if actual_digest != expected_digest:
            digest_drift.append(
                (module.cpp_module, expected_digest, actual_digest)
            )
        require_exact_module_imports(
            text,
            module.cpp_module,
            EXPECTED_IMPORTS[module.cpp_module],
        )
        required_surface = {
            f"export module {module.cpp_module};",
            "namespace rrr {",
        }
        required_surface.update(ABI_SPECS[module.cpp_module].surface)
        missing = sorted(
            fragment for fragment in required_surface if fragment not in text
        )
        if missing:
            raise GateError(
                f"generated module {module.cpp_module} is missing required surface:\n  "
                + "\n  ".join(missing)
            )

        if "namespace rrr::" in text:
            raise GateError(
                f"generated module {module.cpp_module} drifted to a nested namespace"
            )
        atomic_preamble = "#include <rusty/sync/atomic.hpp>"
        # Source of truth is the checked-in module-preambles.toml: exactly the
        # modules that declare rusty/sync/atomic.hpp there may carry it. This
        # list had fallen behind that manifest -- rrr.threading and
        # rrr.epoll_wrapper both declare the atomic preamble and legitimately
        # emit it -- which made a correct generator look like preamble leakage.
        atomic_modules = {
            "rrr.basetypes",
            "rrr.connection_metrics",
            "rrr.completion_tracker",
            "rrr.threading",
            "rrr.epoll_wrapper",
        }
        if module.cpp_module in atomic_modules:
            if text.count(atomic_preamble) != 1:
                raise GateError(
                    f"generated {module.cpp_module} must contain exactly one "
                    "structured atomic preamble include"
                )
            ordered = (
                text.find("\nmodule;\n"),
                text.find(atomic_preamble),
                text.find("#include <cstdint>"),
                text.find(f"export module {module.cpp_module};"),
            )
            if -1 in ordered or list(ordered) != sorted(ordered):
                raise GateError(
                    f"generated {module.cpp_module} atomic preamble is not "
                    "between the global module fragment and standard includes"
                )
        elif atomic_preamble in text:
            raise GateError(
                f"atomic module preamble leaked into {module.cpp_module}"
            )

        rand_preamble = '#include "misc/srpc_rand.h"'
        if module.cpp_module == "rrr.rand":
            require_exact_module_imports(text, "rrr.rand", ["rusty"])
            if text.count(rand_preamble) != 1:
                raise GateError(
                    "generated rrr.rand must contain exactly one structured "
                    "C-kernel preamble include"
                )
            ordered = (
                text.find("\nmodule;\n"),
                text.find(rand_preamble),
                text.find("#include <cstdint>"),
                text.find("export module rrr.rand;"),
            )
            if -1 in ordered or list(ordered) != sorted(ordered):
                raise GateError(
                    "generated rrr.rand C-kernel preamble is not between the "
                    "global module fragment and standard includes"
                )
            if "std::abort()" in text:
                raise GateError(
                    "generated rrr.rand hard-aborts a Rust assertion instead "
                    "of preserving panic/unwind failure semantics"
                )
        elif rand_preamble in text:
            raise GateError(
                f"rand C-kernel preamble leaked into {module.cpp_module}"
            )

        timing_preamble = '#include "misc/srpc_timing.h"'
        # module-preambles.toml is the source of truth; rrr.threading also
        # declares (and legitimately emits) the timing C kernel.
        timing_modules = {
            "rrr.basetypes",
            "rrr.circuit_breaker",
            "rrr.threading",
        }
        if module.cpp_module in timing_modules:
            # Re-assert the module's ratcheted imports rather than a duplicated
            # literal. This branch hard-coded [] , which happened to hold for
            # the two modules it originally covered but is not a property of
            # carrying the timing kernel (rrr.threading imports rrr.debugging).
            require_exact_module_imports(
                text, module.cpp_module, EXPECTED_IMPORTS[module.cpp_module]
            )
            if text.count(timing_preamble) != 1:
                raise GateError(
                    f"generated {module.cpp_module} must contain exactly one "
                    "structured timing-kernel preamble include"
                )
            ordered = (
                text.find("\nmodule;\n"),
                text.find(timing_preamble),
                text.find("#include <cstdint>"),
                text.find(f"export module {module.cpp_module};"),
            )
            if -1 in ordered or list(ordered) != sorted(ordered):
                raise GateError(
                    f"generated {module.cpp_module} timing preamble is not "
                    "between the global module fragment and standard includes"
                )
        elif timing_preamble in text:
            raise GateError(
                f"timing C-kernel preamble leaked into {module.cpp_module}"
            )

        if module.cpp_module == "rrr.request_options":
            require_exact_module_imports(
                text, "rrr.request_options", ["rrr.rand"]
            )
            for forbidden in (
                "namespace rand =",
                "using ::rand::",
                "using ::rrr::rand::",
                "using ::rrr::randgen_",
            ):
                if forbidden in text:
                    raise GateError(
                        "generated request-options private flat import leaked "
                        f"an alias/using surface: {forbidden!r}"
                    )
        elif module.cpp_module == "rrr.reconnect_policy":
            require_exact_module_imports(
                text, "rrr.reconnect_policy", ["rrr.rand"]
            )
            for forbidden in (
                "namespace rand =",
                "using ::rand::",
                "using ::rrr::rand::",
                "using ::rrr::randgen_",
            ):
                if forbidden in text:
                    raise GateError(
                        "generated reconnect-policy private flat import leaked "
                        f"an alias/using surface: {forbidden!r}"
                    )

        netdb_preamble = "#include <netdb.h>"
        if module.cpp_module == "rrr.connection_state":
            require_exact_module_imports(text, "rrr.connection_state", [])
        elif module.cpp_module == "rrr.heartbeat":
            require_exact_module_imports(
                text, "rrr.heartbeat", ["rrr.circuit_breaker"]
            )
        elif module.cpp_module == "rrr.request_queue":
            require_exact_module_imports(
                text, "rrr.request_queue", ["rusty", "rrr.circuit_breaker"]
            )
        elif module.cpp_module == "rrr.load_balancer":
            require_exact_module_imports(text, "rrr.load_balancer", [])
            live_cpp = "\n".join(
                line
                for line in text.splitlines()
                if not line.lstrip().startswith("//")
            )
            for forbidden in (
                "rusty::LoadBalancerClient",
                "rusty::LoadBalancerMetrics",
                "rusty::LoadBalancerClientHandle",
                "rusty::LoadBalancerClientVec",
                "requires ",
            ):
                if forbidden in live_cpp:
                    raise GateError(
                        "rustc-only load-balancer facade leaked into generated "
                        f"C++: {forbidden!r}"
                    )
        elif module.cpp_module == "rrr.utils":
            require_exact_module_imports(text, "rrr.utils", ["rrr.logging"])
            if text.count(netdb_preamble) != 1:
                raise GateError(
                    "generated rrr.utils must contain exactly one structured "
                    "netdb preamble include"
                )
            ordered = (
                text.find("\nmodule;\n"),
                text.find(netdb_preamble),
                text.find("#include <cstdint>"),
                text.find("export module rrr.utils;"),
            )
            if -1 in ordered or list(ordered) != sorted(ordered):
                raise GateError(
                    "generated rrr.utils netdb preamble is not between the "
                    "global module fragment and standard includes"
                )
            for forbidden in (
                "export import rrr.logging;",
                "namespace logging =",
                "using ::rrr::log_line",
                "rrr::logging::log_line",
            ):
                if forbidden in text:
                    raise GateError(
                        "generated utils private indexed import leaked or "
                        f"misresolved its surface: {forbidden!r}"
                    )
        elif module.cpp_module == "rrr.frame_codec":
            require_exact_module_imports(
                text, "rrr.frame_codec", ["rrr.internal_protocol"]
            )
            frame_preambles = ("#include <vector>", "#include <rusty/io.hpp>")
            for preamble in frame_preambles:
                if text.count(preamble) != 1:
                    raise GateError(
                        "generated rrr.frame_codec must contain exactly one "
                        f"structured preamble include {preamble!r}"
                    )
            ordered = (
                text.find("\nmodule;\n"),
                text.find(frame_preambles[0]),
                text.find(frame_preambles[1]),
                text.find("#include <cstdint>"),
                text.find("export module rrr.frame_codec;"),
            )
            if -1 in ordered or list(ordered) != sorted(ordered):
                raise GateError(
                    "generated rrr.frame_codec structured preambles are not "
                    "ordered between the global fragment and standard includes"
                )
            if "rusty::StdVector" in text:
                raise GateError(
                    "rustc-only StdVector facade leaked into generated FrameCodec"
                )
        marker_preamble = '#include "base/rustc_markers.hpp"'
        # module-preambles.toml is the source of truth; rrr.tcp_channel is
        # the third declared owner of the rustc-marker preamble.
        marker_preamble_owners = {
            "rrr.misc",
            "rrr.inmemory_channel",
            "rrr.tcp_channel",
        }
        if module.cpp_module in marker_preamble_owners:
            if text.count(marker_preamble) != 1:
                raise GateError(
                    f"generated {module.cpp_module} must contain exactly one structured "
                    "rustc-marker preamble include"
                )
            ordered = (
                text.find("\nmodule;\n"),
                text.find(marker_preamble),
                text.find("#include <cstdint>"),
                text.find(f"export module {module.cpp_module};"),
            )
            if -1 in ordered or list(ordered) != sorted(ordered):
                raise GateError(
                    f"generated {module.cpp_module} marker preamble is not between the "
                    "global module fragment and standard includes"
                )
        elif marker_preamble in text:
            raise GateError(
                f"rustc-marker preamble leaked into {module.cpp_module}"
            )

        # Leakage checks must be independent of module-specific dependency
        # handling above. Keeping them in that if/elif chain allowed several
        # enumerated siblings to bypass the rejection clauses.
        if module.cpp_module != "rrr.utils" and netdb_preamble in text:
            raise GateError(
                f"utils netdb preamble leaked into {module.cpp_module}"
            )
        if (
            module.cpp_module != "rrr.frame_codec"
            and "#include <rusty/io.hpp>" in text
        ):
            raise GateError(
                f"FrameCodec io preamble leaked into {module.cpp_module}"
            )

    root_text = read_generated(output / "rrr.cppm", "root module")
    if "#include <rusty/sync/atomic.hpp>" in root_text:
        raise GateError("atomic module preamble leaked into the crate root")
    if '#include "misc/srpc_rand.h"' in root_text:
        raise GateError("rand C-kernel preamble leaked into the crate root")
    if '#include "misc/srpc_timing.h"' in root_text:
        raise GateError("timing C-kernel preamble leaked into the crate root")
    if "#include <netdb.h>" in root_text:
        raise GateError("utils netdb preamble leaked into the crate root")
    if "#include <rusty/io.hpp>" in root_text:
        raise GateError("FrameCodec io preamble leaked into the crate root")
    if '#include "base/rustc_markers.hpp"' in root_text:
        raise GateError("misc rustc-marker preamble leaked into the crate root")
    root_required = {
        "export module rrr;",
        "namespace rrr {",
        *(f"export import {module.cpp_module};" for module in modules),
    }
    root_missing = sorted(
        fragment for fragment in root_required if fragment not in root_text
    )
    if root_missing:
        raise GateError(
            "generated root module is missing required surface:\n  "
            + "\n  ".join(root_missing)
        )
    root_imports = re.findall(
        r"^(export )?import ([^;\n]+);[ \t]*$",
        root_text,
        flags=re.MULTILINE,
    )
    expected_root_imports = [
        ("export ", name) for name in sorted(module.cpp_module for module in modules)
    ]
    if root_imports != expected_root_imports:
        raise GateError(
            "generated root imports must be exactly the ordered canonical "
            f"child re-exports; expected={expected_root_imports!r}, "
            f"got={root_imports!r}"
        )

    # Advisory report only. Byte-identity was repealed as an acceptance
    # criterion (see EXPECTED_GENERATED_MODULE_SHA256); drift is surfaced for
    # review but the hard gates are the ABI, import-graph and importer checks.
    if digest_drift:
        print(
            f"ADVISORY: {len(digest_drift)} of {len(modules)} generated module "
            "digests differ from the recorded baseline (not a gate failure; "
            "byte-identity is no longer an acceptance criterion):"
        )
        for module_name, expected_digest, actual_digest in digest_drift:
            print(f"  {module_name}: {expected_digest} -> {actual_digest}")


def require_zero_hand_slots(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError(f"missing generated hand-slot manifest {path}: {exc}") from exc
    if not re.search(
        r"^0 slot\(s\) requiring hand-attention across 0 file\(s\)\.$",
        text,
        re.MULTILINE,
    ):
        raise GateError(f"generated crate does not report zero hand slots: {path}")


def module_symbols(
    nm: Path,
    root: Path,
    binary: Path,
    module_name: str,
) -> set[tuple[str, str]]:
    output = run(
        [str(nm), "--defined-only", "--demangle", str(binary)],
        root,
    )
    symbols: set[tuple[str, str]] = set()
    for line in output.splitlines():
        match = NM_LINE.match(line)
        if match is None:
            continue
        kind, symbol = match.groups()
        # Template instantiations and lambda helpers are optimization-sensitive
        # weak implementation details, not the strong module ABI ratcheted here.
        if not kind.isupper() or kind in {"U", "V", "W"}:
            continue
        if symbol_owner_module(symbol) == module_name:
            symbols.add((kind, symbol))
    return symbols


def completion_raw_symbols(
    nm: Path,
    root: Path,
    binary: Path,
) -> list[tuple[str, str]]:
    """Return completion's strong entries without deduplicating aliases."""

    output = run(
        [str(nm), "--defined-only", "--demangle", str(binary)],
        root,
    )
    initializer = "initializer for module rrr.completion_tracker"
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = NM_LINE.match(line)
        if match is None:
            continue
        kind, symbol = match.groups()
        if not kind.isupper() or kind in {"U", "V", "W"}:
            continue
        if (
            symbol_owner_module(symbol) == "rrr.completion_tracker"
            or symbol == initializer
        ):
            entries.append((kind, symbol))
    return entries


def require_completion_raw_symbols(
    description: str,
    entries: list[tuple[str, str]],
) -> None:
    """Pin initializer and constructor aliases as well as the unique API."""

    expected = Counter(ABI_SPECS["rrr.completion_tracker"].symbols)
    expected.update(
        {
            (
                "T",
                "rrr::CompletionTracker@rrr.completion_tracker::"
                "CompletionTracker()",
            ): 1,
            (
                "T",
                "rrr::CompletionTracker@rrr.completion_tracker::"
                "CompletionTracker(rrr::CompletionTrackerConfig@"
                "rrr.completion_tracker)",
            ): 1,
            ("T", "initializer for module rrr.completion_tracker"): 1,
        }
    )
    actual = Counter(entries)
    if actual == expected:
        return
    missing = sorted((expected - actual).elements())
    unexpected = sorted((actual - expected).elements())
    raise GateError(
        f"{description} completion ABI must contain exactly 33 raw strong "
        "entries (30 unique API symbols, two constructor aliases, and the "
        f"module initializer); missing={missing!r}, unexpected={unexpected!r}"
    )


def rand_raw_symbols(
    nm: Path,
    root: Path,
    binary: Path,
) -> list[tuple[str, str]]:
    """Return rand's strong entries, including its module initializer."""

    output = run(
        [str(nm), "--defined-only", "--demangle", str(binary)],
        root,
    )
    initializer = "initializer for module rrr.rand"
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = NM_LINE.match(line)
        if match is None:
            continue
        kind, symbol = match.groups()
        if not kind.isupper() or kind in {"U", "V", "W"}:
            continue
        if symbol_owner_module(symbol) == "rrr.rand" or symbol == initializer:
            entries.append((kind, symbol))
    return entries


def require_rand_raw_symbols(
    description: str,
    entries: list[tuple[str, str]],
) -> None:
    """Pin rand's 12-function ABI and sole module initializer exactly."""

    expected = Counter(ABI_SPECS["rrr.rand"].symbols)
    expected[("T", "initializer for module rrr.rand")] += 1
    actual = Counter(entries)
    if actual == expected:
        return
    missing = sorted((expected - actual).elements())
    unexpected = sorted((actual - expected).elements())
    raise GateError(
        f"{description} rand ABI must contain exactly 13 raw strong entries "
        "(12 API symbols and the module initializer); "
        f"missing={missing!r}, unexpected={unexpected!r}"
    )


def request_options_raw_symbols(
    nm: Path,
    root: Path,
    binary: Path,
) -> list[tuple[str, str]]:
    """Return request-options strong entries, including its initializer."""

    output = run(
        [str(nm), "--defined-only", "--demangle", str(binary)],
        root,
    )
    initializer = "initializer for module rrr.request_options"
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = NM_LINE.match(line)
        if match is None:
            continue
        kind, symbol = match.groups()
        if not kind.isupper() or kind in {"U", "V", "W"}:
            continue
        if (
            symbol_owner_module(symbol) == "rrr.request_options"
            or symbol == initializer
        ):
            entries.append((kind, symbol))
    return entries


def require_request_options_raw_symbols(
    description: str,
    entries: list[tuple[str, str]],
) -> None:
    """Pin request-options' 12-function ABI and initializer exactly."""

    expected = Counter(ABI_SPECS["rrr.request_options"].symbols)
    expected[("T", "initializer for module rrr.request_options")] += 1
    actual = Counter(entries)
    if actual == expected:
        return
    missing = sorted((expected - actual).elements())
    unexpected = sorted((actual - expected).elements())
    raise GateError(
        f"{description} request-options ABI must contain exactly 13 raw strong "
        "entries (12 API symbols and the module initializer); "
        f"missing={missing!r}, unexpected={unexpected!r}"
    )


def reconnect_policy_raw_symbols(
    nm: Path,
    root: Path,
    binary: Path,
) -> list[tuple[str, str]]:
    """Return reconnect-policy strong entries, including its initializer."""

    output = run(
        [str(nm), "--defined-only", "--demangle", str(binary)],
        root,
    )
    initializer = "initializer for module rrr.reconnect_policy"
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = NM_LINE.match(line)
        if match is None:
            continue
        kind, symbol = match.groups()
        if not kind.isupper() or kind in {"U", "V", "W"}:
            continue
        if (
            symbol_owner_module(symbol) == "rrr.reconnect_policy"
            or symbol == initializer
        ):
            entries.append((kind, symbol))
    return entries


def require_reconnect_policy_raw_symbols(
    description: str,
    entries: list[tuple[str, str]],
) -> None:
    """Pin reconnect-policy's 11-function ABI and initializer exactly."""

    expected = Counter(ABI_SPECS["rrr.reconnect_policy"].symbols)
    expected[("T", "initializer for module rrr.reconnect_policy")] += 1
    actual = Counter(entries)
    if actual == expected:
        return
    missing = sorted((expected - actual).elements())
    unexpected = sorted((actual - expected).elements())
    raise GateError(
        f"{description} reconnect-policy ABI must contain exactly 12 raw "
        "strong entries (11 API symbols and the module initializer); "
        f"missing={missing!r}, unexpected={unexpected!r}"
    )


def circuit_breaker_raw_symbols(
    nm: Path,
    root: Path,
    binary: Path,
) -> list[tuple[str, str]]:
    """Return circuit-breaker strong entries, including its initializer."""

    output = run(
        [str(nm), "--defined-only", "--demangle", str(binary)],
        root,
    )
    initializer = "initializer for module rrr.circuit_breaker"
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = NM_LINE.match(line)
        if match is None:
            continue
        kind, symbol = match.groups()
        if not kind.isupper() or kind in {"U", "V", "W"}:
            continue
        if (
            symbol_owner_module(symbol) == "rrr.circuit_breaker"
            or symbol == initializer
        ):
            entries.append((kind, symbol))
    return entries


def require_circuit_breaker_raw_symbols(
    description: str,
    entries: list[tuple[str, str]],
) -> None:
    """Pin circuit-breaker's 20-function ABI and initializer exactly."""

    expected = Counter(ABI_SPECS["rrr.circuit_breaker"].symbols)
    expected[("T", "initializer for module rrr.circuit_breaker")] += 1
    actual = Counter(entries)
    if actual == expected:
        return
    missing = sorted((expected - actual).elements())
    unexpected = sorted((actual - expected).elements())
    raise GateError(
        f"{description} circuit-breaker ABI must contain exactly 21 raw "
        "strong entries (20 API symbols and the module initializer); "
        f"missing={missing!r}, unexpected={unexpected!r}"
    )


def exact_module_raw_symbols(
    nm: Path,
    root: Path,
    binary: Path,
    module_name: str,
) -> list[tuple[str, str]]:
    """Return one module's strong API entries and its sole initializer."""

    output = run(
        [str(nm), "--defined-only", "--demangle", str(binary)],
        root,
    )
    initializer = f"initializer for module {module_name}"
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = NM_LINE.match(line)
        if match is None:
            continue
        kind, symbol = match.groups()
        if not kind.isupper() or kind in {"U", "V", "W"}:
            continue
        if symbol_owner_module(symbol) == module_name or symbol == initializer:
            entries.append((kind, symbol))
    return entries


def require_exact_module_raw_symbols(
    module_name: str,
    description: str,
    entries: list[tuple[str, str]],
) -> None:
    """Pin a module's complete strong API and sole initializer exactly."""

    expected = Counter(ABI_SPECS[module_name].symbols)
    expected[("T", f"initializer for module {module_name}")] += 1
    actual = Counter(entries)
    if actual == expected:
        return
    missing = sorted((expected - actual).elements())
    unexpected = sorted((actual - expected).elements())
    raise GateError(
        f"{description} {module_name} ABI must contain exactly "
        f"{sum(expected.values())} raw strong entries (API symbols and the "
        f"module initializer); missing={missing!r}, unexpected={unexpected!r}"
    )


def require_all_module_raw_symbols(
    module_name: str,
    description: str,
    entries: list[tuple[str, str]],
    extra: frozenset[tuple[str, str]] = frozenset(),
) -> None:
    """Pin every unique API, ABI alias, and sole module initializer.

    `extra` carries a lane's legitimate additions (the platform
    implementation unit's definitions in the production library). A module
    still has exactly one initializer regardless of its TU count.
    """

    expected = Counter(ABI_SPECS[module_name].symbols)
    expected.update(extra)
    expected.update(RAW_ABI_ALIASES.get(module_name, ()))
    expected[("T", f"initializer for module {module_name}")] += 1
    actual = Counter(entries)
    if actual == expected:
        return
    missing = sorted((expected - actual).elements())
    unexpected = sorted((actual - expected).elements())
    raise GateError(
        f"{description} {module_name} raw ABI must contain exactly "
        f"{sum(expected.values())} entries; missing={missing!r}, "
        f"unexpected={unexpected!r}"
    )


def basetypes_raw_symbols(
    nm: Path,
    root: Path,
    binary: Path,
) -> list[tuple[str, str]]:
    """Return basetypes strong entries, including its initializer."""

    output = run(
        [str(nm), "--defined-only", "--demangle", str(binary)],
        root,
    )
    initializer = "initializer for module rrr.basetypes"
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = NM_LINE.match(line)
        if match is None:
            continue
        kind, symbol = match.groups()
        if not kind.isupper() or kind in {"U", "V", "W"}:
            continue
        if symbol_owner_module(symbol) == "rrr.basetypes" or symbol == initializer:
            entries.append((kind, symbol))
    return entries


def require_basetypes_raw_symbols(
    description: str,
    entries: list[tuple[str, str]],
) -> None:
    """Pin basetypes' 28-entry API/data ABI and initializer exactly."""

    expected = Counter(ABI_SPECS["rrr.basetypes"].symbols)
    expected[("T", "initializer for module rrr.basetypes")] += 1
    actual = Counter(entries)
    if actual == expected:
        return
    missing = sorted((expected - actual).elements())
    unexpected = sorted((actual - expected).elements())
    raise GateError(
        f"{description} basetypes ABI must contain exactly 29 raw strong "
        "entries (28 API/data symbols and the module initializer); "
        f"missing={missing!r}, unexpected={unexpected!r}"
    )


def request_queue_raw_symbols(
    nm: Path,
    root: Path,
    binary: Path,
) -> list[tuple[str, str]]:
    """Return request-queue strong entries without constructor deduplication."""

    output = run(
        [str(nm), "--defined-only", "--demangle", str(binary)],
        root,
    )
    initializer = "initializer for module rrr.request_queue"
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = NM_LINE.match(line)
        if match is None:
            continue
        kind, symbol = match.groups()
        if not kind.isupper() or kind in {"U", "V", "W"}:
            continue
        if symbol_owner_module(symbol) == "rrr.request_queue" or symbol == initializer:
            entries.append((kind, symbol))
    return entries


def require_request_queue_raw_symbols(
    description: str,
    entries: list[tuple[str, str]],
) -> None:
    """Pin request-queue's API, constructor aliases, and initializer exactly."""

    default_constructor = (
        "T",
        "rrr::RequestQueue@rrr.request_queue::RequestQueue()",
    )
    config_constructor = (
        "T",
        "rrr::RequestQueue@rrr.request_queue::RequestQueue("
        "rrr::RequestQueueConfig@rrr.request_queue)",
    )
    expected = Counter(ABI_SPECS["rrr.request_queue"].symbols)
    expected[default_constructor] += 1
    expected[config_constructor] += 1
    expected[("T", "initializer for module rrr.request_queue")] += 1
    actual = Counter(entries)
    if actual == expected:
        return
    missing = sorted((expected - actual).elements())
    unexpected = sorted((actual - expected).elements())
    raise GateError(
        f"{description} request-queue ABI must contain exactly 30 raw strong "
        "entries (27 unique provider-owned symbols, two constructor aliases, "
        f"and the module initializer); missing={missing!r}, "
        f"unexpected={unexpected!r}"
    )


def utils_raw_symbols(
    nm: Path,
    root: Path,
    binary: Path,
) -> list[tuple[str, str]]:
    """Return Utils strong entries without constructor/destructor deduplication."""

    return exact_module_raw_symbols(nm, root, binary, "rrr.utils")


def require_utils_raw_symbols(
    description: str,
    entries: list[tuple[str, str]],
) -> None:
    """Pin Utils' API, C++ ctor/dtor aliases, and initializer exactly."""

    aliased = (
        "rrr::AddrInfo@rrr.utils::AddrInfo()",
        "rrr::AddrInfo@rrr.utils::AddrInfo(addrinfo*)",
        "rrr::AddrInfo@rrr.utils::AddrInfo(addrinfo*, rusty::Cell<bool>)",
        "rrr::AddrInfo@rrr.utils::AddrInfo(rrr::AddrInfo@rrr.utils&&)",
        "rrr::AddrInfo@rrr.utils::~AddrInfo()",
    )
    expected = Counter(ABI_SPECS["rrr.utils"].symbols)
    for symbol in aliased:
        expected[("T", symbol)] += 1
    expected[("T", "initializer for module rrr.utils")] += 1
    actual = Counter(entries)
    if actual == expected:
        return
    missing = sorted((expected - actual).elements())
    unexpected = sorted((actual - expected).elements())
    raise GateError(
        f"{description} Utils ABI must contain exactly 17 raw strong "
        "entries (11 unique provider-owned symbols, five C++ ABI aliases, "
        f"and the module initializer); missing={missing!r}, "
        f"unexpected={unexpected!r}"
    )




def function_parameter_open(symbol: str) -> int:
    """Return the outer function-parameter `(`, or the end for a data symbol."""

    close = symbol.rfind(")")
    if close == -1:
        return len(symbol)
    depth = 0
    for index in range(close, -1, -1):
        character = symbol[index]
        if character == ")":
            depth += 1
        elif character == "(":
            depth -= 1
            if depth == 0:
                return index
    return len(symbol)


def is_operator_angle(text: str, index: int) -> bool:
    """Return whether `<` at index spells a C++ operator rather than a template."""

    operator = text.rfind("operator", 0, index + 1)
    if operator == -1:
        return False
    candidate = "".join(text[operator : index + 1].split())
    return any(
        spelling.startswith(candidate)
        for spelling in (
            "operator<",
            "operator<=",
            "operator<=>",
            "operator<<",
            "operator<<=",
        )
    )


def actual_entity_declarator(symbol: str) -> str:
    """Remove parameter and optional return-type text from a demangled symbol."""

    prefix = symbol[: function_parameter_open(symbol)].rstrip()
    angle_depth = 0
    last_separator = -1
    for index, character in enumerate(prefix):
        if character == "<" and not is_operator_angle(prefix, index):
            angle_depth += 1
        elif character == ">" and angle_depth > 0:
            angle_depth -= 1
        elif character.isspace() and angle_depth == 0:
            last_separator = index
    return prefix[last_separator + 1 :]


def top_level_module_attachment(declarator: str) -> str | None:
    """Return the last module attachment outside template arguments."""

    angle_depth = 0
    owner: str | None = None
    for index, character in enumerate(declarator):
        if character == "<" and not is_operator_angle(declarator, index):
            angle_depth += 1
        elif character == ">" and angle_depth > 0:
            angle_depth -= 1
        elif character == "@" and angle_depth == 0:
            end = index + 1
            while end < len(declarator) and (
                declarator[end].isalnum() or declarator[end] in "._"
            ):
                end += 1
            module = declarator[index + 1 : end]
            if module and (
                end == len(declarator) or declarator[end] in "<:"
            ):
                owner = module
    return owner


def symbol_owner_module(symbol: str) -> str | None:
    """Return the module attached to the symbol's actual declared entity."""

    prefix = symbol[: function_parameter_open(symbol)].rstrip()
    qualified_operator = prefix.rfind("::operator")
    if qualified_operator != -1:
        # Conversion-operator target types may carry their own attachments.
        # A module attached to the qualified class owns the member operator;
        # namespace-qualified free operators instead fall through to the
        # attachment on the operator name itself.
        qualified_entity = actual_entity_declarator(
            prefix[:qualified_operator]
        )
        owner = top_level_module_attachment(qualified_entity)
        if owner is not None:
            return owner

    return top_level_module_attachment(actual_entity_declarator(symbol))


def format_symbols(symbols: set[tuple[str, str]]) -> str:
    return "\n".join(f"  {kind} {name}" for kind, name in sorted(symbols))


def require_expected_symbols(
    module_name: str,
    label: str,
    symbols: set[tuple[str, str]],
    extra: frozenset[tuple[str, str]] = frozenset(),
) -> None:
    """Pin a module's exact strong ABI.

    `extra` carries the symbols a lane legitimately gains beyond the crate
    ABI -- currently only the platform implementation unit's definitions in
    the production library. It is still an exact-set comparison.
    """

    expected = set(ABI_SPECS[module_name].symbols) | set(extra)
    if symbols == expected:
        return
    missing = expected - symbols
    unexpected = symbols - expected
    details = [
        f"{label} does not define the exact {len(expected)}-symbol "
        f"{module_name} ABI"
    ]
    if missing:
        details.append("missing:\n" + format_symbols(missing))
    if unexpected:
        details.append("unexpected:\n" + format_symbols(unexpected))
    raise GateError("\n".join(details))


def importer_source() -> str:
    return """\
#include <rusty/function.hpp>
#include <rusty/cell.hpp>
#include <rusty/io.hpp>
#include <rusty/move.hpp>
#include <rusty/option.hpp>
#include <rusty/refcell.hpp>
#include <rusty/slice.hpp>
#include <rusty/sync/atomic.hpp>
#include <rusty/traits.hpp>

#include <atomic>
#include <algorithm>
#include <array>
#include <bit>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <functional>
#include <iostream>
#include <limits>
#include <memory>
#include <netdb.h>
#include <span>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <typeindex>
#include <tuple>
#include <type_traits>
#include <utility>
#include <vector>
#include <unistd.h>

import rusty;
import rrr.callback_wrapper;
import rrr.basetypes;
import rrr.callbacks;
import rrr.channel;
import rrr.circuit_breaker;
import rrr.completion_tracker;
import rrr.connection_metrics;
import rrr.connection_state;
import rrr.errors;
import rrr.epoll_wrapper;
import rrr.fiber;
import rrr.fiber_channel;
import rrr.frame_codec;
import rrr.future;
import rrr.heartbeat;
import rrr.idempotency;
import rrr.inmemory_channel;
import rrr.internal_protocol;
import rrr.load_balancer;
import rrr.logging;
import rrr.misc;
import rrr.pollable_proxy;
import rrr.rand;
import rrr.reconnect_policy;
import rrr.request_options;
import rrr.request_queue;
import rrr.serializable;
import rrr.serializable_envelope;
import rrr.stat;
import rrr.threading;
import rrr.utils;
import rrr.debugging;
import rrr.any_message;
import rrr.tcp_channel;

static std::int32_t rand_raw_value = 0;
static std::uint32_t rand_raw_draws = 0;
static std::uint32_t rand_destroy_calls = 0;
static std::uint32_t rand_string_evaluations = 0;
static std::uint32_t rand_weight_evaluations = 0;
static std::uint64_t monotonic_now_us = 0;
static std::uint64_t realtime_now_us = 0;
static std::uint64_t gettimeofday_now_us = 0;
static std::uint64_t slept_us = 0;
static std::int32_t selected_open_port = 0;
static std::uint32_t freeaddrinfo_calls = 0;
static std::int32_t hostname_mode = 0;
static std::size_t hostname_buffer_length = 0;

extern "C" int srpc_rand_raw(void) {
    ++rand_raw_draws;
    return rand_raw_value;
}

extern "C" void srpc_rand_destroy(void) {
    ++rand_destroy_calls;
}

extern "C" std::uint64_t srpc_clock_monotonic_us(void) {
    return monotonic_now_us;
}

extern "C" std::uint64_t srpc_rdtsc_raw(void) {
    return monotonic_now_us;
}

extern "C" std::uint64_t srpc_rdtsc(void) {
    return monotonic_now_us;
}

extern "C" std::uint64_t srpc_clock_realtime_coarse_us(void) {
    return realtime_now_us;
}

extern "C" std::uint64_t srpc_gettimeofday_us(void) {
    return gettimeofday_now_us;
}

extern "C" void srpc_sleep_us(std::uint64_t microseconds) {
    slept_us = microseconds;
}

extern "C" void srpc_cpu_pause(void) {}

extern "C" int srpc_find_open_port(void) {
    return selected_open_port;
}

extern "C" void freeaddrinfo(addrinfo* info) {
    ++freeaddrinfo_calls;
    delete info;
}

extern "C" int gethostname(char* name, std::size_t length) {
    hostname_buffer_length = length;
    if (hostname_mode < 0) {
        return -1;
    }
    const char fixed[] = "goal0-host";
    if (length != 0) {
        std::strncpy(name, fixed, length);
        name[length - 1] = '\\0';
    }
    return 0;
}

extern "C" __attribute__((weak))
const char* srpc_path_basename(const char* path) {
    if (path == nullptr) {
        return nullptr;
    }
    const char* basename = path;
    for (const char* cursor = path; *cursor != '\\0'; ++cursor) {
        if (*cursor == '/') {
            basename = cursor + 1;
        }
    }
    return basename;
}

extern "C" __attribute__((weak)) void srpc_time_now_str(char* now) {
    constexpr char kFixedTime[] = "2000-01-02 03:04:05.006";
    std::memcpy(now, kFixedTime, sizeof(kFixedTime));
}

extern "C" __attribute__((weak)) std::int32_t srpc_get_ncpu(void) {
    return 8;
}

extern "C" __attribute__((weak)) std::int32_t srpc_format_fixed_2(
    double value, std::int8_t* output, std::size_t capacity) {
    const int written = std::snprintf(
        reinterpret_cast<char*>(output), capacity, "%.2f", value);
    return written < 0 ? -1 : written;
}

static void install_rand_raw(std::int32_t value) {
    rand_raw_value = value;
    rand_raw_draws = 0;
}

static std::string make_rand_binary_string() {
    ++rand_string_evaluations;
    return std::string({
        static_cast<char>(0x00),
        static_cast<char>(0x80),
        static_cast<char>(0xff),
    });
}

static std::vector<double> make_rand_weights() {
    ++rand_weight_evaluations;
    return {1.0, 2.0, 3.0};
}

struct LoadBalancerProbeMetrics {
    std::uint64_t pending;
    std::uint64_t latency;
    std::uint64_t completed;

    std::uint64_t in_flight_requests() const { return pending; }
    std::uint64_t avg_latency_us() const { return latency; }
    std::uint64_t requests_completed() const { return completed; }
};

struct LoadBalancerProbeClient {
    LoadBalancerProbeMetrics metrics_value;

    const LoadBalancerProbeMetrics& metrics() const { return metrics_value; }
};

using LoadBalancerProbeClients =
    std::vector<std::shared_ptr<LoadBalancerProbeClient>>;

namespace canary {
struct EnvelopePayloadSet {};

struct EnvelopePayload {
    std::int32_t value = 0;
    std::int32_t kind() const { return 61; }
    void save(rrr::BinaryWriteArchive&) const {}
    void load(rrr::BinaryReadArchive&) {}
};

struct EnvelopeLegacyLayout {
    std::int32_t kind_;
    rusty::Option<rrr::SerializableProxy> inner_;
};

struct MiscBound {
    int value;
};

struct MiscValue {
    int value;
    MiscValue(int initial) : value(initial) {}
    MiscValue(const MiscBound& initial) : value(initial.value) {}
};

bool operator<(const MiscValue& value, const MiscBound& bound) {
    return value.value < bound.value;
}

bool operator>(const MiscValue& value, const MiscBound& bound) {
    return value.value > bound.value;
}
}  // namespace canary

namespace rrr {
template <>
struct PayloadMember<canary::EnvelopePayloadSet, canary::EnvelopePayload> {
    static constexpr bool value = true;
    static constexpr std::int32_t KIND = 61;
};
}  // namespace rrr

template <class T>
concept HasSendMarker = requires { T::is_send; };

template <class T>
concept HasSyncMarker = requires { T::is_sync; };

static_assert(std::is_same_v<rrr::RandWeightVec, std::vector<double>>);

static_assert(std::is_same_v<
              rrr::FrameCursor,
              rusty::io::Cursor<std::vector<std::uint8_t>>>);
static_assert(std::is_same_v<
              std::underlying_type_t<rrr::FrameDecodeStatus>,
              std::int32_t>);
static_assert(sizeof(rrr::FrameDecodeStatus) == 4);
static_assert(alignof(rrr::FrameDecodeStatus) == 4);
static_assert(rrr::kFrameHeaderSize == 4);
static_assert(rrr::kMaxFramePayloadSize == INT32_MAX);
static_assert(std::is_standard_layout_v<rrr::FrameHeader>);
static_assert(std::is_trivially_copyable_v<rrr::FrameHeader>);
static_assert(rrr::FrameHeader::is_send && rrr::FrameHeader::is_sync);
static_assert(sizeof(rrr::FrameHeader) == 8);
static_assert(alignof(rrr::FrameHeader) == 4);
static_assert(offsetof(rrr::FrameHeader, payload_size) == 0);
static_assert(offsetof(rrr::FrameHeader, extended_header_flag) == 4);
static_assert(sizeof(rrr::FrameView) == 24);
static_assert(alignof(rrr::FrameView) == 8);
static_assert(offsetof(rrr::FrameView, header) == 0);
static_assert(offsetof(rrr::FrameView, payload) == 8);
static_assert(offsetof(rrr::FrameView, payload_size) == 16);
static_assert(sizeof(rrr::FrameCursor) == 32);
static_assert(alignof(rrr::FrameCursor) == 8);
static_assert(sizeof(rrr::FrameStreamReader) == 40);
static_assert(alignof(rrr::FrameStreamReader) == 8);
static_assert(offsetof(rrr::FrameStreamReader, cursor_) == 0);
static_assert(offsetof(rrr::FrameStreamReader, noncopy_) == 32);
static_assert(!std::is_default_constructible_v<rrr::FrameStreamReader>);
static_assert(!std::is_copy_constructible_v<rrr::FrameStreamReader>);
static_assert(!std::is_copy_assignable_v<rrr::FrameStreamReader>);
static_assert(std::is_move_constructible_v<rrr::FrameStreamReader>);
static_assert(std::is_move_assignable_v<rrr::FrameStreamReader>);
static_assert(std::is_same_v<
              decltype(&rrr::frame_decode_status_to_string),
              std::string_view (*)(rrr::FrameDecodeStatus)>);
static_assert(std::is_same_v<
              decltype(&rrr::frame_codec_write_header),
              bool (*)(std::span<std::uint8_t>, std::int32_t, bool)>);
static_assert(std::is_same_v<
              decltype(&rrr::frame_codec_peek_header),
              rrr::FrameDecodeStatus (*)(
                  std::span<const std::uint8_t>, rrr::FrameHeader&)>);
static_assert(std::is_same_v<
              decltype(&rrr::frame_codec_encode_into),
              bool (*)(std::vector<std::uint8_t>&, const std::uint8_t*,
                       std::int32_t, bool)>);
static_assert(std::is_same_v<
              decltype(&rrr::FrameHeader::total_frame_size),
              std::int32_t (rrr::FrameHeader::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::FrameStreamReader::append),
              void (rrr::FrameStreamReader::*)(
                  const std::uint8_t*, std::size_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::FrameStreamReader::next_frame),
              rrr::FrameDecodeStatus (rrr::FrameStreamReader::*)(
                  rrr::FrameView&) const>);
static_assert(std::is_same_v<
              decltype(&rrr::FrameStreamReader::buffered_bytes),
              std::size_t (rrr::FrameStreamReader::*)() const>);

static_assert(rrr::PayloadMember<
              canary::EnvelopePayloadSet, canary::EnvelopePayload>::value);
static_assert(rrr::PayloadMember<
              canary::EnvelopePayloadSet, canary::EnvelopePayload>::KIND == 61);
static_assert(!rrr::PayloadMember<canary::EnvelopePayloadSet, int>::value);
static_assert(sizeof(rrr::SerializableEnvelope<canary::EnvelopePayloadSet>) ==
              sizeof(canary::EnvelopeLegacyLayout));
static_assert(alignof(rrr::SerializableEnvelope<canary::EnvelopePayloadSet>) ==
              alignof(canary::EnvelopeLegacyLayout));
static_assert(std::is_default_constructible_v<
              rrr::SerializableEnvelope<canary::EnvelopePayloadSet>>);
static_assert(std::is_copy_constructible_v<
              rrr::SerializableEnvelope<canary::EnvelopePayloadSet>>);

static_assert(std::is_default_constructible_v<rrr::FiberFuture<int>>);
static_assert(std::is_default_constructible_v<rrr::FiberPromise<int>>);
static_assert(std::is_move_constructible_v<rrr::FiberFuture<int>>);
static_assert(std::is_same_v<
              decltype(&rrr::FiberFuture<int>::wait_for),
              bool (rrr::FiberFuture<int>::*)(std::uint64_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::FiberPromise<int>::set_value),
              void (rrr::FiberPromise<int>::*)(const int&)>);

static_assert(rrr::Log::FATAL == 0 && rrr::Log::ERROR == 1 &&
              rrr::Log::WARN == 2 && rrr::Log::INFO == 3 &&
              rrr::Log::DEBUG == 4);
static_assert(std::is_same_v<
              decltype(&rrr::log_level_tag),
              std::string_view (*)(std::int32_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::log_line),
              void (*)(std::int32_t, std::int32_t, const std::int8_t*,
                       const std::string&)>);

static_assert(sizeof(rrr::IdempotencyKey) == 16);
static_assert(alignof(rrr::IdempotencyKey) == 8);
static_assert(offsetof(rrr::IdempotencyKey, client_id) == 0);
static_assert(offsetof(rrr::IdempotencyKey, sequence) == 8);
static_assert(std::is_standard_layout_v<rrr::IdempotencyKey>);
static_assert(std::is_trivially_copyable_v<rrr::IdempotencyKey>);
static_assert(HasSendMarker<rrr::IdempotencyKey>);
static_assert(HasSyncMarker<rrr::IdempotencyKey>);
static_assert(sizeof(rrr::IdempotencyKeyHash) == 1);
static_assert(sizeof(rrr::IdempotencyConfig) == 24);
static_assert(alignof(rrr::IdempotencyConfig) == 8);
static_assert(offsetof(rrr::IdempotencyConfig, ttl_ms) == 0);
static_assert(offsetof(rrr::IdempotencyConfig, max_entries) == 8);
static_assert(offsetof(rrr::IdempotencyConfig, enabled) == 16);
static_assert(std::is_trivially_copyable_v<rrr::IdempotencyConfig>);
static_assert(sizeof(rrr::CachedResponse) == 80);
static_assert(alignof(rrr::CachedResponse) == 8);
static_assert(offsetof(rrr::CachedResponse, key) == 0);
static_assert(offsetof(rrr::CachedResponse, error_code) == 16);
static_assert(offsetof(rrr::CachedResponse, response_data) == 24);
static_assert(offsetof(rrr::CachedResponse, timestamp_ms) == 72);
static_assert(sizeof(rrr::IdempotencyKeyGenerator) == 16);
static_assert(offsetof(rrr::IdempotencyKeyGenerator, client_id_field) == 0);
static_assert(offsetof(rrr::IdempotencyKeyGenerator, sequence_field) == 8);
static_assert(HasSendMarker<rrr::IdempotencyKeyGenerator>);
static_assert(!HasSyncMarker<rrr::IdempotencyKeyGenerator>);
static_assert(sizeof(rrr::IdempotencyCache) == 120);
static_assert(alignof(rrr::IdempotencyCache) == 8);
static_assert(offsetof(rrr::IdempotencyCache, config_) == 0);
static_assert(offsetof(rrr::IdempotencyCache, cache_) == 24);
static_assert(offsetof(rrr::IdempotencyCache, hits_) == 96);
static_assert(offsetof(rrr::IdempotencyCache, misses_) == 104);
static_assert(offsetof(rrr::IdempotencyCache, evictions_) == 112);
static_assert(!HasSendMarker<rrr::IdempotencyCache>);
static_assert(!HasSyncMarker<rrr::IdempotencyCache>);
static_assert(std::is_same_v<
              decltype(&rrr::IdempotencyCache::lookup),
              bool (rrr::IdempotencyCache::*)(
                  const rrr::IdempotencyKey&, std::uint64_t, std::int32_t&,
                  rusty::Vec<std::uint8_t>&) const>);
static_assert(std::is_same_v<
              decltype(&rrr::IdempotencyCache::store),
              void (rrr::IdempotencyCache::*)(
                  const rrr::IdempotencyKey&, std::int32_t,
                  const rusty::Vec<std::uint8_t>&, std::uint64_t) const>);

static_assert(std::is_same_v<
              decltype(&rrr::this_fiber::get_id), std::uint64_t (*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::this_fiber::sleep_us),
              void (*)(std::uint64_t)>);

static_assert(sizeof(rrr::Job) == 8);
static_assert(alignof(rrr::Job) == 8);
static_assert(sizeof(rrr::OneTimeJob) == 64);
static_assert(alignof(rrr::OneTimeJob) == 16);
static_assert(std::is_base_of_v<rrr::Job, rrr::OneTimeJob>);
static_assert(std::is_convertible_v<rrr::OneTimeJob*, rrr::Job*>);
static_assert(std::is_same_v<
              decltype(&rrr::get_ncpu), std::int32_t (*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::format_thousands), std::string (*)(double)>);

static_assert(std::is_same_v<
              std::underlying_type_t<rrr::ChannelError>, std::int32_t>);
static_assert(sizeof(rrr::ChannelFrame) == 16);
static_assert(alignof(rrr::ChannelFrame) == 8);
static_assert(offsetof(rrr::ChannelFrame, payload) == 0);
static_assert(offsetof(rrr::ChannelFrame, size) == 8);
static_assert(std::is_abstract_v<rrr::ChannelFactoryBase>);
static_assert(std::is_abstract_v<rrr::ChannelListenerBase>);
static_assert(std::is_abstract_v<rrr::ChannelConnectionBase>);
static_assert(std::is_same_v<
              decltype(&rrr::channel_error_to_string),
              std::string_view (*)(rrr::ChannelError)>);

static_assert(std::is_abstract_v<rrr::Pollable>);
static_assert(std::is_same_v<
              decltype(&rrr::Epoll::fd),
              std::int32_t (rrr::Epoll::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::epoll_bump_remove_count), void (*)()>);
static_assert(std::is_abstract_v<rrr::PollableBase>);
static_assert(std::is_same_v<rrr::PollableProxy,
                             rusty::Box<rrr::PollableBase>>);

static_assert(std::is_same_v<
              decltype(&rrr::ConnectionCallbacks::new_),
              rrr::ConnectionCallbacks (*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::CallbackManager::new_),
              rrr::CallbackManager (*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::CallbackManager::callback_count),
              std::size_t (rrr::CallbackManager::*)() const>);

static_assert(std::is_base_of_v<rrr::ChannelConnectionBase,
                                rrr::InMemoryChannelShim>);
static_assert(std::is_base_of_v<rrr::ChannelListenerBase,
                                rrr::InMemoryListenerShim>);
static_assert(std::is_base_of_v<rrr::ChannelFactoryBase,
                                rrr::InMemoryFactoryShim>);
static_assert(std::is_same_v<
              decltype(&rrr::InMemorySwitchboard::new_),
              rrr::InMemorySwitchboard (*)()>);

static_assert(std::is_same_v<
              decltype(&rrr::FiberChannel::recv_frame),
              rusty::Option<rrr::OwnedFrame> (rrr::FiberChannel::*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::FiberChannel::is_closed),
              bool (rrr::FiberChannel::*)() const>);

static_assert(rrr::SpinLock::is_send && rrr::SpinLock::is_sync);
static_assert(std::is_same_v<
              decltype(&rrr::SpinLock::new_), rrr::SpinLock (*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::cpu_pause), void (*)()>);

static_assert(std::is_same_v<decltype(&rrr::likely), bool (*)(bool)>);
static_assert(std::is_same_v<decltype(&rrr::unlikely), bool (*)(bool)>);
static_assert(std::is_same_v<
              decltype(&rrr::print_stack_trace), void (*)(FILE*)>);

static_assert(sizeof(rrr::AnyMessage) == 40);
static_assert(alignof(rrr::AnyMessage) == 8);
static_assert(std::is_same_v<
              decltype(&rrr::AnyMessage::save),
              void (rrr::AnyMessage::*)(rrr::BinaryWriteArchive&) const>);
static_assert(std::is_same_v<
              decltype(&rrr::AnyMessage::load),
              void (rrr::AnyMessage::*)(rrr::BinaryReadArchive&)>);

static_assert(std::is_same_v<rrr::i8, std::int8_t>);
static_assert(std::is_same_v<rrr::i16, std::int16_t>);
static_assert(std::is_same_v<rrr::i32, std::int32_t>);
static_assert(std::is_same_v<rrr::i64, std::int64_t>);
static_assert(sizeof(rrr::SparseInt) == 1);
static_assert(alignof(rrr::SparseInt) == 1);
static_assert(sizeof(rrr::v32) == 4);
static_assert(alignof(rrr::v32) == 4);
static_assert(sizeof(rrr::v64) == 8);
static_assert(alignof(rrr::v64) == 8);
static_assert(sizeof(rrr::Counter) == 8);
static_assert(alignof(rrr::Counter) == 8);
static_assert(sizeof(rrr::Time) == 1);
static_assert(alignof(rrr::Time) == 1);
static_assert(sizeof(rrr::Timer) == 16);
static_assert(alignof(rrr::Timer) == 8);
static_assert(offsetof(rrr::v32, val_field) == 0);
static_assert(offsetof(rrr::v64, val_field) == 0);
static_assert(offsetof(rrr::Counter, next_field) == 0);
static_assert(offsetof(rrr::Timer, begin_us) == 0);
static_assert(offsetof(rrr::Timer, end_us) == 8);
static_assert(rrr::SparseInt::is_send && rrr::SparseInt::is_sync);
static_assert(rrr::v32::is_send && rrr::v32::is_sync);
static_assert(rrr::v64::is_send && rrr::v64::is_sync);
static_assert(rrr::Counter::is_send && rrr::Counter::is_sync);
static_assert(rrr::Time::is_send && rrr::Time::is_sync);
static_assert(rrr::Timer::is_send && rrr::Timer::is_sync);
static_assert(sizeof(rrr::AtomicI64) == 8);
static_assert(alignof(rrr::AtomicI64) == 8);
static_assert(std::is_copy_constructible_v<rrr::Counter>);
static_assert(std::is_copy_assignable_v<rrr::Counter>);
static_assert(std::is_move_constructible_v<rrr::Counter>);
static_assert(std::is_move_assignable_v<rrr::Counter>);
static_assert(std::is_same_v<
              decltype(&rrr::SparseInt::buf_size),
              std::size_t (*)(std::uint8_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::SparseInt::dump32),
              std::size_t (*)(std::int32_t, std::uint8_t*)>);
static_assert(std::is_same_v<
              decltype(&rrr::SparseInt::dump64),
              std::size_t (*)(std::int64_t, std::uint8_t*)>);
static_assert(std::is_same_v<
              decltype(&rrr::SparseInt::load32),
              std::int32_t (*)(const std::uint8_t*)>);
static_assert(std::is_same_v<
              decltype(&rrr::SparseInt::load64),
              std::int64_t (*)(const std::uint8_t*)>);
static_assert(std::is_same_v<
              decltype(&rrr::Counter::next),
              std::int64_t (rrr::Counter::*)(std::int64_t) const>);
static_assert(std::is_same_v<
              decltype(&rrr::Timer::elapsed),
              double (rrr::Timer::*)() const>);

static_assert(std::is_same_v<
              std::underlying_type_t<rrr::CircuitState>, std::int32_t>);
static_assert(sizeof(rrr::CircuitState) == 4);
static_assert(alignof(rrr::CircuitState) == 4);
static_assert(std::is_standard_layout_v<rrr::CircuitBreakerConfig>);
static_assert(std::is_trivially_copyable_v<rrr::CircuitBreakerConfig>);
static_assert(rrr::CircuitBreakerConfig::is_send);
static_assert(rrr::CircuitBreakerConfig::is_sync);
static_assert(sizeof(rrr::CircuitBreakerConfig) == 16);
static_assert(alignof(rrr::CircuitBreakerConfig) == 4);
static_assert(offsetof(rrr::CircuitBreakerConfig, failure_threshold) == 0);
static_assert(offsetof(rrr::CircuitBreakerConfig, success_threshold) == 4);
static_assert(offsetof(rrr::CircuitBreakerConfig, timeout_ms) == 8);
static_assert(offsetof(rrr::CircuitBreakerConfig, enabled) == 12);
static_assert(sizeof(rrr::CircuitBreaker) == 48);
static_assert(alignof(rrr::CircuitBreaker) == 8);
static_assert(offsetof(rrr::CircuitBreaker, config_field) == 0);
static_assert(offsetof(rrr::CircuitBreaker, state_field) == 16);
static_assert(offsetof(rrr::CircuitBreaker, failure_count_field) == 20);
static_assert(offsetof(rrr::CircuitBreaker, success_count_field) == 24);
static_assert(offsetof(rrr::CircuitBreaker, last_failure_time) == 32);
static_assert(offsetof(rrr::CircuitBreaker, probe_in_progress) == 40);
static_assert(rrr::CircuitBreaker::is_send);
static_assert(!rusty::is_sync<rrr::CircuitBreaker>::value);
static_assert(std::is_same_v<
              decltype(&rrr::CircuitBreaker::new_),
              rrr::CircuitBreaker (*)(rrr::CircuitBreakerConfig)>);
static_assert(std::is_same_v<
              decltype(&rrr::CircuitBreaker::set_config),
              void (rrr::CircuitBreaker::*)(rrr::CircuitBreakerConfig) const>);
static_assert(std::is_same_v<
              decltype(&rrr::CircuitBreaker::allow_request),
              bool (rrr::CircuitBreaker::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::current_time_us), std::uint64_t (*)()>);

static_assert(std::is_same_v<
              std::underlying_type_t<rrr::OverflowStrategy>, std::int32_t>);
static_assert(sizeof(rrr::OverflowStrategy) == 4);
static_assert(alignof(rrr::OverflowStrategy) == 4);
static_assert(std::is_same_v<
              rrr::QueuedRequestCallback,
              rusty::Function<void(std::int32_t)>>);
static_assert(std::is_same_v<
              decltype(&rrr::rq_invoke_callback_safely),
              void (*)(rrr::QueuedRequestCallback, std::int32_t)>);
static_assert(sizeof(rrr::QueuedRequestCallback) == 48);
static_assert(alignof(rrr::QueuedRequestCallback) == 16);
static_assert(sizeof(rrr::QueuedRequest) == 96);
static_assert(alignof(rrr::QueuedRequest) == 16);
static_assert(offsetof(rrr::QueuedRequest, xid) == 0);
static_assert(offsetof(rrr::QueuedRequest, rpc_id) == 8);
static_assert(offsetof(rrr::QueuedRequest, timestamp_us) == 16);
static_assert(offsetof(rrr::QueuedRequest, retry_count) == 24);
static_assert(offsetof(rrr::QueuedRequest, callback) == 32);
static_assert(offsetof(rrr::QueuedRequest, ttl_ms) == 80);
static_assert(!rusty::is_send<rrr::QueuedRequest>::value);
static_assert(!rusty::is_sync<rrr::QueuedRequest>::value);
static_assert(std::is_standard_layout_v<rrr::RequestQueueConfig>);
static_assert(std::is_trivially_copyable_v<rrr::RequestQueueConfig>);
static_assert(rrr::RequestQueueConfig::is_send);
static_assert(rrr::RequestQueueConfig::is_sync);
static_assert(sizeof(rrr::RequestQueueConfig) == 24);
static_assert(alignof(rrr::RequestQueueConfig) == 8);
static_assert(offsetof(rrr::RequestQueueConfig, max_size) == 0);
static_assert(offsetof(rrr::RequestQueueConfig, default_ttl_ms) == 8);
static_assert(offsetof(rrr::RequestQueueConfig, overflow_strategy) == 12);
static_assert(offsetof(rrr::RequestQueueConfig, enabled) == 16);
static_assert(sizeof(rrr::RequestQueue) == 96);
static_assert(alignof(rrr::RequestQueue) == 8);
static_assert(offsetof(rrr::RequestQueue, config_) == 0);
static_assert(offsetof(rrr::RequestQueue, queue_) == 24);
static_assert(!rusty::is_send<rrr::RequestQueue>::value);
static_assert(!rusty::is_sync<rrr::RequestQueue>::value);
static_assert(std::is_same_v<
              decltype(&rrr::RequestQueue::enqueue),
              bool (rrr::RequestQueue::*)(rrr::QueuedRequest) const>);
static_assert(std::is_same_v<
              decltype(&rrr::RequestQueue::dequeue),
              rusty::Option<rrr::QueuedRequest> (rrr::RequestQueue::*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::RequestQueue::expire_stale),
              std::size_t (rrr::RequestQueue::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::RequestQueue::full),
              bool (rrr::RequestQueue::*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::RequestQueue::remaining_capacity),
              std::size_t (rrr::RequestQueue::*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::RequestQueue::clear_all),
              void (rrr::RequestQueue::*)(std::int32_t) const>);
static_assert(std::is_same_v<
              decltype(&rrr::RequestQueue::update_config),
              void (rrr::RequestQueue::*)(rrr::RequestQueueConfig) const>);
static_assert(std::is_same_v<
              decltype(&rrr::randgen_zero_pad),
              std::string (*)(std::string, std::int32_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::RandomGenerator::int2str_n),
              std::string (*)(std::int32_t, std::int32_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::RandomGenerator::weighted_select),
              std::uint32_t (*)(const std::vector<double>&)>);

static_assert(std::is_same_v<
              std::underlying_type_t<rrr::ConnectionState>, std::int32_t>);
static_assert(sizeof(rrr::ConnectionState) == 4);
static_assert(alignof(rrr::ConnectionState) == 4);
static_assert(std::is_same_v<
              rrr::StateChangeCallback,
              rusty::Function<void(rrr::ConnectionState,
                                   rrr::ConnectionState) const>>);
static_assert(sizeof(rrr::StateChangeCallback) == 48);
static_assert(alignof(rrr::StateChangeCallback) == 16);
static_assert(sizeof(rrr::ConnectionStateMachine) == 64);
static_assert(alignof(rrr::ConnectionStateMachine) == 16);
static_assert(offsetof(rrr::ConnectionStateMachine, state_field) == 0);
static_assert(offsetof(rrr::ConnectionStateMachine, on_state_change) == 16);
static_assert(!std::is_copy_constructible_v<rrr::ConnectionStateMachine>);
static_assert(std::is_move_constructible_v<rrr::ConnectionStateMachine>);
static_assert(!rusty::is_send<rrr::StateChangeCallback>::value);
static_assert(!rusty::is_sync<rrr::StateChangeCallback>::value);
static_assert(!rusty::is_send<rrr::ConnectionStateMachine>::value);
static_assert(!rusty::is_sync<rrr::ConnectionStateMachine>::value);
static_assert(std::is_same_v<
              decltype(&rrr::ConnectionStateMachine::set_on_state_change),
              void (rrr::ConnectionStateMachine::*)(rrr::StateChangeCallback)>);
static_assert(std::is_same_v<
              decltype(&rrr::ConnectionStateMachine::transition_to),
              bool (rrr::ConnectionStateMachine::*)(rrr::ConnectionState) const>);

static_assert(std::is_same_v<
              rrr::HeartbeatTimeoutCallback,
              rusty::Function<void()>>);
static_assert(sizeof(rrr::HeartbeatTimeoutCallback) == 48);
static_assert(alignof(rrr::HeartbeatTimeoutCallback) == 16);
static_assert(std::is_standard_layout_v<rrr::HeartbeatConfig>);
static_assert(std::is_trivially_copyable_v<rrr::HeartbeatConfig>);
static_assert(rrr::HeartbeatConfig::is_send);
static_assert(rrr::HeartbeatConfig::is_sync);
static_assert(sizeof(rrr::HeartbeatConfig) == 16);
static_assert(alignof(rrr::HeartbeatConfig) == 4);
static_assert(offsetof(rrr::HeartbeatConfig, enabled) == 0);
static_assert(offsetof(rrr::HeartbeatConfig, interval_ms) == 4);
static_assert(offsetof(rrr::HeartbeatConfig, timeout_ms) == 8);
static_assert(offsetof(rrr::HeartbeatConfig, max_missed) == 12);
static_assert(sizeof(rrr::HeartbeatManager) == 112);
static_assert(alignof(rrr::HeartbeatManager) == 16);
static_assert(offsetof(rrr::HeartbeatManager, config_field) == 0);
static_assert(offsetof(rrr::HeartbeatManager, last_send_time) == 16);
static_assert(offsetof(rrr::HeartbeatManager, last_recv_time) == 24);
static_assert(offsetof(rrr::HeartbeatManager, missed_count_field) == 32);
static_assert(offsetof(rrr::HeartbeatManager, pending_pong) == 36);
static_assert(offsetof(rrr::HeartbeatManager, timed_out) == 37);
static_assert(offsetof(rrr::HeartbeatManager, on_timeout) == 48);
static_assert(!std::is_copy_constructible_v<rrr::HeartbeatManager>);
static_assert(std::is_move_constructible_v<rrr::HeartbeatManager>);
static_assert(!rusty::is_send<rrr::HeartbeatTimeoutCallback>::value);
static_assert(!rusty::is_sync<rrr::HeartbeatTimeoutCallback>::value);
static_assert(!rusty::is_send<rrr::HeartbeatManager>::value);
static_assert(!rusty::is_sync<rrr::HeartbeatManager>::value);
static_assert(std::is_same_v<
              decltype(&rrr::HeartbeatManager::new_),
              rrr::HeartbeatManager (*)(const rrr::HeartbeatConfig&)>);
static_assert(std::is_same_v<
              decltype(&rrr::HeartbeatManager::set_on_timeout),
              void (rrr::HeartbeatManager::*)(rrr::HeartbeatTimeoutCallback) const>);
static_assert(std::is_same_v<
              decltype(&rrr::HeartbeatManager::check_timeout),
              bool (rrr::HeartbeatManager::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::heartbeat_time_us), std::uint64_t (*)()>);
static_assert(std::is_same_v<
              std::underlying_type_t<rrr::LoadBalancingStrategy>,
              std::int32_t>);
static_assert(sizeof(rrr::LoadBalancingStrategy) == 4);
static_assert(alignof(rrr::LoadBalancingStrategy) == 4);
static_assert(sizeof(rrr::LoadBalancerState) == 8);
static_assert(alignof(rrr::LoadBalancerState) == 8);
static_assert(offsetof(rrr::LoadBalancerState, round_robin_index_field) == 0);
static_assert(std::is_standard_layout_v<rrr::LoadBalancerState>);
static_assert(rrr::LoadBalancerState::is_send);
static_assert(!rusty::is_sync<rrr::LoadBalancerState>::value);
static_assert(sizeof(rrr::LoadBalancer) == 1);
static_assert(std::is_empty_v<rrr::LoadBalancer>);
static_assert(rrr::LoadBalancer::is_send && rrr::LoadBalancer::is_sync);
static_assert(std::is_same_v<
              decltype(&rrr::LoadBalancer::select_random),
              std::size_t (*)(std::size_t, std::size_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::LoadBalancer::select_round_robin),
              std::size_t (*)(std::size_t,
                              const rrr::LoadBalancerState&)>);
static_assert(std::is_same_v<
              decltype(&rrr::load_balancing_strategy_to_string),
              std::string_view (*)(rrr::LoadBalancingStrategy)>);
static_assert(sizeof(rrr::AddrInfo) == 16);
static_assert(alignof(rrr::AddrInfo) == 8);
static_assert(offsetof(rrr::AddrInfo, info_) == 0);
static_assert(offsetof(rrr::AddrInfo, owned_) == 8);
static_assert(offsetof(rrr::AddrInfo, _rusty_forgotten) == 9);
static_assert(std::is_standard_layout_v<rrr::AddrInfo>);
static_assert(!std::is_copy_constructible_v<rrr::AddrInfo>);
static_assert(!std::is_copy_assignable_v<rrr::AddrInfo>);
static_assert(std::is_move_constructible_v<rrr::AddrInfo>);
static_assert(std::is_move_assignable_v<rrr::AddrInfo>);
// The move constructor itself is noexcept (pinned in the generated surface),
// but is_nothrow_constructible also accounts for the legacy noexcept(false)
// destructor, so the aggregate trait is deliberately false.
static_assert(!std::is_nothrow_move_constructible_v<rrr::AddrInfo>);
static_assert(std::is_nothrow_move_assignable_v<rrr::AddrInfo>);
static_assert(!std::is_nothrow_destructible_v<rrr::AddrInfo>);
static_assert(std::is_same_v<
              decltype(&rrr::AddrInfo::get),
              addrinfo* (rrr::AddrInfo::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::AddrInfo::valid),
              bool (rrr::AddrInfo::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::find_open_port), std::int32_t (*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::get_host_name), std::string (*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::randgen_zero_pad),
              std::string (*)(std::string, std::int32_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::RandomGenerator::int2str_n),
              std::string (*)(std::int32_t, std::int32_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::RandomGenerator::weighted_select),
              std::uint32_t (*)(const std::vector<double>&)>);

static_assert(std::is_same_v<
              std::underlying_type_t<rrr::TimeoutType>, std::int32_t>);
static_assert(sizeof(rrr::TimeoutType) == 4);
static_assert(alignof(rrr::TimeoutType) == 4);
static_assert(std::is_trivially_copyable_v<rrr::TimeoutType>);
static_assert(std::is_standard_layout_v<rrr::RequestOptions>);
static_assert(std::is_trivially_copyable_v<rrr::RequestOptions>);
static_assert(rrr::RequestOptions::is_send);
static_assert(rrr::RequestOptions::is_sync);
static_assert(sizeof(rrr::RequestOptions) == 32);
static_assert(alignof(rrr::RequestOptions) == 8);
static_assert(offsetof(rrr::RequestOptions, timeout_ms) == 0);
static_assert(offsetof(rrr::RequestOptions, total_timeout_ms) == 8);
static_assert(offsetof(rrr::RequestOptions, max_retries) == 16);
static_assert(offsetof(rrr::RequestOptions, base_delay_ms) == 18);
static_assert(offsetof(rrr::RequestOptions, max_delay_ms) == 20);
static_assert(offsetof(rrr::RequestOptions, jitter_factor) == 24);
static_assert(offsetof(rrr::RequestOptions, idempotent) == 28);
static_assert(std::is_same_v<
              decltype(&rrr::RequestOptions::new_),
              rrr::RequestOptions (*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::RequestOptions::with_retry),
              rrr::RequestOptions (*)(std::uint16_t, std::uint64_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::RequestOptions::can_retry),
              bool (rrr::RequestOptions::*)(std::uint16_t) const>);
static_assert(std::is_same_v<
              decltype(&rrr::RequestOptions::calculate_delay_ms),
              std::uint64_t (rrr::RequestOptions::*)(std::uint16_t) const>);
static_assert(std::is_same_v<
              decltype(&rrr::RequestOptions::is_total_timeout_exceeded),
              bool (rrr::RequestOptions::*)(std::uint64_t) const>);
static_assert(std::is_same_v<
              decltype(&rrr::RequestOptions::remaining_time_ms),
              std::uint64_t (rrr::RequestOptions::*)(std::uint64_t) const>);
static_assert(std::is_same_v<
              decltype(&rrr::timeout_type_to_string),
              std::string_view (*)(rrr::TimeoutType)>);

static_assert(std::is_standard_layout_v<rrr::ReconnectPolicy>);
static_assert(std::is_trivially_copyable_v<rrr::ReconnectPolicy>);
static_assert(rrr::ReconnectPolicy::is_send);
static_assert(rrr::ReconnectPolicy::is_sync);
static_assert(sizeof(rrr::ReconnectPolicy) == 32);
static_assert(alignof(rrr::ReconnectPolicy) == 8);
static_assert(offsetof(rrr::ReconnectPolicy, auto_reconnect) == 0);
static_assert(offsetof(rrr::ReconnectPolicy, max_retries) == 4);
static_assert(offsetof(rrr::ReconnectPolicy, initial_delay_ms) == 8);
static_assert(offsetof(rrr::ReconnectPolicy, max_delay_ms) == 12);
static_assert(offsetof(rrr::ReconnectPolicy, backoff_multiplier) == 16);
static_assert(offsetof(rrr::ReconnectPolicy, jitter_enabled) == 24);
static_assert(sizeof(rrr::ReconnectCalculator) == 16);
static_assert(alignof(rrr::ReconnectCalculator) == 8);
static_assert(!std::is_copy_constructible_v<rrr::ReconnectCalculator>);
static_assert(std::is_move_constructible_v<rrr::ReconnectCalculator>);
static_assert(std::is_same_v<
              decltype(rrr::ReconnectCalculator::policy),
              const rrr::ReconnectPolicy&>);
static_assert(std::is_same_v<
              decltype(rrr::ReconnectCalculator::retries),
              rusty::Cell<std::uint32_t>>);
static_assert(std::is_same_v<
              decltype(&rrr::ReconnectPolicy::new_),
              rrr::ReconnectPolicy (*)()>);
static_assert(std::is_same_v<
              decltype(&rrr::ReconnectCalculator::new_),
              rrr::ReconnectCalculator (*)(const rrr::ReconnectPolicy&)>);
static_assert(std::is_same_v<
              decltype(&rrr::ReconnectCalculator::should_retry),
              bool (rrr::ReconnectCalculator::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::ReconnectCalculator::next_delay_ms),
              std::uint32_t (rrr::ReconnectCalculator::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::ReconnectCalculator::peek_delay_ms),
              std::uint32_t (rrr::ReconnectCalculator::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::ReconnectCalculator::reset),
              void (rrr::ReconnectCalculator::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::ReconnectCalculator::retry_count),
              std::uint32_t (rrr::ReconnectCalculator::*)() const>);
static_assert(std::is_same_v<
              decltype(&rrr::ReconnectCalculator::retries_exhausted),
              bool (rrr::ReconnectCalculator::*)() const>);

static_assert(sizeof(rrr::RpcErrorCategory) == sizeof(std::int32_t));
static_assert(sizeof(rrr::RpcError) == sizeof(std::int32_t));
static_assert(std::is_same_v<
              std::underlying_type_t<rrr::RpcErrorCategory>, std::int32_t>);
static_assert(std::is_same_v<
              std::underlying_type_t<rrr::RpcError>, std::int32_t>);
static_assert(std::is_trivially_copyable_v<rrr::RpcErrorCategory>);
static_assert(std::is_trivially_copyable_v<rrr::RpcError>);

namespace callback_oracle {

template<typename F>
struct CallbackWrapper {
    rusty::Option<rusty::Arc<F>> inner;

    static CallbackWrapper<F> from_callable(F callable) {
        return CallbackWrapper<F>{.inner = rusty::Option<rusty::Arc<F>>(
            rusty::Arc<F>::make(std::move(callable)))};
    }
    bool has_value() const {
        return this->inner.is_some();
    }
    const F& callable() const {
        return rusty::detail::deref_if_pointer_like(
            rusty::detail::deref_if_pointer_like(
                this->inner.as_ref().unwrap()));
    }
    CallbackWrapper<F> clone() const {
        return CallbackWrapper<F>{.inner = rusty::clone(this->inner)};
    }
    static CallbackWrapper<F> default_() {
        return CallbackWrapper<F>{
            .inner = rusty::Option<rusty::Arc<F>>{rusty::None}};
    }
    static constexpr bool is_send =
        rusty::is_send<F>::value && rusty::is_sync<F>::value;
    static constexpr bool is_sync =
        rusty::is_send<F>::value && rusty::is_sync<F>::value;
};

} // namespace callback_oracle

struct CallbackStatefulCallable {
    std::vector<int>* observations;
    mutable int calls = 0;

    void operator()(int) const {
        observations->push_back(++calls);
    }
};

struct CallbackMoveObservedCallable {
    std::shared_ptr<int> moves;

    explicit CallbackMoveObservedCallable(std::shared_ptr<int> count)
        : moves(std::move(count)) {}
    CallbackMoveObservedCallable(const CallbackMoveObservedCallable&) = delete;
    CallbackMoveObservedCallable& operator=(
        const CallbackMoveObservedCallable&) = delete;
    CallbackMoveObservedCallable(CallbackMoveObservedCallable&& other) noexcept
        : moves(std::move(other.moves)) {
        ++*moves;
    }
    CallbackMoveObservedCallable& operator=(
        CallbackMoveObservedCallable&&) = delete;

    void operator()() const {}
};

struct MutableHeartbeatCallable {
    int* calls;

    void operator()() {
        ++*calls;
    }
};

using CallbackFunction = rusty::Function<void(int) const>;
using CallbackActual =
    rrr::detail::CallbackWrapper<CallbackFunction>;
using CallbackOracle =
    callback_oracle::CallbackWrapper<CallbackFunction>;

static_assert(std::is_standard_layout_v<CallbackActual>);
static_assert(std::is_standard_layout_v<CallbackOracle>);
static_assert(sizeof(CallbackActual) == sizeof(CallbackOracle));
static_assert(alignof(CallbackActual) == alignof(CallbackOracle));
static_assert(sizeof(CallbackActual) == 2 * sizeof(void*));
static_assert(alignof(CallbackActual) == alignof(void*));
static_assert(offsetof(CallbackActual, inner) == 0);
static_assert(offsetof(CallbackActual, inner) ==
              offsetof(CallbackOracle, inner));
static_assert(
    std::is_default_constructible_v<CallbackActual> ==
    std::is_default_constructible_v<CallbackOracle>);
static_assert(
    std::is_copy_constructible_v<CallbackActual> ==
    std::is_copy_constructible_v<CallbackOracle>);
static_assert(
    std::is_copy_assignable_v<CallbackActual> ==
    std::is_copy_assignable_v<CallbackOracle>);
static_assert(
    std::is_move_constructible_v<CallbackActual> ==
    std::is_move_constructible_v<CallbackOracle>);
static_assert(
    std::is_move_assignable_v<CallbackActual> ==
    std::is_move_assignable_v<CallbackOracle>);
static_assert(
    std::is_nothrow_move_constructible_v<CallbackActual> ==
    std::is_nothrow_move_constructible_v<CallbackOracle>);
static_assert(
    std::is_nothrow_move_assignable_v<CallbackActual> ==
    std::is_nothrow_move_assignable_v<CallbackOracle>);
static_assert(
    std::is_trivially_destructible_v<CallbackActual> ==
    std::is_trivially_destructible_v<CallbackOracle>);
static_assert(std::is_same_v<
    decltype(CallbackActual::from_callable(
        std::declval<CallbackFunction>())),
    CallbackActual>);
static_assert(std::is_same_v<
    decltype(std::declval<const CallbackActual&>().callable()),
    const CallbackFunction&>);
static_assert(CallbackActual::is_send == CallbackOracle::is_send);
static_assert(CallbackActual::is_sync == CallbackOracle::is_sync);
static_assert(std::is_standard_layout_v<rrr::AvgStat>);
static_assert(std::is_trivially_copyable_v<rrr::AvgStat>);
static_assert(sizeof(rrr::AvgStat) == 5 * sizeof(std::int64_t));
static_assert(alignof(rrr::AvgStat) == alignof(std::int64_t));
static_assert(offsetof(rrr::AvgStat, n_stat_) == 0 * sizeof(std::int64_t));
static_assert(offsetof(rrr::AvgStat, sum_) == 1 * sizeof(std::int64_t));
static_assert(offsetof(rrr::AvgStat, avg_) == 2 * sizeof(std::int64_t));
static_assert(offsetof(rrr::AvgStat, max_) == 3 * sizeof(std::int64_t));
static_assert(offsetof(rrr::AvgStat, min_) == 4 * sizeof(std::int64_t));

using MetricsAtomicU64 = rusty::sync::atomic::AtomicU64;
static_assert(sizeof(MetricsAtomicU64) == sizeof(std::uint64_t));
static_assert(alignof(MetricsAtomicU64) == alignof(std::uint64_t));
static_assert(std::is_standard_layout_v<rrr::ConnectionMetrics>);
static_assert(std::is_copy_constructible_v<rrr::ConnectionMetrics>);
static_assert(std::is_copy_assignable_v<rrr::ConnectionMetrics>);
static_assert(std::is_move_constructible_v<rrr::ConnectionMetrics>);
static_assert(std::is_move_assignable_v<rrr::ConnectionMetrics>);
static_assert(!std::is_trivially_copyable_v<rrr::ConnectionMetrics>);
static_assert(rrr::ConnectionMetrics::is_send);
static_assert(rrr::ConnectionMetrics::is_sync);
static_assert(
    sizeof(rrr::ConnectionMetrics) == 18 * sizeof(std::uint64_t));
static_assert(
    alignof(rrr::ConnectionMetrics) == alignof(std::uint64_t));
static_assert(offsetof(rrr::ConnectionMetrics, requests_sent_field) ==
              0 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, requests_completed_field) ==
              1 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, requests_failed_field) ==
              2 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, requests_timed_out_field) ==
              3 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, in_flight_requests_field) ==
              4 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, bytes_sent_field) ==
              5 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, bytes_received_field) ==
              6 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, reconnect_count_field) ==
              7 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, retry_attempts_field) ==
              8 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, queue_dropped_requests_field) ==
              9 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, circuit_open_rejections_field) ==
              10 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, circuit_open_transitions_field) ==
              11 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, circuit_half_open_transitions_field) ==
              12 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, circuit_closed_transitions_field) ==
              13 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, connect_time_ms_field) ==
              14 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, total_latency_us_field) ==
              15 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, min_latency_us_field) ==
              16 * sizeof(MetricsAtomicU64));
static_assert(offsetof(rrr::ConnectionMetrics, max_latency_us_field) ==
              17 * sizeof(MetricsAtomicU64));

static_assert(std::is_same_v<
              std::underlying_type_t<rrr::CompletionStatus>, std::int32_t>);
static_assert(sizeof(rrr::CompletionStatus) == 4);
static_assert(alignof(rrr::CompletionStatus) == 4);
static_assert(std::is_trivially_copyable_v<rrr::CompletionStatus>);

static_assert(std::is_standard_layout_v<rrr::CompletionTrackerConfig>);
static_assert(std::is_trivially_copyable_v<rrr::CompletionTrackerConfig>);
static_assert(rrr::CompletionTrackerConfig::is_send);
static_assert(rrr::CompletionTrackerConfig::is_sync);
static_assert(sizeof(rrr::CompletionTrackerConfig) == 24);
static_assert(alignof(rrr::CompletionTrackerConfig) == 8);
static_assert(offsetof(rrr::CompletionTrackerConfig, ttl_ms) == 0);
static_assert(offsetof(rrr::CompletionTrackerConfig, max_entries) == 8);
static_assert(offsetof(rrr::CompletionTrackerConfig, enabled) == 16);

static_assert(std::is_standard_layout_v<rrr::CompletedEntry>);
static_assert(std::is_trivially_copyable_v<rrr::CompletedEntry>);
static_assert(rrr::CompletedEntry::is_send);
static_assert(rrr::CompletedEntry::is_sync);
static_assert(sizeof(rrr::CompletedEntry) == 16);
static_assert(alignof(rrr::CompletedEntry) == 8);
static_assert(offsetof(rrr::CompletedEntry, xid) == 0);
static_assert(offsetof(rrr::CompletedEntry, timestamp_ms) == 8);

static_assert(std::is_standard_layout_v<rrr::CompletionQueryResult>);
static_assert(std::is_trivially_copyable_v<rrr::CompletionQueryResult>);
static_assert(rrr::CompletionQueryResult::is_send);
static_assert(rrr::CompletionQueryResult::is_sync);
static_assert(sizeof(rrr::CompletionQueryResult) == 12);
static_assert(alignof(rrr::CompletionQueryResult) == 4);
static_assert(offsetof(rrr::CompletionQueryResult, status) == 0);
static_assert(offsetof(rrr::CompletionQueryResult, error_code) == 4);
static_assert(offsetof(rrr::CompletionQueryResult, has_cached_response) == 8);

static_assert(std::is_standard_layout_v<rrr::CompletionTracker>);
static_assert(rrr::CompletionTracker::is_send);
static_assert(rrr::CompletionTracker::is_sync);
static_assert(sizeof(rrr::CompletionTracker) == 256);
static_assert(alignof(rrr::CompletionTracker) == 8);
static_assert(offsetof(rrr::CompletionTracker, config_) == 0);
static_assert(offsetof(rrr::CompletionTracker, lru_list_) == 64);
static_assert(offsetof(rrr::CompletionTracker, completed_set_) == 136);
static_assert(offsetof(rrr::CompletionTracker, total_tracked_) == 224);
static_assert(offsetof(rrr::CompletionTracker, queries_) == 232);
static_assert(offsetof(rrr::CompletionTracker, query_hits_) == 240);
static_assert(offsetof(rrr::CompletionTracker, evictions_) == 248);
static_assert(std::is_same_v<
              decltype(&rrr::CompletionTracker::mark_completed),
              void (rrr::CompletionTracker::*)(std::int64_t, std::uint64_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::CompletionTracker::is_completed),
              bool (rrr::CompletionTracker::*)(std::int64_t, std::uint64_t)>);
static_assert(std::is_same_v<
              decltype(&rrr::CompletionTracker::set_config),
              void (rrr::CompletionTracker::*)(rrr::CompletionTrackerConfig)>);
static_assert(std::is_same_v<
              decltype(&rrr::CompletionTracker::config),
              rrr::CompletionTrackerConfig (rrr::CompletionTracker::*)() const>);

static bool stat_is(
    const rrr::AvgStat& stat,
    std::int64_t count,
    std::int64_t sum,
    std::int64_t average,
    std::int64_t maximum,
    std::int64_t minimum) {
    return stat.n_stat_ == count && stat.sum_ == sum &&
           stat.avg_ == average && stat.max_ == maximum &&
           stat.min_ == minimum;
}

static bool metrics_are_reset(const rrr::ConnectionMetrics& metrics) {
    using rusty::sync::atomic::Ordering;
    return metrics.requests_sent() == 0 &&
           metrics.requests_completed() == 0 &&
           metrics.requests_failed() == 0 &&
           metrics.requests_timed_out() == 0 &&
           metrics.in_flight_requests() == 0 &&
           metrics.bytes_sent() == 0 &&
           metrics.bytes_received() == 0 &&
           metrics.reconnect_count() == 0 &&
           metrics.retry_attempts() == 0 &&
           metrics.queue_dropped_requests() == 0 &&
           metrics.circuit_open_rejections() == 0 &&
           metrics.circuit_open_transitions() == 0 &&
           metrics.circuit_half_open_transitions() == 0 &&
           metrics.circuit_closed_transitions() == 0 &&
           metrics.connect_time_ms() == 0 &&
           metrics.total_latency_us_field.load(Ordering::Relaxed) == 0 &&
           metrics.min_latency_us_field.load(Ordering::Relaxed) ==
               std::numeric_limits<std::uint64_t>::max() &&
           metrics.min_latency_us() == 0 &&
           metrics.max_latency_us() == 0 &&
           metrics.avg_latency_us() == 0 &&
           metrics.success_rate_percent() == 100;
}

static bool metrics_concurrent_updates_are_atomic() {
    constexpr std::uint64_t kThreads = 8;
    constexpr std::uint64_t kOpsPerThread = 2000;
    constexpr std::uint64_t kRounds = 3;
    constexpr std::uint64_t kUpdates = kThreads * kOpsPerThread;
    constexpr std::uint64_t kLatencyTotal =
        kOpsPerThread * kThreads * (kThreads + 1) / 2;

    for (std::uint64_t round = 0; round < kRounds; ++round) {
        auto metrics = rrr::ConnectionMetrics::new_();
        std::atomic<std::uint64_t> ready{0};
        std::atomic<bool> start{false};
        std::vector<std::thread> workers;
        workers.reserve(kThreads);

        for (std::uint64_t thread_index = 0;
             thread_index < kThreads;
             ++thread_index) {
            workers.emplace_back([&, latency = thread_index + 1] {
                ready.fetch_add(1, std::memory_order_relaxed);
                while (!start.load(std::memory_order_acquire)) {
                    std::this_thread::yield();
                }
                for (std::uint64_t operation = 0;
                     operation < kOpsPerThread;
                     ++operation) {
                    metrics.record_request_sent();
                    metrics.record_request_completed_with_latency(latency);
                    metrics.record_request_sent();
                    metrics.record_request_failed();
                    metrics.record_request_sent();
                    metrics.record_request_timeout();
                    metrics.record_request_sent();
                    metrics.record_request_dropped();
                    metrics.record_bytes_sent(3);
                    metrics.record_bytes_received(5);
                    metrics.record_reconnect();
                    metrics.record_retry_attempt();
                    metrics.record_queue_drop();
                    metrics.record_circuit_open_rejection();
                    metrics.record_circuit_open_transition();
                    metrics.record_circuit_half_open_transition();
                    metrics.record_circuit_closed_transition();
                }
            });
        }
        while (ready.load(std::memory_order_acquire) != kThreads) {
            std::this_thread::yield();
        }
        start.store(true, std::memory_order_release);
        for (auto& worker : workers) {
            worker.join();
        }

        if (metrics.requests_sent() != 4 * kUpdates ||
            metrics.requests_completed() != kUpdates ||
            metrics.requests_failed() != kUpdates ||
            metrics.requests_timed_out() != kUpdates ||
            metrics.in_flight_requests() != 0 ||
            metrics.bytes_sent() != 3 * kUpdates ||
            metrics.bytes_received() != 5 * kUpdates ||
            metrics.reconnect_count() != kUpdates ||
            metrics.retry_attempts() != kUpdates ||
            metrics.queue_dropped_requests() != kUpdates ||
            metrics.circuit_open_rejections() != kUpdates ||
            metrics.circuit_open_transitions() != kUpdates ||
            metrics.circuit_half_open_transitions() != kUpdates ||
            metrics.circuit_closed_transitions() != kUpdates ||
            metrics.total_latency_us_field.load(
                rusty::sync::atomic::Ordering::Relaxed) != kLatencyTotal ||
            metrics.min_latency_us() != 1 ||
            metrics.max_latency_us() != kThreads ||
            metrics.avg_latency_us() != kLatencyTotal / kUpdates ||
            metrics.success_rate_percent() != 25) {
            return false;
        }
        for (std::uint64_t extra = 0; extra < kThreads; ++extra) {
            metrics.record_request_dropped();
        }
        if (metrics.in_flight_requests() != 0) {
            return false;
        }
    }
    return true;
}

static bool completion_tracker_concurrent_operations_are_safe() {
    constexpr std::uint64_t kThreads = 8;
    constexpr std::uint64_t kOpsPerThread = 500;
    constexpr std::uint64_t kRounds = 3;
    constexpr std::uint64_t kUpdates = kThreads * kOpsPerThread;

    for (std::uint64_t round = 0; round < kRounds; ++round) {
        auto config = rrr::CompletionTrackerConfig::defaults();
        config.ttl_ms = 0;
        config.max_entries = kUpdates + 1;
        rrr::CompletionTracker tracker(config);
        std::atomic<std::uint64_t> ready{0};
        std::atomic<bool> start{false};
        std::atomic<bool> failed{false};
        std::vector<std::thread> workers;
        workers.reserve(kThreads);

        for (std::uint64_t thread_index = 0;
             thread_index < kThreads;
             ++thread_index) {
            workers.emplace_back([&, thread_index] {
                ready.fetch_add(1, std::memory_order_relaxed);
                while (!start.load(std::memory_order_acquire)) {
                    std::this_thread::yield();
                }
                for (std::uint64_t operation = 0;
                     operation < kOpsPerThread;
                     ++operation) {
                    const auto xid = static_cast<std::int64_t>(
                        thread_index * kOpsPerThread + operation + 1);
                    tracker.mark_completed(xid, operation);
                    if (!tracker.is_completed(xid, operation)) {
                        failed.store(true, std::memory_order_relaxed);
                    }
                }
            });
        }
        while (ready.load(std::memory_order_acquire) != kThreads) {
            std::this_thread::yield();
        }
        start.store(true, std::memory_order_release);
        for (auto& worker : workers) {
            worker.join();
        }

        if (failed.load(std::memory_order_relaxed) ||
            tracker.size() != kUpdates ||
            tracker.total_tracked() != kUpdates ||
            tracker.queries() != kUpdates ||
            tracker.query_hits() != kUpdates ||
            tracker.evictions() != 0 || tracker.hit_rate() != 1.0) {
            return false;
        }
    }
    return true;
}

template <typename I>
static bool basetypes_round_trip(I value) {
    std::array<std::uint8_t, 12> encoded{};
    const auto sentinel = static_cast<std::uint8_t>(value) ^ 0xff;
    encoded.fill(sentinel);
    std::size_t size = 0;
    I decoded{};
    if constexpr (sizeof(I) == 4) {
        size = rrr::SparseInt::dump32(value, encoded.data());
        decoded = rrr::SparseInt::load32(encoded.data());
    } else {
        size = rrr::SparseInt::dump64(value, encoded.data());
        decoded = rrr::SparseInt::load64(encoded.data());
    }
    if (size != rrr::SparseInt::val_size(static_cast<std::int64_t>(value)) ||
        rrr::SparseInt::buf_size(encoded[0]) != size || decoded != value) {
        return false;
    }
    if constexpr (sizeof(I) == 8) {
        if (size == 8) {
            return encoded[8] != sentinel && encoded[9] == sentinel;
        }
    }
    return encoded[size] == sentinel;
}

static rrr::QueuedRequest make_queued_request(
    std::int64_t xid,
    rrr::QueuedRequestCallback callback = {}) {
    auto request = rrr::QueuedRequest::new_();
    request.xid = xid;
    request.callback = std::move(callback);
    return request;
}

int main() {
    constexpr int kMin = (-2147483647 - 1);
    if (rrr::kInternalHeartbeatRpcId != kMin) {
        return 1;
    }
    if (rrr::kResponseHeaderExtFlag != 0x80000000u ||
        rrr::kResponseSizeMask != 0x7fffffffu) {
        return 2;
    }
    struct Row {
        int input;
        bool has_extended;
        int payload;
        int plain;
        int extended;
    };
    constexpr Row rows[] = {
        {0, false, 0, 0, kMin},
        {1, false, 1, 1, kMin + 1},
        {2147483647, false, 2147483647, 2147483647, -1},
        {kMin, true, 0, 0, kMin},
        {kMin + 1, true, 1, 1, kMin + 1},
        {-1, true, 2147483647, 2147483647, -1},
    };
    for (const auto& row : rows) {
        if (rrr::response_has_extended_header(row.input) != row.has_extended) {
            return 3;
        }
        if (rrr::response_payload_size(row.input) != row.payload) {
            return 4;
        }
        if (rrr::encode_response_size(row.input, false) != row.plain) {
            return 5;
        }
        if (rrr::encode_response_size(row.input, true) != row.extended) {
            return 6;
        }
    }

    auto stat = rrr::AvgStat::new_();
    if (!stat_is(stat, 0, 0, 0, 0, 0) || stat.avg() != 0) {
        return 10;
    }
    stat.sample(3);
    stat.sample(-5);
    stat.sample(8);
    if (!stat_is(stat, 3, 6, 2, 8, -5) || stat.avg() != 2) {
        return 11;
    }
    const auto peeked = stat.peek();
    if (!stat_is(peeked, 3, 6, 2, 8, -5) ||
        !stat_is(stat, 3, 6, 2, 8, -5)) {
        return 12;
    }
    const auto reset = stat.reset();
    if (!stat_is(reset, 3, 6, 2, 8, -5) ||
        !stat_is(stat, 0, 0, 0, 0, 0)) {
        return 13;
    }
    stat.sample(-7);
    stat.sample(-2);
    if (!stat_is(stat, 2, -9, -4, 0, -7) || stat.avg() != -4) {
        return 14;
    }
    stat.clear();
    if (!stat_is(stat, 0, 0, 0, 0, 0)) {
        return 15;
    }

    struct CategoryRow {
        rrr::RpcErrorCategory category;
        int discriminant;
        std::string_view name;
    };
    constexpr CategoryRow categories[] = {
        {rrr::RpcErrorCategory::NONE, 0, "NONE"},
        {rrr::RpcErrorCategory::CONNECTION, 1, "CONNECTION"},
        {rrr::RpcErrorCategory::PROTOCOL, 2, "PROTOCOL"},
        {rrr::RpcErrorCategory::APPLICATION, 3, "APPLICATION"},
        {rrr::RpcErrorCategory::TIMEOUT, 4, "TIMEOUT"},
        {rrr::RpcErrorCategory::INTERNAL, 5, "INTERNAL"},
    };
    for (const auto& row : categories) {
        if (static_cast<int>(row.category) != row.discriminant ||
            rrr::rpc_error_category_to_string(row.category) != row.name) {
            return 20;
        }
    }
    constexpr int invalid_categories[] = {-1, 6, 999};
    for (const auto value : invalid_categories) {
        if (rrr::rpc_error_category_to_string(
                static_cast<rrr::RpcErrorCategory>(value)) != "UNKNOWN") {
            return 21;
        }
    }

    struct ErrorRow {
        rrr::RpcError error;
        int discriminant;
        std::string_view name;
        rrr::RpcErrorCategory category;
        bool retryable;
    };
    constexpr ErrorRow errors[] = {
        {rrr::RpcError::OK, 0, "OK", rrr::RpcErrorCategory::NONE, false},
        {rrr::RpcError::NOT_CONNECTED, 100, "NOT_CONNECTED", rrr::RpcErrorCategory::CONNECTION, false},
        {rrr::RpcError::CONNECTION_REFUSED, 101, "CONNECTION_REFUSED", rrr::RpcErrorCategory::CONNECTION, false},
        {rrr::RpcError::CONNECTION_RESET, 102, "CONNECTION_RESET", rrr::RpcErrorCategory::CONNECTION, true},
        {rrr::RpcError::NETWORK_UNREACHABLE, 103, "NETWORK_UNREACHABLE", rrr::RpcErrorCategory::CONNECTION, true},
        {rrr::RpcError::HOST_UNREACHABLE, 104, "HOST_UNREACHABLE", rrr::RpcErrorCategory::CONNECTION, true},
        {rrr::RpcError::CONNECTION_CLOSED, 105, "CONNECTION_CLOSED", rrr::RpcErrorCategory::CONNECTION, false},
        {rrr::RpcError::CIRCUIT_OPEN, 106, "CIRCUIT_OPEN", rrr::RpcErrorCategory::CONNECTION, false},
        {rrr::RpcError::INVALID_MESSAGE, 200, "INVALID_MESSAGE", rrr::RpcErrorCategory::PROTOCOL, false},
        {rrr::RpcError::UNKNOWN_RPC_ID, 201, "UNKNOWN_RPC_ID", rrr::RpcErrorCategory::PROTOCOL, false},
        {rrr::RpcError::MARSHALLING_ERROR, 202, "MARSHALLING_ERROR", rrr::RpcErrorCategory::PROTOCOL, false},
        {rrr::RpcError::VERSION_MISMATCH, 203, "VERSION_MISMATCH", rrr::RpcErrorCategory::PROTOCOL, false},
        {rrr::RpcError::CHECKSUM_ERROR, 204, "CHECKSUM_ERROR", rrr::RpcErrorCategory::PROTOCOL, false},
        {rrr::RpcError::RPC_FAILED, 300, "RPC_FAILED", rrr::RpcErrorCategory::APPLICATION, false},
        {rrr::RpcError::SERVICE_UNAVAILABLE, 301, "SERVICE_UNAVAILABLE", rrr::RpcErrorCategory::APPLICATION, true},
        {rrr::RpcError::PERMISSION_DENIED, 302, "PERMISSION_DENIED", rrr::RpcErrorCategory::APPLICATION, false},
        {rrr::RpcError::INVALID_ARGUMENT, 303, "INVALID_ARGUMENT", rrr::RpcErrorCategory::APPLICATION, false},
        {rrr::RpcError::NOT_FOUND, 304, "NOT_FOUND", rrr::RpcErrorCategory::APPLICATION, false},
        {rrr::RpcError::ALREADY_EXISTS, 305, "ALREADY_EXISTS", rrr::RpcErrorCategory::APPLICATION, false},
        {rrr::RpcError::CONNECT_TIMEOUT, 400, "CONNECT_TIMEOUT", rrr::RpcErrorCategory::TIMEOUT, true},
        {rrr::RpcError::REQUEST_TIMEOUT, 401, "REQUEST_TIMEOUT", rrr::RpcErrorCategory::TIMEOUT, true},
        {rrr::RpcError::RESPONSE_TIMEOUT, 402, "RESPONSE_TIMEOUT", rrr::RpcErrorCategory::TIMEOUT, true},
        {rrr::RpcError::IDLE_TIMEOUT, 403, "IDLE_TIMEOUT", rrr::RpcErrorCategory::TIMEOUT, false},
        {rrr::RpcError::HEARTBEAT_TIMEOUT, 404, "HEARTBEAT_TIMEOUT", rrr::RpcErrorCategory::TIMEOUT, false},
        {rrr::RpcError::UNKNOWN_ERROR, 500, "UNKNOWN_ERROR", rrr::RpcErrorCategory::INTERNAL, false},
        {rrr::RpcError::OUT_OF_MEMORY, 501, "OUT_OF_MEMORY", rrr::RpcErrorCategory::INTERNAL, false},
        {rrr::RpcError::INVALID_STATE, 502, "INVALID_STATE", rrr::RpcErrorCategory::INTERNAL, false},
        {rrr::RpcError::INTERNAL_ERROR, 503, "INTERNAL_ERROR", rrr::RpcErrorCategory::INTERNAL, false},
    };
    for (const auto& row : errors) {
        if (static_cast<int>(row.error) != row.discriminant ||
            rrr::rpc_error_to_string(row.error) != row.name ||
            rrr::get_error_category(row.error) != row.category ||
            rrr::is_connection_error(row.error) !=
                (row.category == rrr::RpcErrorCategory::CONNECTION) ||
            rrr::is_timeout_error(row.error) !=
                (row.category == rrr::RpcErrorCategory::TIMEOUT) ||
            rrr::is_retryable_error(row.error) != row.retryable) {
            return 22;
        }
    }

    struct ErrorBoundaryRow {
        int code;
        std::string_view name;
        rrr::RpcErrorCategory category;
        bool connection;
        bool timeout;
        bool retryable;
    };
    constexpr ErrorBoundaryRow boundaries[] = {
        {99, "UNKNOWN", rrr::RpcErrorCategory::INTERNAL, false, false, false},
        {100, "NOT_CONNECTED", rrr::RpcErrorCategory::CONNECTION, true, false, false},
        {199, "UNKNOWN", rrr::RpcErrorCategory::CONNECTION, true, false, false},
        {200, "INVALID_MESSAGE", rrr::RpcErrorCategory::PROTOCOL, false, false, false},
        {399, "UNKNOWN", rrr::RpcErrorCategory::APPLICATION, false, false, false},
        {400, "CONNECT_TIMEOUT", rrr::RpcErrorCategory::TIMEOUT, false, true, true},
        {499, "UNKNOWN", rrr::RpcErrorCategory::TIMEOUT, false, true, false},
        {500, "UNKNOWN_ERROR", rrr::RpcErrorCategory::INTERNAL, false, false, false},
        {999, "UNKNOWN", rrr::RpcErrorCategory::INTERNAL, false, false, false},
    };
    for (const auto& row : boundaries) {
        const auto error = static_cast<rrr::RpcError>(row.code);
        if (rrr::rpc_error_to_string(error) != row.name ||
            rrr::get_error_category(error) != row.category ||
            rrr::is_connection_error(error) != row.connection ||
            rrr::is_timeout_error(error) != row.timeout ||
            rrr::is_retryable_error(error) != row.retryable) {
            return 23;
        }
    }

    auto metrics = rrr::ConnectionMetrics::new_();
    if (!metrics_are_reset(metrics) || metrics.uptime_ms(1234) != 0) {
        return 30;
    }
    metrics.record_request_dropped();
    if (metrics.in_flight_requests() != 0) {
        return 31;
    }
    metrics.record_request_sent();
    metrics.record_request_sent();
    metrics.record_request_sent();
    metrics.record_request_completed_with_latency(30);
    metrics.record_request_completed_with_latency(10);
    if (metrics.requests_sent() != 3 ||
        metrics.requests_completed() != 2 ||
        metrics.in_flight_requests() != 1 ||
        metrics.total_latency_us_field.load(
            rusty::sync::atomic::Ordering::Relaxed) != 40 ||
        metrics.min_latency_us() != 10 || metrics.max_latency_us() != 30 ||
        metrics.avg_latency_us() != 20 ||
        metrics.success_rate_percent() != 66) {
        return 32;
    }
    metrics.record_request_failed();
    metrics.record_request_timeout();
    metrics.record_request_dropped();
    if (metrics.requests_failed() != 1 ||
        metrics.requests_timed_out() != 1 ||
        metrics.in_flight_requests() != 0) {
        return 33;
    }
    metrics.record_request_completed();
    if (metrics.requests_completed() != 3 ||
        metrics.avg_latency_us() != 13 ||
        metrics.success_rate_percent() != 100 ||
        metrics.in_flight_requests() != 0) {
        return 34;
    }

    metrics.record_bytes_sent(11);
    metrics.record_bytes_sent(7);
    metrics.record_bytes_received(23);
    metrics.record_reconnect();
    metrics.record_retry_attempt();
    metrics.record_queue_drop();
    metrics.record_circuit_open_rejection();
    metrics.record_circuit_open_transition();
    metrics.record_circuit_half_open_transition();
    metrics.record_circuit_closed_transition();
    if (metrics.bytes_sent() != 18 || metrics.bytes_received() != 23 ||
        metrics.reconnect_count() != 1 || metrics.retry_attempts() != 1 ||
        metrics.queue_dropped_requests() != 1 ||
        metrics.circuit_open_rejections() != 1 ||
        metrics.circuit_open_transitions() != 1 ||
        metrics.circuit_half_open_transitions() != 1 ||
        metrics.circuit_closed_transitions() != 1) {
        return 35;
    }

    metrics.record_connect(1000);
    if (metrics.connect_time_ms() != 1000 || metrics.uptime_ms(999) != 0 ||
        metrics.uptime_ms(1000) != 0 || metrics.uptime_ms(1123) != 123) {
        return 36;
    }
    const auto metrics_snapshot = metrics;
    metrics.record_bytes_sent(1);
    if (metrics_snapshot.bytes_sent() != 18 || metrics.bytes_sent() != 19) {
        return 37;
    }
    metrics.reset();
    if (!metrics_are_reset(metrics)) {
        return 38;
    }
    metrics.bytes_sent_field.store(
        std::numeric_limits<std::uint64_t>::max(),
        rusty::sync::atomic::Ordering::Relaxed);
    metrics.record_bytes_sent(1);
    if (metrics.bytes_sent() != 0) {
        return 39;
    }
    metrics.requests_completed_field.store(
        std::numeric_limits<std::uint64_t>::max(),
        rusty::sync::atomic::Ordering::Relaxed);
    metrics.requests_sent_field.store(
        3, rusty::sync::atomic::Ordering::Relaxed);
    constexpr auto kWrappedPercent =
        (std::numeric_limits<std::uint64_t>::max() * std::uint64_t{100}) /
        std::uint64_t{3};
    if (metrics.success_rate_percent() != kWrappedPercent) {
        return 40;
    }
    if (!metrics_concurrent_updates_are_atomic()) {
        return 41;
    }

    CallbackActual empty;
    if (empty.has_value() || CallbackActual::default_().has_value()) {
        return 50;
    }

    std::vector<int> observations;
    auto original = CallbackActual::from_callable(
        CallbackStatefulCallable{&observations});
    auto copy = original;
    auto cloned = original.clone();
    original.callable()(1);
    copy.callable()(2);
    cloned.callable()(3);
    if (observations != std::vector<int>{1, 2, 3}) {
        return 51;
    }
    if (&original.callable() != &copy.callable() ||
        &original.callable() != &cloned.callable()) {
        return 52;
    }

    auto owned = std::make_unique<int>(41);
    int result = 0;
    auto move_only = CallbackActual::from_callable(
        [payload = std::move(owned), &result](int value) {
            result = *payload + value;
        });
    if (owned != nullptr || !move_only.has_value()) {
        return 53;
    }
    move_only.callable()(1);
    if (result != 42) {
        return 54;
    }

    int named_result = 0;
    std::function<void(int)> named =
        [&](int value) { named_result = value; };
    auto named_wrapper = CallbackActual::from_callable(std::move(named));
    named_wrapper.callable()(17);
    if (named_result != 17) {
        return 55;
    }

    using CallbackActualMove =
        rrr::detail::CallbackWrapper<CallbackMoveObservedCallable>;
    using CallbackOracleMove =
        callback_oracle::CallbackWrapper<CallbackMoveObservedCallable>;
    static_assert(sizeof(CallbackActualMove) == sizeof(CallbackOracleMove));
    static_assert(alignof(CallbackActualMove) == alignof(CallbackOracleMove));
    auto actual_moves = std::make_shared<int>(0);
    auto oracle_moves = std::make_shared<int>(0);
    auto actual_move_wrapper = CallbackActualMove::from_callable(
        CallbackMoveObservedCallable{actual_moves});
    auto oracle_move_wrapper = CallbackOracleMove::from_callable(
        CallbackMoveObservedCallable{oracle_moves});
    if (!actual_move_wrapper.has_value() ||
        !oracle_move_wrapper.has_value()) {
        return 56;
    }
    if (*actual_moves != 1 || *oracle_moves != 1 ||
        *actual_moves != *oracle_moves) {
        return 57;
    }

    const auto completion_defaults =
        rrr::CompletionTrackerConfig::defaults();
    const auto completion_small = rrr::CompletionTrackerConfig::small();
    const auto completion_large = rrr::CompletionTrackerConfig::large();
    const auto completion_disabled =
        rrr::CompletionTrackerConfig::disabled();
    if (completion_defaults.ttl_ms != 60000 ||
        completion_defaults.max_entries != 100000 ||
        !completion_defaults.enabled ||
        completion_small.ttl_ms != 30000 ||
        completion_small.max_entries != 10000 ||
        !completion_small.enabled ||
        completion_large.ttl_ms != 300000 ||
        completion_large.max_entries != 1000000 ||
        !completion_large.enabled || completion_disabled.enabled) {
        return 60;
    }

    const auto completion_not_found =
        rrr::CompletionQueryResult::not_found();
    const auto completion_ok =
        rrr::CompletionQueryResult::completed(0, true);
    const auto completion_error =
        rrr::CompletionQueryResult::completed(-7, false);
    const auto completion_expired =
        rrr::CompletionQueryResult::expired();
    if (completion_not_found.status != rrr::CompletionStatus::NOT_FOUND ||
        completion_not_found.error_code != 0 ||
        completion_not_found.has_cached_response ||
        completion_not_found.is_completed() ||
        completion_ok.status != rrr::CompletionStatus::COMPLETED ||
        completion_ok.error_code != 0 ||
        !completion_ok.has_cached_response || !completion_ok.is_completed() ||
        completion_error.status !=
            rrr::CompletionStatus::COMPLETED_WITH_ERROR ||
        completion_error.error_code != -7 ||
        completion_error.has_cached_response ||
        !completion_error.is_completed() ||
        completion_expired.status != rrr::CompletionStatus::EXPIRED ||
        completion_expired.is_completed() ||
        rrr::completion_status_to_string(rrr::CompletionStatus::NOT_FOUND) !=
            "NOT_FOUND" ||
        rrr::completion_status_to_string(rrr::CompletionStatus::COMPLETED) !=
            "COMPLETED" ||
        rrr::completion_status_to_string(
            rrr::CompletionStatus::COMPLETED_WITH_ERROR) !=
            "COMPLETED_WITH_ERROR" ||
        rrr::completion_status_to_string(rrr::CompletionStatus::EXPIRED) !=
            "EXPIRED" ||
        rrr::completion_status_to_string(
            static_cast<rrr::CompletionStatus>(99)) != "UNKNOWN") {
        return 61;
    }

    const auto wrapping_entry = rrr::CompletedEntry::new_(
        77, std::numeric_limits<std::uint64_t>::max() - 5);
    if (wrapping_entry.xid != 77 ||
        wrapping_entry.timestamp_ms !=
            std::numeric_limits<std::uint64_t>::max() - 5 ||
        wrapping_entry.is_expired(1000, 0) ||
        wrapping_entry.is_expired(4, 10) ||
        !wrapping_entry.is_expired(5, 10)) {
        return 62;
    }

    rrr::CompletionTracker disabled_tracker(completion_disabled);
    disabled_tracker.mark_completed(1, 0);
    if (disabled_tracker.enabled() || disabled_tracker.size() != 0 ||
        disabled_tracker.total_tracked() != 0 ||
        disabled_tracker.is_completed(1, 0) ||
        disabled_tracker.queries() != 1 ||
        disabled_tracker.query_hits() != 0) {
        return 63;
    }

    auto lifecycle_config = rrr::CompletionTrackerConfig::defaults();
    lifecycle_config.ttl_ms = 10;
    lifecycle_config.max_entries = 2;
    rrr::CompletionTracker lifecycle_tracker(lifecycle_config);
    lifecycle_tracker.mark_completed(1, 0);
    lifecycle_tracker.mark_completed(1, 1);
    lifecycle_tracker.mark_completed(2, 0);
    if (lifecycle_tracker.size() != 2 ||
        lifecycle_tracker.total_tracked() != 2 ||
        lifecycle_tracker.queries() != 0 ||
        lifecycle_tracker.hit_rate() != 0.0 ||
        !lifecycle_tracker.is_completed(1, 10) ||
        lifecycle_tracker.is_completed(1, 11) ||
        lifecycle_tracker.size() != 1 ||
        lifecycle_tracker.queries() != 2 ||
        lifecycle_tracker.query_hits() != 1) {
        return 64;
    }
    lifecycle_tracker.mark_completed(3, 20);
    lifecycle_tracker.mark_completed(4, 20);
    if (lifecycle_tracker.size() != 2 ||
        lifecycle_tracker.total_tracked() != 4 ||
        lifecycle_tracker.evictions() != 1 ||
        lifecycle_tracker.is_completed(2, 20) ||
        lifecycle_tracker.evict_expired(31) != 2 ||
        lifecycle_tracker.size() != 0 ||
        lifecycle_tracker.evictions() != 3) {
        return 65;
    }

    auto mutation_config = rrr::CompletionTrackerConfig::defaults();
    mutation_config.ttl_ms = 0;
    rrr::CompletionTracker mutation_tracker(mutation_config);
    mutation_tracker.mark_completed(10, 1);
    mutation_tracker.mark_completed(11, 1);
    if (!mutation_tracker.remove(10) || mutation_tracker.remove(10) ||
        mutation_tracker.size() != 1) {
        return 66;
    }
    mutation_tracker.clear();
    mutation_tracker.set_config(completion_disabled);
    const auto mutated_config = mutation_tracker.config();
    mutation_tracker.mark_completed(12, 1);
    if (mutation_tracker.size() != 0 || mutated_config.enabled ||
        mutated_config.ttl_ms != completion_disabled.ttl_ms ||
        mutated_config.max_entries != completion_disabled.max_entries) {
        return 67;
    }

    auto overflow_config = rrr::CompletionTrackerConfig::defaults();
    overflow_config.ttl_ms = 0;
    overflow_config.max_entries = 1;
    rrr::CompletionTracker overflow_tracker(overflow_config);
    overflow_tracker.mark_completed(1, 0);
    using rusty::sync::atomic::Ordering;
    overflow_tracker.total_tracked_.store(
        std::numeric_limits<std::uint64_t>::max(), Ordering::Relaxed);
    overflow_tracker.queries_.store(
        std::numeric_limits<std::uint64_t>::max(), Ordering::Relaxed);
    overflow_tracker.query_hits_.store(
        std::numeric_limits<std::uint64_t>::max(), Ordering::Relaxed);
    overflow_tracker.evictions_.store(
        std::numeric_limits<std::uint64_t>::max(), Ordering::Relaxed);
    overflow_tracker.mark_completed(2, 0);
    if (!overflow_tracker.is_completed(2, 0) ||
        overflow_tracker.size() != 1 ||
        overflow_tracker.total_tracked() != 0 ||
        overflow_tracker.queries() != 0 ||
        overflow_tracker.query_hits() != 0 ||
        overflow_tracker.evictions() != 0 ||
        overflow_tracker.hit_rate() != 0.0) {
        return 68;
    }
    overflow_tracker.reset_stats();
    if (overflow_tracker.total_tracked() != 0 ||
        overflow_tracker.queries() != 0 ||
        overflow_tracker.query_hits() != 0 ||
        overflow_tracker.evictions() != 0) {
        return 69;
    }
    if (!completion_tracker_concurrent_operations_are_safe()) {
        return 70;
    }

    if (rrr::randgen_rand_max() !=
            static_cast<double>(std::numeric_limits<std::int32_t>::max()) ||
        rrr::randgen_nu_constant_now() != 0) {
        return 71;
    }

    rand_string_evaluations = 0;
    const auto padded_binary =
        rrr::randgen_zero_pad(make_rand_binary_string(), 5);
    const auto truncated_binary =
        rrr::randgen_zero_pad(make_rand_binary_string(), 2);
    if (rand_string_evaluations != 2 || padded_binary.size() != 5 ||
        padded_binary[0] != '0' || padded_binary[1] != '0' ||
        static_cast<unsigned char>(padded_binary[2]) != 0x00 ||
        static_cast<unsigned char>(padded_binary[3]) != 0x80 ||
        static_cast<unsigned char>(padded_binary[4]) != 0xff ||
        truncated_binary.size() != 2 ||
        static_cast<unsigned char>(truncated_binary[0]) != 0x80 ||
        static_cast<unsigned char>(truncated_binary[1]) != 0xff ||
        rrr::randgen_zero_pad("7", 3) != "007" ||
        rrr::randgen_zero_pad("1234", 3) != "234" ||
        rrr::randgen_zero_pad("1234", 0) != "") {
        return 72;
    }

    if (rrr::RandomGenerator::int2str_n(0, 1) != "0" ||
        rrr::RandomGenerator::int2str_n(42, 5) != "00042" ||
        rrr::RandomGenerator::int2str_n(-7, 4) != "00-7" ||
        rrr::RandomGenerator::int2str_n(12345, 3) != "345" ||
        rrr::RandomGenerator::int2str_n(-12345, 4) != "2345" ||
        rrr::RandomGenerator::int2str_n(
            std::numeric_limits<std::int32_t>::max(), 10) != "2147483647" ||
        rrr::RandomGenerator::int2str_n(
            std::numeric_limits<std::int32_t>::min(), 11) != "-2147483648" ||
        rrr::RandomGenerator::int2str_n(
            std::numeric_limits<std::int32_t>::min(), 10) != "2147483648") {
        return 73;
    }

    install_rand_raw(17);
    if (rrr::randgen_rand_raw() != 17 || rand_raw_draws != 1) {
        return 74;
    }
    install_rand_raw(5);
    if (rrr::RandomGenerator::rand(-10, -5) != -5 || rand_raw_draws != 1) {
        return 75;
    }
    install_rand_raw(std::numeric_limits<std::int32_t>::max());
    if (rrr::RandomGenerator::rand(
            0, std::numeric_limits<std::int32_t>::max()) !=
            std::numeric_limits<std::int32_t>::max() ||
        rand_raw_draws != 1) {
        return 76;
    }

    install_rand_raw(123);
    if (rrr::RandomGenerator::rand_double(4.5, 4.5) != 4.5 ||
        rand_raw_draws != 0) {
        return 77;
    }
    const auto scaled_rand = rrr::RandomGenerator::rand_double(-1.0, 1.0);
    const auto expected_scaled_rand =
        (123.0 /
         (static_cast<double>(std::numeric_limits<std::int32_t>::max()) / 2.0)) -
        1.0;
    if (scaled_rand != expected_scaled_rand || rand_raw_draws != 1) {
        return 78;
    }

    install_rand_raw(0);
    if (rrr::RandomGenerator::percentage_true(0) || rand_raw_draws != 1) {
        return 79;
    }
    install_rand_raw(0);
    if (!rrr::RandomGenerator::percentage_true(1) || rand_raw_draws != 1) {
        return 80;
    }
    install_rand_raw(5);
    if (rrr::RandomGenerator::nu_rand(1022, 0, 999) != 5 ||
        rand_raw_draws != 2) {
        return 81;
    }

    install_rand_raw(99);
    const std::vector<double> empty_weights;
    if (rrr::RandomGenerator::weighted_select(empty_weights) !=
            std::numeric_limits<std::uint32_t>::max() ||
        rand_raw_draws != 0) {
        return 82;
    }
    install_rand_raw(99);
    const std::vector<double> zero_weights{0.0, 0.0};
    if (rrr::RandomGenerator::weighted_select(zero_weights) != 0 ||
        rand_raw_draws != 0) {
        return 83;
    }

    const std::vector<double> weights{1.0, 2.0, 3.0};
    install_rand_raw(0);
    if (rrr::RandomGenerator::weighted_select(weights) != 0 ||
        rand_raw_draws != 1) {
        return 84;
    }
    install_rand_raw(std::numeric_limits<std::int32_t>::max() / 2);
    if (rrr::RandomGenerator::weighted_select(weights) != 1 ||
        rand_raw_draws != 1) {
        return 85;
    }
    rand_weight_evaluations = 0;
    install_rand_raw(std::numeric_limits<std::int32_t>::max());
    if (rrr::RandomGenerator::weighted_select(make_rand_weights()) != 2 ||
        rand_raw_draws != 1 || rand_weight_evaluations != 1) {
        return 86;
    }

    const auto destroys_before = rand_destroy_calls;
    rrr::randgen_destroy();
    rrr::RandomGenerator::destroy();
    if (rand_destroy_calls != destroys_before + 2) {
        return 87;
    }

    install_rand_raw(std::numeric_limits<std::int32_t>::max());
    if (rrr::RandomGenerator::rand(7, 7) != 7 || rand_raw_draws != 1) {
        return 88;
    }
    install_rand_raw(std::numeric_limits<std::int32_t>::max());
    if (rrr::RandomGenerator::rand(
            std::numeric_limits<std::int32_t>::min(), -1) != -1 ||
        rand_raw_draws != 1) {
        return 89;
    }

    bool rand_failed = false;
    install_rand_raw(std::numeric_limits<std::int32_t>::max());
    try {
        static_cast<void>(rrr::RandomGenerator::rand(
            std::numeric_limits<std::int32_t>::min(),
            std::numeric_limits<std::int32_t>::max()));
    } catch (...) {
        rand_failed = true;
    }
    if (!rand_failed || rand_raw_draws != 1) {
        return 90;
    }

    rand_failed = false;
    install_rand_raw(11);
    try {
        static_cast<void>(rrr::RandomGenerator::rand(9, 8));
    } catch (...) {
        rand_failed = true;
    }
    if (!rand_failed || rand_raw_draws != 0) {
        return 91;
    }

    rand_failed = false;
    install_rand_raw(123);
    try {
        static_cast<void>(rrr::RandomGenerator::rand_double(2.0, 1.0));
    } catch (...) {
        rand_failed = true;
    }
    if (!rand_failed || rand_raw_draws != 0) {
        return 92;
    }

    rand_failed = false;
    install_rand_raw(123);
    try {
        static_cast<void>(rrr::RandomGenerator::rand_double(
            0.0, std::numeric_limits<double>::quiet_NaN()));
    } catch (...) {
        rand_failed = true;
    }
    if (!rand_failed || rand_raw_draws != 0) {
        return 93;
    }

    rand_failed = false;
    install_rand_raw(std::numeric_limits<std::int32_t>::max());
    try {
        static_cast<void>(rrr::RandomGenerator::nu_rand(
            0, std::numeric_limits<std::int32_t>::min(),
            std::numeric_limits<std::int32_t>::max()));
    } catch (...) {
        rand_failed = true;
    }
    if (!rand_failed || rand_raw_draws != 2) {
        return 94;
    }

    const std::vector<double> positive_boundary_weights{
        1.0,
        static_cast<double>(std::numeric_limits<std::int32_t>::max() - 1),
    };
    install_rand_raw(1);
    if (rrr::RandomGenerator::weighted_select(positive_boundary_weights) != 0 ||
        rand_raw_draws != 1) {
        return 95;
    }

    if (static_cast<std::int32_t>(rrr::TimeoutType::NONE) != 0 ||
        static_cast<std::int32_t>(rrr::TimeoutType::CONNECT_TIMEOUT) != 1 ||
        static_cast<std::int32_t>(rrr::TimeoutType::REQUEST_TIMEOUT) != 2 ||
        static_cast<std::int32_t>(rrr::TimeoutType::RESPONSE_TIMEOUT) != 3 ||
        static_cast<std::int32_t>(rrr::TimeoutType::TOTAL_TIMEOUT) != 4 ||
        rrr::TimeoutType_NONE() != rrr::TimeoutType::NONE ||
        rrr::TimeoutType_CONNECT_TIMEOUT() !=
            rrr::TimeoutType::CONNECT_TIMEOUT ||
        rrr::TimeoutType_REQUEST_TIMEOUT() !=
            rrr::TimeoutType::REQUEST_TIMEOUT ||
        rrr::TimeoutType_RESPONSE_TIMEOUT() !=
            rrr::TimeoutType::RESPONSE_TIMEOUT ||
        rrr::TimeoutType_TOTAL_TIMEOUT() != rrr::TimeoutType::TOTAL_TIMEOUT ||
        rrr::timeout_type_to_string(rrr::TimeoutType::NONE) != "NONE" ||
        rrr::timeout_type_to_string(rrr::TimeoutType::CONNECT_TIMEOUT) !=
            "CONNECT_TIMEOUT" ||
        rrr::timeout_type_to_string(rrr::TimeoutType::REQUEST_TIMEOUT) !=
            "REQUEST_TIMEOUT" ||
        rrr::timeout_type_to_string(rrr::TimeoutType::RESPONSE_TIMEOUT) !=
            "RESPONSE_TIMEOUT" ||
        rrr::timeout_type_to_string(rrr::TimeoutType::TOTAL_TIMEOUT) !=
            "TOTAL_TIMEOUT" ||
        rrr::timeout_type_to_string(static_cast<rrr::TimeoutType>(99)) !=
            "UNKNOWN") {
        return 96;
    }

    const auto request_defaults = rrr::RequestOptions::defaults();
    const auto request_new = rrr::RequestOptions::new_();
    if (request_defaults.timeout_ms != 1000 ||
        request_defaults.total_timeout_ms != 0 ||
        request_defaults.max_retries != 0 ||
        request_defaults.base_delay_ms != 50 ||
        request_defaults.max_delay_ms != 5000 ||
        request_defaults.jitter_factor != 0.1f ||
        request_defaults.idempotent ||
        request_new.timeout_ms != request_defaults.timeout_ms ||
        request_new.total_timeout_ms != request_defaults.total_timeout_ms ||
        request_new.max_retries != request_defaults.max_retries ||
        request_new.base_delay_ms != request_defaults.base_delay_ms ||
        request_new.max_delay_ms != request_defaults.max_delay_ms ||
        request_new.jitter_factor != request_defaults.jitter_factor ||
        request_new.idempotent != request_defaults.idempotent ||
        request_defaults.can_retry(0)) {
        return 97;
    }

    const auto request_retry = rrr::RequestOptions::with_retry(3, 2000);
    const auto request_idempotent =
        rrr::RequestOptions::idempotent_retry(10);
    const auto request_no_timeout = rrr::RequestOptions::no_timeout();
    const auto request_fast = rrr::RequestOptions::fast();
    const auto request_patient = rrr::RequestOptions::patient();
    if (request_retry.timeout_ms != 2000 || request_retry.max_retries != 3 ||
        !request_retry.idempotent || !request_retry.can_retry(0) ||
        !request_retry.can_retry(2) || request_retry.can_retry(3) ||
        request_idempotent.timeout_ms != 1000 ||
        request_idempotent.max_retries != 10 ||
        !request_idempotent.idempotent || request_no_timeout.timeout_ms != 0 ||
        request_fast.timeout_ms != 100 || request_fast.max_retries != 2 ||
        request_fast.base_delay_ms != 10 || request_fast.max_delay_ms != 100 ||
        request_patient.timeout_ms != 10000 ||
        request_patient.total_timeout_ms != 60000 ||
        request_patient.max_retries != 5 ||
        request_patient.base_delay_ms != 500 ||
        request_patient.max_delay_ms != 10000) {
        return 98;
    }

    auto request_limited = request_defaults;
    request_limited.total_timeout_ms = 5000;
    if (request_limited.is_total_timeout_exceeded(4999) ||
        !request_limited.is_total_timeout_exceeded(5000) ||
        request_limited.remaining_time_ms(0) != 5000 ||
        request_limited.remaining_time_ms(4999) != 1 ||
        request_limited.remaining_time_ms(5000) != 0 ||
        request_limited.remaining_time_ms(
            std::numeric_limits<std::uint64_t>::max()) != 0 ||
        request_defaults.remaining_time_ms(
            std::numeric_limits<std::uint64_t>::max()) !=
            std::numeric_limits<std::uint64_t>::max()) {
        return 99;
    }

    auto request_delay = request_defaults;
    request_delay.base_delay_ms = 100;
    request_delay.max_delay_ms = 500;
    request_delay.jitter_factor = 0.0f;
    install_rand_raw(17);
    if (request_delay.calculate_delay_ms(0) != 100 ||
        request_delay.calculate_delay_ms(1) != 200 ||
        request_delay.calculate_delay_ms(2) != 400 ||
        request_delay.calculate_delay_ms(3) != 500 ||
        request_delay.calculate_delay_ms(
            std::numeric_limits<std::uint16_t>::max()) != 500 ||
        rand_raw_draws != 0) {
        return 100;
    }

    request_delay.jitter_factor = -0.1f;
    if (request_delay.calculate_delay_ms(0) != 100 || rand_raw_draws != 0) {
        return 101;
    }
    request_delay.jitter_factor = std::numeric_limits<float>::quiet_NaN();
    if (request_delay.calculate_delay_ms(0) != 100 || rand_raw_draws != 0) {
        return 102;
    }

    request_delay.jitter_factor = 0.2f;
    install_rand_raw(0);
    const auto request_low_expected = static_cast<std::uint64_t>(
        100.0 + 100.0 * static_cast<double>(request_delay.jitter_factor) *
                    ((0.0 / static_cast<double>(
                                std::numeric_limits<std::int32_t>::max())) -
                     0.5));
    if (request_delay.calculate_delay_ms(0) != request_low_expected ||
        rand_raw_draws != 1) {
        return 103;
    }
    install_rand_raw(std::numeric_limits<std::int32_t>::max());
    const auto request_high_expected = static_cast<std::uint64_t>(
        100.0 + 100.0 * static_cast<double>(request_delay.jitter_factor) * 0.5);
    if (request_delay.calculate_delay_ms(0) != request_high_expected ||
        rand_raw_draws != 1) {
        return 104;
    }

    request_delay.base_delay_ms = 1000;
    request_delay.max_delay_ms = 500;
    install_rand_raw(std::numeric_limits<std::int32_t>::max());
    const auto request_capped_expected = static_cast<std::uint64_t>(
        500.0 + 500.0 * static_cast<double>(request_delay.jitter_factor) * 0.5);
    if (request_delay.calculate_delay_ms(0) != request_capped_expected ||
        rand_raw_draws != 1) {
        return 105;
    }

    request_delay.base_delay_ms = 0;
    install_rand_raw(123);
    if (request_delay.calculate_delay_ms(
            std::numeric_limits<std::uint16_t>::max()) != 0 ||
        rand_raw_draws != 1) {
        return 106;
    }

    request_delay.base_delay_ms = 100;
    request_delay.max_delay_ms = 500;
    request_delay.jitter_factor = 10.0f;
    install_rand_raw(-std::numeric_limits<std::int32_t>::max());
    if (request_delay.calculate_delay_ms(0) != 0 || rand_raw_draws != 1) {
        return 107;
    }

    request_delay.base_delay_ms = std::numeric_limits<std::uint16_t>::max();
    request_delay.max_delay_ms = std::numeric_limits<std::uint16_t>::max();
    request_delay.jitter_factor = std::numeric_limits<float>::max();
    install_rand_raw(std::numeric_limits<std::int32_t>::max());
    if (request_delay.calculate_delay_ms(0) !=
            std::numeric_limits<std::uint64_t>::max() ||
        rand_raw_draws != 1) {
        return 108;
    }

    const auto reconnect_new = rrr::ReconnectPolicy::new_();
    const auto reconnect_conservative = rrr::ReconnectPolicy::conservative();
    const auto reconnect_aggressive = rrr::ReconnectPolicy::aggressive();
    const auto reconnect_none = rrr::ReconnectPolicy::no_retry();
    if (!reconnect_new.auto_reconnect || reconnect_new.max_retries != 5 ||
        reconnect_new.initial_delay_ms != 1000 ||
        reconnect_new.max_delay_ms != 30000 ||
        reconnect_new.backoff_multiplier != 2.0 ||
        !reconnect_new.jitter_enabled ||
        reconnect_conservative.auto_reconnect != reconnect_new.auto_reconnect ||
        reconnect_conservative.max_retries != reconnect_new.max_retries ||
        reconnect_conservative.initial_delay_ms !=
            reconnect_new.initial_delay_ms ||
        reconnect_conservative.max_delay_ms != reconnect_new.max_delay_ms ||
        reconnect_conservative.backoff_multiplier !=
            reconnect_new.backoff_multiplier ||
        reconnect_conservative.jitter_enabled !=
            reconnect_new.jitter_enabled ||
        !reconnect_aggressive.auto_reconnect ||
        reconnect_aggressive.max_retries != 0 ||
        reconnect_aggressive.initial_delay_ms != 100 ||
        reconnect_aggressive.max_delay_ms != 5000 ||
        reconnect_aggressive.backoff_multiplier != 1.5 ||
        !reconnect_aggressive.jitter_enabled ||
        reconnect_none.auto_reconnect || reconnect_none.max_retries != 0 ||
        reconnect_none.initial_delay_ms != 0 ||
        reconnect_none.max_delay_ms != 0 ||
        reconnect_none.backoff_multiplier != 1.0 ||
        reconnect_none.jitter_enabled) {
        return 109;
    }

    auto reconnect_limited = reconnect_new;
    reconnect_limited.max_retries = 3;
    reconnect_limited.initial_delay_ms = 100;
    reconnect_limited.max_delay_ms = 250;
    reconnect_limited.jitter_enabled = false;
    auto reconnect_calculator =
        rrr::ReconnectCalculator::new_(reconnect_limited);
    if (&reconnect_calculator.policy != &reconnect_limited ||
        reconnect_calculator.retry_count() != 0 ||
        reconnect_calculator.peek_delay_ms() != 100 ||
        !reconnect_calculator.should_retry() ||
        reconnect_calculator.retries_exhausted()) {
        return 110;
    }
    install_rand_raw(17);
    if (reconnect_calculator.next_delay_ms() != 100 ||
        reconnect_calculator.retry_count() != 1 ||
        reconnect_calculator.peek_delay_ms() != 200 ||
        reconnect_calculator.next_delay_ms() != 200 ||
        reconnect_calculator.retry_count() != 2 ||
        reconnect_calculator.peek_delay_ms() != 250 ||
        reconnect_calculator.next_delay_ms() != 250 ||
        reconnect_calculator.retry_count() != 3 ||
        reconnect_calculator.should_retry() ||
        !reconnect_calculator.retries_exhausted() || rand_raw_draws != 0) {
        return 111;
    }
    reconnect_calculator.reset();
    if (reconnect_calculator.retry_count() != 0 ||
        !reconnect_calculator.should_retry() ||
        reconnect_calculator.retries_exhausted()) {
        return 112;
    }

    auto reconnect_unlimited = reconnect_aggressive;
    reconnect_unlimited.jitter_enabled = false;
    auto unlimited_calculator =
        rrr::ReconnectCalculator::new_(reconnect_unlimited);
    auto no_retry_calculator = rrr::ReconnectCalculator::new_(reconnect_none);
    if (!unlimited_calculator.should_retry() ||
        unlimited_calculator.retries_exhausted() ||
        no_retry_calculator.should_retry() ||
        !no_retry_calculator.retries_exhausted()) {
        return 113;
    }

    auto reconnect_jitter = reconnect_new;
    reconnect_jitter.initial_delay_ms = 100;
    reconnect_jitter.max_delay_ms = 1000;
    auto jitter_calculator =
        rrr::ReconnectCalculator::new_(reconnect_jitter);
    install_rand_raw(0);
    if (jitter_calculator.next_delay_ms() != 50 || rand_raw_draws != 1 ||
        jitter_calculator.peek_delay_ms() != 200 || rand_raw_draws != 1) {
        return 114;
    }
    jitter_calculator.reset();
    install_rand_raw(std::numeric_limits<std::int32_t>::max());
    if (jitter_calculator.next_delay_ms() != 150 || rand_raw_draws != 1) {
        return 115;
    }
    reconnect_jitter.initial_delay_ms = 0;
    jitter_calculator.reset();
    install_rand_raw(123);
    if (jitter_calculator.next_delay_ms() != 0 || rand_raw_draws != 0) {
        return 116;
    }

    const auto circuit_new = rrr::CircuitBreakerConfig::new_();
    const auto circuit_defaults = rrr::CircuitBreakerConfig::defaults();
    const auto circuit_sensitive = rrr::CircuitBreakerConfig::sensitive();
    const auto circuit_relaxed = rrr::CircuitBreakerConfig::relaxed();
    const auto circuit_disabled = rrr::CircuitBreakerConfig::disabled();
    if (circuit_new.failure_threshold != circuit_defaults.failure_threshold ||
        circuit_new.success_threshold != circuit_defaults.success_threshold ||
        circuit_new.timeout_ms != circuit_defaults.timeout_ms ||
        circuit_new.enabled != circuit_defaults.enabled ||
        circuit_defaults.failure_threshold != 5 ||
        circuit_defaults.success_threshold != 3 ||
        circuit_defaults.timeout_ms != 30000 || !circuit_defaults.enabled ||
        circuit_sensitive.failure_threshold != 3 ||
        circuit_sensitive.success_threshold != 5 ||
        circuit_sensitive.timeout_ms != 60000 ||
        circuit_relaxed.failure_threshold != 10 ||
        circuit_relaxed.success_threshold != 2 ||
        circuit_relaxed.timeout_ms != 15000 ||
        circuit_disabled.failure_threshold != 0 ||
        circuit_disabled.success_threshold != 0 ||
        circuit_disabled.timeout_ms != 0 || circuit_disabled.enabled ||
        rrr::circuit_state_to_string(rrr::CircuitState::CLOSED) != "CLOSED" ||
        rrr::circuit_state_to_string(rrr::CircuitState::OPEN) != "OPEN" ||
        rrr::circuit_state_to_string(rrr::CircuitState::HALF_OPEN) !=
            "HALF_OPEN") {
        return 117;
    }

    monotonic_now_us = 1'000'000;
    auto circuit_config = circuit_defaults;
    circuit_config.failure_threshold = 2;
    circuit_config.success_threshold = 2;
    circuit_config.timeout_ms = 10;
    auto circuit = rrr::CircuitBreaker::new_(circuit_config);
    if (!circuit.is_closed() || circuit.is_open() ||
        !circuit.allow_request() || circuit.failure_count() != 0 ||
        rrr::current_time_us() != monotonic_now_us) {
        return 118;
    }
    circuit.record_failure();
    circuit.record_failure();
    if (!circuit.is_open() || circuit.failure_count() != 0 ||
        circuit.allow_request() || circuit.last_failure_time.get() != 1'000'000) {
        return 119;
    }
    monotonic_now_us = 1'009'999;
    if (circuit.allow_request()) {
        return 120;
    }
    monotonic_now_us = 1'010'000;
    if (!circuit.allow_request() || !circuit.is_half_open() ||
        circuit.allow_request()) {
        return 121;
    }
    circuit.record_success();
    if (circuit.success_count() != 1 || !circuit.allow_request()) {
        return 122;
    }
    circuit.record_success();
    if (!circuit.is_closed() || circuit.success_count() != 0) {
        return 123;
    }
    circuit.failure_count_field.set(std::numeric_limits<std::uint32_t>::max());
    circuit.record_failure();
    if (circuit.failure_count() != 0 || !circuit.is_closed()) {
        return 124;
    }

    rrr::StateChangeCallback empty_state_callback{};
    if (empty_state_callback || !empty_state_callback.is_empty()) {
        return 125;
    }
    auto state_machine = rrr::ConnectionStateMachine::new_();
    if (!state_machine.on_state_change.is_empty() ||
        state_machine.state() != rrr::ConnectionState::NEW ||
        state_machine.transition_to(rrr::ConnectionState::CONNECTED) ||
        !state_machine.transition_to(rrr::ConnectionState::CONNECTING)) {
        return 126;
    }
    int state_callback_calls = 0;
    rrr::ConnectionState observed_from = rrr::ConnectionState::NEW;
    rrr::ConnectionState observed_to = rrr::ConnectionState::NEW;
    state_machine.set_on_state_change(
        [&](rrr::ConnectionState from, rrr::ConnectionState to) {
            ++state_callback_calls;
            observed_from = from;
            observed_to = to;
        });
    if (state_machine.on_state_change.is_empty() ||
        !state_machine.transition_to(rrr::ConnectionState::CONNECTED) ||
        state_callback_calls != 1 ||
        observed_from != rrr::ConnectionState::CONNECTING ||
        observed_to != rrr::ConnectionState::CONNECTED) {
        return 127;
    }
    state_machine.force_state(rrr::ConnectionState::FAILED);
    if (state_callback_calls != 2 ||
        observed_from != rrr::ConnectionState::CONNECTED ||
        observed_to != rrr::ConnectionState::FAILED ||
        !state_machine.is_failed() || !state_machine.is_terminal()) {
        return 128;
    }

    rrr::HeartbeatTimeoutCallback empty_heartbeat_callback{};
    if (empty_heartbeat_callback || !empty_heartbeat_callback.is_empty()) {
        return 129;
    }
    int moved_callback_calls = 0;
    rrr::HeartbeatTimeoutCallback moved_from =
        MutableHeartbeatCallable{&moved_callback_calls};
    auto moved_to = std::move(moved_from);
    if (moved_from || !moved_from.is_empty() || !moved_to ||
        moved_to.is_empty()) {
        return 130;
    }
    moved_to();
    if (moved_callback_calls != 1) {
        return 131;
    }

    const auto heartbeat_defaults = rrr::HeartbeatConfig::defaults();
    const auto heartbeat_aggressive = rrr::HeartbeatConfig::aggressive();
    const auto heartbeat_relaxed = rrr::HeartbeatConfig::relaxed();
    const auto heartbeat_disabled = rrr::HeartbeatConfig::disabled();
    if (!heartbeat_defaults.enabled || heartbeat_defaults.interval_ms != 10000 ||
        heartbeat_defaults.timeout_ms != 5000 ||
        heartbeat_defaults.max_missed != 3 ||
        heartbeat_aggressive.interval_ms != 5000 ||
        heartbeat_aggressive.timeout_ms != 2000 ||
        heartbeat_aggressive.max_missed != 2 ||
        heartbeat_relaxed.interval_ms != 30000 ||
        heartbeat_relaxed.timeout_ms != 15000 ||
        heartbeat_relaxed.max_missed != 5 || heartbeat_disabled.enabled) {
        return 132;
    }

    auto empty_timeout_config = heartbeat_defaults;
    empty_timeout_config.interval_ms = 1;
    empty_timeout_config.timeout_ms = 0;
    empty_timeout_config.max_missed = 1;
    auto empty_timeout = rrr::HeartbeatManager::new_(empty_timeout_config);
    if (!(*empty_timeout.on_timeout.borrow()).is_empty()) {
        return 133;
    }
    monotonic_now_us = std::numeric_limits<std::uint64_t>::max() - 5;
    empty_timeout.on_heartbeat_sent();
    monotonic_now_us = 4;
    if (!empty_timeout.check_timeout() || !empty_timeout.is_timed_out() ||
        empty_timeout.missed_count() != 1 ||
        empty_timeout.is_pending_pong()) {
        return 134;
    }

    auto heartbeat_config = heartbeat_defaults;
    heartbeat_config.interval_ms = 1;
    heartbeat_config.timeout_ms = 2;
    heartbeat_config.max_missed = 2;
    auto heartbeat = rrr::HeartbeatManager::new_(heartbeat_config);
    int heartbeat_callback_calls = 0;
    heartbeat.set_on_timeout(
        MutableHeartbeatCallable{&heartbeat_callback_calls});
    monotonic_now_us = 1'000'000;
    if (rrr::heartbeat_time_us() != monotonic_now_us ||
        !heartbeat.should_send_heartbeat()) {
        return 135;
    }
    heartbeat.on_heartbeat_sent();
    monotonic_now_us = 1'001'999;
    if (heartbeat.check_timeout()) {
        return 136;
    }
    monotonic_now_us = 1'002'000;
    if (heartbeat.check_timeout() || heartbeat.missed_count() != 1 ||
        heartbeat.is_timed_out()) {
        return 137;
    }
    monotonic_now_us = 1'003'000;
    if (!heartbeat.should_send_heartbeat()) {
        return 138;
    }
    heartbeat.on_heartbeat_sent();
    monotonic_now_us = 1'005'000;
    if (!heartbeat.check_timeout() || !heartbeat.is_timed_out() ||
        heartbeat_callback_calls != 1 || heartbeat.check_timeout() ||
        heartbeat_callback_calls != 1) {
        return 139;
    }
    heartbeat.reset();
    if (heartbeat.missed_count() != 0 || heartbeat.is_timed_out() ||
        heartbeat.is_pending_pong()) {
        return 140;
    }

    auto wrapping_heartbeat = rrr::HeartbeatManager::new_(heartbeat_config);
    wrapping_heartbeat.missed_count_field.set(
        std::numeric_limits<std::uint32_t>::max());
    monotonic_now_us = std::numeric_limits<std::uint64_t>::max() - 5;
    wrapping_heartbeat.on_heartbeat_sent();
    // The wrapped delta is exactly 2,000 us: 1,994 - (UINT64_MAX - 5).
    monotonic_now_us = 1'994;
    if (wrapping_heartbeat.check_timeout() ||
        wrapping_heartbeat.missed_count() != 0 ||
        wrapping_heartbeat.is_timed_out()) {
        return 141;
    }

    using enum rrr::LoadBalancingStrategy;
    if (rrr::load_balancing_strategy_to_string(RANDOM) != "RANDOM" ||
        rrr::load_balancing_strategy_to_string(ROUND_ROBIN) !=
            "ROUND_ROBIN" ||
        rrr::load_balancing_strategy_to_string(LEAST_CONNECTIONS) !=
            "LEAST_CONNECTIONS" ||
        rrr::load_balancing_strategy_to_string(LEAST_LATENCY) !=
            "LEAST_LATENCY" ||
        rrr::load_balancing_strategy_to_string(
            static_cast<rrr::LoadBalancingStrategy>(255)) != "UNKNOWN") {
        return 177;
    }

    auto load_balancer_state = rrr::LoadBalancerState::new_();
    if (load_balancer_state.next_round_robin_index(0) != 0 ||
        load_balancer_state.next_round_robin_index(3) != 0 ||
        load_balancer_state.next_round_robin_index(3) != 1 ||
        load_balancer_state.next_round_robin_index(3) != 2 ||
        load_balancer_state.next_round_robin_index(3) != 0) {
        return 178;
    }
    load_balancer_state.reset();
    if (load_balancer_state.next_round_robin_index(3) != 0) {
        return 179;
    }
    load_balancer_state.round_robin_index_field.set(
        std::numeric_limits<std::size_t>::max());
    if (load_balancer_state.next_round_robin_index(3) !=
            std::numeric_limits<std::size_t>::max() ||
        load_balancer_state.round_robin_index_field.get() != 0) {
        return 180;
    }

    LoadBalancerProbeClients empty_load_balancer_clients;
    if (rrr::LoadBalancer::select(
            RANDOM, empty_load_balancer_clients, load_balancer_state, 19) != 0) {
        return 181;
    }
    LoadBalancerProbeClients load_balancer_clients{
        std::make_shared<LoadBalancerProbeClient>(
            LoadBalancerProbeClient{LoadBalancerProbeMetrics{5, 0, 0}}),
        std::make_shared<LoadBalancerProbeClient>(
            LoadBalancerProbeClient{LoadBalancerProbeMetrics{2, 80, 10}}),
        std::make_shared<LoadBalancerProbeClient>(
            LoadBalancerProbeClient{LoadBalancerProbeMetrics{2, 30, 3}}),
    };
    if (rrr::lb_pool_size(load_balancer_clients) != 3 ||
        rrr::LoadBalancer::select(
            RANDOM, load_balancer_clients, load_balancer_state, 8) != 2 ||
        rrr::LoadBalancer::select(
            static_cast<rrr::LoadBalancingStrategy>(255),
            load_balancer_clients,
            load_balancer_state,
            8) != 2 ||
        rrr::LoadBalancer::select(
            LEAST_CONNECTIONS,
            load_balancer_clients,
            load_balancer_state,
            0) != 1 ||
        rrr::LoadBalancer::select(
            LEAST_LATENCY,
            load_balancer_clients,
            load_balancer_state,
            0) != 2) {
        return 182;
    }
    load_balancer_state.reset();
    if (rrr::LoadBalancer::select(
            ROUND_ROBIN, load_balancer_clients, load_balancer_state, 0) != 0 ||
        rrr::LoadBalancer::select(
            ROUND_ROBIN, load_balancer_clients, load_balancer_state, 0) != 1 ||
        rrr::LoadBalancer::select(
            ROUND_ROBIN, load_balancer_clients, load_balancer_state, 0) != 2) {
        return 183;
    }

    {
        rrr::AddrInfo empty;
        if (empty.get() != nullptr || empty.valid() || empty.owned_.get() ||
            empty._rusty_forgotten) {
            return 184;
        }
    }
    const auto free_before = freeaddrinfo_calls;
    {
        auto* first = new addrinfo{};
        auto* second = new addrinfo{};
        rrr::AddrInfo source(first);
        if (source.get() != first || !source.valid() || !source.owned_.get()) {
            return 185;
        }
        rrr::AddrInfo moved(std::move(source));
        if (moved.get() != first || !moved.owned_.get() ||
            source.get() != first || !source._rusty_forgotten) {
            return 186;
        }
        rrr::AddrInfo target(second);
        target = std::move(moved);
        if (target.get() != first || !target.owned_.get() ||
            moved.get() != first || !moved._rusty_forgotten) {
            return 187;
        }
        target = std::move(target);
        if (target.get() != first || target._rusty_forgotten) {
            return 188;
        }
    }
    if (freeaddrinfo_calls - free_before != 2) {
        return 189;
    }

    std::ostringstream utils_log;
    std::streambuf* original_cout = std::cout.rdbuf(utils_log.rdbuf());
    auto reset_utils_log = [&]() {
        utils_log.str({});
        utils_log.clear();
    };

    selected_open_port = 4321;
    reset_utils_log();
    if (rrr::find_open_port() != 4321 ||
        utils_log.str() !=
            "I [<unknown>:0] 2000-01-02 03:04:05.006 | "
            "Found open port: 4321\\n") {
        return 190;
    }
    selected_open_port = 0;
    reset_utils_log();
    if (rrr::find_open_port() != -1 ||
        utils_log.str() !=
            "E [<unknown>:0] 2000-01-02 03:04:05.006 | "
            "Failed to find open port.\\n") {
        return 191;
    }
    selected_open_port = -1;
    reset_utils_log();
    if (rrr::find_open_port() != -1 ||
        utils_log.str() !=
            "E [<unknown>:0] 2000-01-02 03:04:05.006 | "
            "Failed to find open port.\\n") {
        return 192;
    }

    hostname_mode = 1;
    reset_utils_log();
    if (rrr::get_host_name() != "goal0-host" ||
        hostname_buffer_length != 255 || !utils_log.str().empty()) {
        return 193;
    }
    hostname_mode = -1;
    reset_utils_log();
    if (!rrr::get_host_name().empty() ||
        utils_log.str() !=
            "E [<unknown>:0] 2000-01-02 03:04:05.006 | "
            "Failed to get hostname.\\n") {
        return 194;
    }
    std::cout.rdbuf(original_cout);


    if (rrr::frame_decode_status_to_string(
            rrr::FrameDecodeStatus::NeedMoreBytes) != "NeedMoreBytes" ||
        rrr::frame_decode_status_to_string(
            rrr::FrameDecodeStatus::Complete) != "Complete" ||
        rrr::frame_decode_status_to_string(
            rrr::FrameDecodeStatus::Malformed) != "Malformed") {
        return 195;
    }
    bool invalid_frame_status_threw = false;
    try {
        (void)rrr::frame_decode_status_to_string(
            static_cast<rrr::FrameDecodeStatus>(99));
    } catch (const std::exception&) {
        invalid_frame_status_threw = true;
    }
    if (!invalid_frame_status_threw) {
        return 196;
    }

    const auto frame_native_bytes = [](std::int32_t value) {
        return std::bit_cast<std::array<std::uint8_t, 4>>(value);
    };
    std::array<std::uint8_t, 5> frame_header_bytes{
        0xa1, 0xa2, 0xa3, 0xa4, 0xa5};
    const auto original_frame_header_bytes = frame_header_bytes;
    if (rrr::frame_codec_write_header(
            std::span<std::uint8_t>(frame_header_bytes.data(), 3), 1, false) ||
        frame_header_bytes != original_frame_header_bytes ||
        rrr::frame_codec_write_header(frame_header_bytes, -1, true) ||
        frame_header_bytes != original_frame_header_bytes) {
        return 197;
    }
    if (!rrr::frame_codec_write_header(frame_header_bytes, 0, true) ||
        std::memcmp(frame_header_bytes.data(),
                    frame_native_bytes(INT32_MIN).data(), 4) != 0 ||
        frame_header_bytes[4] != 0xa5 ||
        !rrr::frame_codec_write_header(
            frame_header_bytes, INT32_MAX, true) ||
        std::memcmp(frame_header_bytes.data(),
                    frame_native_bytes(-1).data(), 4) != 0) {
        return 198;
    }

    rrr::FrameHeader decoded_frame_header{17, true};
    if (rrr::frame_codec_peek_header(
            std::span<const std::uint8_t>(frame_header_bytes.data(), 3),
            decoded_frame_header) != rrr::FrameDecodeStatus::NeedMoreBytes ||
        decoded_frame_header.payload_size != 17 ||
        !decoded_frame_header.extended_header_flag) {
        return 199;
    }
    for (const auto [encoded, payload, extended] :
         std::array{
             std::tuple{0, 0, false},
             std::tuple{INT32_MAX, INT32_MAX, false},
             std::tuple{INT32_MIN, 0, true},
             std::tuple{-1, INT32_MAX, true},
         }) {
        const auto bytes = frame_native_bytes(encoded);
        decoded_frame_header = rrr::FrameHeader{-7, !extended};
        if (rrr::frame_codec_peek_header(bytes, decoded_frame_header) !=
                rrr::FrameDecodeStatus::Complete ||
            decoded_frame_header.payload_size != payload ||
            decoded_frame_header.extended_header_flag != extended) {
            return 200;
        }
    }
    if (rrr::FrameHeader{INT32_MAX, false}.total_frame_size() !=
        INT32_MIN + 3) {
        return 201;
    }

    std::vector<std::uint8_t> encoded_frame{9, 8};
    const auto untouched_frame = encoded_frame;
    if (rrr::frame_codec_encode_into(
            encoded_frame, nullptr, -1, false) ||
        encoded_frame != untouched_frame ||
        rrr::frame_codec_encode_into(
            encoded_frame, nullptr, 1, false) ||
        encoded_frame != untouched_frame ||
        !rrr::frame_codec_encode_into(
            encoded_frame, nullptr, 0, false) ||
        encoded_frame.size() != 6) {
        return 202;
    }
    constexpr std::array<std::uint8_t, 3> first_frame_payload{'a', 'b', 'c'};
    std::vector<std::uint8_t> first_frame;
    if (!rrr::frame_codec_encode_into(
            first_frame,
            first_frame_payload.data(),
            static_cast<std::int32_t>(first_frame_payload.size()),
            false)) {
        return 203;
    }

    auto frame_reader = rrr::FrameStreamReader::new_();
    frame_reader.cursor_.set_position(99);
    rrr::FrameView frame_view{
        rrr::FrameHeader{91, true},
        reinterpret_cast<const std::uint8_t*>(1),
        77,
    };
    if (frame_reader.next_frame(frame_view) !=
            rrr::FrameDecodeStatus::NeedMoreBytes ||
        frame_reader.buffered_bytes() != 0) {
        return 204;
    }
    frame_reader.consume_frame();
    if (frame_reader.cursor_.position() != 99) {
        return 205;
    }
    frame_reader.reset();
    for (std::size_t index = 0; index < first_frame.size(); ++index) {
        frame_reader.append(&first_frame[index], 1);
        const auto status = frame_reader.next_frame(frame_view);
        if ((index + 1 < first_frame.size() &&
             status != rrr::FrameDecodeStatus::NeedMoreBytes) ||
            (index + 1 == first_frame.size() &&
             status != rrr::FrameDecodeStatus::Complete)) {
            return 206;
        }
    }
    if (frame_view.payload_size != first_frame_payload.size() ||
        std::memcmp(frame_view.payload,
                    first_frame_payload.data(),
                    first_frame_payload.size()) != 0) {
        return 207;
    }
    frame_reader.consume_frame();

    constexpr std::array<std::uint8_t, 2> second_frame_payload{0x55, 0xaa};
    std::vector<std::uint8_t> second_frame;
    if (!rrr::frame_codec_encode_into(
            second_frame,
            second_frame_payload.data(),
            static_cast<std::int32_t>(second_frame_payload.size()),
            true)) {
        return 208;
    }
    std::vector<std::uint8_t> coalesced_frames = first_frame;
    coalesced_frames.insert(
        coalesced_frames.end(), second_frame.begin(), second_frame.end());
    frame_reader.append(coalesced_frames.data(), coalesced_frames.size());
    if (frame_reader.next_frame(frame_view) !=
        rrr::FrameDecodeStatus::Complete) {
        return 209;
    }
    frame_reader.consume_frame();
    if (frame_reader.buffered_bytes() != second_frame.size() ||
        frame_reader.next_frame(frame_view) !=
            rrr::FrameDecodeStatus::Complete ||
        !frame_view.header.extended_header_flag ||
        std::memcmp(frame_view.payload,
                    second_frame_payload.data(),
                    second_frame_payload.size()) != 0) {
        return 210;
    }
    frame_reader.consume_frame();

    constexpr std::size_t compact_total = 64 * 1024;
    std::vector<std::uint8_t> compact_payload(compact_total - 4);
    for (std::size_t index = 0; index < compact_payload.size(); ++index) {
        compact_payload[index] = static_cast<std::uint8_t>((index * 17) & 0xff);
    }
    std::vector<std::uint8_t> compact_frame;
    if (!rrr::frame_codec_encode_into(
            compact_frame,
            compact_payload.data(),
            static_cast<std::int32_t>(compact_payload.size()),
            false)) {
        return 211;
    }
    compact_frame.insert(
        compact_frame.end(), second_frame.begin(), second_frame.end());
    frame_reader.append(compact_frame.data(), compact_frame.size());
    if (frame_reader.next_frame(frame_view) !=
        rrr::FrameDecodeStatus::Complete) {
        return 212;
    }
    frame_reader.consume_frame();
    if (frame_reader.cursor_.position() != 0 ||
        frame_reader.cursor_.get_ref().size() != second_frame.size() ||
        frame_reader.next_frame(frame_view) !=
            rrr::FrameDecodeStatus::Complete ||
        std::memcmp(frame_view.payload,
                    second_frame_payload.data(),
                    second_frame_payload.size()) != 0) {
        return 213;
    }


    constexpr std::array<std::int64_t, 37> sparse_boundaries{
        INT64_MIN,
        -36028797018963969LL, -36028797018963968LL,
        -36028797018963967LL, -281474976710657LL,
        -281474976710656LL, -281474976710655LL,
        -2199023255553LL, -2199023255552LL, -2199023255551LL,
        -17179869185LL, -17179869184LL, -17179869183LL,
        -134217729LL, -134217728LL, -134217727LL,
        -1048577LL, -1048576LL, -1048575LL,
        -8193LL, -8192LL, -8191LL, -65LL, -64LL, -63LL,
        -1LL, 0LL, 1LL, 62LL, 63LL, 64LL, 8191LL, 8192LL,
        1048575LL, 134217727LL, 36028797018963967LL, INT64_MAX,
    };
    for (const auto value : sparse_boundaries) {
        if (!basetypes_round_trip(value)) {
            return 142;
        }
        if (value >= INT32_MIN && value <= INT32_MAX &&
            !basetypes_round_trip(static_cast<std::int32_t>(value))) {
            return 143;
        }
    }
    std::uint64_t sparse_state = UINT64_C(0x9e3779b97f4a7c15);
    std::uint64_t sparse_wire_digest = UINT64_C(0xcbf29ce484222325);
    const auto hash_sparse_byte = [](std::uint64_t hash, std::uint8_t byte) {
        return (hash ^ byte) * UINT64_C(0x100000001b3);
    };
    for (std::size_t i = 0; i < 100000; ++i) {
        sparse_state ^= sparse_state >> 12;
        sparse_state ^= sparse_state << 25;
        sparse_state ^= sparse_state >> 27;
        const auto value = static_cast<std::int64_t>(
            sparse_state * UINT64_C(0x2545f4914f6cdd1d));
        if (!basetypes_round_trip(value) ||
            !basetypes_round_trip(static_cast<std::int32_t>(value))) {
            return 144;
        }
        std::array<std::uint8_t, 9> encoded64{};
        const auto reported64 =
            rrr::SparseInt::dump64(value, encoded64.data());
        sparse_wire_digest = hash_sparse_byte(sparse_wire_digest, 64);
        sparse_wire_digest = hash_sparse_byte(
            sparse_wire_digest, static_cast<std::uint8_t>(reported64));
        const auto written64 = reported64 == 8 ? 9 : reported64;
        for (std::size_t byte = 0; byte < written64; ++byte) {
            sparse_wire_digest =
                hash_sparse_byte(sparse_wire_digest, encoded64[byte]);
        }
        std::array<std::uint8_t, 5> encoded32{};
        const auto reported32 = rrr::SparseInt::dump32(
            static_cast<std::int32_t>(value), encoded32.data());
        sparse_wire_digest = hash_sparse_byte(sparse_wire_digest, 32);
        sparse_wire_digest = hash_sparse_byte(
            sparse_wire_digest, static_cast<std::uint8_t>(reported32));
        for (std::size_t byte = 0; byte < reported32; ++byte) {
            sparse_wire_digest =
                hash_sparse_byte(sparse_wire_digest, encoded32[byte]);
        }
    }
    if (sparse_wire_digest != UINT64_C(0x6d2ddf1efe2ab0b6)) {
        return 156;
    }
    for (const auto [value, truncated] : std::array{
             std::pair{INT64_C(36028797018963967),
                       INT64_C(36028797018963712)},
             std::pair{INT64_C(-36028797018963967),
                       INT64_C(-36028797018963968)},
         }) {
        std::array<std::uint8_t, 9> encoded{};
        const auto reported = rrr::SparseInt::dump64(value, encoded.data());
        std::array<std::uint8_t, 9> persisted{};
        std::copy_n(encoded.begin(), reported, persisted.begin());
        if (reported != 8 || encoded[0] != 0xfe ||
            rrr::SparseInt::load64(persisted.data()) != truncated) {
            return 145;
        }
    }

    auto base_v32 = rrr::v32::new_(-8192);
    base_v32.set(8192);
    auto base_v64 = rrr::v64::new_(36028797018963968LL);
    if (base_v32.get() != 8192 || base_v32.val_size() != 3 ||
        base_v64.get() != 36028797018963968LL || base_v64.val_size() != 9) {
        return 146;
    }
    auto base_counter = rrr::Counter::new_(7);
    if (base_counter.peek_next() != 7 || base_counter.next(5) != 7 ||
        base_counter.peek_next() != 12) {
        return 147;
    }
    base_counter.reset(std::numeric_limits<std::int64_t>::max());
    if (base_counter.next(1) != std::numeric_limits<std::int64_t>::max() ||
        base_counter.peek_next() != std::numeric_limits<std::int64_t>::min()) {
        return 148;
    }
    auto concurrent_counter = rrr::Counter::new_(0);
    {
        std::vector<std::thread> workers;
        for (std::size_t worker = 0; worker < 8; ++worker) {
            workers.emplace_back([&concurrent_counter]() {
                for (std::size_t i = 0; i < 10000; ++i) {
                    concurrent_counter.next(1);
                }
            });
        }
        for (auto& worker : workers) {
            worker.join();
        }
    }
    if (concurrent_counter.peek_next() != 80000) {
        return 155;
    }
    rrr::AtomicI64 exported_atomic = rrr::AtomicI64::new_(11);
    if (exported_atomic.load(rrr::Ordering::Relaxed) != 11 ||
        rrr::RRR_USEC_PER_SEC != 1000000) {
        return 149;
    }

    monotonic_now_us = 10;
    realtime_now_us = 20;
    gettimeofday_now_us = 1000000;
    slept_us = 0;
    rrr::abort_if_false(true);
    if (rrr::time_now_us(true) != 10 || rrr::Time::now(false) != 20) {
        return 150;
    }
    rrr::Time::sleep(37);
    auto base_timer = rrr::Timer::new_();
    base_timer.start();
    gettimeofday_now_us = 3250000;
    if (slept_us != 37 || base_timer.elapsed() != 2.25) {
        return 151;
    }
    base_timer.stop();
    gettimeofday_now_us = 9000000;
    if (base_timer.elapsed() != 2.25) {
        return 152;
    }
    base_timer.begin_us = 10;
    base_timer.end_us = 5;
    if (base_timer.elapsed() !=
        static_cast<double>(std::numeric_limits<std::uint64_t>::max() - 4) /
            1000000.0) {
        return 153;
    }
    base_timer.reset();
    if (base_timer.begin_us != 0 || base_timer.end_us != 0) {
        return 154;
    }
    if (rrr::kRequestQueueRejectedError != EAGAIN ||
        rrr::kRequestQueueExpiredError != ETIMEDOUT ||
        rrr::overflow_strategy_to_string(rrr::OverflowStrategy::DROP_OLDEST) !=
            "DROP_OLDEST" ||
        rrr::overflow_strategy_to_string(rrr::OverflowStrategy::DROP_NEWEST) !=
            "DROP_NEWEST" ||
        rrr::overflow_strategy_to_string(rrr::OverflowStrategy::FAIL_FAST) !=
            "FAIL_FAST" ||
        rrr::overflow_strategy_to_string(
            static_cast<rrr::OverflowStrategy>(99)) != "UNKNOWN") {
        return 157;
    }

    const auto queue_defaults = rrr::RequestQueueConfig::defaults();
    if (queue_defaults.max_size != 1000 ||
        queue_defaults.default_ttl_ms != 30000 ||
        queue_defaults.overflow_strategy != rrr::OverflowStrategy::DROP_OLDEST ||
        !queue_defaults.enabled || rrr::RequestQueueConfig::small().max_size != 10 ||
        rrr::RequestQueueConfig::large().max_size != 10000 ||
        rrr::RequestQueueConfig::disabled().enabled) {
        return 158;
    }

    bool direct_callback_called = false;
    rrr::rq_invoke_callback_safely(
        rrr::QueuedRequestCallback([&](std::int32_t error) {
            direct_callback_called = error == 314;
        }),
        314);
    if (!direct_callback_called) {
        return 156;
    }

    monotonic_now_us = 1'000'000;
    auto timed_request = rrr::QueuedRequest::new_();
    if (rrr::queued_request_time_us() != monotonic_now_us ||
        timed_request.timestamp_us != monotonic_now_us ||
        timed_request.xid != 0 || timed_request.rpc_id != 0 ||
        timed_request.retry_count != 0 || timed_request.callback ||
        timed_request.ttl_ms != 30000) {
        return 159;
    }
    timed_request.ttl_ms = 10;
    monotonic_now_us = 1'010'000;
    if (timed_request.is_expired() || timed_request.age_ms() != 10) {
        return 160;
    }
    monotonic_now_us = 1'011'000;
    if (!timed_request.is_expired() || timed_request.age_ms() != 11) {
        return 161;
    }
    timed_request.timestamp_us = std::numeric_limits<std::uint64_t>::max() - 499;
    timed_request.ttl_ms = 0;
    monotonic_now_us = 500;
    if (!timed_request.is_expired() || timed_request.age_ms() != 1) {
        return 162;
    }

    auto fifo_config = queue_defaults;
    fifo_config.max_size = 2;
    fifo_config.default_ttl_ms = 77;
    rrr::RequestQueue fifo(fifo_config);
    if (!fifo.empty() || fifo.remaining_capacity() != 2) {
        return 163;
    }
    auto first = make_queued_request(1);
    first.ttl_ms = 0;
    if (!fifo.enqueue(std::move(first)) ||
        !fifo.enqueue(make_queued_request(2)) || !fifo.full() ||
        fifo.remaining_capacity() != 0) {
        return 164;
    }
    auto first_out = fifo.dequeue();
    auto second_out = fifo.dequeue();
    if (first_out.is_none() || second_out.is_none()) {
        return 165;
    }
    auto first_value = first_out.unwrap();
    auto second_value = second_out.unwrap();
    if (first_value.xid != 1 || first_value.ttl_ms != 77 ||
        second_value.xid != 2 || fifo.dequeue().is_some()) {
        return 165;
    }
    fifo.update_config(rrr::RequestQueueConfig::small());
    if (fifo.config().max_size != 10 || !fifo.enabled() || fifo.max_size() != 10) {
        return 166;
    }

    for (auto strategy : {rrr::OverflowStrategy::DROP_NEWEST,
                          rrr::OverflowStrategy::FAIL_FAST}) {
        auto config = queue_defaults;
        config.max_size = 1;
        config.overflow_strategy = strategy;
        rrr::RequestQueue queue(config);
        if (!queue.enqueue(make_queued_request(3))) {
            return 167;
        }
        bool called = false;
        auto rejected = make_queued_request(
            4,
            rrr::QueuedRequestCallback([&](std::int32_t error) {
                if (error != rrr::kRequestQueueRejectedError ||
                    queue.queue_.try_lock().is_some()) {
                    throw std::logic_error("rejection callback lock contract");
                }
                called = true;
                throw std::runtime_error("expected rejection callback exception");
            }));
        if (queue.enqueue(std::move(rejected)) || !called || queue.size() != 1) {
            return 168;
        }
    }

    auto oldest_config = queue_defaults;
    oldest_config.max_size = 1;
    rrr::RequestQueue oldest_queue(oldest_config);
    bool oldest_called = false;
    auto oldest = make_queued_request(
        5,
        rrr::QueuedRequestCallback([&](std::int32_t error) {
            if (error != rrr::kRequestQueueRejectedError ||
                oldest_queue.queue_.try_lock().is_some()) {
                throw std::logic_error("oldest callback lock contract");
            }
            oldest_called = true;
            throw std::runtime_error("expected oldest callback exception");
        }));
    if (!oldest_queue.enqueue(std::move(oldest)) ||
        !oldest_queue.enqueue(make_queued_request(6)) || !oldest_called) {
        return 169;
    }
    auto retained = oldest_queue.dequeue();
    if (retained.is_none() || retained.unwrap().xid != 6) {
        return 170;
    }

    rrr::RequestQueue disabled_queue(rrr::RequestQueueConfig::disabled());
    bool disabled_called = false;
    auto disabled_request = make_queued_request(
        7,
        rrr::QueuedRequestCallback([&](std::int32_t error) {
            if (error != rrr::kRequestQueueRejectedError ||
                disabled_queue.queue_.try_lock().is_none()) {
                throw std::logic_error("disabled callback lock contract");
            }
            disabled_called = true;
            throw std::runtime_error("expected disabled callback exception");
        }));
    if (disabled_queue.enqueue(std::move(disabled_request)) ||
        !disabled_called || !disabled_queue.empty()) {
        return 171;
    }

    monotonic_now_us = 2'000'000;
    rrr::RequestQueue expiring;
    std::vector<std::int64_t> expired_order;
    for (std::int64_t xid : {8, 9}) {
        auto request = make_queued_request(
            xid,
            rrr::QueuedRequestCallback([&, xid](std::int32_t error) {
                if (error != rrr::kRequestQueueExpiredError ||
                    expiring.queue_.try_lock().is_none()) {
                    throw std::logic_error("expiration callback lock contract");
                }
                expired_order.push_back(xid);
                if (xid == 8) {
                    throw std::runtime_error("expected expiration callback exception");
                }
            }));
        request.timestamp_us = monotonic_now_us - 2'000;
        request.ttl_ms = 1;
        if (!expiring.enqueue(std::move(request))) {
            return 172;
        }
    }
    auto live = make_queued_request(10);
    live.timestamp_us = monotonic_now_us - 1'000;
    live.ttl_ms = 1;
    if (!expiring.enqueue(std::move(live)) || expiring.expire_stale() != 2 ||
        expired_order != std::vector<std::int64_t>({8, 9}) ||
        expiring.size() != 1) {
        return 173;
    }

    rrr::RequestQueue clearing;
    std::vector<std::int64_t> cleared_order;
    for (std::int64_t xid : {11, 12}) {
        auto request = make_queued_request(
            xid,
            rrr::QueuedRequestCallback([&, xid](std::int32_t error) {
                if (error != -77 || clearing.queue_.try_lock().is_none()) {
                    throw std::logic_error("clear callback lock contract");
                }
                cleared_order.push_back(xid);
                if (xid == 11) {
                    throw std::runtime_error("expected clear callback exception");
                }
            }));
        if (!clearing.enqueue(std::move(request))) {
            return 174;
        }
    }
    clearing.clear_all(-77);
    if (cleared_order != std::vector<std::int64_t>({11, 12}) ||
        !clearing.empty()) {
        return 175;
    }

    auto invalid_config = queue_defaults;
    invalid_config.max_size = 0;
    invalid_config.overflow_strategy = static_cast<rrr::OverflowStrategy>(99);
    rrr::RequestQueue invalid_queue(invalid_config);
    if (!invalid_queue.enqueue(make_queued_request(13)) ||
        invalid_queue.size() != 1) {
        return 176;
    }

    using Envelope = rrr::SerializableEnvelope<canary::EnvelopePayloadSet>;
    Envelope empty_envelope;
    auto cloned_empty_envelope = empty_envelope.clone();
    if (empty_envelope.has_value() || empty_envelope.kind() != 0 ||
        !(empty_envelope == cloned_empty_envelope)) {
        return 214;
    }
    canary::EnvelopePayload envelope_payload{7};
    auto packed_envelope = Envelope::pack(envelope_payload);
    const auto* unpacked_envelope =
        packed_envelope.template unpack<canary::EnvelopePayload>();
    auto shared_envelope =
        rrr::marshallable_cast<canary::EnvelopePayload>(packed_envelope);
    if (!packed_envelope.has_value() || packed_envelope.kind() != 61 ||
        !packed_envelope.template is_a<canary::EnvelopePayload>() ||
        unpacked_envelope == nullptr || unpacked_envelope->value != 7 ||
        shared_envelope.is_none() || shared_envelope.unwrap()->value != 7) {
        return 215;
    }
    auto cloned_envelope = packed_envelope.clone();
    auto* mutable_envelope =
        cloned_envelope.template unpack_mut<canary::EnvelopePayload>();
    if (!(packed_envelope == cloned_envelope) || mutable_envelope == nullptr) {
        return 216;
    }
    mutable_envelope->value = 9;
    if (packed_envelope.template unpack<canary::EnvelopePayload>()->value != 9) {
        return 217;
    }

    rrr::FiberFuture<int> invalid_future;
    if (invalid_future.valid() || invalid_future.is_ready() ||
        invalid_future.wait_for(1)) {
        return 218;
    }
    rrr::FiberPromise<int> promise;
    auto future = promise.get_future();
    if (!future.valid() || future.is_ready()) {
        return 219;
    }
    bool duplicate_future_threw = false;
    try {
        (void)promise.get_future();
    } catch (const std::logic_error&) {
        duplicate_future_threw = true;
    }
    promise.set_value(42);
    if (!duplicate_future_threw || !promise.is_ready() ||
        !future.is_ready() || future.get() != 42 || future.get() != 42) {
        return 220;
    }
    bool duplicate_value_threw = false;
    try {
        promise.set_value(7);
    } catch (const std::logic_error&) {
        duplicate_value_threw = true;
    }
    if (!duplicate_value_threw) {
        return 221;
    }
    auto [pair_promise, pair_future] = rrr::make_promise<std::string>();
    pair_promise.set_value("pair");
    auto ready_future = rrr::make_ready_future<std::string>("ready");
    if (pair_future.get() != "pair" || !ready_future.wait_for(1) ||
        ready_future.get() != "ready") {
        return 222;
    }

    if (rrr::log_level_tag(rrr::Log::FATAL) != "F " ||
        rrr::log_level_tag(rrr::Log::ERROR) != "E " ||
        rrr::log_level_tag(rrr::Log::WARN) != "W " ||
        rrr::log_level_tag(rrr::Log::INFO) != "I " ||
        rrr::log_level_tag(rrr::Log::DEBUG) != "D " ||
        rrr::log_level_tag(99) != "? " ||
        rrr::log_basename(nullptr) != "<unknown>" ||
        rrr::log_basename(reinterpret_cast<const std::int8_t*>("a/b/file.cc")) !=
            "file.cc" ||
        rrr::log_time_now() != "2000-01-02 03:04:05.006") {
        return 223;
    }
    std::ostringstream logging_sink;
    original_cout = std::cout.rdbuf(logging_sink.rdbuf());
    rrr::Log::set_level(rrr::Log::WARN);
    rrr::log_line(rrr::Log::INFO, 42,
                  reinterpret_cast<const std::int8_t*>("file.cc"),
                  "filtered");
    rrr::log_line(rrr::Log::ERROR, 42,
                  reinterpret_cast<const std::int8_t*>("a/b/file.cc"),
                  "visible");
    std::cout.rdbuf(original_cout);
    rrr::Log::set_level(rrr::Log::DEBUG);
    if (logging_sink.str() !=
        "E [file.cc:42] 2000-01-02 03:04:05.006 | visible\\n") {
        return 224;
    }

    const auto idempotency_empty = rrr::IdempotencyKey::empty();
    const auto idempotency_key = rrr::IdempotencyKey::new_(12'345, 67'890);
    rrr::IdempotencyKeyHash idempotency_hash;
    if (idempotency_empty.is_valid() || !idempotency_key.is_valid() ||
        !(idempotency_key == rrr::IdempotencyKey{12'345, 67'890}) ||
        idempotency_hash.hash_one(idempotency_key) !=
            (12'345ULL ^ (67'890ULL * 0x9e3779b97f4a7c15ULL))) {
        return 225;
    }
    rrr::BufferSink idempotency_sink;
    rrr::BinaryWriteArchive idempotency_writer(
        rrr::make_sink_proxy(&idempotency_sink));
    rrr::serialize(idempotency_key, idempotency_writer);
    if (idempotency_sink.bytes.len() != 16) {
        return 226;
    }
    rrr::BufferSource idempotency_source(
        idempotency_sink.bytes.data(), idempotency_sink.bytes.len());
    rrr::BinaryReadArchive idempotency_reader(
        rrr::make_source_proxy(&idempotency_source));
    auto restored_key = rrr::IdempotencyKey::empty();
    rrr::deserialize(restored_key, idempotency_reader);
    if (!(restored_key == idempotency_key)) {
        return 227;
    }
    auto idempotency_generator = rrr::IdempotencyKeyGenerator::new_(7);
    if (!(idempotency_generator.next() == rrr::IdempotencyKey{7, 0})) {
        return 228;
    }
    idempotency_generator.sequence_field.set(
        std::numeric_limits<std::uint64_t>::max());
    if (idempotency_generator.next().sequence !=
            std::numeric_limits<std::uint64_t>::max() ||
        idempotency_generator.current_sequence() != 0) {
        return 229;
    }
    const auto idempotency_defaults = rrr::IdempotencyConfig::defaults();
    if (idempotency_defaults.ttl_ms != 60'000 ||
        idempotency_defaults.max_entries != 10'000 ||
        !idempotency_defaults.enabled ||
        rrr::IdempotencyConfig::disabled().enabled) {
        return 230;
    }
    rrr::CachedResponse wrapped_response{
        rrr::IdempotencyKey::empty(), 0, {},
        std::numeric_limits<std::uint64_t>::max() - 5};
    if (!wrapped_response.is_expired(5, 10) ||
        wrapped_response.is_expired(5, 0)) {
        return 231;
    }
    auto idempotency_config = idempotency_defaults;
    idempotency_config.ttl_ms = 100;
    idempotency_config.max_entries = 2;
    rrr::IdempotencyCache idempotency_cache(idempotency_config);
    rusty::Vec<std::uint8_t> payload_one;
    payload_one.push(1);
    payload_one.push(2);
    rusty::Vec<std::uint8_t> payload_two;
    payload_two.push(4);
    const rrr::IdempotencyKey first_key{1, 1};
    const rrr::IdempotencyKey second_key{1, 2};
    const rrr::IdempotencyKey third_key{1, 3};
    idempotency_cache.store(first_key, 11, payload_one, 1'000);
    idempotency_cache.store(second_key, 22, payload_two, 1'050);
    std::int32_t cached_error = -1;
    rusty::Vec<std::uint8_t> cached_payload;
    if (!idempotency_cache.lookup(
            first_key, 1'050, cached_error, cached_payload) ||
        cached_error != 11 || cached_payload.len() != 2) {
        return 232;
    }
    idempotency_cache.store(third_key, 33, payload_two, 1'100);
    if (idempotency_cache.size() != 2 ||
        idempotency_cache.evictions() != 1 ||
        idempotency_cache.lookup(
            second_key, 1'100, cached_error, cached_payload) ||
        !idempotency_cache.lookup(
            first_key, 1'100, cached_error, cached_payload)) {
        return 233;
    }
    idempotency_cache.store(first_key, 44, payload_two, 1'110);
    if (!idempotency_cache.lookup(
            first_key, 1'110, cached_error, cached_payload) ||
        cached_error != 44 || idempotency_cache.hits() != 3 ||
        idempotency_cache.misses() != 1 ||
        idempotency_cache.evict_expired(1'211) != 2 ||
        idempotency_cache.size() != 0) {
        return 234;
    }
    idempotency_cache.reset_stats();
    idempotency_cache.set_config(rrr::IdempotencyConfig::disabled());
    idempotency_cache.store({2, 1}, 0, payload_one, 0);
    if (idempotency_cache.enabled() || idempotency_cache.size() != 0 ||
        idempotency_cache.hits() != 0 || idempotency_cache.misses() != 0 ||
        idempotency_cache.evictions() != 0) {
        return 235;
    }

    if (rrr::this_fiber::get_id() != 0 ||
        rrr::this_fiber::current().is_some() ||
        rrr::this_fiber::in_fiber_context()) {
        return 236;
    }
    rrr::this_fiber::yield();
    rrr::this_fiber::sleep_us(0);
    rrr::this_fiber::sleep_ms(0);
    rrr::this_fiber::sleep_s(0);
    rrr::this_fiber::sleep_until_us(rrr::Time::now(true));

    if (rrr::clamp(5, 0, 10) != 5 || rrr::clamp(-2, 0, 10) != 0 ||
        rrr::clamp(12, 0, 10) != 10 ||
        rrr::clamp(canary::MiscValue(-2), canary::MiscBound{0},
                   canary::MiscBound{10}).value != 0 ||
        rrr::clamp(canary::MiscValue(12), canary::MiscBound{0},
                   canary::MiscBound{10}).value != 10) {
        return 237;
    }
    int job_calls = 0;
    auto one_time_job = rrr::OneTimeJob::new_(
        rusty::Function<void()>([&job_calls]() { ++job_calls; }));
    rrr::Job* job = &one_time_job;
    if (!job->Ready() || job->Done()) {
        return 238;
    }
    job->Work();
    if (job->Ready() || !job->Done() || job_calls != 1 ||
        rrr::get_ncpu() <= 0 || rrr::format_thousands(0.0) != "0.00" ||
        rrr::format_thousands(-0.0) != "0.00" ||
        rrr::format_thousands(1234.5) != "1,234.50" ||
        rrr::format_thousands(-1234567.89) != "-1,234,567.89" ||
        rrr::format_thousands(999.999) != "1,000.00") {
        return 239;
    }
    if (rrr::channel_error_to_string(rrr::ChannelError::None) != "None" ||
        rrr::channel_error_to_string(rrr::ChannelError::Timeout) != "Timeout" ||
        rrr::channel_error_to_string(
            static_cast<rrr::ChannelError>(-1)) != "Unknown") {
        return 240;
    }
    const auto remove_count_before = rrr::epoll_remove_count.load(
        rusty::sync::atomic::Ordering::SeqCst);
    rrr::epoll_bump_remove_count();
    if (rrr::epoll_remove_count.load(
            rusty::sync::atomic::Ordering::SeqCst) !=
        remove_count_before + 1) {
        return 241;
    }
    auto callback_manager = rrr::CallbackManager::new_();
    if (callback_manager.has_callbacks() ||
        callback_manager.callback_count() != 0) {
        return 242;
    }
    auto switchboard = rrr::InMemorySwitchboard::new_();
    if (switchboard.find_listener("missing").is_some()) {
        return 243;
    }
    auto spin_lock = rrr::SpinLock::new_();
    spin_lock.lock();
    spin_lock.unlock();
    rrr::cpu_pause();
    if (!rrr::likely(true) || rrr::unlikely(false)) {
        return 244;
    }
    rrr::any_message_registry::clear_for_testing();
    if (rrr::any_message_registry::is_registered_name("missing") ||
        rrr::any_message_registry::is_registered_type(
            std::type_index(typeid(int)))) {
        return 245;
    }
    if (rrr::kTcpConnectionOutboundHighWaterDefault !=
        static_cast<size_t>(4) * 1024 * 1024) {
        return 246;
    }
    return 0;
}
"""


def require_importer_coverage(modules: list[extraction.ModuleEntry]) -> None:
    """Require one direct import and one concrete use per canonical module."""

    source = importer_source()
    canonical = {module.cpp_module for module in modules}
    if canonical != set(IMPORTER_USE_MARKERS):
        raise GateError(
            "combined-importer use ratchet does not equal canonical manifest"
        )
    imports = Counter(
        re.findall(r"^import (rrr\.[^;\n]+);[ \t]*$", source, re.MULTILINE)
    )
    missing_or_duplicated = sorted(
        module for module in canonical if imports[module] != 1
    )
    if missing_or_duplicated:
        raise GateError(
            "combined importer must directly import every canonical module "
            f"exactly once: {missing_or_duplicated!r}"
        )
    missing_uses = sorted(
        module
        for module, marker in IMPORTER_USE_MARKERS.items()
        if marker not in source
    )
    if missing_uses:
        raise GateError(
            "combined importer lacks concrete canonical module use(s): "
            + ", ".join(missing_uses)
        )


def compile_module(
    clang: Path,
    root: Path,
    include: Path,
    source_dir: Path,
    work_dir: Path,
    module_name: str,
    cxx_flags: list[str],
    prebuilt_module_dirs: list[Path],
    configured_module_map: dict[str, Path],
    configured_module_dependencies: dict[str, set[str]],
) -> Path:
    source = source_dir / f"{module_name}.cppm"
    pcm = work_dir / f"{module_name}.pcm"
    object_file = work_dir / f"{module_name}.o"
    direct_module_map = generated_lane_module_map(
        configured_module_map,
        work_dir,
        dependency_names=(
            set(configured_module_map)
            if module_name == "rrr"
            else configured_module_dependencies[module_name]
        ),
        exclude=module_name,
    )
    module_path_flags = (
        module_file_flags(direct_module_map)
        if configured_module_map
        else [
            f"-fprebuilt-module-path={path}"
            for path in (work_dir, *prebuilt_module_dirs)
        ]
    )
    run(
        [
            str(clang),
            "-std=gnu++23",
            *cxx_flags,
            "-Wno-deprecated-declarations",
            "-I",
            str(include),
            "-I",
            str(root),
            *module_path_flags,
            "--precompile",
            str(source),
            "-o",
            str(pcm),
        ],
        root,
    )
    run(
        [
            str(clang),
            "-std=gnu++23",
            *cxx_flags,
            "-I",
            str(root),
            *module_path_flags,
            "-c",
            str(pcm),
            "-o",
            str(object_file),
        ],
        root,
    )
    return object_file


def grouped_link_inputs(paths: list[Path]) -> list[str]:
    rendered = [str(path) for path in paths]
    if sys.platform.startswith("linux"):
        return ["-Wl,--start-group", *rendered, "-Wl,--end-group"]
    return rendered


def resolve_file(root: Path, raw: str, description: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_file():
        raise GateError(f"{description} is unavailable: {path}")
    return path


def resolve_generated_dir(root: Path, raw: str) -> Path:
    output = Path(raw)
    if not output.is_absolute():
        output = root / output
    output = output.resolve()
    if not output.is_dir():
        raise GateError(f"generated crate directory is unavailable: {output}")
    return output


def resolve_prebuilt_module_dirs(
    root: Path,
    raw_roots: list[str],
    *,
    required_pcm: str | None = None,
) -> list[Path]:
    directories: set[Path] = set()
    found_required_pcm = False
    for raw in raw_roots:
        module_root = Path(raw)
        if not module_root.is_absolute():
            module_root = root / module_root
        module_root = module_root.resolve()
        if not module_root.is_dir():
            raise GateError(
                f"runtime prebuilt-module root is unavailable: {module_root}"
            )
        for pcm in module_root.rglob("*.pcm"):
            if pcm.is_file():
                directories.add(pcm.parent.resolve())
                found_required_pcm = (
                    found_required_pcm or pcm.name == required_pcm
                )
    if raw_roots and required_pcm is not None and not found_required_pcm:
        raise GateError(
            "prebuilt-module roots do not contain required BMI "
            f"{required_pcm}"
        )
    return sorted(directories)


def resolve_configured_module_map(
    root: Path, raw_build_roots: list[str]
) -> dict[str, Path]:
    """Read CMake's explicit module map without guessing BMI filenames."""

    mappings: dict[str, Path] = {}
    for raw in raw_build_roots:
        build_root = Path(raw)
        if not build_root.is_absolute():
            build_root = root / build_root
        build_root = build_root.resolve()
        if not build_root.is_dir():
            raise GateError(
                f"configured module-map root is unavailable: {build_root}"
            )
        for modmap in build_root.rglob("*.o.modmap"):
            if not modmap.is_file() or "rrr.dir" not in modmap.parts:
                continue
            # Retired historical .cpp carriers can leave stale CMake modmaps
            # in an incremental build tree. Only generated canonical owners
            # under goal0-crate-cpp define the dependency closure for this
            # lane; inline providers live elsewhere and are not queried here.
            if "goal0-crate-cpp" not in modmap.parts:
                continue
            for line in modmap.read_text(encoding="utf-8").splitlines():
                fields = shlex.split(line)
                if len(fields) != 1:
                    continue
                field = fields[0]
                if field.startswith("-fmodule-file="):
                    assignment = field.removeprefix("-fmodule-file=")
                    if "=" not in assignment:
                        continue
                    module_name, raw_path = assignment.split("=", 1)
                elif field.startswith("-fmodule-output="):
                    raw_path = field.removeprefix("-fmodule-output=")
                    module_name = Path(raw_path).stem
                    if not module_name.startswith("rrr."):
                        continue
                else:
                    continue
                module_path = Path(raw_path)
                if not module_path.is_absolute():
                    module_path = build_root / module_path
                module_path = module_path.resolve()
                if not module_path.is_file():
                    raise GateError(
                        f"configured BMI for {module_name} is unavailable: "
                        f"{module_path}"
                    )
                previous = mappings.get(module_name)
                if previous is not None and previous != module_path:
                    raise GateError(
                        f"configured BMI mapping for {module_name} is "
                        f"ambiguous: {previous} vs {module_path}"
                    )
                mappings[module_name] = module_path
    if raw_build_roots and not mappings:
        raise GateError("configured module-map roots contain no CMake modmaps")
    return mappings


def resolve_configured_module_dependencies(
    root: Path, raw_build_roots: list[str]
) -> dict[str, set[str]]:
    """Read each configured rrr provider's exact CMake BMI closure."""

    dependencies: dict[str, set[str]] = {}
    for raw in raw_build_roots:
        build_root = Path(raw)
        if not build_root.is_absolute():
            build_root = root / build_root
        build_root = build_root.resolve()
        if not build_root.is_dir():
            raise GateError(
                f"configured module-map root is unavailable: {build_root}"
            )
        for modmap in build_root.rglob("*.o.modmap"):
            if not modmap.is_file() or "rrr.dir" not in modmap.parts:
                continue
            # Retired historical .cpp carriers can leave stale CMake modmaps
            # in an incremental build tree. Canonical dependency closures
            # come only from the generated crate-provider directory.
            if "goal0-crate-cpp" not in modmap.parts:
                continue
            output_name: str | None = None
            imported: set[str] = set()
            for line in modmap.read_text(encoding="utf-8").splitlines():
                fields = shlex.split(line)
                if len(fields) != 1:
                    continue
                field = fields[0]
                if field.startswith("-fmodule-output="):
                    output_name = Path(
                        field.removeprefix("-fmodule-output=")
                    ).stem
                elif field.startswith("-fmodule-file="):
                    assignment = field.removeprefix("-fmodule-file=")
                    if "=" in assignment:
                        imported.add(assignment.split("=", 1)[0])
            if output_name is None or not output_name.startswith("rrr."):
                continue
            previous = dependencies.get(output_name)
            if previous is not None and previous != imported:
                raise GateError(
                    f"configured dependency closure for {output_name} is "
                    f"ambiguous: {sorted(previous)!r} vs {sorted(imported)!r}"
                )
            dependencies[output_name] = imported
    if raw_build_roots and not dependencies:
        raise GateError("configured module-map roots contain no rrr provider maps")
    return dependencies


def module_file_flags(
    module_map: dict[str, Path], *, exclude: str | None = None
) -> list[str]:
    return [
        f"-fmodule-file={name}={path}"
        for name, path in sorted(module_map.items())
        if name != exclude
    ]


def generated_lane_module_map(
    configured_module_map: dict[str, Path],
    work_dir: Path,
    *,
    dependency_names: set[str],
    exclude: str | None = None,
) -> dict[str, Path]:
    """Use all and only CMake's configured dependency BMI closure.

    Inline BMIs may themselves import canonical modules, so Clang requires the
    exact configured canonical BMI identities used to build those inline
    dependencies. The provider object under test is still compiled directly
    from generated source and linked ahead of the production archive; exact
    output/surface/ABI ratchets prove its declaration and implementation.
    """

    missing_configured = dependency_names - set(configured_module_map)
    if missing_configured:
        raise GateError(
            "generated lane dependency closure lacks configured BMI(s): "
            + ", ".join(sorted(missing_configured))
        )
    result = {name: configured_module_map[name] for name in dependency_names}
    if exclude is not None:
        result.pop(exclude, None)
    return result


def generated_module_order(
    output: Path,
    modules: list[extraction.ModuleEntry],
    configured_module_dependencies: dict[str, set[str]],
) -> list[extraction.ModuleEntry]:
    """Return a stable dependency order for generated canonical modules."""

    by_name = {module.cpp_module: module for module in modules}
    original_index = {
        module.cpp_module: index for index, module in enumerate(modules)
    }
    dependencies: dict[str, set[str]] = {}
    for module in modules:
        text = read_generated(
            output / f"{module.cpp_module}.cppm",
            f"child module {module.cpp_module}",
        )
        imports = set(
            re.findall(
                r"^(?:export )?import (rrr\.[^;\s]+);[ \t]*$",
                text,
                flags=re.MULTILINE,
            )
        )
        dependencies[module.cpp_module] = (
            imports | configured_module_dependencies[module.cpp_module]
        ) & by_name.keys()

    ordered: list[extraction.ModuleEntry] = []
    remaining = set(by_name)
    while remaining:
        ready = sorted(
            (
                name
                for name in remaining
                if not (dependencies[name] & remaining)
            ),
            key=original_index.__getitem__,
        )
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise GateError(
                "generated canonical module import cycle prevents independent "
                f"compilation: {cycle}"
            )
        for name in ready:
            ordered.append(by_name[name])
            remaining.remove(name)
    return ordered


def check_generated_output(
    *,
    root: Path,
    output: Path,
    modules: list[extraction.ModuleEntry],
    clang: Path,
    nm: Path,
    production: Path | None,
    runtime_libraries: list[Path],
    cxx_flags: list[str],
    link_flags: list[str],
    prebuilt_module_dirs: list[Path],
    configured_module_map: dict[str, Path],
    configured_module_dependencies: dict[str, set[str]],
) -> None:
    require_importer_coverage(modules)
    require_cpp_surfaces(root, output, modules)
    require_zero_hand_slots(output / "rusty_hand_slots.md")
    include = root / "third-party/rusty-cpp/include"

    # Compilation products live outside the build-tree generation directory.
    # That directory remains a deterministic crate-output census shared by the
    # production target and this gate.
    # BMIs can be tens of MiB each. Keep this transient lane beside the build
    # tree rather than on a potentially small/shared /tmp tmpfs.
    with tempfile.TemporaryDirectory(
        prefix=".rrr-crate-mode-compile-", dir=output.parent
    ) as temporary:
        work = Path(temporary)
        generated_object_by_name: dict[str, Path] = {}
        for module in generated_module_order(
            output, modules, configured_module_dependencies
        ):
            generated_object_by_name[module.cpp_module] = compile_module(
                clang,
                root,
                include,
                output,
                work,
                module.cpp_module,
                cxx_flags,
                prebuilt_module_dirs,
                configured_module_map,
                configured_module_dependencies,
            )
        generated_objects = [
            generated_object_by_name[module.cpp_module] for module in modules
        ]
        # Compile the partial umbrella as a syntax/import-closure proof. The
        # configured CMake BMI map keeps its dependency graph coherent; the
        # root is not a production provider or link input.
        compile_module(
            clang,
            root,
            include,
            output,
            work,
            "rrr",
            cxx_flags,
            prebuilt_module_dirs,
            configured_module_map,
            configured_module_dependencies,
        )

        importer = work / "importer.cpp"
        importer_object = work / "importer.o"
        importer.write_text(importer_source(), encoding="utf-8")
        importer_module_flags = (
            module_file_flags(
                generated_lane_module_map(
                    configured_module_map,
                    work,
                    dependency_names=set(configured_module_map),
                )
            )
            if configured_module_map
            else [
                f"-fprebuilt-module-path={work}",
                *(
                    f"-fprebuilt-module-path={path}"
                    for path in prebuilt_module_dirs
                ),
            ]
        )
        run(
            [
                str(clang),
                "-std=gnu++23",
                *cxx_flags,
                "-I",
                str(include),
                *importer_module_flags,
                "-c",
                str(importer),
                "-o",
                str(importer_object),
            ],
            root,
        )

        generated_link_inputs = [*generated_objects]
        # Generated canonical providers still import the production archive's
        # remaining inline modules (for example rrr.debugging, rrr.reactor,
        # and rrr.serializable). Because every generated object precedes the
        # archive, the linker extracts only those inline dependencies; the
        # independently compiled canonical providers cannot be substituted.
        if production is not None:
            generated_link_inputs.append(production)
        generated_link_inputs.extend(runtime_libraries)
        link_sets: list[tuple[str, list[Path]]] = [
            ("generated", generated_link_inputs),
        ]
        if production is not None:
            link_sets.append(
                (
                    "production",
                    [production, *runtime_libraries],
                )
            )
        for label, link_inputs in link_sets:
            executable_path = work / f"importer-{label}"
            run(
                [
                    str(clang),
                    "-std=gnu++23",
                    *cxx_flags,
                    str(importer_object),
                    *grouped_link_inputs(link_inputs),
                    *link_flags,
                    "-o",
                    str(executable_path),
                ],
                root,
            )
            run([str(executable_path)], root)

        generated_symbol_count = 0
        production_symbol_count = 0
        for module, generated_object in zip(
            modules, generated_objects, strict=True
        ):
            generated_symbols = module_symbols(
                nm, root, generated_object, module.cpp_module
            )
            generated_symbol_count += len(generated_symbols)
            require_expected_symbols(
                module.cpp_module,
                "crate-generated object",
                generated_symbols,
            )

            require_all_module_raw_symbols(
                module.cpp_module,
                "crate-generated object",
                exact_module_raw_symbols(
                    nm, root, generated_object, module.cpp_module
                ),
            )

            if production is not None:
                # The production library also carries any hand-written module
                # implementation unit for this module (see
                # PLATFORM_IMPL_SYMBOLS); the crate object cannot.
                platform_symbols = PLATFORM_IMPL_SYMBOLS.get(
                    module.cpp_module, frozenset()
                )
                production_symbols = module_symbols(
                    nm, root, production, module.cpp_module
                )
                production_symbol_count += len(production_symbols)
                require_expected_symbols(
                    module.cpp_module,
                    "production library",
                    production_symbols,
                    extra=platform_symbols,
                )
                if production_symbols != generated_symbols | set(
                    platform_symbols
                ):
                    raise GateError(
                        f"production {module.cpp_module} ABI differs from "
                        "the independently compiled generated-object ABI "
                        "plus its ratcheted platform implementation unit"
                    )
                require_all_module_raw_symbols(
                    module.cpp_module,
                    "production library",
                    exact_module_raw_symbols(
                        nm, root, production, module.cpp_module
                    ),
                    extra=platform_symbols,
                )

        if generated_symbol_count != EXPECTED_TOTAL_PROVIDER_SYMBOLS:
            raise GateError(
                "crate-generated provider ABI must contain exactly "
                f"{EXPECTED_TOTAL_PROVIDER_SYMBOLS} unique strong symbols; "
                f"got {generated_symbol_count}"
            )
        expected_production_total = (
            EXPECTED_TOTAL_PROVIDER_SYMBOLS + EXPECTED_TOTAL_PLATFORM_SYMBOLS
        )
        if production is not None and (
            production_symbol_count != expected_production_total
        ):
            raise GateError(
                "production provider ABI must contain exactly "
                f"{expected_production_total} unique strong symbols "
                f"({EXPECTED_TOTAL_PROVIDER_SYMBOLS} crate-generated plus "
                f"{EXPECTED_TOTAL_PLATFORM_SYMBOLS} from platform "
                f"implementation units); got {production_symbol_count}"
            )


def check(args: argparse.Namespace) -> None:
    root = repository_root()
    # Crate mode validates local runtime-provided dependencies relative to the
    # manifest path it receives. Always hand crate mode a root-resolved
    # absolute manifest so its dependency walk is independent of this gate's
    # working directory.
    crate_manifest = (root / "Cargo.toml").resolve()
    transpiler = executable(root, args.transpiler, "rusty-cpp transpiler")
    verify_pinned_toolchain(root, transpiler)
    require_extraction_check(root, transpiler)
    modules = load_owned_modules(root)
    clang = executable(root, args.clang, "Clang C++ compiler")
    nm = executable(root, args.nm, "nm")
    production_raw = getattr(args, "production_library", None)
    production = (
        resolve_file(root, production_raw, "production library")
        if production_raw
        else None
    )
    runtime_libraries = [
        resolve_file(root, raw, "runtime library")
        for raw in (getattr(args, "runtime_library", None) or [])
    ]
    cxx_flags = list(getattr(args, "cxx_flag", None) or [])
    link_flags = list(getattr(args, "link_flag", None) or [])
    runtime_module_dirs = resolve_prebuilt_module_dirs(
        root,
        list(getattr(args, "runtime_module_root", None) or []),
        required_pcm="rusty.pcm",
    )
    dependency_module_dirs = resolve_prebuilt_module_dirs(
        root,
        list(getattr(args, "dependency_module_root", None) or []),
    )
    prebuilt_module_dirs = sorted(
        set(runtime_module_dirs) | set(dependency_module_dirs)
    )
    configured_module_map = resolve_configured_module_map(
        root,
        list(getattr(args, "configured_module_map_root", None) or []),
    )
    configured_module_dependencies = resolve_configured_module_dependencies(
        root,
        list(getattr(args, "configured_module_map_root", None) or []),
    )
    if configured_module_map:
        missing_configured_modules = sorted(
            module.cpp_module
            for module in modules
            if module.cpp_module not in configured_module_map
        )
        if missing_configured_modules:
            raise GateError(
                "configured CMake BMI map is missing canonical modules: "
                + ", ".join(missing_configured_modules)
            )
        missing_dependency_closures = sorted(
            module.cpp_module
            for module in modules
            if module.cpp_module not in configured_module_dependencies
        )
        if missing_dependency_closures:
            raise GateError(
                "configured CMake dependency map is missing canonical modules: "
                + ", ".join(missing_dependency_closures)
            )

    generated_raw = getattr(args, "generated_dir", None)
    if generated_raw:
        output = resolve_generated_dir(root, generated_raw)
        check_generated_output(
            root=root,
            output=output,
            modules=modules,
            clang=clang,
            nm=nm,
            production=production,
            runtime_libraries=runtime_libraries,
            cxx_flags=cxx_flags,
            link_flags=link_flags,
            prebuilt_module_dirs=prebuilt_module_dirs,
            configured_module_map=configured_module_map,
            configured_module_dependencies=configured_module_dependencies,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="rrr-crate-mode-") as temporary:
            output = Path(temporary)
            run(
                [
                    str(transpiler),
                    "--crate",
                    str(crate_manifest),
                    "--output-dir",
                    str(output),
                    "--cxx-namespace",
                    "rrr",
                    "--module-preamble",
                    str(root / MODULE_PREAMBLE),
                    "--type-map",
                    str(root / TYPE_MAP),
                    "--cpp-module-index",
                    str(root / CPP_MODULE_INDEX),
                ],
                root,
            )
            check_generated_output(
                root=root,
                output=output,
                modules=modules,
                clang=clang,
                nm=nm,
                production=production,
                runtime_libraries=runtime_libraries,
                cxx_flags=cxx_flags,
                link_flags=link_flags,
                prebuilt_module_dirs=prebuilt_module_dirs,
                configured_module_map=configured_module_map,
                configured_module_dependencies=configured_module_dependencies,
            )

    symbol_count = EXPECTED_TOTAL_PROVIDER_SYMBOLS
    production_label = " and production library" if production is not None else ""
    print(
        f"checked whole rrr crate ({len(modules) + 1} modules compiled, "
        "partial root compile-only, 0 hand slots), combined importer against generated "
        f"objects{production_label}, "
        "CallbackWrapper C++ layout/runtime/move parity, AvgStat layout/runtime, "
        "RpcError runtime contracts, ConnectionMetrics layout/concurrent/wrapping "
        "runtime contracts, CompletionTracker C++ layout/thread-safe lifecycle/"
        "wrapping runtime contracts, RandomGenerator byte-adapter/single-evaluation/"
        "precondition/wrapping/empty-weight/C-FFI runtime contracts, "
        "RequestOptions layout/factory/retry/timeout/jitter runtime contracts, "
        "ReconnectPolicy layout/factory/backoff/retry/jitter runtime contracts, and "
        "CircuitBreaker layout/factory/state/timeout/wrapping runtime contracts, "
        "RequestQueue layout/FIFO/config/overflow/expiry/callback-isolation/"
        "wrapping runtime contracts, "
        "Basetypes aliases/layout/sparse-wire/atomic/timing runtime contracts, "
        "ConnectionState layout/empty-callback/transition runtime contracts, and "
        "Heartbeat layout/empty-and-moved-callback/timing/timeout/wrapping runtime "
        "contracts, LoadBalancer layout/strategy/selection/wrapping runtime "
        "contracts, Utils layout/move/teardown/port/hostname/logging runtime "
        "contracts, FrameCodec layout/wire/fragmentation/compaction/wrapping runtime "
        "contracts, SerializableEnvelope template/layout/archive/runtime contracts, "
        "Future promise/ready/error runtime contracts, Logging exact tag/filter/line "
        "runtime contracts, Idempotency layout/wire/LRU/expiry/counter runtime "
        "contracts, Fiber context/sleep runtime contracts, Misc Job/clamp/ncpu/format "
        "runtime contracts, and "
        f"{symbol_count} exact provider-owned strong ABI symbols"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--production-library",
        help="production librrr archive to link/run and compare with direct generated objects",
    )
    parser.add_argument(
        "--generated-dir",
        help=(
            "pre-generated rusty-cpp crate output directory; when omitted, "
            "the gate generates into a temporary directory"
        ),
    )
    parser.add_argument(
        "--runtime-library",
        action="append",
        default=[],
        help=(
            "support archive appended to every link lane; may be repeated"
        ),
    )
    parser.add_argument(
        "--runtime-module-root",
        action="append",
        default=[],
        help=(
            "tree containing the configured rusty-cpp .pcm files needed by "
            "crate modules that import rusty; may be repeated"
        ),
    )
    parser.add_argument(
        "--dependency-module-root",
        action="append",
        default=[],
        help=(
            "tree containing configured dependency .pcm files used by "
            "generated rrr modules; may be repeated"
        ),
    )
    parser.add_argument(
        "--configured-module-map-root",
        action="append",
        default=[],
        help=(
            "configured CMake build tree whose explicit .modmap BMI mappings "
            "should be reused; may be repeated"
        ),
    )
    parser.add_argument(
        "--cxx-flag",
        action="append",
        default=[],
        help="compiler-driver flag used for module compilation and linking",
    )
    parser.add_argument(
        "--link-flag",
        action="append",
        default=[],
        help="additional flag appended to every link command",
    )
    parser.add_argument(
        "--transpiler",
        default=os.environ.get("RUSTY_CPP_TRANSPILER", DEFAULT_TRANSPILER),
    )
    parser.add_argument("--clang", default=os.environ.get("CXX", "clang++"))
    parser.add_argument("--nm", default=os.environ.get("NM", "nm"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        check(parse_args(argv))
    except GateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
