//! Messages crossing the pipe, one length-prefixed payload at a time.
//!
//! A pipe carries no message boundaries: two payloads written back to back are
//! one run of bytes to the reader. So each message is prefixed with its length
//! in four big-endian bytes, and the reader takes exactly that many.
//!
//! Both directions use that framing. Scenes arrive on stdin from
//! `arena_composer.viewer_process.pack_scene`; requests leave on stdout for
//! `arena_composer.view` to serve. stdout is free for this because Bevy logs
//! to stderr — keep it that way, since one stray `println!` in the viewer
//! would land mid-message and desynchronise the reader.
//!
//! A layout is most of a message's bytes, and most of those are the geometry
//! the layout before it drew too. So `arena_composer.view._send_layout` names
//! that by its id alone, and the reader fills it back in: [`SceneReader`].

use std::collections::HashMap;
use std::io::{ErrorKind, Read, Write};
use std::sync::Arc;
use std::sync::mpsc::Sender;

use anyhow::Context;
use bevy::log::error;

use crate::scene::{Node, Scene};
use crate::spec::Request;

/// Reads scenes off a stream, giving back the geometry a layout names by id
/// alone from the layout before it.
///
/// It keeps the geometry of the last layout it read and no other, which is
/// what `_send_layout` takes it to hold: a message carrying no layout, as an
/// edit's first answer does, leaves that alone. Past the reader, every
/// geometry carries its nodes.
pub struct SceneReader<R> {
    stream: R,
    held: HashMap<String, Arc<Vec<Node>>>,
}

impl<R: Read> SceneReader<R> {
    pub fn new(stream: R) -> Self {
        Self {
            stream,
            held: HashMap::new(),
        }
    }

    /// Read one scene, or `None` once the stream has ended.
    pub fn read(&mut self) -> anyhow::Result<Option<Scene>> {
        let mut length = [0u8; 4];
        match self.stream.read_exact(&mut length) {
            Ok(()) => {}
            // A closed pipe reads as EOF, as does a scene cut short; both end it.
            Err(error) if error.kind() == ErrorKind::UnexpectedEof => return Ok(None),
            Err(error) => return Err(error.into()),
        }
        let mut payload = vec![0; u32::from_be_bytes(length) as usize];
        self.stream.read_exact(&mut payload)?;
        let mut scene = Scene::from_msgpack(&payload)?;
        if scene.assets.is_some() {
            self.fill(&mut scene)?;
        }
        Ok(Some(scene))
    }

    /// Give each of a layout's geometries its nodes, and keep them for the
    /// next layout to name.
    fn fill(&mut self, scene: &mut Scene) -> anyhow::Result<()> {
        let mut held = HashMap::new();
        for geometry in &mut scene.geometries {
            let nodes = match geometry.nodes.take() {
                Some(nodes) => nodes,
                None => self.held.get(&geometry.id).cloned().with_context(|| {
                    format!("geometry {} is named by its id, and the last layout did not draw it", geometry.id)
                })?,
            };
            held.insert(geometry.id.clone(), nodes.clone());
            geometry.nodes = Some(nodes);
        }
        self.held = held;
        Ok(())
    }

    /// Send on every scene in the stream until it ends.
    ///
    /// Dropping `scenes` on the way out is the signal that no more are coming —
    /// the viewer closes its window on it.
    pub fn forward(mut self, scenes: Sender<Scene>) {
        loop {
            match self.read() {
                Ok(Some(scene)) => {
                    // An error here is the window being gone: nobody to draw for.
                    if scenes.send(scene).is_err() {
                        return;
                    }
                }
                Ok(None) => return,
                // A malformed scene leaves no way to find where the next
                // starts, and a geometry named by an id the reader does not
                // hold means the two ends no longer agree on what it holds.
                Err(error) => return error!("scene stream: {error:#}"),
            }
        }
    }
}

/// Send one request, framed the way a scene is.
///
/// Flushed rather than left to the buffer: a pipe is block-buffered and a
/// request carries no newline, so an unflushed one waits for the next — which
/// is the user's next keystroke, or never.
pub fn write_request(sink: &mut impl Write, request: &Request) -> anyhow::Result<()> {
    let payload = rmp_serde::to_vec_named(request)?;
    sink.write_all(&(payload.len() as u32).to_be_bytes())?;
    sink.write_all(&payload)?;
    sink.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::io::Cursor;

    /// The smallest scene there is, framed for the pipe.
    fn packed(env_name: &str) -> Vec<u8> {
        framed(&BTreeMap::from([("env_name", env_name)]))
    }

    fn framed(scene: &impl serde::Serialize) -> Vec<u8> {
        let payload = rmp_serde::to_vec_named(scene).unwrap();
        [&(payload.len() as u32).to_be_bytes()[..], &payload].concat()
    }

    /// Two scenes written back to back come back as two.
    #[test]
    fn scenes_are_read_one_at_a_time() {
        let mut reader = SceneReader::new(Cursor::new([packed("first"), packed("second")].concat()));
        assert_eq!(reader.read().unwrap().unwrap().env_name, "first");
        assert_eq!(reader.read().unwrap().unwrap().env_name, "second");
        assert!(reader.read().unwrap().is_none());
    }

    /// A layout names what the layout before it drew by its id alone, and
    /// gets it back whole — the same nodes, not a copy. What the reader keeps
    /// is the last layout's and no other, and a message with no layout, an
    /// edit's answer, keeps it as it was.
    #[test]
    fn a_layout_gets_back_what_the_one_before_drew() {
        use serde_json::json;

        let whole = |id: &str| json!({"id": id, "nodes": [{"path": format!("/{id}")}]});
        let layout = |geometries: Vec<serde_json::Value>| framed(&json!({"assets": [], "geometries": geometries}));
        let stream = [
            layout(vec![whole("table"), whole("robot")]),
            framed(&json!({"spec": {"env_name": "edited"}})),
            layout(vec![json!({"id": "table"}), whole("bowl")]),
            layout(vec![json!({"id": "robot"})]),
        ];
        let mut reader = SceneReader::new(Cursor::new(stream.concat()));

        let opening = reader.read().unwrap().unwrap();
        assert!(reader.read().unwrap().unwrap().geometries.is_empty());
        let edited = reader.read().unwrap().unwrap();
        let paths: Vec<&str> = edited.geometries.iter().map(|geometry| geometry.nodes()[0].path.as_str()).collect();
        assert_eq!(paths, ["/table", "/bowl"]);
        let shared = |scene: &Scene| scene.geometries[0].nodes.clone().unwrap();
        assert!(Arc::ptr_eq(&shared(&opening), &shared(&edited)), "the table was decoded again");

        let error = reader.read().expect_err("the robot was let go of a layout back");
        assert!(format!("{error:#}").contains("geometry robot"), "{error:#}");
    }

    /// A request goes out framed the same way a scene comes in, so the reader
    /// on the other end can be the mirror of [`SceneReader::read`].
    #[test]
    fn a_request_is_framed_by_its_length() {
        use crate::spec::{Step, Value};

        let mut sink = Vec::new();
        write_request(
            &mut sink,
            &Request::Set {
                path: vec![Step::from("relations"), Step::from(2usize), Step::from("params")],
                value: Value::Number(0.08),
            },
        )
        .unwrap();

        let length = u32::from_be_bytes(sink[..4].try_into().unwrap()) as usize;
        assert_eq!(length, sink.len() - 4, "the prefix has to cover exactly the payload");

        // The shape `arena_composer.spec_edit.apply` is called with. A path
        // step is a bare key or index, so it addresses the spec the way it
        // reads rather than needing a parser on the other side.
        let decoded: serde_json::Value = rmp_serde::from_slice(&sink[4..]).unwrap();
        assert_eq!(
            decoded,
            serde_json::json!({"op": "set", "path": ["relations", 2, "params"], "value": 0.08}),
        );
    }

    /// An asset request carries no path, and is told apart by `op` alone —
    /// which is what `arena_composer.view.ASSET_REQUEST` matches on before it
    /// tries to read one as an edit.
    #[test]
    fn an_asset_request_is_named_by_its_op() {
        let mut sink = Vec::new();
        write_request(&mut sink, &Request::Reindex).unwrap();
        let decoded: serde_json::Value = rmp_serde::from_slice(&sink[4..]).unwrap();
        assert_eq!(decoded, serde_json::json!({"op": "reindex"}));
    }

    /// The end of the stream reaches the viewer as a closed channel.
    #[test]
    fn the_channel_ends_with_the_stream() {
        let (sender, scenes) = std::sync::mpsc::channel();
        SceneReader::new(Cursor::new(packed("only"))).forward(sender);
        assert_eq!(scenes.recv().unwrap().env_name, "only");
        assert!(scenes.recv().is_err());
    }
}
