//! Writes the window into a video file, one captured frame per video frame,
//! for the README's demo video ([`crate::tour`]).
//!
//! A screen recorder keeps every stall as a freeze: the frames a layout takes
//! to upload, a software renderer drawing a few frames a second. Here the
//! video's clock is the frame count — the tour steps Bevy's clock one video
//! frame per update — so a stall costs one frame however long it took.
//!
//! Frames are piped to `ffmpeg`, which has to be on `PATH`. That keeps a video
//! encoder out of the dependency tree. It encodes H.264, so the file extension
//! has to name a container that holds it: mp4, mkv or mov.
//!
//! A recording that fails closes the window with an error: the run is not one
//! to keep.

use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};

use anyhow::{Context, bail, ensure};
use bevy::prelude::*;
use bevy::render::render_resource::TextureFormat;
use bevy::render::view::screenshot::{Screenshot, ScreenshotCaptured};

/// How many out-of-order frames may wait before the one they are waiting for
/// is given up on. See [`ready`].
const REORDER_LIMIT: usize = 8;

/// Records every update to `path` at `fps`, but those that find [`Hold`].
pub fn plugin(app: &mut App, path: PathBuf, fps: f64) {
    app.insert_resource(Recorder::new(path, fps))
        .add_systems(Update, capture.run_if(not(resource_exists::<Hold>)))
        .add_observer(encode);
}

/// Nothing is captured while this resource exists: the tour holds the
/// recording through its warmup, and through whatever it waits on that a
/// viewer need not sit through.
#[derive(Resource)]
pub struct Hold;

/// The frame's place in the video, kept on the screenshot entity so the
/// capture can be put back in order when it lands.
#[derive(Component)]
struct Frame(u64);

#[derive(Resource)]
struct Recorder {
    path: PathBuf,
    fps: f64,
    /// The index the next capture gets.
    spawned: u64,
    /// The index the encoder wants next.
    next: u64,
    /// Captures that landed before the frames in front of them.
    early: BTreeMap<u64, Image>,
    /// Started by the first frame, not by [`plugin`] — see [`Encoder::spawn`].
    encoder: Option<Encoder>,
    /// Set once something has gone wrong, which stops the capturing too: the
    /// run carries on without a recording rather than logging per frame.
    stopped: bool,
}

impl Recorder {
    fn new(path: PathBuf, fps: f64) -> Self {
        Self { path, fps, spawned: 0, next: 0, early: BTreeMap::new(), encoder: None, stopped: false }
    }

    /// Hands one frame to `ffmpeg`, or gives up on the recording.
    fn feed(&mut self, image: &mut Image) {
        // Captures still on their way when it stopped would start a second
        // ffmpeg over the file.
        if self.stopped {
            return;
        }
        if let Err(err) = self.try_feed(image) {
            error!("recording stopped: {err:#}");
            self.stopped = true;
            // Dropping the encoder finishes the file, so what was recorded up
            // to the failure stays playable.
            self.encoder = None;
        }
    }

    fn try_feed(&mut self, image: &mut Image) -> anyhow::Result<()> {
        let size = UVec2::new(image.width(), image.height());
        let data = image.data.take().context("a capture came back empty")?;
        if self.encoder.is_none() {
            self.encoder = Some(Encoder::spawn(&self.path, self.fps, size, image.texture_descriptor.format)?);
        }
        let encoder = self.encoder.as_mut().expect("started just above");
        ensure!(
            encoder.size == size,
            "the window resized to {size} mid-recording, and ffmpeg takes the {} it was started with",
            encoder.size,
        );
        encoder.write(&data)
    }
}

/// Screenshots the window, once an update.
fn capture(mut commands: Commands, mut recorder: ResMut<Recorder>) {
    if recorder.stopped {
        return;
    }
    commands.spawn((Screenshot::primary_window(), Frame(recorder.spawned)));
    recorder.spawned += 1;
}

/// Writes captured frames out in the order they were spawned.
fn encode(
    mut captured: On<ScreenshotCaptured>,
    frames: Query<&Frame>,
    recorder: ResMut<Recorder>,
    mut exit: MessageWriter<AppExit>,
) {
    // `into_inner` so the two fields below can be borrowed separately.
    let recorder = recorder.into_inner();
    // Behind `next`, it was given up on: written now, it would jump back.
    let Some(frame) = frames.get(captured.entity).ok().filter(|frame| frame.0 >= recorder.next) else {
        return;
    };
    // Moved out rather than cloned: a frame is several megabytes.
    let image = core::mem::take(&mut captured.image);
    recorder.early.insert(frame.0, image);
    for mut image in ready(&mut recorder.early, &mut recorder.next) {
        recorder.feed(&mut image);
    }
    if recorder.stopped {
        exit.write(AppExit::error());
    }
}

/// Takes the frames that can be written now off the reorder buffer.
///
/// Readbacks finish on the task pool, so two captures can land swapped; a
/// frame waits in `early` until the ones in front of it are in. Past
/// [`REORDER_LIMIT`] waiting frames the missing one is given up on — a capture
/// the renderer never completes would otherwise hold every later frame in
/// memory for the rest of the run.
fn ready<T>(early: &mut BTreeMap<u64, T>, next: &mut u64) -> Vec<T> {
    if early.len() > REORDER_LIMIT
        && let Some(oldest) = early.keys().next()
    {
        *next = *oldest;
    }
    let mut frames = Vec::new();
    while let Some(frame) = early.remove(next) {
        *next += 1;
        frames.push(frame);
    }
    frames
}

/// A running `ffmpeg`, taking raw frames on stdin.
struct Encoder {
    child: Child,
    /// The frame size its input was opened with.
    size: UVec2,
}

impl Encoder {
    /// Starts `ffmpeg` for frames of this size and format.
    ///
    /// Called on the first capture rather than at startup: raw frames carry no
    /// header, so `ffmpeg` has to be told the size and channel order up front,
    /// and a capture is what knows them.
    fn spawn(path: &Path, fps: f64, size: UVec2, format: TextureFormat) -> anyhow::Result<Self> {
        // The bytes are the swapchain's, already gamma-encoded for the
        // display, so the pixel format only has to name the channel order.
        let pixel_format = match format {
            TextureFormat::Bgra8UnormSrgb | TextureFormat::Bgra8Unorm => "bgra",
            TextureFormat::Rgba8UnormSrgb | TextureFormat::Rgba8Unorm => "rgba",
            other => bail!("frames come back as {other:?}, which has no raw ffmpeg equivalent"),
        };
        let mut ffmpeg = Command::new("ffmpeg");
        ffmpeg
            .args(["-y", "-loglevel", "warning"])
            // One input frame per captured frame, timed by the frame count
            // alone — which is what turns a stall into one frame.
            .args(["-f", "rawvideo", "-pixel_format", pixel_format])
            .args(["-video_size", &format!("{}x{}", size.x, size.y)])
            .args(["-framerate", &fps.to_string(), "-i", "-"])
            // yuv420p is what plays everywhere, and it needs even dimensions.
            .args(["-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2"])
            // Nearly lossless: this is the master `docs/video/record.sh`
            // encodes the upload from.
            .args(["-c:v", "libx264", "-preset", "veryfast", "-crf", "12"])
            .args(["-pix_fmt", "yuv420p"])
            // Keeps a truncated file playable, for the run that is killed
            // rather than closed: a plain mp4 is only readable once its index
            // has been written at the end. Other muxers ignore it.
            .args(["-movflags", "+frag_keyframe+empty_moov"])
            .arg(path)
            .stdin(Stdio::piped())
            // The viewer's stdout is the pipe its edits go up to Python on,
            // and this would inherit it.
            .stdout(Stdio::null());
        // Ctrl-C in a terminal goes to the whole foreground process group, and
        // an ffmpeg killed halfway through a frame leaves the file unfinished.
        // Its own group keeps it alive until the recorder closes the pipe,
        // which is also what finishes the file when the app is killed outright.
        #[cfg(unix)]
        std::os::unix::process::CommandExt::process_group(&mut ffmpeg, 0);
        let child = ffmpeg.spawn().context("ffmpeg has to be on PATH to record")?;
        info!("recording {size} at {fps} fps to {}", path.display());
        Ok(Self { child, size })
    }

    fn write(&mut self, frame: &[u8]) -> anyhow::Result<()> {
        let stdin = self.child.stdin.as_mut().context("ffmpeg's stdin is gone")?;
        // Blocking on a slow encoder only slows the app down; it cannot cost
        // the video a frame, because the video is timed by frame count.
        stdin.write_all(frame).context("ffmpeg stopped reading")
    }
}

impl Drop for Encoder {
    fn drop(&mut self) {
        // `wait` closes stdin first, which is what tells ffmpeg the stream has
        // ended and to finish the file.
        match self.child.wait() {
            Ok(status) if status.success() => info!("recording finished"),
            Ok(status) => error!("ffmpeg exited with {status}"),
            Err(err) => error!("could not wait for ffmpeg: {err}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Frames must reach the encoder in order whatever order they land in, and
    /// one that never lands must not hold up the rest for good.
    #[test]
    fn frames_go_out_in_order_and_a_lost_one_is_given_up_on() {
        let mut early = BTreeMap::new();
        let mut next = 0;

        // 1 landed first, so it waits for 0 and then both go.
        early.insert(1u64, 1u64);
        assert!(ready(&mut early, &mut next).is_empty());
        early.insert(0, 0);
        assert_eq!(ready(&mut early, &mut next), vec![0, 1]);

        // 2 never lands. The frames behind it wait...
        for frame in 3..3 + REORDER_LIMIT as u64 {
            early.insert(frame, frame);
        }
        assert!(ready(&mut early, &mut next).is_empty());
        // ...until one too many has piled up, and then they all go.
        let last = 3 + REORDER_LIMIT as u64;
        early.insert(last, last);
        assert_eq!(ready(&mut early, &mut next), (3..=last).collect::<Vec<_>>());
        assert_eq!(next, last + 1);
    }
}
