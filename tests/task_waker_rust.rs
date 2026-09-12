use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, Waker};

struct RetainWake {
    slot: Arc<Mutex<Option<Waker>>>,
}

impl Future for RetainWake {
    type Output = ();

    fn poll(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<()> {
        *self.slot.lock().unwrap() = Some(context.waker().clone());
        Poll::Pending
    }
}

#[test]
#[allow(unsafe_code)]
fn retained_native_waker_owns_its_callback_after_task_and_context_drop() {
    let calls = Arc::new(AtomicUsize::new(0));
    let callback_lifetime = Arc::downgrade(&calls);
    let slot = Arc::new(Mutex::new(None));
    {
        let mut waker = rusty::Waker {
            wake_fn: Arc::new(move || {
                calls.fetch_add(1, Ordering::Relaxed);
            }),
        };
        let mut context = rusty::Context {
            waker: &raw mut waker,
        };
        let mut task = rusty::Task::from_future(RetainWake { slot: slot.clone() });
        // SAFETY: the local Waker remains live and unchanged during this poll.
        assert!(unsafe { task.poll(&mut context) }.is_pending());
    }
    // This assertion detects a raw borrowed-waker bridge without dereferencing
    // its freed allocation. The retained native waker must own the capture.
    assert!(callback_lifetime.upgrade().is_some());
    let retained = slot.lock().unwrap().take().unwrap();
    let observed = callback_lifetime.clone();
    std::thread::spawn(move || {
        for _ in 0..1000 {
            retained.wake_by_ref();
        }
        assert_eq!(observed.upgrade().unwrap().load(Ordering::Relaxed), 1000);
    })
    .join()
    .unwrap();
    assert!(callback_lifetime.upgrade().is_none());
}
