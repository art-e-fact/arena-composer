---
name: readme-video
description: Re-records the README's demo video of the visual editor with docs/video/record.sh, and changes what it shows — the clicks, drags, typing and edits a pointer makes in arena-view's window, and their timing — by editing the storyboard docs/video/tour.json. Use after a change to how the panel, the dock, the browser, the graph or the 3D view look or are used, when a new feature should be shown in the video, or when asked to update, re-record or re-cut it. Not for still screenshots (readme-screenshots).
---

# README video

The README's video is arena-view used by hand: a turn round the kitchen, a search over the assets
and the SimReady props, a prop dragged onto the plate and solved into place, the spatial graph, a
relation picked there and its distance dragged out until the bowl moves. One command records it:

```bash
docs/video/record.sh
```

It builds this checkout's Rust into the extension, opens a copy of the screenshots' scene
(`docs/screenshots/kitchen_island.yaml`) in a real `arena-view` on a nested X server (Xephyr), drawn
by Mesa's software Vulkan (lavapipe), and plays the storyboard `docs/video/tour.json` there with a
pointer of its own. It writes `target/video/arena-composer.mp4` and, beside it,
`arena-composer-sheet.png`: the frames at 0, 2, 4… seconds, four to a row. Minutes, not seconds.

It needs Arena's venv active and the network — arena-view solves every edit the tour makes, and
Lightwheel's SDK asks its server for the kitchen — and cargo, Xephyr, lavapipe and ffmpeg.

## Re-record it

1. Run `docs/video/record.sh`. On a failed step it stops, naming the step and why: a target not on
   screen lists what is there instead; a text on screen more than once says where each is.
2. Check the sheet, and the frames around every step, against the list below. The script prints a
   `tour: step N at T s` line per step, `N` counting the storyboard's `steps` from 1 and `T` seconds
   into the video. `ffmpeg -ss T -i target/video/arena-composer.mp4 -frames:v 1 -update 1 frame.png`
   takes the frame at `T`. A video that fails one is not done: change the storyboard, never the
   video.
3. Hand the mp4 to a person to upload. GitHub plays only video it hosts, and only a signed-in browser
   can put it there, by dragging the file into an issue comment or the README's editor on
   github.com. The `https://github.com/user-attachments/assets/…` URL that gives goes on a line of
   its own in README.md, under the screenshots' table, in place of the last one.

A video is right when:

- its first frame shows the whole kitchen, shaded, and the browser's tiles have their pictures. If
  anything pops in, raise the storyboard's `warmup`, the wall-clock seconds the window gets before
  the video starts.
- the pointer reaches each thing before it acts on it, and every edit has visibly landed — the
  object moved, the new card in the panel — before the next step starts
- no error toast is in the view's corner
- the panel's text is legible at the README's width, about 830 px
- the file is under 10 MB, the most GitHub takes on a free plan. The script spends 9 MB whatever the
  length, so a longer video gets fewer bits a second: cut steps rather than raise it.

## Change what it shows

The storyboard is the window (`size` in physical pixels, and `scale`, which enlarges the panel),
`fps`, `warmup`, and `steps`, played one after the other. The format is documented on `Tour`,
`Step` and `Target` in [src/tour.rs](../../../src/tour.rs); read those first. In short:

- **A step is one action and a pause**: `move`, `click` or `double_click` a target; `drag` one `to`
  another, or `by` `[x, y]` logical pixels; `scroll` one by `lines` (positive scrolls up, or zooms
  the view in, a notch a frame); `type` text into whatever has the keyboard; press a `key`; or wait `until` the
  edit before is `solved`. `secs` is how long the action takes, `pause` (0.4 by default) how long
  to hold after it, and a step with no action is a pause alone. `note` is a comment.
- **A target is what is on screen**, as a person reads it: a string is a text egui draws — a tab, a
  caption, a tile's or a node's name, a value in a field; not a tooltip. Text drawn exactly so wins
  over text that only holds it. Where it is drawn more than once, `{"text": …}` takes `in`
  (`panel`, `dock` or `view`), `near` another target, or `nth` in reading order; an entry of a menu
  just opened is the one `near` `{"pointer": true}`. `{"object": "plate"}` is the middle of an
  object's top, where a drop would stand it, and only while the view shows it; `{"in": "view"}`
  alone is the middle of the view. `offset` moves any of them.
- **An edit is two steps**: the action that makes it, then `{"until": "solved"}`, which shows
  `secs` (0.6) of the spinner and cuts the rest of the wait out of the video. It fails on an error.
  Edits persist: every later step sees them.
- **The camera** turns about a point well behind the island, where Arena's framing puts it: a drag
  of 25 pixels across the view turns the island about 15°, and each wheel notch closes a fifth of
  the distance to that point, so two put the camera over the island. An object turned out of the
  view is no longer a target.
- **Drag what moves under the pointer `by`**, not `to`: a number's text changes as it is dragged,
  and the view turns.
- **The scene** is the screenshots' (`scene` in `record.sh`); the tour edits only its copy. An
  object a step names by id has to be in it, or added by a step before.

Finding a shot: a scratch storyboard of a few steps, recorded with
`docs/video/record.sh scratch.json /tmp/scratch.mp4`. To watch one play on the desktop instead,
without recording, on the wall clock:
`ARENA_VIEWER_TOUR=scratch.json arena-view <a copy of the scene>`. It edits the spec it opens.

## Something no step can do

A new feature with a caption needs nothing but steps naming it. One drawn without text — an icon, a
painted handle — is reached from a text beside it with `offset`. A key missing from `key` is a line
in `Press::named`. A new kind of action is a field on `Step` and the beats it comes to in
`Step::beats`, done the way a mouse or keyboard would do it — the input messages winit writes, so
the app answers it as it answers a hand — never by reaching into the app's state.
