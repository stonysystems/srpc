#pragma once

// Compatibility import for srpc.serializable, generated from misc/serializable.rs.
//
// Consumers that #include "serializable.hpp" at TU scope get the
// import propagated to them — works because every existing include
// site is at the top of a .cc/.cpp file or top of a header that is
// itself included at TU top.

import srpc.serializable;
