---
name: readme-screenshots
description: Retakes the README's screenshots of the visual editor (arena-view's window) with docs/screenshots/shoot.sh, and changes what they show — the demo scene, the dock tab, the search, the picked card, the relation gizmos. Use after a change to how the panel, the dock, the graph, the gizmos or the 3D view look, or when asked to update, retake or add a README or docs screenshot. Not for checking a layout (arena-render, or arena-mcp's render tool).
---

# README screenshots

The README shows the visual editor twice, side by side: `docs/screenshots/assets.png`, the asset
browser searched and an object's card open, and `docs/screenshots/graph.png`, the spatial graph with
a relation picked and gizmos drawn over the layout. Both are one command away:

```bash
docs/screenshots/shoot.sh
```

It builds `arena-viewer`, starts a nested X server (Xephyr), and opens each shot's scene in the
state its `shoot` line sets, drawn by Mesa's software Vulkan (lavapipe), writing the window to the
PNG. A few seconds a shot. Arena's venv has to be active only when a scene has to be packed.

## Retake them

1. Run `docs/screenshots/shoot.sh`, from any directory.
2. Read every PNG it names, and check it against the list below. A shot that fails one is not done:
   change the state or the scene, never the picture.
3. Run `docs/publish.sh docs/screenshots/*.png` on the branch whose pull request changes the
   window. The PNGs stay out of git, so that a retake does not grow the clone: it uploads them in a
   comment on the pull request, where reviewers see them, and puts the URL GitHub gives each on the
   README line under its marker, `<!-- graph.png -->`. Commit README.md with the change. It needs
   gh 2.102 or later, for `--attach`, and push access; it posts, so run it once the shots are right.

A shot is right when:

- no error toast is in the view's corner, and the status line's `drawn/total` count is short only
  by the scene's object references, which their parent draws
- every tile in the browser's grid shows a picture rather than a box. Few of Arena's own assets have
  one, so search for SimReady props: a folder word like `kitchenware` matches them
- the picked card is outlined and wholly on screen, and the graph lights the same entry
- the gizmos drawn read against the layout, and none runs off the view (a `position_limits_box`
  without z limits draws its edges to the sky)
- the graph's labels are legible, and nothing overlaps

## Change what a shot shows

A shot is one line at the foot of `shoot.sh`: the PNG, the scene, and the `ARENA_VIEWER_*` variables
that set the window's state, listed in the table in
[docs/development.md](../../../docs/development.md#iterating-on-the-rust-side). Every shot opens at
1440×900. A new shot is a new line, and a new cell in the README's table under a marker naming its
PNG, with any `https://github.com/user-attachments/assets/…` URL in it for `docs/publish.sh` to
replace.

Relations are named by position in the spec's `relations:` list, from 0, in both `ARENA_VIEWER_PICK`
and `ARENA_VIEWER_SHOW_RELATIONS`. Editing that list moves them.

The scene is an ordinary spec beside the script, `docs/screenshots/kitchen_island.yaml`, written to
show the editor off. The kitchen fills the view from the background's own camera, which no spec can
move, and position limits keep the objects inside it. `shoot.sh` packs `<scene>.msgpack` beside the
spec whenever the spec is newer, with this checkout's packer. That needs:

- Arena's venv active, and the network: Lightwheel's SDK asks its server for every kitchen, cached
  or not.
- ~90 MB of disk for the pack. It names files in this machine's asset cache, so it is gitignored.
  Delete it to repack after a change to the payload or the USD readers.

## A state no variable reaches

Add a variable rather than editing a picture. Each one seeds state a person could reach by hand,
read in the `Default` of the struct it seeds (`PanelState`, `Browser`, `Dock`) or where the window
is built (`viewer::run`), and documented there and in the table in docs/development.md.

The first frames are slow while the scene uploads, so a variable acting on time rather than on
frames has to last past them: `ARENA_VIEWER_PICK` dates its pick a second ahead, or the card's
reveal ends before the panel has scrolled to it.

What only a real pointer shows — a drag in flight, a hover — needs input sent to the window. Run
`arena-viewer` on a Xephyr display, send motion and button events with `libXtst`'s
`XTestFakeMotionEvent` and `XTestFakeButtonEvent` through `ctypes` (there is no xdotool here), and
grab the display with `ffmpeg -f x11grab -video_size <W>x<H> -i :<display> -frames:v 1 out.png`.
