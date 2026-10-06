# Development

## Setup

The package runs in Arena's venv, and the specs it works on belong to an Arena project, not to this
repo. `isaaclab_arena` is not on PyPI, so this checkout goes into the venv Arena already built. The
first install builds the extension with cargo and takes minutes:

```bash
uv pip install --python <Arena's venv>/bin/python -e .
```

Python edits take effect at once; a Rust edit takes effect when that install runs again. In
artefacts-arena-template, put `ARENA_COMPOSER_SRC=<this checkout>` in its `.env` instead: every
`source setup.sh` there reinstalls this checkout, and `source setup.sh --pinned` switches to the
pinned release.

With that venv active, `cargo` and `pytest` run from this repo's root, and the `arena-*` commands
run from the Arena project, whose spec paths the examples below use.

## Iterating on the Rust side

A packed payload skips Arena's import, the solve and the USD reads. Pack one in the project:

```bash
arena-resolve-layout environments/tasks/banana_in_bowl.yaml --payload /tmp/scene.msgpack
```

Then run the dev binary here, `arena-viewer` (`src/main.rs`). On its own it opens the window:

```bash
cargo run --bin arena-viewer -- /tmp/scene.msgpack
```

Given `VIEW=PNG` pairs it draws them headless and exits, through the same `render.rs` that
`arena-render` and `render_views` run:

```bash
cargo run --bin arena-viewer -- /tmp/scene.msgpack top=top.png front=front.png
```

To see the window without a person at the keyboard:

| Variable | Effect | Defined in |
| --- | --- | --- |
| `ARENA_VIEWER_SCREENSHOT=shot.png` | draw a moment, write the window (only) to the file, quit | [viewer.rs](../src/viewer.rs) |
| `ARENA_VIEWER_SIZE=1440x900` | open the window that size, in logical pixels | [viewer.rs](../src/viewer.rs) `resolution` |
| `ARENA_VIEWER_SEARCH=basket` | open the asset browser filtered, with the first match picked | [browser.rs](../src/editor/browser.rs) `Browser` |
| `ARENA_VIEWER_DOCK=graph` | open the dock on the spatial graph | [dock.rs](../src/editor/dock.rs) `Dock` |
| `ARENA_VIEWER_DOCK_HEIGHT=420` | open the dock that tall | [dock.rs](../src/editor/dock.rs) `Dock` |
| `ARENA_VIEWER_SHOW_RELATIONS=all` or `=2,5` | open with those relations' gizmos drawn | [form.rs](../src/editor/form.rs) `PanelState` |
| `ARENA_VIEWER_PICK=bowl` or `=3` | open with that node, or the relation at that position, picked in the graph and its card open | [form.rs](../src/editor/form.rs) `PanelState` |
| `ARENA_VIEWER_TOUR=tour.json` | play a storyboard with a pointer of the tour's own, and close the window after it | [tour.rs](../src/tour.rs) |
| `ARENA_VIEWER_RECORD=raw.mp4` | record the tour, a video frame per update, with `ffmpeg` | [record.rs](../src/record.rs) |

The README's screenshots are taken this way, by [docs/screenshots/shoot.sh](screenshots/shoot.sh):
run it after changing what they show. The
[readme-screenshots](../.claude/skills/readme-screenshots/SKILL.md) skill says how to change a shot.

Its video is a tour played in a real `arena-view`, so the edits it makes are solved:
[docs/video/record.sh](video/record.sh) records it, and the
[readme-video](../.claude/skills/readme-video/SKILL.md) skill says how to change it. Neither the
shots nor the video are committed: [docs/publish.sh](publish.sh) uploads them to the pull request
and points the README at them.

## Tests

```bash
pytest
```

```bash
cargo test
```

`cargo test` covers the payload decode, mesh and material building and the frame conventions, and
needs no fixtures. `pytest` covers the spec edits against the real validator, the USD readers (see
[usd/AGENTS.md](../python/arena_composer/usd/AGENTS.md#tests)), and the render path as far as it goes
without a GPU.

Tests that need a real packed scene are opt-in. Pack one in the project, then run both suites here
with it:

```bash
arena-resolve-layout environments/tasks/droid_banana_on_plate_maple_table.yaml --payload /tmp/scene.msgpack
```

```bash
ARENA_COMPOSER_PAYLOAD=/tmp/scene.msgpack cargo test -- --ignored
```

```bash
ARENA_COMPOSER_PAYLOAD=/tmp/scene.msgpack pytest
```

CI ([ci.yml](../.github/workflows/ci.yml)) has no Arena. It runs `cargo test`, and runs pytest
against the wheel it built, skipping the files [tests/conftest.py](../tests/conftest.py) lists. A new
test file that imports Arena has to be added to that list, or CI fails.

## The wheel

```bash
uv build --wheel && uvx auditwheel show dist/*.whl
```

One wheel serves every Python from 3.12 (abi3, see [pyproject.toml](../pyproject.toml)). Two rules
keep it installable:

- **It links only glibc and libgcc_s**, the libraries a manylinux wheel may assume. That is why
  Bevy's default features are off ([Cargo.toml](../Cargo.toml)). Windowing and Vulkan are loaded
  with `dlopen` and do not count. CI builds in a manylinux_2_28 container because the platform tag
  follows the newest glibc symbol the extension references.
- **It is stripped** (`[profile.release]`). Stripped, the extension is about 105 MB installed and
  the wheel about 35 MB, against PyPI's 100 MB limit per file.

## Releasing

The version lives in `Cargo.toml` and is the wheel's version too. Before 1.0, a release that adds or
changes behaviour bumps the minor version and one that only fixes bumps the patch.

1. In [CHANGELOG.md](../CHANGELOG.md), rename `## [Unreleased]` to `## [<version>] - <date>`, put a
   fresh `## [Unreleased]` above it, and point the compare links at its foot at the new tag.
2. Set the version in `Cargo.toml`, and in the README's install example.
3. Run the whole suite locally, opt-in tests included: CI has no Arena.
4. Merge, then push a tag `v<version>` on `main`.

CI builds the wheel from the tag and attaches it to the tag's GitHub Release. The release notes are
the version's section of the changelog, with GitHub's list of the merged pull requests under it,
and a tag whose version has no section there is refused.

Between releases, [changelog.yml](../.github/workflows/changelog.yml) keeps `## [Unreleased]`
filled: a pull request that changes the package and not the changelog fails it, unless it is
labelled `no changelog`.
