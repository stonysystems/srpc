# srpc's async runtime

srpc has an async runtime. It is not Tokio, not async-std, and not any crate:
**the srpc crate has exactly one dependency, the local `rusty` facade.** The
executor is hand-written and lives in two halves that mirror each other across
the language boundary.

This document exists because the component had no name. Its pieces are dissolved
into two large files (`reactor/reactor.rs` is 3,745 lines; `rusty-rustc/src/lib.rs`
is ~3,100), and nothing announced "this is the async runtime" — so the design was
effectively undiscoverable without reading both end to end.

## Why it is hand-written

Every canonical module (`base/ misc/ rpc/ reactor/`) is transpiled to a C++23
module by rusty-cpp. Tokio — plus mio, its work-stealing scheduler and waker
machinery — is categorically outside what that transpiler translates, so no
runtime crate can live in canonical code.

That constraint shaped the whole design: **every piece was chosen to have a C++20
counterpart.** `async fn` lowers to a C++ coroutine returning `rusty::Task<T>`,
`.await` lowers to `co_await`, and the reactor that polls tasks in Rust is the
same reactor that drives them in C++. The absence of Tokio is not a gap waiting
to be filled; it is the constraint expressing itself.

## The two halves

### Canonical half — `reactor/reactor.rs`

The executor proper. Roughly 200 of the file's 3,745 lines:

| Piece | Where |
| --- | --- |
| Task table | `stackless_tasks_: RefCell<Vec<StacklessTaskEntry>>` — entries are `{ active, queued, poll_once }` |
| Ready queue | `ready_stackless_tasks_: RefCell<VecDeque<usize>>` |
| Poll pass | `run_loop(..)` calls `process_stackless_tasks()` every iteration |
| Wake → re-poll | waking pushes the task's index onto the ready queue |
| Spawn | `reactor_spawn_stackless_task_with_result` registers a `StacklessTaskEntry` and returns its index |
| Accounting | `stackless_wake_*` routing, cancel counters, profile counters |

Public surface is deliberately small: `StacklessTaskEntry`,
`StacklessCancelReport`, `stackless_cancel_report`,
`reactor_spawn_stackless_task_with_result`.

### Facade half — `rusty-rustc/src/lib.rs`

The rustc-lane types, shaped to match the C++ coroutine types they stand in for:
`Task<T>`, `Waker`, `Context`, `Poll`, and `PollThread`.

The interesting part is `Task::from_future`. Under rustc an `async fn` is an
ordinary Rust `Future`, so the facade bridges the two waker worlds: it wraps
srpc's own `Waker` in a `FacadeWake` implementing `std::task::Wake`, builds a real
`std::task::Waker` from it, polls the native future with a real
`std::task::Context`, and maps `std::task::Poll::Ready/Pending` back onto srpc's
`Poll`. Waking flows the other way — `FacadeWake::wake_by_ref` calls srpc's
`Waker::wake`, which enqueues the task index for the next pass.

`PollThread` is a real epoll loop (edge-triggered, 1 ms tick, command queue, job
queue) and it is what *drives* the executor: it calls `Reactor::run_loop` through
`add_tick_hook` on every pass — the same thing pollworker's C++ loop does
natively.

## How one `async fn` actually runs

1. `async fn` compiles to a normal Rust `Future` state machine (rustc does this).
2. It is handed to `Task::from_future`, which boxes and pins it.
3. `reactor_spawn_stackless_task_with_result` registers the task and hands back an
   index.
4. `PollThread`'s tick hook calls `run_loop`, which drains the ready queue and
   polls each task through the waker bridge.
5. When the future's waker fires, the task's index is enqueued and it is polled
   again on the next pass.

## Honest limits

This is a **task executor, not a full async I/O runtime**:

- **Single-threaded per reactor.** Reactors are thread-local; `reactor_tls_get()`
  lazily creates one per thread. There is no work-stealing and no cross-thread
  task migration.
- **Cooperative.** A task that does not yield blocks its reactor's pass.
- **Timers come from the epoll loop's 1 ms tick**, not a timer wheel, so wake
  latency is bounded below by the tick.
- **Async I/O is not integrated the way Tokio integrates it.** srpc's I/O path is
  the epoll loop plus *stackful fibers* (1 MiB mmap'd stacks, context-switch
  assembly, `Future::wait()` and `FiberChannel::recv_frame()` block a fiber).
  Stackless tasks are a separate mechanism layered alongside, not the I/O
  foundation.

Those limits are design consequences, not defects: a multi-threaded scheduler
would have no C++20 counterpart to lower to.

## If you are evaluating Tokio

Tokio cannot live in canonical code (see above). The only place it could go is the
facade, which is not transpiled — it would back the **rustc lane only**, leaving
the C++ lane on the reactor. Weigh that carefully: the facade is the one component
whose divergence from shipped C++ the project has been actively *shrinking* (see
`scripts/check_facade_shadow.py`), and a full async runtime would be the largest
divergence yet added to it.

The prerequisite either way is a named executor with a defined interface — which
is what this document, and any follow-on extraction, is for.
