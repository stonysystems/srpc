#![allow(unsafe_code)]

use rrr::channel::ChannelError;
use rrr::tcp_channel::{
    TcpConnection, TcpListener, kTcpConnectionOutboundHighWaterDefault,
};
use std::os::fd::IntoRawFd;
use std::os::unix::net::UnixStream;

// Cargo's Rust lane does not compile the production plain-C syscall seam.
// These inert definitions satisfy references in unexercised TCP I/O helpers;
// the CMake/Clang runtime lane links and exercises rpc/srpc_connect.c itself.
#[no_mangle]
pub extern "C" fn srpc_tcp_recv_scratch() -> *mut u8 {
    core::ptr::null_mut()
}

#[no_mangle]
pub extern "C" fn srpc_tcp_recv_bytes(_fd: i32, _data: *mut u8, _size: usize) -> i64 {
    -1
}

#[no_mangle]
pub extern "C" fn srpc_tcp_send_bytes(_fd: i32, _data: *const u8, _size: usize) -> i64 {
    -1
}

#[no_mangle]
pub extern "C" fn srpc_tcp_shutdown(_fd: i32) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn srpc_tcp_set_nonblocking(_fd: i32) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn srpc_tcp_last_errno() -> i32 {
    0
}

#[test]
fn fresh_listener_preserves_the_invalid_fd_and_basic_pollable_contract() {
    let listener = TcpListener::new();

    assert_eq!(listener.fd(), -1);
    assert_eq!(listener.local_address(), "");
    assert!(!listener.is_closed());
    assert_eq!(listener.poll_mode(), 1);
    assert_eq!(listener.content_size(), 0);
    assert_eq!(listener.handle_write(), -1);
    assert!(!listener.check_pending_write_update());
    assert!(!listener.handle_read());
}

#[test]
fn listener_bind_close_and_single_use_state_match_the_cpp_contract() {
    let listener = TcpListener::new();

    assert_eq!(listener.listen("not-an-address"), ChannelError::AddressInvalid);
    assert_eq!(listener.fd(), -1);
    assert_eq!(listener.listen("127.0.0.1:0"), ChannelError::None);
    assert!(listener.fd() >= 0);
    assert!(listener.local_address().starts_with("127.0.0.1:"));
    assert_eq!(
        listener.listen("127.0.0.1:0"),
        ChannelError::AddressInUse
    );

    listener.close();
    listener.close();
    assert!(listener.is_closed());
    assert_eq!(listener.fd(), -1);
    assert_eq!(
        listener.listen("127.0.0.1:0"),
        ChannelError::AddressInUse
    );
}

#[test]
fn connection_constructor_owns_the_fd_and_preserves_initial_state() {
    let (owned, _peer) = UnixStream::pair().unwrap();
    let raw_fd = owned.into_raw_fd();
    let connection = TcpConnection::new(raw_fd, "test-peer".to_string());

    assert_eq!(connection.fd(), raw_fd);
    assert_eq!(connection.peer_address(), "test-peer");
    assert!(!connection.is_closed());
    assert_eq!(connection.poll_mode(), 1);
    assert_eq!(connection.content_size(), 0);
    assert!(!connection.check_pending_write_update());
    assert_eq!(kTcpConnectionOutboundHighWaterDefault, 4 * 1024 * 1024);
}
