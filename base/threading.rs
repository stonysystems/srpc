//! Canonical Rust owner for the `srpc.threading` pthread wrappers and spin lock.

#![allow(non_camel_case_types, non_snake_case)]

use crate::debugging::verify_at;
use std::sync::Mutex;

/// A synchronized value snapshot shared by transport and caller threads.
/// The lock never escapes an operation, so callers can invoke callbacks or
/// suspend after reading without retaining a borrow into shared state.
pub struct SharedCell<T> {
    value_: Mutex<T>,
}

impl<T> SharedCell<T> {
    pub fn new(value: T) -> Self {
        Self { value_: Mutex::new(value) }
    }

    pub fn set(&self, value: T) {
        *self.value_.lock().unwrap() = value;
    }

    pub fn replace(&self, value: T) -> T {
        std::mem::replace(&mut *self.value_.lock().unwrap(), value)
    }
}

impl<T: Clone> SharedCell<T> {
    pub fn get(&self) -> T {
        self.value_.lock().unwrap().clone()
    }
}

pub type AtomicBool = rusty::sync::atomic::AtomicBool;
pub type Ordering = rusty::sync::atomic::Ordering;

#[allow(unsafe_code)]
unsafe extern "C" {
    fn pthread_spin_init(lock: *mut rusty::PthreadSpinlock, pshared: i32) -> i32;
    fn pthread_spin_lock(lock: *mut rusty::PthreadSpinlock) -> i32;
    fn pthread_spin_unlock(lock: *mut rusty::PthreadSpinlock) -> i32;
    fn pthread_spin_destroy(lock: *mut rusty::PthreadSpinlock) -> i32;

    fn pthread_mutex_init(
        mutex: *mut rusty::PthreadMutex,
        attr: *const rusty::PthreadMutexAttr,
    ) -> i32;
    fn pthread_mutex_lock(mutex: *mut rusty::PthreadMutex) -> i32;
    fn pthread_mutex_unlock(mutex: *mut rusty::PthreadMutex) -> i32;
    fn pthread_mutex_destroy(mutex: *mut rusty::PthreadMutex) -> i32;

    fn pthread_cond_init(cond: *mut rusty::PthreadCond, attr: *const rusty::PthreadCondAttr)
        -> i32;
    fn pthread_cond_destroy(cond: *mut rusty::PthreadCond) -> i32;
    fn pthread_cond_signal(cond: *mut rusty::PthreadCond) -> i32;
    fn pthread_cond_broadcast(cond: *mut rusty::PthreadCond) -> i32;
    fn pthread_cond_wait(cond: *mut rusty::PthreadCond, mutex: *mut rusty::PthreadMutex) -> i32;

    fn srpc_cpu_pause();
}

/// Initialize caller-owned pthread spin-lock storage.
///
/// # Safety
///
/// `lock` must satisfy `pthread_spin_init`'s pointer and lifetime contract.
#[allow(unsafe_code)]
pub unsafe fn Pthread_spin_init(lock: *mut rusty::PthreadSpinlock, pshared: i32) {
    unsafe { verify_at(pthread_spin_init(lock, pshared) == 0, file!(), line!()) };
}

/// Lock a live initialized pthread spin lock.
///
/// # Safety
///
/// `lock` must point to a live initialized pthread spin lock.
#[allow(unsafe_code)]
pub unsafe fn Pthread_spin_lock(lock: *mut rusty::PthreadSpinlock) {
    unsafe { verify_at(pthread_spin_lock(lock) == 0, file!(), line!()) };
}

/// Unlock a pthread spin lock held by this thread.
///
/// # Safety
///
/// `lock` must point to a live spin lock held by the current thread.
#[allow(unsafe_code)]
pub unsafe fn Pthread_spin_unlock(lock: *mut rusty::PthreadSpinlock) {
    unsafe { verify_at(pthread_spin_unlock(lock) == 0, file!(), line!()) };
}

/// Destroy initialized, unlocked pthread spin-lock storage.
///
/// # Safety
///
/// `lock` must point to an initialized, unlocked pthread spin lock.
#[allow(unsafe_code)]
pub unsafe fn Pthread_spin_destroy(lock: *mut rusty::PthreadSpinlock) {
    unsafe { verify_at(pthread_spin_destroy(lock) == 0, file!(), line!()) };
}

/// Initialize caller-owned pthread mutex storage.
///
/// # Safety
///
/// `mutex` and non-null `attr` must satisfy `pthread_mutex_init`'s contract.
#[allow(unsafe_code)]
pub unsafe fn Pthread_mutex_init(
    mutex: *mut rusty::PthreadMutex,
    attr: *const rusty::PthreadMutexAttr,
) {
    unsafe { verify_at(pthread_mutex_init(mutex, attr) == 0, file!(), line!()) };
}

/// Lock a live initialized pthread mutex.
///
/// # Safety
///
/// `mutex` must point to a live initialized pthread mutex.
#[allow(unsafe_code)]
pub unsafe fn Pthread_mutex_lock(mutex: *mut rusty::PthreadMutex) {
    unsafe { verify_at(pthread_mutex_lock(mutex) == 0, file!(), line!()) };
}

/// Unlock a pthread mutex held by this thread.
///
/// # Safety
///
/// `mutex` must point to a live pthread mutex held by this thread.
#[allow(unsafe_code)]
pub unsafe fn Pthread_mutex_unlock(mutex: *mut rusty::PthreadMutex) {
    unsafe { verify_at(pthread_mutex_unlock(mutex) == 0, file!(), line!()) };
}

/// Destroy initialized, unlocked pthread mutex storage.
///
/// # Safety
///
/// `mutex` must point to an initialized, unlocked pthread mutex.
#[allow(unsafe_code)]
pub unsafe fn Pthread_mutex_destroy(mutex: *mut rusty::PthreadMutex) {
    unsafe { verify_at(pthread_mutex_destroy(mutex) == 0, file!(), line!()) };
}

/// Initialize caller-owned pthread condition-variable storage.
///
/// # Safety
///
/// `cond` and non-null `attr` must satisfy `pthread_cond_init`'s contract.
#[allow(unsafe_code)]
pub unsafe fn Pthread_cond_init(
    cond: *mut rusty::PthreadCond,
    attr: *const rusty::PthreadCondAttr,
) {
    unsafe { verify_at(pthread_cond_init(cond, attr) == 0, file!(), line!()) };
}

/// Destroy an initialized condition variable with no waiters.
///
/// # Safety
///
/// `cond` must point to an initialized condition variable with no waiters.
#[allow(unsafe_code)]
pub unsafe fn Pthread_cond_destroy(cond: *mut rusty::PthreadCond) {
    unsafe { verify_at(pthread_cond_destroy(cond) == 0, file!(), line!()) };
}

/// Signal a live initialized pthread condition variable.
///
/// # Safety
///
/// `cond` must point to a live initialized condition variable.
#[allow(unsafe_code)]
pub unsafe fn Pthread_cond_signal(cond: *mut rusty::PthreadCond) {
    unsafe { verify_at(pthread_cond_signal(cond) == 0, file!(), line!()) };
}

/// Broadcast to a live initialized pthread condition variable.
///
/// # Safety
///
/// `cond` must point to a live initialized condition variable.
#[allow(unsafe_code)]
pub unsafe fn Pthread_cond_broadcast(cond: *mut rusty::PthreadCond) {
    unsafe { verify_at(pthread_cond_broadcast(cond) == 0, file!(), line!()) };
}

/// Wait on a live condition-variable and mutex pair.
///
/// # Safety
///
/// Both pointers must be live, initialized, and satisfy `pthread_cond_wait`'s
/// locking and lifetime contract.
#[allow(unsafe_code)]
pub unsafe fn Pthread_cond_wait(cond: *mut rusty::PthreadCond, mutex: *mut rusty::PthreadMutex) {
    unsafe { verify_at(pthread_cond_wait(cond, mutex) == 0, file!(), line!()) };
}

pub fn cpu_pause() {
    #[allow(unsafe_code)]
    unsafe {
        srpc_cpu_pause();
    }
}

#[repr(C)]
pub struct SpinLock {
    pub locked_field: AtomicBool,
}

impl SpinLock {
    #[allow(clippy::new_without_default)]
    pub fn new() -> SpinLock {
        SpinLock {
            locked_field: AtomicBool::new(false),
        }
    }

    pub fn lock(&self) {
        if self
            .locked_field
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_ok()
        {
            return;
        }

        let mut wait = 1_000_i32;
        while wait > 0_i32 && self.locked_field.load(Ordering::Relaxed) {
            cpu_pause();
            wait -= 1_i32;
        }

        while self
            .locked_field
            .compare_exchange_weak(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            rusty::sys::time::sleep_us(50_u64);
        }
    }

    pub fn unlock(&self) {
        self.locked_field.store(false, Ordering::Release);
    }
}
