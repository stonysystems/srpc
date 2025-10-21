#include <utility>

#include <functional>
#include <iostream>
#include <memory>
#include <boost/coroutine2/protected_fixedsize_stack.hpp>
#include "../base/all.hpp"
#include "coroutine.h"
#include "reactor.h"

// #define USE_PROTECTED_STACK

namespace rrr {
uint64_t Coroutine::global_id = 0;

Coroutine::Coroutine(std::move_only_function<void()> func)
    : func_(std::move(func)), status_(INIT), id(Coroutine::global_id++) {
}

Coroutine::~Coroutine() {
  verify(up_boost_coro_task_ != nullptr);
  up_boost_coro_task_.reset();
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
    verify(func_);
    func_();
    func_ = {};
    status_ = FINISHED;
    if (needs_finalize_) {
      Log_info("Warning: We did not deal with backlog issues");
      needs_finalize_ = false;
    }
    Reactor::GetReactor()->n_active_coroutines_--;
    yield();
  }
}

void Coroutine::Run() {
  verify(!up_boost_coro_task_);
  verify(status_ == INIT);
  status_ = STARTED;
  auto reactor = Reactor::GetReactor();
//  reactor->coros_;
  auto sz = reactor->coros_.size();
  verify(sz > 0);

#ifdef USE_PROTECTED_STACK
  up_boost_coro_task_ = std::make_unique<boost_coro_task_t>(
      boost::coroutines2::protected_fixedsize_stack(boost::context::stack_traits::default_size() * 2),
      std::bind(&Coroutine::BoostRunWrapper, this, std::placeholders::_1)
      );
#else
  up_boost_coro_task_ = std::make_unique<boost_coro_task_t>(
      boost::coroutines2::default_stack(boost::context::stack_traits::default_size() * 2),
      std::bind(&Coroutine::BoostRunWrapper, this, std::placeholders::_1)
      );
#endif
#ifdef USE_BOOST_COROUTINE1
  (*up_boost_coro_task_)();
#endif
}

void Coroutine::Yield() {
  verify(boost_coro_yield_);
  verify(status_ == STARTED || status_ == RESUMED || status_ == FINALIZING);
  status_ = PAUSED;
  Reactor::GetReactor()->n_active_coroutines_--;
  boost_coro_yield_.value()();
}

void Coroutine::Continue() {
  verify(status_ == PAUSED || status_ == RECYCLED);
  verify(up_boost_coro_task_);
  status_ = RESUMED;
  (*up_boost_coro_task_)();
  // some events might have been triggered from last coroutine,
  // but you have to manually call the scheduler to loop.
}

bool Coroutine::Finished() {
  return status_ == FINISHED || status_ == RECYCLED;
}

} // namespace rrr
