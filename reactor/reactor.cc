
#include <unistd.h>
#include <string.h>
#include <errno.h>
#include <unordered_map>
#include <unordered_set>
#include <functional>
#include <mutex>
#include <utility>
#include <cstdlib>
#include <atomic>
#include "../base/all.hpp"
#include "reactor.h"
#include "coroutine.h"
#include "event.h"
#include "quorum_event.h"
#include "epoll_wrapper.h"
#include "sys/times.h"

// #define DEBUG_WAIT

namespace rrr {

const int64_t n_max_coroutine = 2000;

thread_local std::shared_ptr<Reactor> Reactor::sp_reactor_th_{};
thread_local std::shared_ptr<Reactor> Reactor::sp_disk_reactor_th_{};
thread_local std::shared_ptr<Coroutine> Reactor::sp_running_coro_th_{};
thread_local std::unordered_map<std::string, std::vector<std::shared_ptr<rrr::Pollable>>> Reactor::clients_{};
thread_local std::unordered_set<std::string> Reactor::dangling_ips_{};
SpinLock Reactor::disk_job_;
SpinLock Reactor::trying_job_;

// @safe - Returns current thread-local coroutine
std::shared_ptr<Coroutine> Coroutine::CurrentCoroutine() {
  // TODO re-enable this verify when all callers guarantee a running coroutine
  return Reactor::sp_running_coro_th_;
}

// @unsafe - Creates and runs a new coroutine with function wrapping
// SAFETY: Reactor manages coroutine lifecycle properly
std::shared_ptr<Coroutine>
Coroutine::CreateRunImpl(std::move_only_function<void()> func, const char* file, int64_t line) {
  auto& reactor = *Reactor::GetReactor();
  auto coro = reactor.CreateRunCoroutine(std::move(func), file, line);
  // some events might be triggered in the last coroutine.
  return coro;
}

void Coroutine::Sleep(uint64_t microseconds) {
  auto x = Reactor::CreateSpEvent<TimeoutEvent>(microseconds);
  x->Wait();
}

// @unsafe - Returns thread-local reactor instance, creates if needed
// SAFETY: Thread-local storage ensures thread safety
std::shared_ptr<Reactor>
Reactor::GetReactor() {
  if (!sp_reactor_th_) {
    Log_debug("create a coroutine scheduler");
    if (!REUSING_CORO)
      Log_warn("reusing coroutine not enabled!");
    sp_reactor_th_ = std::make_shared<Reactor>();
    sp_reactor_th_->thread_id_ = std::this_thread::get_id();
  }
  return sp_reactor_th_;
}

std::shared_ptr<Reactor>
Reactor::GetDiskReactor() {
  if (!sp_disk_reactor_th_) {
    Log_debug("create a disk coroutine scheduler");
    sp_disk_reactor_th_ = std::make_shared<Reactor>();
    sp_disk_reactor_th_->thread_id_ = std::this_thread::get_id();
  }
  return sp_disk_reactor_th_;
}

/**
 * @param func
 * @return
 */
// @unsafe - Creates and runs coroutine with complex state management
// SAFETY: Proper lifecycle management with shared_ptr
// std::shared_ptr<Coroutine>
// Reactor::CreateRunCoroutine(const std::function<void()> func, const char *file, int64_t line) {
//   std::shared_ptr<Coroutine> sp_coro;
//   const bool reusing = REUSING_CORO && !available_coros_.empty();
//   if (reusing) {
//     n_idle_coroutines_--;
//     sp_coro = available_coros_.back();
//     sp_coro->id = Coroutine::global_id++;
//     available_coros_.pop_back();
//     verify(!sp_coro->func_);
//     sp_coro->func_ = func;
//   } else {
//     // if (n_created_coroutines_ >= n_max_coroutine)
//     //   return nullptr;
//     sp_coro = std::make_shared<Coroutine>(func);
//     verify(sp_coro->status_ == Coroutine::INIT);
//     n_created_coroutines_++;
//     if (n_created_coroutines_ % 1024 == 0) {
//       Log_info("created %d, busy %d, idle %d coroutines on server %d, "
//                "recent %s:%llx",
//                (int)n_created_coroutines_,
//                (int)n_busy_coroutines_,
//                (int)n_idle_coroutines_,
//                server_id_,
//                file, line);
//     }

//   }
//   n_busy_coroutines_++;
//   coros_.insert(sp_coro);
//   ContinueCoro(sp_coro);
// //  Loop();
//   return sp_coro;
// }

std::shared_ptr<Coroutine>
Reactor::CreateRunCoroutine(std::move_only_function<void()> func, const char* file, int64_t line) {
  std::shared_ptr<Coroutine> sp_coro;
  std::move_only_function<void()> local_func(std::move(func));
  const bool reusing = REUSING_CORO && !available_coros_.empty();
  if (reusing) {
    n_idle_coroutines_--;
    sp_coro = available_coros_.back();
    sp_coro->id = Coroutine::global_id++;
    available_coros_.pop_back();
    verify(!sp_coro->func_);
    sp_coro->func_ = std::move(local_func);
  } else {
    sp_coro = std::make_shared<Coroutine>(std::move(local_func));
    verify(sp_coro->status_ == Coroutine::INIT);
    n_created_coroutines_++;
    if (n_created_coroutines_ % 1024 == 0) {
      Log_info("created %d, busy %d, idle %d coroutines on server %d, recent %s:%llx",
               (int)n_created_coroutines_,
               (int)n_busy_coroutines_,
               (int)n_idle_coroutines_,
               server_id_,
               file,
               (long long)line);
    }
  }
  n_busy_coroutines_++;
  coros_.insert(sp_coro);
  ContinueCoro(sp_coro);
  Loop(false, true);  // Process events AND check timeouts (match original mako behavior)
  return sp_coro;
}

// @safe - Checks timeout events and moves ready ones to ready list
void Reactor::CheckTimeout(std::vector<std::shared_ptr<Event>>& ready_events ) {
  auto time_now = Time::now(true);
  for (auto it = timeout_events_.begin(); it != timeout_events_.end();) {
    Event& event = **it;
    auto status = event.status_;
    switch (status) {
      case Event::INIT:
        verify(0);
      case Event::WAIT: {
        const auto &wakeup_time = event.wakeup_time_;
        verify(wakeup_time > 0);
        if (time_now > wakeup_time) {
          if (event.IsReady()) {
            // This is because our event mechanism is not perfect, some events
            // don't get triggered with arbitrary condition change.
            event.status_ = Event::READY;
          } else {
            event.status_ = Event::TIMEOUT;
          }
          // Event will be removed from waiting_events_ when reactor loop scans it
          ready_events.push_back(*it);
          it = timeout_events_.erase(it);
        } else {
          it++;
        }
        break;
      }
      case Event::READY:
      case Event::DONE:
        it = timeout_events_.erase(it);
        break;
      default:
        verify(0);
    }
  }

}

//  be careful this could be called from different coroutines.
// @unsafe - Main event loop with complex event processing
// SAFETY: Thread-safe via thread_id verification
// Merged implementation supporting both Paxos (mako) and Raft (jetpack) paths
void Reactor::Loop(bool infinite, bool check_timeout) {
  verify(std::this_thread::get_id() == thread_id_);
  looping_ = infinite;

  do {
    // Process disk events (jetpack path)
    disk_job_.lock();
    bool has_disk_events = !ready_disk_events_.empty();
    if (has_disk_events) {
      auto disk_event = ready_disk_events_.front();
      ready_disk_events_.pop_front();
      disk_job_.unlock();

      auto sp_coro = disk_event->wp_coro_.lock();
      if (sp_coro) {
        disk_event->status_ = Event::READY;
        if (disk_event->status_ == Event::READY) {
          disk_event->status_ = Event::DONE;
        }
        ContinueCoro(sp_coro);
      }
    } else {
      disk_job_.unlock();
    }

    // Keep processing events until no new ready events are found
    bool found_ready_events = true;
    while (found_ready_events) {
      found_ready_events = false;
      std::vector<std::shared_ptr<Event>> ready_events;

      // Get thread-safe ready events (jetpack/raft path)
      {
        std::lock_guard<std::mutex> lock(ready_events_mutex_);
        if (!ready_events_.empty()) {
          ready_events = std::move(ready_events_);
          ready_events_.clear();
          found_ready_events = true;
        }
      }

      // Scan waiting_events_ and move ready ones (paxos/mako path)
      auto& events = waiting_events_;
      for (auto it = events.begin(); it != events.end();) {
        Event& event = **it;
        event.Test();
        if (event.status_ == Event::READY) {
          ready_events.push_back(std::move(*it));
          it = events.erase(it);
          found_ready_events = true;
        } else if (event.status_ == Event::DONE) {
          it = events.erase(it);
        } else {
          ++it;
        }
      }

      // Check timeouts if requested
      if (check_timeout) {
        size_t before = ready_events.size();
        CheckTimeout(ready_events);
        if (ready_events.size() > before) {
          found_ready_events = true;
        }
      }

      // Process all ready events
      for (auto& sp_event : ready_events) {
        // Event might already be DONE (processed by another thread in multi-threaded Raft)
        if (sp_event->status_ == Event::DONE) {
          continue;
        }
        auto sp_coro = sp_event->wp_coro_.lock();
        if (!sp_coro) {
          continue;
        }
        // Check if coroutine still exists (might have finished already)
        if (coros_.find(sp_coro) == coros_.end()) {
          continue;
        }
        verify(sp_coro->status_ == Coroutine::PAUSED);
        if (sp_event->status_ == Event::READY) {
          sp_event->status_ = Event::DONE;
        } else {
          verify(sp_event->status_ == Event::TIMEOUT);
        }
        ContinueCoro(sp_coro);
      }

      // If not in infinite mode and no events found, stop inner loop
      if (!infinite && !found_ready_events) {
        break;
      }
    }

  } while (looping_);
}

// @unsafe - Continues execution of paused coroutine
// SAFETY: Manages coroutine state transitions properly
void Reactor::ContinueCoro(std::shared_ptr<Coroutine> sp_coro) {
//  verify(!sp_running_coro_th_); // disallow nested coros
  verify(sp_running_coro_th_ != sp_coro);
  auto sp_old_coro = sp_running_coro_th_;
  sp_running_coro_th_ = sp_coro;
  verify(sp_running_coro_th_->status_ != Coroutine::FINISHED);
  n_active_coroutines_++;

  struct timespec begin_marshal, begin_marshal_cpu, end_marshal, end_marshal_cpu;
  /*clock_gettime(CLOCK_MONOTONIC_RAW, &begin_marshal);
  clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &begin_marshal_cpu);*/
  //Log_info("start of %d", sp_coro->id);

  struct timespec begin, end;
  clock_gettime(CLOCK_MONOTONIC, &begin);

  if (sp_coro->status_ == Coroutine::INIT) {
    sp_coro->Run();
  } else {
    // PAUSED or RECYCLED
    sp_coro->Continue();
  }

  verify(sp_running_coro_th_ == sp_coro);
  if (sp_running_coro_th_->Finished()) {
    Recycle(sp_coro);
  }
  sp_running_coro_th_ = sp_old_coro;
}

void Reactor::Recycle(std::shared_ptr<Coroutine>& sp_coro) {

  // This fixes the bug that coroutines are not recycling if they don't finish immediately.
  if (REUSING_CORO) {
    sp_coro->status_ = Coroutine::RECYCLED;
    sp_coro->func_ = {};
    n_idle_coroutines_++;
    available_coros_.push_back(sp_coro);
  }
  n_busy_coroutines_--;
  coros_.erase(sp_coro);
}

void Reactor::DiskLoop(){

  Reactor::GetReactor()->disk_job_.lock();
  auto disk_events = Reactor::GetReactor()->disk_events_;
  auto it = Reactor::GetReactor()->disk_events_.begin();
  std::vector<std::shared_ptr<DiskEvent>> pending_disk_events_{};
  while(it != Reactor::GetReactor()->disk_events_.end()){
    auto disk_event = std::static_pointer_cast<DiskEvent>(*it);
    it = Reactor::GetReactor()->disk_events_.erase(it);
    pending_disk_events_.push_back(disk_event);
  }
  Reactor::GetReactor()->disk_job_.unlock();

  int total_written = 0;
  std::unordered_set<std::string> sync_set{};
  for (int i = 0; i < pending_disk_events_.size(); i++) {
    total_written += pending_disk_events_[i]->Handle();
    if (pending_disk_events_[i]->sync) {
      auto it = sync_set.find(pending_disk_events_[i]->file);
      if (it == sync_set.end()) {
        sync_set.insert(pending_disk_events_[i]->file);
      }
    }
  }

  for (auto it = sync_set.begin(); it != sync_set.end(); it++) {
    int fd = ::open(it->c_str(), O_WRONLY | O_APPEND | O_CREAT, 0777);
    ::fsync(fd);
    ::close(fd);
  }

  for(int i = 0; i < pending_disk_events_.size(); i++){
    Reactor::GetReactor()->disk_job_.lock();
    Reactor::GetReactor()->ready_disk_events_.push_back(pending_disk_events_[i]);
    Reactor::GetReactor()->disk_job_.unlock();
  }
}

// TODO PollThreadWorker -> Reactor

// Private constructor - doesn't start thread
PollThreadWorker::PollThreadWorker()
    : l_(std::make_unique<SpinLock>()),
      pending_remove_l_(std::make_unique<SpinLock>()),
      lock_job_(std::make_unique<SpinLock>()),
      stop_flag_(std::make_unique<std::atomic<bool>>(false)) {
  // Don't start thread here - factory will do it
}

// Factory method creates Arc<PollThreadWorker> and starts thread
rusty::Arc<PollThreadWorker> PollThreadWorker::create() {
  // Create Arc directly (methods are const, so no need for Mutex)
  auto arc = rusty::Arc<PollThreadWorker>::new_(PollThreadWorker());

  // Clone Arc for thread
  auto thread_arc = arc.clone();

  // Spawn thread with explicit parameter passing (enforces Send trait checking)
  // This properly validates that Arc<PollThreadWorker> is Send
  auto handle = rusty::thread::spawn(
    [](rusty::Arc<PollThreadWorker> arc) {
      arc->poll_loop();
    },
    thread_arc
  );

  // Store handle (using const method)
  arc->join_handle_ = rusty::Some(std::move(handle));

  return arc;
}

// Explicit shutdown method
void PollThreadWorker::shutdown() const {
  // Remove pollables before stopping
  for (auto& pair : fd_to_pollable_) {
    this->remove(*pair.second);
  }

  // Signal thread to stop
  stop_flag_->store(true);

  // Join thread
  if (join_handle_.is_some()) {
    join_handle_.take().unwrap().join();
  }
}

// Destructor just warns if not shut down
PollThreadWorker::~PollThreadWorker() {
  // Check if stop_flag_ is not null (it may be null if object was moved)
  if (stop_flag_ && !stop_flag_->load()) {
    Log_error("PollThreadWorker destroyed without shutdown() - thread may leak!");
  }
}

// @unsafe - Triggers ready jobs in coroutines
// SAFETY: Uses spinlock for thread safety
void PollThreadWorker::TriggerJob() const {
  lock_job_->lock();
  auto jobs_exec = set_sp_jobs_;
  set_sp_jobs_.clear();
  lock_job_->unlock();
  auto it = jobs_exec.begin();
  while (it != jobs_exec.end()) {
    auto sp_job = *it;
    if (sp_job->Ready()) {
      Coroutine::CreateRun([sp_job]() {sp_job->Work();});
      it = jobs_exec.erase(it);
    }
    else {
      it++;
    }
  }
}

// @unsafe - Main polling loop with complex synchronization
// SAFETY: Uses spinlocks and proper synchronization primitives
void PollThreadWorker::poll_loop() const {
  while (!stop_flag_->load()) {
    TriggerJob();
    Reactor::GetReactor()->Loop(false, true);
    // Wait() now directly casts userdata to Pollable* and calls handlers
    // Safe because deferred removal guarantees object stays in fd_to_pollable_ map
    poll_.Wait();
    TriggerJob();

    // Process deferred removals AFTER all events handled
    pending_remove_l_->lock();
    std::unordered_set<int> remove_fds = std::move(pending_remove_);
    pending_remove_.clear();
    pending_remove_l_->unlock();

    for (int fd : remove_fds) {
      l_->lock();

      auto it = fd_to_pollable_.find(fd);
      if (it == fd_to_pollable_.end()) {
        l_->unlock();
        continue;
      }

      auto sp_poll = it->second;

      // Check if fd was NOT reused (still in mode_ map)
      if (mode_.find(fd) != mode_.end()) {
        poll_.Remove(sp_poll);
      }

      // Remove from map - object may be destroyed here
      fd_to_pollable_.erase(it);
      mode_.erase(fd);

      l_->unlock();
    }
    TriggerJob();
    Reactor::GetReactor()->Loop(false, true);
  }

  // Process any final pending removals after stop_flag_ is set
  // This ensures destructor cleanup is processed even if the thread
  // exits the loop before processing the last batch
  pending_remove_l_->lock();
  std::unordered_set<int> remove_fds = std::move(pending_remove_);
  pending_remove_.clear();
  pending_remove_l_->unlock();

  for (int fd : remove_fds) {
    l_->lock();

    auto it = fd_to_pollable_.find(fd);
    if (it == fd_to_pollable_.end()) {
      l_->unlock();
      continue;
    }

    auto sp_poll = it->second;

    // Check if fd was NOT reused (still in mode_ map)
    if (mode_.find(fd) != mode_.end()) {
      poll_.Remove(sp_poll);
    }

    // Remove from map - object may be destroyed here
    fd_to_pollable_.erase(it);
    mode_.erase(fd);

    l_->unlock();
  }
}

// @safe - Thread-safe job addition with spinlock
void PollThreadWorker::add(std::shared_ptr<Job> sp_job) const {
  lock_job_->lock();
  set_sp_jobs_.insert(sp_job);
  lock_job_->unlock();
}

// @safe - Thread-safe job removal with spinlock
void PollThreadWorker::remove(std::shared_ptr<Job> sp_job) const {
  lock_job_->lock();
  set_sp_jobs_.erase(sp_job);
  lock_job_->unlock();
}

// @safe - Adds pollable with shared_ptr ownership
// SAFETY: Stores shared_ptr in map, passes raw pointer to epoll
void PollThreadWorker::add(std::shared_ptr<Pollable> sp_poll) const {
  int fd = sp_poll->fd();
  int poll_mode = sp_poll->poll_mode();

  l_->lock();

  // Check if already exists
  if (fd_to_pollable_.find(fd) != fd_to_pollable_.end()) {
    l_->unlock();
    return;
  }

  // Store in map
  fd_to_pollable_[fd] = sp_poll;
  mode_[fd] = poll_mode;

  // userdata = raw Pollable* for lookup
  void* userdata = sp_poll.get();

  poll_.Add(sp_poll, userdata);

  l_->unlock();
}

// @unsafe - Removes pollable with deferred cleanup
// SAFETY: Deferred removal ensures safe cleanup
void PollThreadWorker::remove(Pollable& poll) const {
  int fd = poll.fd();

  l_->lock();
  if (fd_to_pollable_.find(fd) == fd_to_pollable_.end()) {
    l_->unlock();
    return;  // Not found
  }
  l_->unlock();

  // Add to pending_remove (actual removal happens after epoll_wait)
  pending_remove_l_->lock();
  pending_remove_.insert(fd);
  pending_remove_l_->unlock();
}

// @unsafe - Updates poll mode
// SAFETY: Protected by spinlock, validates poll existence
void PollThreadWorker::update_mode(Pollable& poll, int new_mode) const {
  int fd = poll.fd();
  l_->lock();

  // Verify the pollable is registered
  if (fd_to_pollable_.find(fd) == fd_to_pollable_.end()) {
    l_->unlock();
    return;
  }

  auto mode_it = mode_.find(fd);
  verify(mode_it != mode_.end());
  int old_mode = mode_it->second;
  mode_it->second = new_mode;

  if (new_mode != old_mode) {
    void* userdata = &poll;  // Use address of reference
    poll_.Update(poll, userdata, new_mode, old_mode);
  }

  l_->unlock();
}

void Reactor::ReadyEventsThreadSafePushBack(std::shared_ptr<Event> ev) {
  // Log_info("!!!!!!!!! acquire ready_events_mutex_");
  std::lock_guard<std::mutex> lock(ready_events_mutex_);
  ready_events_.push_back(ev);
  // Log_info("!!!!!!!!! release ready_events_mutex_");
}

} // namespace rrr
