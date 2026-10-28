# Assets and pictures

A spec can name two kinds of asset. Arena's registered ones come live from its registries
([catalog.py](../python/arena_composer/catalog.py)). NVIDIA's SimReady props are not registered: a
spec names one as `registry_name: simready_usd_object` with the prop's URL in `params.usd_path`, and
the URLs come from a local index ([simready.py](../python/arena_composer/simready.py), module docs)
of two sources: Isaac Sim 6.0's public bucket, and [SimReady Central](#simready-central), a Hugging
Face dataset. `catalog.search` merges the registry and the index, and the viewer's browser lists both.

The index is `simready-index.json` in the asset cache (`ARENA_COMPOSER_ASSET_CACHE`, default
`~/.cache/arena-composer/usd`, see [usd_assets.py](../python/arena_composer/usd_assets.py)), beside the
USDs it names. The MCP server and a viewer read the same file, and either may rebuild it.

## Where each step is documented

| Step | Where |
| --- | --- |
| list the bucket and Central into the index; downloads nothing | `simready.build` (shared with the MCP tool `reindex_assets`) |
| which Central props join the bucket's, pinned at which commit | `simready.central`, `simready.multi_body` |
| a layer's picture URL, `.thumbs/256x256/<layer>.png` | `simready.thumbnail_url` |
| a layer's picture on disk: the bucket's mirrored, Central's downscaled | `simready.picture` |
| fetch pictures in bulk, skipping what is on disk | `simready.fetch_thumbnails` |
| a Central prop's package, in the Hugging Face cache | `usd_assets._hf_package` |
| why props ride `remote` and not the vocabulary | [payload.py](../python/arena_composer/payload.py) module docs |
| an asset's entry: `usd_path` beside an empty `params`; a local picture path or empty; a prop's `category`, its folder in the browser's tree; a Central prop's `credit` | `payload._asset`, `payload._thumbnail` |
| the reindex button: crawl, write the index, fetch every prop's picture | `view._serve_assets` |
| `indexing` goes true as soon as the request is read | `view._serve_requests` |
| an asset answer is retried, since it is the request's only reply | `view._answer` |
| the registered assets' pictures, once at startup | `view._catch_up_on_pictures` |
| another process rebuilt the index | `view._reload_on_change` |
| keep the last prop list; drop a half-made edit only on a new spec | `editor::Form::take` |

The viewer never asks for a picture and never receives one over the pipe. It loads the local path
the payload gives it off disk. An empty path means there is no picture, not that it hasn't arrived
yet, so a card with an empty square is final.

## SimReady Central

[`nvidia/simready-assets`](https://huggingface.co/datasets/nvidia/simready-assets) holds a package
per prop: an entry layer `<package>/simready_usd/sm_<package>_01.usd` (or `.usda`), with its
payloads, materials, textures and source files beside it. It holds nearly every bucket prop again
under the same layer name, and ~1,100 more.

- **The bucket's prop wins.** A layer both have, by file name with case aside, is indexed from the
  bucket alone: it already resolves through the mirror, as one layer rather than a package of tens to
  hundreds of megabytes. Central adds the rest.
- **A prop known to be more than one rigid body is not offered.** Arena spawns a SimReady prop only as
  exactly one rigid body. The count is in each entry layer's metadata, which only opening it reads,
  so the build asks SimReady Central's search (`nvidia-simready-central.hf.space`), which has it
  indexed. That search is in development, covers other datasets (filtered out) and lags the dataset
  by days, so it is a hint: a prop it has not seen is offered, and `check_usd_params` still refuses
  a wrong one at resolve. ~730 of the ~1,100 are offered. The packages it named are kept in the
  index, so a build whose search does not answer to the end — an error, a cursor that repeats or
  runs out, a page cap, no names at all — leaves out the last build's, and says so as
  `central_filter`.
- **Every URL is pinned.** The dataset's head moves several times a week, and has withdrawn props.
  The build pins `HfApi().dataset_info(...).sha` and writes it into every URL, in the form Arena's
  Central loader takes: `https://huggingface.co/datasets/nvidia/simready-assets/resolve/<sha>/<key>`.
  A rebuild moves the pin; a spec written before keeps its own, which Hugging Face keeps resolving.
- **The index keeps Central apart**, under `central: {revision, assets, multi_body}` beside the
  bucket's `assets`. An index from before has none and reads as the bucket alone, and an older
  composer reading a new one sees the bucket's props alone, since it reads `assets` and nothing else.
- **Central failing costs the build only Central.** Any failure listing it, or no answer within
  `simready.CENTRAL_DEADLINE`, keeps the Central section the index had, or writes none, beside a
  fresh bucket listing, and says why as `central_error`. Reindexing needs only S3, as it did.
  `reindex_assets` returns `central_error` and `central_filter` as keys, and the reindex button's
  answer carries them as the panel's errors. The deadline is the build's own: huggingface_hub's
  client waits on a stalled connection for ever, and it is Arena's client too.
- **A package lives in the Hugging Face cache, not here.** A resolve has Arena's
  `download_and_wrap_central_asset` download it (`resolve_layout.check_usd_params`), and Arena builds
  the prop from the wrapper layer that writes, so no Central URL reaches the mirror. One that reaches
  `usd_assets.localize` anyway — through `Usd.Stage.Open`, or Arena's own `retrieve_file_path`,
  which `_route_retrievals` sends there — is downloaded the same way: file by file with
  `hf_hub_download` at the pinned commit, so the two share every file and `HF_HOME` moves them.
  Nothing of it is mirrored into composer's cache, and none of its layers is relinked: the files are
  the Hugging Face cache's, shared with Arena. The Hub is asked once a process for the package's
  files, cached or not: a cached entry is no sign of a whole package, since Arena's loader downloads
  in no order and may have been stopped halfway, and with the commit pinned a cached file costs a
  stat. Only a Hub that cannot be reached is answered from the cache. Arena's loader asks the Hub
  every time, so a resolve naming a Central prop needs huggingface.co either way.
- **A picture is kept once, small.** Central publishes them at 1024 px whatever their `256x256`
  folder says, ~850 KB each. `simready.fetch_thumbnails` fetches each and keeps one 256 px copy,
  ~60 KB, under `simready-central/` in the asset cache, named by its layer and not the commit, so a
  rebuild at a newer commit fetches nothing again. The reindex button fetches the ~730 offered:
  ~660 MB over the wire, ~40 MB on disk.
- **The license is CC-BY-4.0**, which asks for credit wherever a prop is passed on. Each Central
  prop carries `credit` — `simready.CENTRAL_CREDIT` — through the payload to the browser's details,
  and in `search_assets` hits for the agent.

## Rules

- **Never widen `_catch_up_on_pictures` to `catalog.remote()`.** An index built before its pictures
  — by the MCP server, or by an older viewer — would turn every launch into an unrequested ~830 MB
  download. Only the reindex button fetches prop pictures.
- **Never hand the viewer a picture from the Hugging Face cache.** A pulled package holds its
  1024 px original; the viewer draws the downscaled copy `simready.picture` names.
- **Never write into the Hugging Face cache.** Its files are links into content-addressed blobs that
  Arena reads too; `_relink_url_references` and anything like it stay off a Central package.
- **Never offer a prop as an object-set member.** Members are built with no arguments, so there is
  nowhere to put the `usd_path` a `simready_usd_object` requires, and Arena's validator refuses the
  name outright.
- **Nothing downloads a prop until a spec naming it is resolved.** Arena's validator checks that a
  registry name exists, not what the asset behind it needs. `resolve_layout.check_usd_params` covers
  the URL and the rigid-body count at resolve time, and `catalog.missing_params` — run by
  `spec_edit` on every write — refuses a `simready_usd_object` without its `usd_path`.
