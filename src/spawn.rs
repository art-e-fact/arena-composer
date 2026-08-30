//! Projecting one asset's USD stage into entities under its placement.
//!
//! This is [`usd_bevy::live::project_stage`] with three changes. Two follow
//! from a scene placing several assets, so prim paths are not unique in this
//! world and there is no single stage to be the source of truth: the prims are
//! parented under a caller-supplied entity instead of a global stage root, and
//! the path→entity map stays local instead of populating `PrimEntities`. The
//! third is that only the default prim's subtree is drawn — see
//! [`default_prim_path`]. Everything below the traversal is `usd_bevy`'s:
//! [`SchemaRegistry::project_prim`] does meshes, materials, transforms,
//! visibility and purpose for one prim.

use std::collections::HashMap;

use bevy::prelude::*;
use openusd::usd::{PrimPredicate, PrimStatus, Stage};
use usd_bevy::SchemaRegistry;

/// The prim subtree's parent: carries the stage's own axis and unit
/// convention, kept off the placement entity so the solved pose stays clean.
#[derive(Component)]
pub struct StageRoot;

/// Project every prim of `stage` into entities parented under `placement`.
///
/// `placement` keeps its own `Transform` — the solved pose, in Arena's frame.
/// The convention correction goes on an intermediate [`StageRoot`] child.
pub fn spawn_asset(world: &mut World, stage: &Stage, placement: Entity) {
    let registry = world
        .get_resource::<SchemaRegistry>()
        .cloned()
        .unwrap_or_else(SchemaRegistry::builtin);

    let stage_root = world
        .spawn((
            StageRoot,
            convention(stage),
            Visibility::default(),
            ChildOf(placement),
        ))
        .id();

    let root_prim = default_prim_path(stage);
    let mut entities: HashMap<String, Entity> = HashMap::new();
    let _ = stage.traverse(predicate(), |path: &openusd::sdf::Path| {
        if !under(path.as_str(), root_prim.as_deref()) {
            return;
        }
        // Pre-order, so the namespace parent's entity already exists.
        let parent = entities
            .get(parent_path(path.as_str()))
            .copied()
            .unwrap_or(stage_root);
        let entity = world.spawn(ChildOf(parent)).id();
        entities.insert(path.as_str().to_string(), entity);
        registry.project_prim(stage, path, world, entity);
    });

    unbake_default_prim_scale(world, stage, &entities);
    debug!("projected {} prims under {placement}", entities.len());
}

/// `/<defaultPrim>`, the only subtree Isaac Lab composes in when it spawns an
/// asset: referencing a USD without a prim path targets its default prim, so
/// everything beside that prim is authoring the file kept to itself.
///
/// Skipping the rest is what keeps a Kit-authored asset's leftovers out of the
/// scene — `/Render` settings and viewport cameras, and, in a couple of the
/// object assets, a 50 m collision ground plane that would otherwise be drawn
/// wherever the solver placed a bagel. `None` (no default prim) draws it all.
fn default_prim_path(stage: &Stage) -> Option<String> {
    stage
        .default_prim()
        .map(|name| format!("/{}", name.as_str()))
}

/// Whether `path` is `root` or below it. Namespace-aware: `/bagel_00` is not
/// under `/bagel`.
fn under(path: &str, root: Option<&str>) -> bool {
    let Some(root) = root else {
        return true;
    };
    path == root
        || path
            .strip_prefix(root)
            .is_some_and(|rest| rest.starts_with('/'))
}

/// The stage's axis and unit convention, expressed **in Arena's frame**.
///
/// Not the same job as a general USD viewer's: Isaac Lab references assets into
/// a Z-up metres stage without correcting either, so Z-up metres is what an
/// asset is assumed to be unless it says otherwise. USD's own fallbacks (Y-up,
/// centimetres) would be wrong here — Arena composes robot USDs on the fly and
/// authors neither field, which under USD's fallbacks lands the robot on its
/// side at 1/100 scale.
fn convention(stage: &Stage) -> Transform {
    let up_y = matches!(
        stage.stage_metadata("upAxis").ok().flatten(),
        Some(openusd::sdf::Value::Token(t)) if t.as_str() == "Y"
    );
    // Y-up content stood up into the scene's Z-up frame.
    let rotation = if up_y {
        Quat::from_rotation_x(core::f32::consts::FRAC_PI_2)
    } else {
        Quat::IDENTITY
    };

    let scale = match stage.stage_metadata("metersPerUnit").ok().flatten() {
        Some(openusd::sdf::Value::Double(m)) if m > 0.0 => m as f32,
        _ => 1.0,
    };

    Transform::from_rotation(rotation).with_scale(Vec3::splat(scale))
}

/// Drop the default prim's own `xformOp:scale`, which Isaac Lab's spawner
/// ignores — the `scale` the resolver reports is the only one that applies.
///
/// Matching this matters beyond looks: `resolve_layout`'s bounding boxes are
/// computed with the same op unbaked, so leaving it in makes the geometry and
/// the numbers the solver placed disagree. Clearing the entity's scale is
/// exactly right because Bevy composes a transform as `T·R·S`, so the
/// translate and rotate ops are left untouched.
fn unbake_default_prim_scale(world: &mut World, stage: &Stage, entities: &HashMap<String, Entity>) {
    let Some(default_prim) = stage.default_prim() else {
        return;
    };
    let Some(entity) = entities.get(&format!("/{}", default_prim.as_str())) else {
        return;
    };
    if let Some(mut transform) = world.get_mut::<Transform>(*entity) {
        transform.scale = Vec3::ONE;
    }
}

/// Active, defined, non-abstract prims, descending into instance subtrees.
///
/// Abstract prims are `class` prototype libraries, which must not draw.
///
/// The instance proxies are the part that is not optional. SimReady assets
/// author every piece of geometry as `instanceable = true` with a sub-root
/// reference into a shared `instances.usda`, so the composed prim carries no
/// children of its own — its subtree belongs to a prototype (spec 11.3.3), and
/// a traversal that stops at instances walks the asset's whole skeleton and
/// finds not one mesh. Descending costs a separate projection per instance,
/// but `usd_bevy`'s `ProjectionCache` interns the baked meshes, so identical
/// prims still share one GPU asset.
fn predicate() -> PrimPredicate {
    PrimPredicate::new(
        PrimStatus::ACTIVE.union(PrimStatus::DEFINED),
        PrimStatus::ABSTRACT,
    )
    .with_instance_proxies(true)
}

/// The namespace parent of a prim path; `/` for a top-level prim.
fn parent_path(path: &str) -> &str {
    match path.rfind('/') {
        Some(0) | None => "/",
        Some(i) => &path[..i],
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;

    #[test]
    fn parent_path_walks_up_to_the_pseudo_root() {
        assert_eq!(parent_path("/World/Prop/Mesh"), "/World/Prop");
        assert_eq!(parent_path("/World"), "/");
        assert_eq!(parent_path("/"), "/");
    }

    /// A sibling of the default prim is somebody else's business, and a prim
    /// whose name merely starts the same way is not a descendant.
    #[test]
    fn only_the_default_prims_subtree_is_drawn() {
        assert!(under("/bagel", Some("/bagel")));
        assert!(under("/bagel/Geometry/mesh", Some("/bagel")));
        assert!(!under("/GroundPlane/CollisionMesh", Some("/bagel")));
        assert!(!under("/bagel_00", Some("/bagel")));
        // A stage without a default prim draws whatever it has.
        assert!(under("/GroundPlane", None));
    }

    fn stage_with(metadata: &str) -> Stage {
        let stage = Stage::builder().in_memory("conv.usda").unwrap();
        let id = stage.root_layer().identifier().to_string();
        let mut layer = stage.layer_mut(&id).unwrap();
        layer
            .edit(|edit| {
                let mut root = edit.pseudo_root_mut()?;
                if metadata.contains('Y') {
                    root.set("upAxis", openusd::sdf::Value::token("Y"));
                }
                if let Some(m) = metadata.strip_prefix("m=") {
                    root.set(
                        "metersPerUnit",
                        openusd::sdf::Value::Double(m.parse().unwrap()),
                    );
                }
                Ok(())
            })
            .unwrap();
        drop(layer);
        stage
    }

    /// A Y-up asset has to be stood up into Arena's Z-up world.
    #[test]
    fn a_y_up_stage_is_rotated_onto_z_up() {
        let up = convention(&stage_with("Y")).rotation * Vec3::Y;
        assert!(
            up.abs_diff_eq(Vec3::Z, 1e-5),
            "asset +Y maps to +Z, got {up}"
        );
    }

    /// The common case, and the one Arena's own generated USDs land in by
    /// authoring no stage metadata at all.
    #[test]
    fn an_unauthored_or_z_up_stage_is_left_alone() {
        assert_eq!(convention(&stage_with("")).rotation, Quat::IDENTITY);
        assert_eq!(convention(&stage_with("")).scale, Vec3::ONE);
    }

    /// A centimetre stage placed without its scale would be 100x too big.
    #[test]
    fn meters_per_unit_becomes_a_uniform_scale() {
        assert_eq!(convention(&stage_with("m=0.01")).scale, Vec3::splat(0.01));
    }

    /// The whole draw path minus the window: open a real mirrored asset,
    /// project it, and check the world got renderable geometry whose texture
    /// resolves to a file that is actually there.
    ///
    /// Ignored because it needs the asset cache `arena-resolve-layout`
    /// populates; run that on `environments/tasks/banana_in_bowl.yaml` first.
    #[test]
    #[ignore = "needs a mirrored asset in the arena-composer cache"]
    fn a_real_asset_projects_with_a_resolvable_texture() {
        use bevy::asset::{AssetPlugin, UnapprovedPathMode};
        use bevy::image::Image;

        let cache = std::env::var("ARENA_COMPOSER_ASSET_CACHE").unwrap_or_else(|_| {
            format!(
                "{}/.cache/arena-composer/usd",
                std::env::var("HOME").unwrap()
            )
        });
        let entry = PathBuf::from(cache)
            .join("Assets/Isaac/6.0/Isaac/IsaacLab/Arena/assets/object_library")
            .join("srl_robolab_assets/objects/ycb/banana.usd");
        assert!(
            entry.is_file(),
            "populate the cache first: arena-resolve-layout environments/tasks/banana_in_bowl.yaml"
        );

        // Enough of an app for the routes to have somewhere to put their
        // bakes; no window, no renderer.
        let mut app = App::new();
        app.add_plugins((
            MinimalPlugins,
            AssetPlugin {
                unapproved_path_mode: UnapprovedPathMode::Allow,
                ..default()
            },
        ))
        .init_asset::<Mesh>()
        .init_asset::<StandardMaterial>()
        .init_asset::<Image>()
        .insert_resource(crate::material::registry())
        .add_plugins(usd_bevy::UsdPlugin);

        let stage = Stage::open(entry.to_str().unwrap()).expect("opening the asset");
        let placement = app.world_mut().spawn(Transform::default()).id();
        spawn_asset(app.world_mut(), &stage, placement);

        let world = app.world_mut();
        let meshes = world
            .query_filtered::<Entity, With<Mesh3d>>()
            .iter(world)
            .count();
        assert!(meshes > 0, "projecting the asset produced no `Mesh3d`");

        // Every texture the material kept must point at a real file — the USD
        // authors them relative to its own directory, not Bevy's asset root.
        let handles: Vec<_> = world
            .query::<&MeshMaterial3d<StandardMaterial>>()
            .iter(world)
            .map(|m| m.0.clone())
            .collect();
        let server = world.resource::<AssetServer>().clone();
        let materials = world.resource::<Assets<StandardMaterial>>();
        let mut checked = 0;
        for texture in handles
            .iter()
            .filter_map(|h| materials.get(h))
            .filter_map(|m| m.base_color_texture.as_ref())
        {
            let path = server.get_path(texture.id()).expect("a texture path");
            assert!(path.path().is_file(), "unresolved texture {path:?}");
            checked += 1;
        }
        assert!(checked > 0, "the banana lost its texture binding entirely");
    }
}
