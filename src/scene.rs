//! The resolved-layout JSON, and the one convention that comes with it.
//!
//! `arena_composer.resolve_layout` dumps a scene in **Arena's frame**: Z-up,
//! metres, quaternions as `xyzw`. Bevy is Y-up, so everything decoded here is
//! spawned under a single root carrying [`arena_to_bevy`] and can otherwise be
//! used verbatim — no per-value axis juggling.
//!
//! Only the fields the viewer draws are decoded. The rest of the dump (task,
//! relations, placement diagnostics) is deliberately ignored rather than
//! mirrored, so a change to those does not break deserialization here.

use bevy::prelude::*;
use serde::Deserialize;
use std::f32::consts::FRAC_PI_2;

/// Lays Arena's Z-up world down onto Bevy's Y-up one.
pub fn arena_to_bevy() -> Quat {
    Quat::from_rotation_x(-FRAC_PI_2)
}

#[derive(Debug, Clone, Deserialize)]
pub struct Scene {
    #[serde(default)]
    pub env_name: String,
    pub assets: Vec<Asset>,
    #[serde(default)]
    pub lighting: Lighting,
    #[serde(default)]
    pub viewer: Option<ViewerCfg>,
}

impl Scene {
    pub fn from_json(json: &str) -> anyhow::Result<Self> {
        Ok(serde_json::from_str(json)?)
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct Asset {
    pub id: String,
    /// `embodiment`, `background`, `object`, `object_set` or `object_reference`.
    pub role: String,
    /// The mirrored, openable copy of `usd_path`. `None` for procedurally
    /// spawned assets, which have no USD to project.
    #[serde(default)]
    pub local_usd_path: Option<String>,
    #[serde(default = "unit_scale")]
    pub scale: [f32; 3],
    pub position_xyz: [f32; 3],
    pub rotation_xyzw: [f32; 4],
    /// Root-relative bounds, used only to frame the camera.
    pub bbox_min: [f32; 3],
    pub bbox_max: [f32; 3],
}

impl Asset {
    /// The solved (or configured) pose, in Arena's frame.
    pub fn transform(&self) -> Transform {
        let [x, y, z, w] = self.rotation_xyzw;
        Transform {
            translation: Vec3::from(self.position_xyz),
            rotation: Quat::from_xyzw(x, y, z, w).normalize(),
            scale: Vec3::from(self.scale),
        }
    }

    /// The USD to project, or `None` when this asset draws nothing of its own.
    ///
    /// An object reference is a prim *inside* its parent's stage, so drawing it
    /// separately would double up geometry the background already contributes.
    pub fn usd_to_draw(&self) -> Option<&str> {
        if self.role == "object_reference" {
            return None;
        }
        self.local_usd_path.as_deref()
    }
}

/// Where light comes from. The graph spec has no lighting fields, so Arena
/// injects a dome light unless the scene supplies one — and a viewer that
/// wants to match what the sim renders has to do the same.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Lighting {
    #[serde(default)]
    pub auto_injected_dome_light: bool,
}

/// The background's suggested camera framing, in Arena's frame.
#[derive(Debug, Clone, Deserialize)]
pub struct ViewerCfg {
    pub eye: [f32; 3],
    pub lookat: [f32; 3],
}

fn unit_scale() -> [f32; 3] {
    [1.0; 3]
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The root rotation has to take Arena's up onto Bevy's, or the whole
    /// scene lies on its side.
    #[test]
    fn arena_up_maps_onto_bevy_up() {
        assert!((arena_to_bevy() * Vec3::Z).abs_diff_eq(Vec3::Y, 1e-6));
        assert!((arena_to_bevy() * Vec3::X).abs_diff_eq(Vec3::X, 1e-6));
    }

    /// Unknown keys are ignored and absent optional ones default, so the dump
    /// can grow fields without the viewer needing a matching change.
    #[test]
    fn a_minimal_asset_entry_decodes() {
        let scene: Scene = serde_json::from_str(
            r#"{"env_name":"t","assets":[{"id":"a","role":"object","future_field":1,
                "position_xyz":[1,2,3],"rotation_xyzw":[0,0,0,1],
                "bbox_min":[0,0,0],"bbox_max":[1,1,1]}]}"#,
        )
        .unwrap();
        let asset = &scene.assets[0];
        assert_eq!(asset.transform().translation, Vec3::new(1.0, 2.0, 3.0));
        assert_eq!(asset.transform().scale, Vec3::ONE);
        assert_eq!(asset.usd_to_draw(), None);
        assert!(!scene.lighting.auto_injected_dome_light);
    }
}
