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

The screenshots the README and the guide show are taken this way, by
[docs/screenshots/shoot.sh](screenshots/shoot.sh):
run it after changing what they show. The
[readme-screenshots](../.claude/skills/readme-screenshots/SKILL.md) skill says how to change a shot.

Its video is a tour played in a real `arena-view`, so the edits it makes are solved:
[docs/video/record.sh](video/record.sh) records it, and the
[readme-video](../.claude/skills/readme-video/SKILL.md) skill says how to change it. Neither the
shots nor the video are committed: [docs/publish.sh](publish.sh) uploads them to the pull request
and points the README and the guide at them.

### The web viewer

The same window runs in a browser, over a packed payload a static page fetches: a prototype of a
hosted composer, read-only since no Arena stands behind the page. [web/build.sh](../web/build.sh)
builds the crate for `wasm32-unknown-unknown` ([web.rs](../src/web.rs) is its entry) into a folder
any static server can serve, and readies the payloads it is given with
[web/export.py](../web/export.py), which copies the textures and pictures a payload names out of the
asset cache and rewrites the payload to name the copies:

```bash
web/build.sh /tmp/scene.msgpack
python -m http.server -d web/site    # open http://localhost:8000/?payload=scene.msgpack
```

It needs rustup's wasm32 target and the `wasm-bindgen` CLI at the version `Cargo.lock` pins
(`cargo install wasm-bindgen-cli --version <it>`). It draws through WebGL2. An edit in the panel
stays in its card, as it does in `arena-viewer`.

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

### The showcase

[showcase/](../showcase/) draws a gallery for a person to look over: a few of each family of Arena's
own specs, and themes that put together robots, backgrounds and SimReady props no shipped spec uses.
`pytest` does not collect it. Run it here, on a machine that can draw, after a change to how a scene
is read or drawn:

```bash
pytest showcase
```

Every case is drawn from every view into `$ARENA_SHOWCASE_DIR` (`/tmp/arena-showcase` by default),
beside a `junit.xml` with a test case per spec. A case fails when its spec does not build or draw;
one that draws lists its images and its placement verdict as properties. `ARENA_SHOWCASE_SEED`
draws other props and other layouts. The first run downloads the props, which takes minutes.
`artefacts run showcase` runs it as an Artefacts job instead, into the run's upload: see the
[README](../README.md#the-showcase-on-artefacts). A theme
is a spec in `showcase/themes/` with a `draw` key, which
[test_showcase.py](../showcase/test_showcase.py) describes.

The README's picture of the showcase is six of its cases, each from a camera placed for a person
rather than from the views it checks. `python showcase/collage.py` draws it beside the gallery as
`showcase.png`, and [docs/publish.sh](publish.sh) puts it on the README as it does the screenshots.
Its `TILES` name the cases and their cameras.

### The Isaac Sim comparison

[parity/](../parity/) checks the composer's drawing against Isaac Sim's, which is what the sim's
cameras see. Each spec in [parity/specs/](../parity/specs/) is solved once, and both draw it from the
same cameras. Isaac Sim builds the spec in a process of its own and reports where physics left
everything, and the composer draws the scene moved to those poses. Four of the specs stand robolab's,
Arena's and SimReady's props in rows on a table at fixed poses, so that each gets a close-up and
nothing moves them; the other two are a Lightwheel kitchen and a GR1 humanoid in one. It needs Isaac
Sim in Arena's venv, its EULA accepted, and a GPU that RTX renders on; nothing else here starts Isaac
Sim.

```bash
OMNI_KIT_ACCEPT_EULA=YES python parity/compare.py parity/specs/*.yaml --out /tmp/arena-parity
```

Each spec takes about a minute, half of it Isaac Sim starting, once its assets are downloaded.
`/tmp/arena-parity` gets a `<spec>-<camera>.png` per camera, Isaac Sim's picture left of the
composer's, the pictures themselves and Isaac Sim's log under `<spec>/`, a `tests_junit.xml` and a
`metrics.json`.

A camera is scored by how far the composer's colours are from Isaac Sim's once exposure is matched:
one gain per pair, then the mean difference in sRGB, 0–255, over 8×8-pixel blocks, leaving out the
composer's backdrop, where Isaac Sim shows its sky. A camera Isaac Sim renders dark, which stands
inside something, is skipped. A camera fails when its error rises more than 3 above the one
[parity/baseline.json](../parity/baseline.json) records, so a change that draws further from Isaac
Sim fails a test. A change that draws closer lowers the error; record it with `--write-baseline`,
and the next run holds it.

`artefacts run parity` runs it as an Artefacts job, a scenario per spec: see the
[README](../README.md#the-isaac-sim-comparison-on-artefacts).

## The guide's site

[art-e-fact.github.io/arena-composer](https://art-e-fact.github.io/arena-composer/) is the guide: the
README, the pages in [guide/](../guide/) and the changelog, built with [Zensical](https://zensical.org)
from where they live in the repository. The internals under `docs/` stay off it, and are read on
GitHub. The nav in [zensical.toml](../zensical.toml) lists every page on the site by its path, and
[site/stage.py](site/stage.py) copies those pages to `target/docs` under the same paths: a link
between two pages works as written, and every other link, to source or to a page left off the site,
is pointed at GitHub at the commit being built. In the staged copy, the README's video line becomes
a `<video>` tag, and the lines between the `mcp-tools` markers in [guide/agents.md](../guide/agents.md)
become a table of the MCP tools, drawn from their docstrings. A page under `guide/` missing from the
nav fails [test_docs_site.py](../tests/test_docs_site.py), and so does one that names a private
symbol.

```bash
python3 docs/site/stage.py
```

```bash
uvx zensical@0.0.71 serve
```

It serves at `localhost:8000/arena-composer/`, and rebuilds when the staged copy changes, so run the
script again after editing a page. `zensical build` writes the site to `target/site` instead.
[docs.yml](../.github/workflows/docs.yml) builds it with `--strict` on every pull request, which fails
a link to a page or a heading that does not exist, and deploys it to GitHub Pages from `main`.
[site/artefacts.css](site/artefacts.css) gives it Artefacts' colours and type.

On the site's pages, indent a nested list four spaces. GitHub nests one at two, but the site does
not, and draws it as part of the list above, so [test_docs_site.py](../tests/test_docs_site.py)
fails one that is not a multiple of four.

A diagram on them is a `mermaid` code block, which GitHub and the site both draw, each in its own colours.
Its `accTitle` and `accDescr` are what a screen reader says for it, so
[test_docs_site.py](../tests/test_docs_site.py) fails a diagram without them. It is drawn in the
reader's browser, so one that does not parse fails no build: look at it on the pull request. Keep
one under about 690 px wide, the column's width on a laptop, or it is shrunk to fit, text and all.

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

PyPI's page for the package is the README the wheel carries, where a relative link resolves against
pypi.org. CI runs `python3 docs/site/stage.py --readme` before building, which points each one at
GitHub at the commit being built. It leaves HTML alone, so a `src` or `href` in the README has to be
a full URL already; [test_docs_site.py](../tests/test_docs_site.py) fails one that is not.

## Releasing

The version lives in `Cargo.toml` and is the wheel's version too. Before 1.0, a release that adds or
changes behaviour bumps the minor version and one that only fixes bumps the patch.

1. In [CHANGELOG.md](../CHANGELOG.md), rename `## [Unreleased]` to `## [<version>] - <date>`, put a
   fresh `## [Unreleased]` above it, and point the compare links at its foot at the new tag.
2. Set the version in `Cargo.toml`.
3. Run the whole suite locally, opt-in tests included: CI has no Arena.
4. In the README's [Arena section](../README.md#which-arena-it-runs-on), record the Arena `main`
   commit the release is synced with, in a table with a row per release.
5. Merge, then push a tag `v<version>` on `main`.
6. Approve the tag's run deploying to `pypi`: Review deployments, on the run's page.

CI builds the wheel from the tag and attaches it to the tag's GitHub Release. The release notes are
the version's section of the changelog, with GitHub's list of the merged pull requests under it,
and a tag whose version has no section there is refused. Then the `pypi` job waits for that
approval and uploads the same wheel to [PyPI](https://pypi.org/project/arena-composer/). PyPI takes
a version once, even after it is deleted, so a broken release is fixed by the next patch, and
yanked.

The upload needs no token: PyPI trusts `ci.yml` in this repository, run in the `pypi` environment
(trusted publishing, set in the project's settings on PyPI). That environment, in the repository's
settings, deploys only tags matching `v*`, and only once a required reviewer approves.

Between releases, [changelog.yml](../.github/workflows/changelog.yml) keeps `## [Unreleased]`
filled: a pull request that changes the package and not the changelog fails it, unless it is
labelled `no changelog`.
