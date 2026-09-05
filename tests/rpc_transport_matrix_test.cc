// @unsafe - Test file exercising the RPC round trip over multiple transports.
// @unsafe {
//
// Tier 2.3 of docs/testing-plan.md: one request/reply body run across more
// than one transport (gRPC fixture-matrix style), so a divergence in the
// shared server-dispatch / client-demux path shows up regardless of which
// channel carried it. This is the C++ lane's home for the matrix because TCP
// needs the linked plain-C socket kernels, which the pure-cargo Rust lane
// (no build.rs) cannot link -- the Rust lane already covers the in-memory
// round trip (tests/rpc_roundtrip_inmemory_rust.rs), and this is the first
// full RPC round trip in the gating C++ battery.
//
// The service echoes an i64 doubled, over a fast RPC (inline dispatch, no
// fiber -- keeps the test synchronous and deterministic). It is run over:
//   * the in-memory switchboard (set_channel_factory before start/connect);
//   * TCP loopback (auto-installed; bind to port 0, read the real port back).

#include <stddef.h>
#include <string.h>

#include <gtest/gtest.h>
#include <rusty/arc.hpp>
#include <rusty/box.hpp>
#include <rusty/function.hpp>
#include "../srpc.hpp"

// Serialization and the in-memory transport are trimmed from the umbrella.
import srpc.serializable;
import srpc.inmemory_channel;

import std;

using namespace srpc;

namespace {

constexpr int32_t ECHO_DOUBLE_RPC_ID = 0x00E0'1001;

// A raw Service (the shape a `raw` IDL method hands a handler, and the whole
// Rust service API): read an i64, reply with twice its value, on the fast
// path so dispatch is inline.
class EchoDoubleService : public Service {
 public:
    int32_t __reg_to__(Server& server, size_t svc_index) override {
        return server.reg_fast_rpc(ECHO_DOUBLE_RPC_ID, svc_index);
    }

    void __dispatch__(int32_t rpc_id, rusty::Box<Request> req,
                      WeakServerConnection weak_sconn) override {
        if (rpc_id != ECHO_DOUBLE_RPC_ID) {
            return;
        }
        int64_t value = 0;
        {
            BinaryReadArchive ar{.source_ = make_source_proxy_buffer(&req->src)};
            Deserialize_::deserialize(value, ar);
        }
        auto sconn_opt = weak_sconn.upgrade();
        if (sconn_opt.is_some()) {
            auto sconn = sconn_opt.unwrap();
            const int64_t doubled = value * 2;
            const_cast<ServerConnection&>(*sconn).reply(
                *req, 0, ServerReplyFn{[doubled](BinaryWriteArchive& out) {
                    Serialize_::serialize(doubled, out);
                }});
        }
    }
};

// Issue one echo request and return the doubled reply, asserting success.
int64_t echo_once(const rusty::Arc<Client>& client, int64_t arg) {
    auto fu_result = client->request(
        ECHO_DOUBLE_RPC_ID, FutureAttr{},
        [arg](BinaryWriteArchive& m) { Serialize_::serialize(arg, m); });
    EXPECT_TRUE(fu_result.is_ok());
    auto fu = fu_result.unwrap();
    fu->wait();
    EXPECT_EQ(fu->get_error_code(), 0);
    int64_t back = 0;
    deserialize_from(fu->get_reply(), back);
    return back;
}

TEST(RpcTransportMatrix, InMemoryRoundTrip) {
    auto switchboard = rusty::Arc<InMemorySwitchboard>::make(InMemorySwitchboard::new_());
    const char* addr = "inmemory://matrix";

    auto server = Server::new_(rusty::Some(PollThread::create()));
    server.set_channel_factory(make_inmemory_factory_proxy(
        rusty::Arc<InMemoryFactory>::make(InMemoryFactory::new_(switchboard.clone()))));
    server.reg_service_typed(rusty::make_box<EchoDoubleService>());
    ASSERT_EQ(server.start(reinterpret_cast<const int8_t*>(addr)), 0);

    auto client = Client::create(PollThread::create());
    client->set_channel_factory(make_inmemory_factory_proxy(
        rusty::Arc<InMemoryFactory>::make(InMemoryFactory::new_(switchboard.clone()))));
    ASSERT_EQ(client->connect(reinterpret_cast<const int8_t*>(addr), true), 0);

    EXPECT_EQ(echo_once(client, 21), 42);
    EXPECT_EQ(echo_once(client, -5), -10);

    client->close();
}

TEST(RpcTransportMatrix, TcpLoopbackRoundTrip) {
    auto poll = PollThread::create();
    auto server = Server::new_(rusty::Some(poll.clone()));
    server.reg_service_typed(rusty::make_box<EchoDoubleService>());
    // Bind to port 0; TCP factory is auto-installed.
    ASSERT_EQ(server.start(reinterpret_cast<const int8_t*>("127.0.0.1:0")), 0);
    const int32_t port = server.get_bound_port();
    ASSERT_GT(port, 0);

    std::string addr = "127.0.0.1:" + std::to_string(port);
    auto client = Client::create(poll.clone());
    ASSERT_EQ(client->connect(reinterpret_cast<const int8_t*>(addr.c_str()), true), 0);

    EXPECT_EQ(echo_once(client, 21), 42);
    EXPECT_EQ(echo_once(client, 1000), 2000);

    client->close();
}

}  // namespace
// @unsafe }
