# Assets and pictures

A spec can name two kinds of asset. Arena's registered ones come live from its registries
([catalog.py](../python/arena_composer/catalog.py)). NVIDIA's SimReady props are not registered: a
spec names one as `registry_name: simready_usd_object` with the prop's URL in `params.usd_path`, and
the URLs come from a local index of the public bucket ([simready.py](../python/arena_composer/simready.py),
module docs). `catalog.search` merges the two, and the viewer's browser lists both.

The index is `simready-index.json` in the asset cache (`ARENA_COMPOSER_ASSET_CACHE`, default
`~/.cache/arena-composer/usd`, see [usd_assets.py](../python/arena_composer/usd_assets.py)), beside the
USDs it names. The MCP server and a viewer read the same file, and either may rebuild it.

## Where each step is documented

| Step | Where |
| --- | --- |
| list the bucket into the index; downloads nothing | `simready.build` (shared with the MCP tool `reindex_assets`) |
| a layer's picture URL, `.thumbs/256x256/<layer>.png` | `simready.thumbnail_url` |
| fetch pictures in bulk, skipping what is on disk | `simready.fetch_thumbnails` |
| why props ride `remote` and not the vocabulary | [payload.py](../python/arena_composer/payload.py) module docs |
| an asset's entry: `usd_path` beside an empty `params`; a local picture path or empty; a prop's `category`, its folder in the browser's tree | `payload._asset`, `payload._thumbnail` |
| the reindex button: crawl, write the index, fetch every prop's picture | `view._serve_assets` |
| `indexing` goes true as soon as the request is read | `view._serve_requests` |
| an asset answer is retried, since it is the request's only reply | `view._answer` |
| the registered assets' pictures, once at startup | `view._catch_up_on_pictures` |
| another process rebuilt the index | `view._reload_on_change` |
| keep the last prop list; drop a half-made edit only on a new spec | `editor::Form::take` |

The viewer never asks for a picture and never receives one over the pipe. It loads the local path
the payload gives it off disk. An empty path means there is no picture, not that it hasn't arrived
yet, so a card with an empty square is final.

## Rules

- **Never widen `_catch_up_on_pictures` to `catalog.remote()`.** An index built before its pictures
  — by the MCP server, or by an older viewer — would turn every launch into an unrequested ~170 MB
  download. Only the reindex button fetches prop pictures.
- **Never offer a prop as an object-set member.** Members are built with no arguments, so there is
  nowhere to put the `usd_path` a `simready_usd_object` requires, and Arena's validator refuses the
  name outright.
- **Nothing downloads a prop until a spec naming it is resolved.** Arena's validator checks that a
  registry name exists, not what the asset behind it needs. `resolve_layout.check_usd_params` covers
  the URL and the rigid-body count at resolve time, and `catalog.missing_params` — run by
  `spec_edit` on every write — refuses a `simready_usd_object` without its `usd_path`.
