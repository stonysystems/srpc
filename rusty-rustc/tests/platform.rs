use std::sync::{Arc, atomic::{AtomicUsize, Ordering}};

#[test]
fn native_threads_run_and_have_distinct_stable_identities() {
    let owner = rusty::thread::current_id();
    assert_eq!(owner, rusty::thread::current_id());
    assert_ne!(owner, rusty::thread::ThreadId::default());
    let observed = Arc::new(AtomicUsize::new(0));
    let child_observed = observed.clone();
    rusty::thread::spawn(move || {
        assert_ne!(owner, rusty::thread::current_id());
        assert_eq!(rusty::thread::current_id(), rusty::thread::current_id());
        child_observed.store(37, Ordering::Release);
    }).join();
    assert_eq!(observed.load(Ordering::Acquire), 37);
}

#[test]
fn thread_panics_do_not_disappear() {
    if std::env::var_os("SRPC_ADAPTER_PANIC_CHILD").is_some() {
        rusty::thread::spawn(|| panic!("uncaught native thread panic")).join();
        panic!("thread panic was swallowed");
    }
    use std::os::unix::process::ExitStatusExt;
    let output = std::process::Command::new("sh")
        .args(["-c", "ulimit -c 0\nexec \"$1\" --exact thread_panics_do_not_disappear --nocapture", "sh"])
        .arg(std::env::current_exe().unwrap())
        .env("SRPC_ADAPTER_PANIC_CHILD", "1")
        .output().unwrap();
    assert_eq!(output.status.signal(), Some(6), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("uncaught native thread panic"));
}
