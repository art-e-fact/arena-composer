# The payload

The MessagePack payload that `arena_composer.payload` packs is the whole contract between the Python
side (`python/`) and the Bevy viewer (`src/`). Both ends change together.

MessagePack rather than JSON, because the geometry is megabytes of float arrays: JSON spends ~760 ms
formatting them as decimal text against ~2 ms for `bin` blobs. Arrays travel as little-endian
`f32`/`u32` and are cast back with one copy through `bytemuck::pod_collect_to_vec` — a `bin` payload
lands at an arbitrary offset, so it cannot be assumed aligned. Everything around the arrays stays an
ordinary map, so unknown keys are ignored and absent ones default.

Geometry arrives in **Arena's frame** — Z-up, metres — with the transforms already baked into the
vertex positions. That is what lets the Rust side hold no USD conventions: no up-axis, no unit scale,
no xform-op ordering, and no matrix convention to reconcile (USD composes row-vector transforms, glam
column-vector ones). Keep it that way — USD concepts do not belong under `src/`.

Baking costs two things: a mesh repeated inside one asset is emitted twice (assets sharing a USD are
still extracted only once), and nothing can move at runtime. Revisit both when the viewer needs to
animate.
