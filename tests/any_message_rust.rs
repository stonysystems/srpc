#![allow(unsafe_code)]

use rrr::any_message::any_message_registry;
use rrr::any_message::cpp::rrr::serializable::{
    BinaryReadArchive, BinaryWriteArchive, SerializablePayload,
};
use rrr::any_message::{deserialize, reg_any_message_as, serialize, AnyMessage};
use std::any::TypeId;
use std::sync::{Arc, Mutex};

static TEST_LOCK: Mutex<()> = Mutex::new(());
const GRAPH_NAME: &str = "rrr.test.GraphPayload";
const ALIAS: &str = "rrr.test.GraphPayload.v2";

#[derive(Default, Debug, Eq, PartialEq)]
struct GraphPayload {
    node_count: i32,
    label: String,
}

impl SerializablePayload for GraphPayload {
    fn save(&self, archive: &mut BinaryWriteArchive) {
        archive.write_bytes(&self.node_count.to_le_bytes());
        archive.write_bytes(&[self.label.len() as u8]);
        archive.write_bytes(self.label.as_bytes());
    }

    fn load(&mut self, archive: &mut BinaryReadArchive) {
        let mut node_count = [0_u8; 4];
        archive.read_exact(&mut node_count);
        self.node_count = i32::from_le_bytes(node_count);
        let mut length = [0_u8; 1];
        archive.read_exact(&mut length);
        let mut label = vec![0_u8; length[0] as usize];
        archive.read_exact(&mut label);
        self.label = String::from_utf8(label).unwrap();
    }

    fn kind(&self) -> i32 {
        self.node_count
    }
}

fn reset_and_register() {
    any_message_registry::clear_for_testing();
    assert_eq!(
        reg_any_message_as::<GraphPayload>(GRAPH_NAME.to_owned()),
        0_i32
    );
}

#[test]
fn registry_and_pack_preserve_name_type_and_shared_payload_identity() {
    let _guard = TEST_LOCK.lock().unwrap();
    any_message_registry::clear_for_testing();

    let empty = AnyMessage::default();
    assert!(empty.type_name_.is_empty());
    assert!(empty.payload_.is_none());
    assert!(!empty.is_a::<GraphPayload>());
    assert!(any_message_registry::name_for_type_owned(TypeId::of::<GraphPayload>()).is_empty());

    reset_and_register();
    assert!(any_message_registry::is_registered_name(
        &GRAPH_NAME.to_owned()
    ));
    assert!(any_message_registry::is_registered_type(TypeId::of::<
        GraphPayload,
    >()));

    let payload = Arc::new(GraphPayload {
        node_count: 42_i32,
        label: "shared".to_owned(),
    });
    let message = AnyMessage::pack(payload.clone());
    let recovered = message.unpack::<GraphPayload>().unwrap();
    assert_eq!(message.type_name_, GRAPH_NAME);
    assert!(message.is_a::<GraphPayload>());
    assert!(Arc::ptr_eq(&payload, &recovered));
}

#[test]
fn direct_and_free_archive_paths_preserve_independent_wire_bytes() {
    let _guard = TEST_LOCK.lock().unwrap();
    reset_and_register();

    let outgoing = AnyMessage::pack(Arc::new(GraphPayload {
        node_count: 0x1234_5678_i32,
        label: "wire-trip".to_owned(),
    }));
    let mut direct_writer = BinaryWriteArchive::new();
    outgoing.save(&mut direct_writer);
    let mut free_writer = BinaryWriteArchive::new();
    serialize(&outgoing, &mut free_writer);
    assert_eq!(direct_writer.as_bytes(), free_writer.as_bytes());
    assert_eq!(free_writer.as_bytes()[0], GRAPH_NAME.len() as u8);
    assert_eq!(
        &free_writer.as_bytes()[1..1 + GRAPH_NAME.len()],
        GRAPH_NAME.as_bytes()
    );

    let mut reader = BinaryReadArchive::new(free_writer.as_bytes());
    let mut incoming = AnyMessage::default();
    deserialize(&mut incoming, &mut reader);
    assert_eq!(reader.remaining(), 0_usize);
    assert_eq!(
        incoming.unpack::<GraphPayload>().unwrap().as_ref(),
        &GraphPayload {
            node_count: 0x1234_5678_i32,
            label: "wire-trip".to_owned(),
        }
    );
}

#[test]
fn alias_decodes_but_the_first_registered_name_remains_canonical() {
    let _guard = TEST_LOCK.lock().unwrap();
    reset_and_register();
    assert_eq!(reg_any_message_as::<GraphPayload>(ALIAS.to_owned()), 0_i32);
    assert_eq!(
        any_message_registry::name_for_type_owned(TypeId::of::<GraphPayload>()),
        GRAPH_NAME
    );

    let outgoing = AnyMessage::pack_as(
        ALIAS.to_owned(),
        Arc::new(GraphPayload {
            node_count: 5_i32,
            label: "alias".to_owned(),
        }),
    );
    let mut writer = BinaryWriteArchive::new();
    serialize(&outgoing, &mut writer);
    let mut reader = BinaryReadArchive::new(writer.as_bytes());
    let mut incoming = AnyMessage::default();
    deserialize(&mut incoming, &mut reader);

    assert_eq!(incoming.type_name_, ALIAS);
    assert!(!incoming.is_a::<GraphPayload>());
    assert!(incoming.unpack::<GraphPayload>().is_none());
}
