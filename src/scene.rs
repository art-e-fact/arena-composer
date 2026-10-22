//! The viewer payload, and the one convention that comes with it.
//!
//! `arena_composer.payload` packs a resolved layout and its geometry as
//! MessagePack: ordinary maps for the scene, `bin` blobs of little-endian
//! `f32`/`u32` for the vertex arrays. Only the fields a front end uses are
//! decoded — the rest of the resolver's dump (placement diagnostics, the USD
//! paths) stays in its JSON form and is deliberately not mirrored here, so
//! growing it cannot break deserialization.
//!
//! What the viewer draws lives here. What the editor panel edits — the authored
//! spec and the vocabulary behind it — lives in [`crate::spec`]; the two ride
//! one payload because a resolve produces both at once.
//!
//! Geometry arrives in **Arena's frame**: Z-up, metres, quaternions as `xyzw`,
//! with every USD convention already resolved on the Python side. Bevy is Y-up,
//! so the whole scene hangs under one root carrying [`arena_to_bevy`] and
//! nothing below it needs a per-value axis flip.

use bevy::prelude::*;
use serde::Deserialize;
use std::collections::HashMap;
use std::f32::consts::FRAC_PI_2;

use crate::spec::{CatalogAsset, Problem, Spec, Vocabulary};

/// Lays Arena's Z-up world down onto Bevy's Y-up one.
pub fn arena_to_bevy() -> Quat {
    Quat::from_rotation_x(-FRAC_PI_2)
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Scene {
    pub env_name: String,
    /// The resolved layout, absent when the message carries the spec alone.
    /// An accepted edit is answered the moment it is written, seconds before
    /// the solver has caught up with it; what is drawn stays drawn until it
    /// has. See [`Scene::assets`].
    pub assets: Option<Vec<Asset>>,
    /// Every layout the solve drew, one per sample, the first being `assets`:
    /// the layouts the viewer pages through. Empty on a message carrying no
    /// layout, and from a payload packed before there were samples.
    pub layouts: Vec<Layout>,
    /// The placement seed the layouts were drawn from, absent with no layout.
    pub seed: Option<i64>,
    /// Extracted USD stages, shared by index — assets spawned from the same
    /// USD point at the same entry.
    pub geometries: Vec<Vec<Node>>,
    pub lighting: Lighting,
    pub viewer: Option<ViewerCfg>,
    /// The spec this message is about, for the editor panel to edit. See
    /// [`crate::spec`]. The panel is opened on the first message's copy and
    /// answered by the messages carrying no layout after that: a layout's copy
    /// was read when its solve began, and an edit made during those seconds
    /// leaves it behind. Which is why it is still here — a packed payload run
    /// through `arena-viewer` on its own has no other way to fill the panel.
    pub spec: Spec,
    /// Which of the task and scene files holds each top-level key.
    pub owners: HashMap<String, String>,
    pub vocabulary: Vocabulary,
    /// The SimReady props the local index knows, absent when this message
    /// changes nothing about them — which is every message but the first, an
    /// answer to [`Request::Reindex`](crate::spec::Request::Reindex), a rebuild
    /// noticed on disk, and the startup pass that fetches the pictures the
    /// catalogue is missing. They are a megabyte of table against a vocabulary's
    /// thirty kilobytes, so they travel only when they have changed and the
    /// panel keeps the last list it was sent.
    pub remote: Option<Vec<CatalogAsset>>,
    /// Whether a crawl of the bucket is under way, absent when this message
    /// says nothing about one. Python's to answer, because only it knows
    /// whether the request is queued, running or done — the panel would have
    /// to guess from the next prop list, and those arrive for other reasons.
    pub indexing: Option<bool>,
    /// Why the last edit was refused, empty when it landed, and absent when
    /// this message is not about an edit — a message about assets knows
    /// nothing of the last one, and must not clear its refusal.
    pub errors: Option<Vec<String>>,
    /// Whether a layout for this spec is still being solved, so the panel can
    /// say the view it is over has yet to catch up. Absent, for the same
    /// reason as `errors`, from a message that knows nothing of the solve.
    pub solving: Option<bool>,
    /// What is wrong with `spec`, a line per problem at the entry it is about,
    /// for the panel to mark that entry's card. Where `errors` are events, these
    /// are the spec as it stands, so each message's replace the last. Absent
    /// where the file no longer reads, and `spec` is the one it last read to.
    pub problems: Option<Vec<Problem>>,
}

impl Scene {
    pub fn from_msgpack(bytes: &[u8]) -> anyhow::Result<Self> {
        Ok(rmp_serde::from_slice(bytes)?)
    }

    /// The layout to draw, empty when the message carried none.
    pub fn assets(&self) -> &[Asset] {
        self.assets.as_deref().unwrap_or_default()
    }

    /// The pieces to draw for an asset, empty when it contributes none.
    pub fn geometry_of(&self, asset: &Asset) -> &[Node] {
        asset
            .geometry
            .and_then(|index| self.geometries.get(index))
            .map_or(&[], Vec::as_slice)
    }
}

/// One of the layouts a solve drew, and what the checks a dry solve runs made
/// of it.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Layout {
    pub assets: Vec<Asset>,
    /// Whether it passed every check; absent where nothing was placed, and so
    /// nothing checked.
    pub success: Option<bool>,
    /// The checks it failed, by Arena's names for them.
    pub failed_checks: Vec<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Asset {
    pub id: String,
    /// `embodiment`, `background`, `object`, `object_set` or `object_reference`.
    pub role: String,
    pub scale: Scale,
    pub position_xyz: [f32; 3],
    pub rotation_xyzw: Rotation,
    /// Root-relative bounds, used only to frame the camera.
    pub bbox_min: [f32; 3],
    pub bbox_max: [f32; 3],
    /// Index into [`Scene::geometries`]; `None` when the asset draws nothing of
    /// its own, as an object reference does — it is a prim inside its parent's
    /// stage, so drawing it again would double up the parent's geometry.
    pub geometry: Option<usize>,
}

impl Asset {
    /// The solved (or configured) pose, in Arena's frame.
    pub fn transform(&self) -> Transform {
        let [x, y, z, w] = self.rotation_xyzw.0;
        Transform {
            translation: Vec3::from(self.position_xyz),
            rotation: Quat::from_xyzw(x, y, z, w).normalize(),
            scale: Vec3::from(self.scale.0),
        }
    }
}

/// One drawable piece: triangles already in the asset's own frame, plus the
/// material to shade them with. A mesh whose faces bind different materials
/// arrives as one node per `GeomSubset`.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Node {
    /// Source prim path. Carried for debugging only.
    pub path: String,
    #[serde(with = "serde_bytes")]
    pub positions: Vec<u8>,
    #[serde(with = "serde_bytes")]
    pub indices: Vec<u8>,
    #[serde(with = "serde_bytes")]
    pub normals: Option<Vec<u8>>,
    #[serde(with = "serde_bytes")]
    pub uvs: Option<Vec<u8>>,
    pub base_color: [f32; 3],
    pub opacity: f32,
    pub metallic: f32,
    pub roughness: f32,
    pub emissive: [f32; 3],
    pub double_sided: bool,
    /// Absolute paths, by slot: `base_color`, `normal`, `metallic_roughness`,
    /// `emissive`, `occlusion`. Only paths that exist are sent.
    pub textures: HashMap<String, String>,
    /// The colour slots whose texture holds linear values, not sRGB: where the USD says so, or leaves
    /// it to a 16-bit file. The data slots always hold linear values.
    pub linear: Vec<String>,
}

impl Node {
    pub fn positions(&self) -> Vec<[f32; 3]> {
        floats(&self.positions)
    }

    pub fn normals(&self) -> Option<Vec<[f32; 3]>> {
        self.normals.as_deref().map(floats)
    }

    pub fn uvs(&self) -> Option<Vec<[f32; 2]>> {
        self.uvs.as_deref().map(floats)
    }

    pub fn indices(&self) -> Vec<u32> {
        floats(&self.indices)
    }
}

/// Reinterpret a blob as a vertex array.
///
/// `pod_collect_to_vec` copies rather than borrows, which is the right trade
/// here twice over: a MessagePack `bin` payload lands at an arbitrary offset in
/// the buffer and so cannot be assumed aligned for `f32`, and Bevy's mesh
/// attributes want an owned `Vec` regardless. Little-endian is assumed, matching
/// what the Python side writes.
fn floats<T: bytemuck::Pod>(bytes: &[u8]) -> Vec<T> {
    if !bytes.len().is_multiple_of(size_of::<T>()) {
        warn!(
            "dropping a {} byte array that is not a whole number of elements",
            bytes.len()
        );
        return Vec::new();
    }
    bytemuck::pod_collect_to_vec(bytes)
}

/// Where light comes from. The graph spec has no lighting fields, so Arena
/// injects a dome light unless the scene supplies one — and a viewer that
/// wants to match what the sim renders has to do the same.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Lighting {
    pub auto_injected_dome_light: bool,
}

/// The background's suggested camera framing, in Arena's frame.
#[derive(Debug, Clone, Deserialize)]
pub struct ViewerCfg {
    pub eye: [f32; 3],
    pub lookat: [f32; 3],
}

/// Defaults to 1, not 0 — an absent scale must leave the asset alone.
#[derive(Debug, Clone, Deserialize)]
pub struct Scale(pub [f32; 3]);

impl Default for Scale {
    fn default() -> Self {
        Self([1.0; 3])
    }
}

/// Defaults to the identity quaternion, `xyzw`.
#[derive(Debug, Clone, Deserialize)]
pub struct Rotation(pub [f32; 4]);

impl Default for Rotation {
    fn default() -> Self {
        Self([0.0, 0.0, 0.0, 1.0])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Serialize;

    /// The root rotation has to take Arena's up onto Bevy's, or the whole
    /// scene lies on its side.
    #[test]
    fn arena_up_maps_onto_bevy_up() {
        assert!((arena_to_bevy() * Vec3::Z).abs_diff_eq(Vec3::Y, 1e-6));
        assert!((arena_to_bevy() * Vec3::X).abs_diff_eq(Vec3::X, 1e-6));
    }

    /// Stands in for a future Python side that has grown a field this build
    /// does not know about, and dropped an optional one it does.
    #[derive(Serialize)]
    struct FutureAsset {
        id: &'static str,
        role: &'static str,
        position_xyz: [f32; 3],
        bbox_min: [f32; 3],
        bbox_max: [f32; 3],
        future_field: u32,
    }

    #[derive(Serialize)]
    struct FutureScene {
        env_name: &'static str,
        assets: Vec<FutureAsset>,
    }

    /// Unknown keys are ignored and absent ones default, so the payload can
    /// grow without the viewer needing a matching change.
    #[test]
    fn a_minimal_asset_entry_decodes() {
        let packed = rmp_serde::to_vec_named(&FutureScene {
            env_name: "t",
            assets: vec![FutureAsset {
                id: "a",
                role: "object_reference",
                position_xyz: [1.0, 2.0, 3.0],
                bbox_min: [0.0; 3],
                bbox_max: [1.0; 3],
                future_field: 1,
            }],
        })
        .unwrap();

        let scene = Scene::from_msgpack(&packed).unwrap();
        let asset = &scene.assets()[0];
        assert_eq!(asset.transform().translation, Vec3::new(1.0, 2.0, 3.0));
        assert_eq!(asset.transform().scale, Vec3::ONE);
        assert_eq!(asset.transform().rotation, Quat::IDENTITY);
        assert!(scene.geometry_of(asset).is_empty());
        assert!(!scene.lighting.auto_injected_dome_light);
    }

    /// Almost no message carries the prop list, and one that omits it must not
    /// read as an empty list — the panel would blank its browser on every edit.
    #[test]
    fn a_message_without_props_carries_no_list() {
        let packed = |scene: serde_json::Value| Scene::from_msgpack(&rmp_serde::to_vec_named(&scene).unwrap()).unwrap();

        assert!(packed(serde_json::json!({"env_name": "t"})).remote.is_none());
        let sent = packed(serde_json::json!({
            "remote": [{
                "registry_name": "simready_usd_object",
                "name": "Plastic_Basket_A01",
                "params": {"usd_path": "https://bucket/basket.usd"},
                "thumbnail": "/cache/https/bucket/.thumbs/256x256/basket.usd.png",
            }],
        }));
        let props = sent.remote.expect("the list this message carried");
        assert_eq!(props[0].name, "Plastic_Basket_A01");
        assert_eq!(props[0].params["usd_path"].as_text(), Some("https://bucket/basket.usd"));
        assert!(props[0].thumbnail.ends_with("basket.usd.png"));
    }

    /// The blobs are raw little-endian arrays, and a truncated one is dropped
    /// rather than reinterpreted across element boundaries.
    #[test]
    fn vertex_blobs_decode_as_arrays() {
        let node = Node {
            positions: [1.0f32, 2.0, 3.0, 4.0, 5.0, 6.0]
                .iter()
                .flat_map(|f| f.to_le_bytes())
                .collect(),
            indices: [0u32, 1, 2].iter().flat_map(|i| i.to_le_bytes()).collect(),
            normals: Some(vec![0u8; 7]),
            ..default()
        };
        assert_eq!(node.positions(), vec![[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]);
        assert_eq!(node.indices(), vec![0, 1, 2]);
        assert_eq!(node.normals(), Some(Vec::new()));
    }
}
