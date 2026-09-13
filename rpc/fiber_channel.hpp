#pragma once
// Anchor shim. The real `FiberChannel` declaration lives in the
// `srpc.fiber_channel` module, generated from rpc/fiber_channel.rs.
// `<memory>` is the actual anchor that keeps libc++ `operator new`
// in global-module attachment for downstream TUs.
#include <memory>
