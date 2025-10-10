#pragma once
#include <algorithm>
#include <list>
#include <memory>
#include <set>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include <rusty/rusty.hpp>
#include <rusty/thread.hpp>
#include "base/misc.hpp"
#include "event.h"
#include "quorum_event.h"
#include "coroutine.h"
#include "epoll_wrapper.h"

// External safety annotations for system functions used in this module
// @external: {
//   pthread_setname_np: [unsafe, (pthread_t, const char*) -> int]
// }

// External safety annotations for STL operations
// @external: {
//   operator!=: [safe, (auto, auto) -> bool]
//   operator==: [safe, (auto, auto) -> bool]
//   std::*::find: [safe, (auto) -> auto]
//   std::*::end: [safe, () -> auto]
// }

namespace rrr {

using std::make_unique;
using std::make_shared;

class Coroutine;
// TODO for now we depend on the rpc services, fix in the future.
// @safe - Thread-safe reactor with thread-local storage
class Reactor {
 public:
  // @safe - Returns thread-local reactor instance
  static std::shared_ptr<Reactor> GetReactor();
  static thread_local std::shared_ptr<Reactor> sp_reactor_th_;
  static thread_local std::shared_ptr<Coroutine> sp_running_coro_th_;
  /**
   * A reactor needs to keep reference to all coroutines created,
   * in case it is freed by the caller after a yield.
   */
  std::list<std::shared_ptr<Event>> all_events_{};
  std::list<std::shared_ptr<Event>> waiting_events_{};
  std::set<std::shared_ptr<Coroutine>> coros_{};
  std::vector<std::shared_ptr<Coroutine>> available_coros_{};
  std::unordered_map<uint64_t, std::function<void(Event&)>> processors_{};
  std::list<std::shared_ptr<Event>> timeout_events_{};
  bool looping_{false};
  std::thread::id thread_id_{};
#ifdef REUSE_CORO
#define REUSING_CORO (true)
#else
#define REUSING_CORO (false)
#endif

  // @safe - Checks and processes timeout events
  void CheckTimeout(std::vector<std::shared_ptr<Event>>&);
  /**
   * @param ev. is usually allocated on coroutine stack. memory managed by user.
   */
  // @safe - Creates and runs a new coroutine
  std::shared_ptr<Coroutine> CreateRunCoroutine(std::function<void()> func);
  // @safe - Main event loop
  void Loop(bool infinite = false);
  // @safe - Continues execution of a paused coroutine
  void ContinueCoro(std::shared_ptr<Coroutine> sp_coro);

  ~Reactor() {
//    verify(0);
  }
  friend Event;

  // @safe - Creates shared_ptr event with perfect forwarding
  template <typename Ev, typename... Args>
  static shared_ptr<Ev> CreateSpEvent(Args&&... args) {
    auto sp_ev = make_shared<Ev>(args...);
    sp_ev->__debug_creator = 1;
    // TODO push them into a wait queue when they actually wait.
    auto& events = GetReactor()->all_events_;
    events.push_back(sp_ev);
    return sp_ev;
  }

  // @safe - Creates event and returns reference
  template <typename Ev, typename... Args>
  static Ev& CreateEvent(Args&&... args) {
    return *CreateSpEvent<Ev>(args...);
  }
};

// @safe - Thread-safe polling thread with automatic memory management
class PollThread {
private:
    Epoll poll_{};

    SpinLock l_;
    // Authoritative storage: fd -> shared_ptr<Pollable>
    std::unordered_map<int, std::shared_ptr<Pollable>> fd_to_pollable_;
    std::unordered_map<int, int> mode_; // fd->mode

    std::set<std::shared_ptr<Job>> set_sp_jobs_;

    std::unordered_set<int> pending_remove_;  // Store fds to remove
    SpinLock pending_remove_l_;
    SpinLock lock_job_;

    rusty::Option<rusty::thread::JoinHandle<void>> join_handle_;
    bool stop_flag_;

    void poll_loop();

    // @safe - Launches polling thread using rusty::thread
    void start();

    // @unsafe - Triggers ready jobs in coroutines
    // SAFETY: Uses spinlock for thread safety
    void TriggerJob();

public:
    ~PollThread();
    // @safe - Creates thread using rusty::thread for memory safety
    // Note: n_threads parameter ignored (kept for backward compatibility)
    PollThread(int n_threads = 1);
    PollThread(const PollThread&) = delete;
    PollThread& operator=(const PollThread&) = delete;

    // @safe - Thread-safe addition of pollable object
    void add(std::shared_ptr<Pollable> poll);
    // @safe - Thread-safe removal of pollable object
    void remove(Pollable& poll);
    // @safe - Thread-safe mode update
    void update_mode(Pollable& poll, int new_mode);

    // Frequent Job
    // @safe - Thread-safe job management
    void add(std::shared_ptr<Job> sp_job);
    void remove(std::shared_ptr<Job> sp_job);

    // For testing: get number of epoll Remove() calls
    int get_remove_count() const { return poll_.remove_count_.load(); }
};

} // namespace rrr
