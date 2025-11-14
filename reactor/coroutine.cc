#include <utility>

#include <functional>
#include <iostream>
#include "../base/all.hpp"
#include "coroutine.h"
#include "reactor.h"

namespace rrr {

Coroutine::Coroutine(std::move_only_function<void()> func)
    : func_(std::move(func)),
      status_(INIT) {
}

Coroutine::~Coroutine() {
  // rusty::Box automatically handles cleanup
//  verify(0);
}

void Coroutine::BoostRunWrapper(boost_coro_yield_t& yield) {
  boost_coro_yield_ = yield;
  verify(func_);
  auto reactor = Reactor::GetReactor();
//  reactor->coros_;
  while (true) {
    auto sz = reactor->coros_.size();
    verify(sz > 0);
    func_();
    func_ = {};
    status_ = FINISHED;
    yield();
  }
}

void Coroutine::Run() const {
  verify(boost_coro_task_.is_none());
  verify(status_ == INIT);
  status_ = STARTED;
  auto reactor = Reactor::GetReactor();
//  reactor->coros_;
  auto sz = reactor->coros_.size();
  verify(sz > 0);
  auto task = std::bind(&Coroutine::BoostRunWrapper, const_cast<Coroutine*>(this), std::placeholders::_1);
  boost_coro_task_ = rusty::Some(rusty::make_box<boost_coro_task_t>(std::move(task)));
#ifdef USE_BOOST_COROUTINE1
  (*boost_coro_task_.unwrap_ref())();
#endif
}

void Coroutine::Yield() const {
  verify(boost_coro_yield_);
  verify(status_ == STARTED || status_ == RESUMED);
  status_ = PAUSED;
  boost_coro_yield_.value()();
}

void Coroutine::Continue() const {
  verify(status_ == PAUSED || status_ == RECYCLED);
  verify(boost_coro_task_.is_some());
  status_ = RESUMED;
  (*boost_coro_task_.unwrap_ref())();
  // some events might have been triggered from last coroutine,
  // but you have to manually call the scheduler to loop.
}

bool Coroutine::Finished() const {
  return status_ == FINISHED;
}

// WeakCoroutine::lock() implementation
// This is unsafe but necessary for the current architecture
// The Reactor owns all coroutines, so as long as we're in the reactor thread,
// the coroutine should still be alive
rusty::Rc<Coroutine> WeakCoroutine::lock() const {
  if (!raw_ptr_) {
    return rusty::Rc<Coroutine>();  // Return empty Rc
  }

  // Search for the coroutine in the reactor's coros_ set
  auto reactor = Reactor::GetReactor();
  for (const auto& rc_coro : reactor->coros_) {
    if (rc_coro.get() == raw_ptr_) {
      return rc_coro.clone();
    }
  }

  // Not found - return empty Rc
  return rusty::Rc<Coroutine>();
}

} // namespace rrr
