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

use std::io::{ErrorKind, Read, Write};
use std::sync::mpsc::Sender;

use bevy::log::error;

use crate::scene::Scene;
use crate::spec::Request;

/// Read one scene, or `None` once the stream has ended.
pub fn read_scene(stream: &mut impl Read) -> anyhow::Result<Option<Scene>> {
    let mut length = [0u8; 4];
    match stream.read_exact(&mut length) {
        Ok(()) => {}
        // A closed pipe reads as EOF, as does a scene cut short; both end it.
        Err(error) if error.kind() == ErrorKind::UnexpectedEof => return Ok(None),
        Err(error) => return Err(error.into()),
    }
    let mut payload = vec![0; u32::from_be_bytes(length) as usize];
    stream.read_exact(&mut payload)?;
    Ok(Some(Scene::from_msgpack(&payload)?))
}

/// Send on every scene in `stream` until it ends.
///
/// Dropping `scenes` on the way out is the signal that no more are coming —
/// the viewer closes its window on it.
pub fn forward_scenes(mut stream: impl Read, scenes: Sender<Scene>) {
    loop {
        match read_scene(&mut stream) {
            Ok(Some(scene)) => {
                // An error here is the window being gone: nobody to draw for.
                if scenes.send(scene).is_err() {
                    return;
                }
            }
            Ok(None) => return,
            // A malformed scene leaves no way to find where the next starts.
            Err(error) => return error!("scene stream: {error:#}"),
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
        let payload = rmp_serde::to_vec_named(&BTreeMap::from([("env_name", env_name)])).unwrap();
        [&(payload.len() as u32).to_be_bytes()[..], &payload].concat()
    }

    /// Two scenes written back to back come back as two.
    #[test]
    fn scenes_are_read_one_at_a_time() {
        let mut stream = Cursor::new([packed("first"), packed("second")].concat());
        assert_eq!(read_scene(&mut stream).unwrap().unwrap().env_name, "first");
        assert_eq!(read_scene(&mut stream).unwrap().unwrap().env_name, "second");
        assert!(read_scene(&mut stream).unwrap().is_none());
    }

    /// A request goes out framed the same way a scene comes in, so the reader
    /// on the other end can be the mirror of `read_scene`.
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

    /// The two asset requests carry no path, and are told apart by `op` alone —
    /// which is what `arena_composer.view.ASSET_REQUESTS` matches on before it
    /// tries to read one as an edit.
    #[test]
    fn an_asset_request_is_named_by_its_op() {
        let sent = |request: &Request| {
            let mut sink = Vec::new();
            write_request(&mut sink, request).unwrap();
            rmp_serde::from_slice::<serde_json::Value>(&sink[4..]).unwrap()
        };
        assert_eq!(sent(&Request::Reindex), serde_json::json!({"op": "reindex"}));
        assert_eq!(
            sent(&Request::Thumbnails {
                urls: vec!["https://bucket/basket.usd".into()],
            }),
            serde_json::json!({"op": "thumbnails", "urls": ["https://bucket/basket.usd"]}),
        );
    }

    /// The end of the stream reaches the viewer as a closed channel.
    #[test]
    fn the_channel_ends_with_the_stream() {
        let (sender, scenes) = std::sync::mpsc::channel();
        forward_scenes(Cursor::new(packed("only")), sender);
        assert_eq!(scenes.recv().unwrap().env_name, "only");
        assert!(scenes.recv().is_err());
    }
}
