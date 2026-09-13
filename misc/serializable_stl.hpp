#pragma once

// Included in the exported module epilogue after the canonical declarations.
// These specializations expose trait interfaces for C++ STL fields. All calls
// forward through the same bounded ADL adapters as ordinary archives.
namespace srpc {

// Match the generated adapters' recursive trait bounds. Container shape alone
// does not make an unsupported element serializable.
template<class T> inline constexpr bool stl_serialize_adapter = [] {
    if constexpr (std::is_same_v<T, std::string> || std::is_same_v<T, std::string_view>) {
        return true;
    } else if constexpr (rusty::srpc_stl_map<T>) {
        return (requires { sizeof(SerializeAdapter<typename T::key_type>); }
                || std::is_base_of_v<Serialize, typename T::key_type>)
            && (requires { sizeof(SerializeAdapter<typename T::mapped_type>); }
                || std::is_base_of_v<Serialize, typename T::mapped_type>);
    } else if constexpr (rusty::srpc_stl_sequence<T>) {
        return requires { sizeof(SerializeAdapter<typename T::value_type>); }
            || std::is_base_of_v<Serialize, typename T::value_type>;
    } else return false;
}();

template<class T> inline constexpr bool stl_deserialize_key = [] {
    if constexpr (requires { typename T::key_compare; }) {
        return std::totally_ordered<typename T::key_type>;
    } else if constexpr (requires { typename T::hasher; }) {
        return std::equality_comparable<typename T::key_type>
            && requires(const typename T::key_type& key) { std::hash<typename T::key_type>{}(key); };
    } else return true;
}();

template<class T> inline constexpr bool stl_deserialize_adapter = [] {
    if constexpr (std::is_same_v<T, std::string>) {
        return true;
    } else if constexpr (rusty::srpc_stl_map<T>) {
        return (requires { sizeof(DeserializeAdapter<typename T::key_type>); }
                || std::is_base_of_v<Deserialize, typename T::key_type>)
            && (requires { sizeof(DeserializeAdapter<typename T::mapped_type>); }
                || std::is_base_of_v<Deserialize, typename T::mapped_type>)
            && std::default_initializable<typename T::key_type>
            && std::default_initializable<typename T::mapped_type>
            && stl_deserialize_key<T>;
    } else if constexpr (rusty::srpc_stl_sequence<T>) {
        return (requires { sizeof(DeserializeAdapter<typename T::value_type>); }
                || std::is_base_of_v<Deserialize, typename T::value_type>)
            && std::default_initializable<typename T::value_type>
            && stl_deserialize_key<T>;
    } else return false;
}();

template<class T> requires stl_serialize_adapter<T>
class SerializeAdapter<T> final : public Serialize {
    T value_;
public:
    SerializeAdapter(T value) : value_(std::move(value)) {}
    SerializeAdapter(SerializeAdapter&& other) : value_(std::move(other.value_)) {}
    void serialize(BinaryWriteArchive& archive) const override {
        rusty::srpc_adl_serialize(value_, archive);
    }
};

template<class T> requires stl_serialize_adapter<T>
class SerializeAdapterRef<T> final : public Serialize {
    const T& value_;
public:
    explicit SerializeAdapterRef(const T& value) : value_(value) {}
    void serialize(BinaryWriteArchive& archive) const override {
        rusty::srpc_adl_serialize(value_, archive);
    }
};

template<class T> requires stl_serialize_adapter<T>
class SerializeAdapterRefMut<T> final : public Serialize {
    T& value_;
public:
    explicit SerializeAdapterRefMut(T& value) : value_(value) {}
    void serialize(BinaryWriteArchive& archive) const override {
        rusty::srpc_adl_serialize(value_, archive);
    }
};

template<class T> requires stl_deserialize_adapter<T>
class DeserializeAdapter<T> final : public Deserialize {
    T value_;
public:
    DeserializeAdapter(T value) : value_(std::move(value)) {}
    DeserializeAdapter(DeserializeAdapter&& other) : value_(std::move(other.value_)) {}
    void deserialize(BinaryReadArchive& archive) override {
        rusty::srpc_adl_deserialize(value_, archive);
    }
};

template<class T> requires stl_deserialize_adapter<T>
class DeserializeAdapterRef<T> final : public Deserialize {
    const T& value_;
public:
    explicit DeserializeAdapterRef(const T& value) : value_(value) {}
    void deserialize(BinaryReadArchive&) override {
        std::abort();  // A const reference cannot expose mutable trait dispatch.
    }
};

template<class T> requires stl_deserialize_adapter<T>
class DeserializeAdapterRefMut<T> final : public Deserialize {
    T& value_;
public:
    explicit DeserializeAdapterRefMut(T& value) : value_(value) {}
    void deserialize(BinaryReadArchive& archive) override {
        rusty::srpc_adl_deserialize(value_, archive);
    }
};

}  // namespace srpc
