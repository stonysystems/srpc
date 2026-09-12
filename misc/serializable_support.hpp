#pragma once

#include <cstddef>
#include <cstdint>

extern "C" {
std::int64_t srpc_fd_write_once(int fd, const void* pointer, std::size_t length);
std::int64_t srpc_fd_read_once(int fd, void* pointer, std::size_t length);
std::int32_t srpc_fd_last_errno();
std::int32_t srpc_fd_interrupted_errno();
}

// C++ ADL and erased sink/source call adapters. The Rust counterparts forward
// into canonical traits; all serialization and payload ownership lives there.
namespace rusty {
namespace srpc_adl_detail {

// Poison ordinary lookup.  Only an overload in a payload/archive associated
// namespace can satisfy the dependent call below.
void serialize();
void deserialize();

template <typename T, typename Archive>
decltype(auto) call_serialize(const T& value, Archive& archive) {
    return serialize(value, archive);
}

template <typename T, typename Archive>
decltype(auto) call_deserialize(T& value, Archive& archive) {
    return deserialize(value, archive);
}

}  // namespace srpc_adl_detail

template <typename T, typename Archive>
void srpc_adl_serialize(const T& value, Archive& archive) {
    srpc_adl_detail::call_serialize(value, archive);
}

template <typename T, typename Archive>
void srpc_adl_deserialize(T& value, Archive& archive) {
    srpc_adl_detail::call_deserialize(value, archive);
}

template <typename Sink>
void srpc_sink_write(Sink& sink, const unsigned char* pointer, std::size_t length) {
    sink.write_bytes(pointer, length);
}

template <typename Source>
std::size_t srpc_source_read(Source& source, unsigned char* pointer, std::size_t length) {
    return source.read_bytes(pointer, length);
}

}  // namespace rusty
