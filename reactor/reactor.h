#pragma once
#include <set>
#include <algorithm>
#include <unordered_map>
#include <list>
#include <thread>
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
  static std::shared_ptr<Reactor> GetDiskReactor();
  static thread_local std::shared_ptr<Reactor> sp_reactor_th_;
  static thread_local std::shared_ptr<Reactor> sp_disk_reactor_th_;
  static thread_local std::shared_ptr<Coroutine> sp_running_coro_th_;
  int server_id_;
  std::mutex ready_events_mutex_;

  struct thread_params{
		std::string ip_;
	};
  /*struct eventComp{
    bool operator()(const std::shared_ptr<Event>& lhs, const std::shared_ptf<Event>& rhs) const{
      return lhs->timeout
    }
  }*/

  /**
   * A reactor needs to keep reference to all coroutines created,
   * in case it is freed by the caller after a yield.
   */
  std::list<std::shared_ptr<Event>> all_events_{};
  std::set<std::shared_ptr<Event>> waiting_events_{};
  std::vector<std::shared_ptr<Event>> ready_events_{};
  std::list<std::shared_ptr<Event>> timeout_events_{};
  std::vector<std::shared_ptr<Event>> disk_events_{};
  std::list<std::shared_ptr<Event>> ready_disk_events_{};
  std::vector<std::shared_ptr<Event>> network_events_{};
  std::list<std::shared_ptr<Event>> ready_network_events_{};
  std::set<std::shared_ptr<Coroutine>> coros_{};
  std::vector<std::shared_ptr<Coroutine>> available_coros_{};
  std::unordered_map<uint64_t, std::function<void(Event&)>> processors_{};
  std::unordered_map<std::string, FILE*> opened_files_{};
	static thread_local std::unordered_map<std::string, std::vector<std::shared_ptr<rrr::Pollable>>> clients_;
	static thread_local std::unordered_set<std::string> dangling_ips_;
  bool looping_{false};
	bool slow_{false};
	long disk_times[50];
	int disk_count{0};
	int disk_index{0};
	int slow_count{0};
	int trying_count{0};
  std::thread::id thread_id_{};
  int64_t n_created_coroutines_{0};
  int64_t n_busy_coroutines_{0};
  int64_t n_active_coroutines_{0};
  int64_t n_active_coroutines_2_{0};
  int64_t n_idle_coroutines_{0};
  static SpinLock disk_job_;
	static SpinLock trying_job_;
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
  std::shared_ptr<Coroutine> CreateRunCoroutine(std::function<void()> func, const char *file="", int64_t line=0);
  // @safe - Main event loop
  void Loop(bool infinite = false, bool check_timeout = false);
	void DiskLoop();
	void NetworkLoop();
  // @safe - Continues execution of a paused coroutine
  void ContinueCoro(std::shared_ptr<Coroutine> sp_coro);
  void Recycle(std::shared_ptr<Coroutine>& sp_coro);
  void DisplayWaitingEv();
  void ReadyEventsThreadSafePushBack(std::shared_ptr<Event> ev);

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
    //events.push_back(sp_ev);
    //Log_info("ADDING %s %d", typeid(sp_ev).name(), events.size());
    return sp_ev;
  }

  // @safe - Creates event and returns reference
  template <typename Ev, typename... Args>
  static Ev& CreateEvent(Args&&... args) {
    auto sp_ev = CreateSpEvent<Ev>(args...);
    return *sp_ev;
  }
};

// @safe - Thread-safe polling manager with reference counting
class PollMgr: public rrr::RefCounted {
 public:
    class PollThread;

    PollThread* poll_threads_;
    const int n_threads_;
    bool need_disk_ = false;

protected:

    // RefCounted object uses protected dtor to prevent accidental deletion
    ~PollMgr();

public:

    // @unsafe - Creates threads and manages raw pthread handles
    // SAFETY: Proper thread lifecycle management
    PollMgr(int n_threads = 1, bool need_disk=false);
    PollMgr(const PollMgr&) = delete;
    PollMgr& operator=(const PollMgr&) = delete;
    // @unsafe - Returns raw pthread handle
    // SAFETY: Valid as long as PollMgr exists
    pthread_t* GetPthreads(int);

    // @safe - Thread-safe addition of pollable object
    void add(shared_ptr<Pollable>);
    // @safe - Thread-safe removal of pollable object  
    void remove(shared_ptr<Pollable>);
    // @safe - Thread-safe mode update
    void update_mode(shared_ptr<Pollable>, int new_mode);
    void pause();
    void resume();
  void slow(uint32_t sleep_usec);

  // Frequent Job
    // @safe - Thread-safe job management
    void add(std::shared_ptr<Job> sp_job);
    void remove(std::shared_ptr<Job> sp_job);
};

} // namespace rrr
