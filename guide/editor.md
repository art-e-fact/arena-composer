# The visual editor

`arena-view` opens a spec in a window: the layout Arena solves for it in 3D, the spec as a panel of
cards beside it, and a dock underneath with the asset browser and the spatial graph. What you change
is written to the YAML at once, and every change to the file, whoever made it, is solved again and
redrawn.

```bash
arena-view environments/tasks/banana_in_bowl.yaml
```

<!-- assets.png -->
[![The visual editor with the asset browser searched](https://github.com/user-attachments/assets/400a1839-d059-4580-a59d-1bfa5339b4bd)](https://github.com/user-attachments/assets/400a1839-d059-4580-a59d-1bfa5339b4bd)

> [!TIP]
> Leave it open while you edit the YAML in your text editor, or while your agent works on it: the
> window follows the file.

## The window

- **The view.** Drag to orbit, right-drag to pan, scroll to zoom. Click something to pick it: its
  card opens and its node in the graph is outlined. Hovering outlines what a click would pick, a
  double-click turns the camera to it, and a click on the backdrop lets go. Right-click it for a
  menu: the relations naming it (hover one to draw it, click it to open its card), *+ relation* to
  add one with it as the subject, its kind and then what it is placed against picked from the
  submenus, and *remove*.
- **The panel's header** pages through the layouts the last solve drew, five by default
  (`--samples N`), one for each environment Arena would start, and says whether the one on screen
  passes the checks, or which it fails. ⟳ draws new ones from a fresh seed, shown beside it to pass
  back as `--seed`. While a solve runs, a spinner takes its place.
- **The panel**, under its header, is the spec: a section each for the embodiment (the robot), the
  background, the objects, object sets, object references, the relations and the task, each naming
  the file it lives in.
- **The dock**, under the view, holds the asset browser and the spatial graph, a tab each. Fold it
  to give the view the room.

## Cards

Every entry of the spec is a card, folded to one line or open to its fields:

- **Its title is its id.** Type to rename it, and the relations that name it follow. A relation or a
  subtask has no id, so its kind takes the title's place.
- **× removes it,** with the relations that name it.
- **Fields are menus wherever they can be.** An id, a kind or an asset is picked from what exists, so
  a card cannot name something that is not there.
- **A relation shows its shape.** Hover its card to draw it over the layout, and click its 👁 to keep
  it drawn. Drag one of its numbers and the shape moves with you; the edit is sent when you let go.
- **⚠ marks an entry that is wrong.** Hover it to read what: red for what Arena would refuse to
  build, amber for what it builds but the sim will not do as meant, such as an object nothing
  places. It goes when the entry is fixed, however you fixed it.

An object's card carries its asset's picture, and the colours of its node in the graph.

## Add an object

1. **Find it** in the asset browser. Search its name and tags, or walk the folders: Arena's assets
   by role, NVIDIA's SimReady props by category.
2. **Click its tile** to see its details.
3. **Put it in the scene.** Drag the tile onto what it should stand on. Or press ✚ add, or
   double-click the tile, to stand it on whatever the menu beside the button names: the anchor,
   unless you pick another.

An add writes two entries: the object, and an `'on'` relation naming what it stands on.

The robot and the background are swapped rather than added: pick one in the browser and press *use
as the robot* or *use as the background*, or choose another from its card's menu. Their tiles do not
drag, so a stray click cannot replace either.

SimReady props join the browser once they are listed: ⟳, in the browser's header, lists them and
fetches their pictures. That takes a couple of minutes and about 830 MB of downloads, once. They
come from two places: about 1,500 from the bucket Isaac Sim ships its assets from, and about 700 more
from [SimReady Central](https://huggingface.co/datasets/nvidia/simready-assets), under
*SimReady › Central*. A Central prop is downloaded on its first solve into the Hugging Face cache
(`$HF_HOME`), where Arena's own Central loader looks for it too, so the two never download it twice.

SimReady Central's props are licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): a
Central prop's details give the credit to keep with whatever you share that uses it.

## The spatial graph

<!-- graph.png -->
[![The visual editor with the spatial graph and a relation drawn over the layout](https://github.com/user-attachments/assets/4ff5b088-8cdc-4287-a3dc-703ce46225f8)](https://github.com/user-attachments/assets/4ff5b088-8cdc-4287-a3dc-703ce46225f8)

The graph draws the spec the way Arena's own review GUI does: a node per object, and an edge per
relation, labelled with its kind. What a thing is placed against is drawn below it, so the anchor
ends up along the foot. A relation on one object alone is a chip on its node.

- **Click** a node, a chip or a label to open its card in the panel.
- **Hover** one to show it in the view: an object's box, or a relation's shape.
- **Drag** to pan, and **scroll** to zoom. *fit*, or a double-click on the background, shows the
  whole graph.

## What happens when you edit

1. **The file changes at once.** Your edit is written to the YAML, and the card shows it.
2. **The view catches up.** Solving the new layouts takes seconds. Meanwhile the header's spinner
   says the view is behind, and any edits you make share the next solve. The page you were on stays
   turned, so a layout moves only as far as your edit moves it.
3. **A refused edit changes nothing.** An edit that would break the spec is not written, and the
   reason shows as an error in the corner of the view, to copy or close.

Changes from anywhere else, your text editor or your agent, are solved and drawn the same way. One
that does not build leaves the last layout up, with the reason as an error.

## Going deeper

- [How it works](how-it-works.md): specs, relations, the solver, and how a wrong spec is caught.
- [Working with an agent](agents.md): the same edits, made by an agent while you watch.
- For contributors: [how the panel is built](../docs/editor.md).
