// The canonical owner reactor resumes a task that wakes while its first poll suspends.

use std::future::Future;
use std::pin::Pin;
use std::sync::mpsc;
use std::task::{Context, Poll};

use srpc::reactor::{reactor_spawn_stackless_task_with_result, Reactor};

/// Ready on the second poll; wakes itself during the first, exercising the
/// pending-wake re-arm rather than a leisurely wake-after-park.
struct PendingOnce {
    polls: u32,
}

impl Future for PendingOnce {
    type Output = i64;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<i64> {
        self.polls += 1;
        if self.polls == 1 {
            cx.waker().wake_by_ref();
            Poll::Pending
        } else {
            Poll::Ready(7)
        }
    }
}

/// The composed workload both tests spawn: suspend once, then run the
/// canonical `async fn` chain (7 -> 14).
fn suspending_workload() -> rusty::Task<i64> {
    rusty::Task::from_future(async {
        let seven = PendingOnce { polls: 0 }.await;
        srpc::misc::async_double(seven).await
    })
}

#[test]
fn layer1_manual_run_loop_completes_a_suspended_task() {
    let reactor = Reactor::get_reactor();

    let (tx, rx) = mpsc::channel::<i64>();
    reactor_spawn_stackless_task_with_result(&reactor, suspending_workload(), move |value| {
        tx.send(value).expect("receiver alive");
    });

    // Parked, not completed: the spawn's early poll saw Pending.
    assert!(
        rx.try_recv().is_err(),
        "a suspended task must not complete inside the spawn call"
    );

    // One pump is the whole protocol: drain the wake ingress, re-poll.
    reactor.run_loop(false, true);
    assert_eq!(
        rx.try_recv().expect("run_loop must re-poll the woken task"),
        14,
        "PendingOnce yields 7, async_double doubles it"
    );
}
