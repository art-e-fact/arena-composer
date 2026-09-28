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
run from the Arena project, whose spec paths the examples below use. Arena exits on import unless
the Isaac Sim EULA is accepted, so export `OMNI_KIT_ACCEPT_EULA=YES`.

## Iterating on the Rust side

A packed payload skips Arena's import, the solve and the USD reads. Pack one in the project:

```bash
arena-resolve-layout environments/tasks/banana_in_bowl.yaml --payload /tmp/scene.msgpack
```

Then run the binaries here. `arena-viewer` is `src/main.rs`. `arena-render` has no `[[bin]]` entry:
cargo finds it at `src/bin/arena-render.rs`.

```bash
cargo run --bin arena-viewer -- /tmp/scene.msgpack
```

```bash
cargo run --bin arena-render -- /tmp/scene.msgpack top=top.png front=front.png
```

To see the window without a person at the keyboard:

| Variable | Effect | Defined in |
| --- | --- | --- |
| `ARENA_VIEWER_SCREENSHOT=shot.png` | draw a moment, write the window (only) to the file, quit | [viewer.rs](../src/viewer.rs) |
| `ARENA_VIEWER_SEARCH=basket` | open the asset browser filtered | [form.rs](../src/editor/form.rs) `PanelState` |
| `ARENA_VIEWER_SHOW_RELATIONS=all` or `=2,5` | open with those relations' gizmos drawn | [form.rs](../src/editor/form.rs) `PanelState` |

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

The version lives in `Cargo.toml` and is the wheel's version too. Push a tag `v<that version>` and
CI attaches the wheel to that tag's GitHub Release. CI has no Arena, so run the whole suite locally,
opt-in tests included, before tagging.
