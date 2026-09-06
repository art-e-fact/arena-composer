//! Scenes arriving on a stream, one length-prefixed payload at a time.
//!
//! A pipe carries no message boundaries: two payloads written back to back are
//! one run of bytes to the reader. So each scene is prefixed with its length in
//! four big-endian bytes, and the reader takes exactly that many. The writing
//! end is `arena_composer.viewer_process.pack_scene`.

use std::io::{ErrorKind, Read};
use std::sync::mpsc::Sender;

use bevy::log::error;

use crate::scene::Scene;

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

    /// The end of the stream reaches the viewer as a closed channel.
    #[test]
    fn the_channel_ends_with_the_stream() {
        let (sender, scenes) = std::sync::mpsc::channel();
        forward_scenes(Cursor::new(packed("only")), sender);
        assert_eq!(scenes.recv().unwrap().env_name, "only");
        assert!(scenes.recv().is_err());
    }
}
