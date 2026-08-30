//! Materials for a projected prim, replacing `usd_bevy`'s material route.
//!
//! Two things the Arena assets need that the built-in route doesn't do:
//!
//! * **Texture paths resolve.** A USD authors them relative to the layer that
//!   wrote them (`./textures/foo.png`); `usd_bevy` passes that straight to the
//!   [`AssetServer`], which looks under Bevy's own asset root and fails.
//!   openusd anchors asset paths during value resolution, so the correct path
//!   is already sitting on the value — [`resolved_paths`] goes back for it.
//! * **`GeomSubset` bindings apply.** Isaac's robots bind a material per face
//!   subset rather than per mesh, and a route that reads only the gprim's own
//!   binding leaves every link the placeholder white.

use std::collections::{HashMap, HashSet};

use bevy::prelude::*;
use openusd::sdf::{Path as SdfPath, Value};
use openusd::usd::Stage;
use usd_bevy::mesh::mesh_from_usd_subset;
use usd_bevy::read::geom::{read_double_sided, read_mesh};
use usd_bevy::read::shade::{ReadPreviewMaterial, read_material_binding, read_preview_material};
use usd_bevy::route::cache::intern_mesh;
use usd_bevy::route::{
    PrimRoute, RouteCtx, SchemaRegistry, audio, camera, coverage, curves, dome, geom, instancer,
    light, payload, physics, points, reflect, shapes, skel, xform,
};

/// [`SchemaRegistry::builtin`] with this module's material route in place of
/// `usd_bevy`'s. Insert it before [`usd_bevy::UsdPlugin`], which only installs
/// the built-in registry when none is present.
///
/// The list mirrors `builtin()`'s, order included — later routes read what
/// earlier ones wrote, and materials have to come after the geometry they
/// shade. `usd_bevy` is pinned to a revision, so it can only fall behind when
/// that pin moves.
pub fn registry() -> SchemaRegistry {
    let mut r = SchemaRegistry::new();
    r.register(xform::XformRoute);
    r.register(geom::VisibilityRoute);
    r.register(geom::MeshRoute);
    r.register(shapes::ShapesRoute);
    r.register(points::PointsRoute);
    r.register(curves::CurvesRoute);
    r.register(MaterialRoute);
    r.register(skel::SkinRoute);
    r.register(light::LightRoute);
    r.register(dome::DomeLightRoute);
    r.register(camera::CameraRoute);
    r.register(instancer::PointInstancerRoute);
    r.register(physics::PhysicsRoute);
    r.register(audio::SpatialAudioRoute);
    r.register(audio::VolumeRoute);
    r.register(coverage::RenderSettingsRoute);
    r.register(coverage::ProceduralRoute);
    r.register(coverage::BackdropRoute);
    r.register(payload::PayloadRoute);
    r.register(reflect::ReflectRoute);
    r
}

/// One mesh drawn for one `GeomSubset`. Marked so a reprojection can clear the
/// previous set rather than stack a second copy on top of it.
#[derive(Component)]
struct SubsetPiece;

/// A gprim's bound `UsdShade` Material(s) → [`StandardMaterial`].
pub struct MaterialRoute;

impl PrimRoute for MaterialRoute {
    fn matches(&self, ctx: &RouteCtx) -> bool {
        binding_of(ctx.stage, ctx.path).is_some() || has_subsets(ctx.stage, ctx.path)
    }

    fn project(&self, ctx: &RouteCtx, world: &mut World, entity: Entity) {
        // Nothing to attach a material to when running headless.
        if world.get_resource::<Assets<StandardMaterial>>().is_none() {
            return;
        }
        clear_subsets(world, entity);
        split_subsets(ctx, world, entity);

        // The gprim's own binding, which still shades whatever faces the
        // subsets left on it.
        let Some(binding) = binding_of(ctx.stage, ctx.path) else {
            return;
        };
        let double_sided = read_double_sided(ctx.stage, ctx.path).unwrap_or(false);
        let Some(handle) = material(ctx.stage, &binding, double_sided, world) else {
            return;
        };
        if let Some(mut slot) = world.get_mut::<MeshMaterial3d<StandardMaterial>>(entity) {
            slot.0 = handle;
        } else if let Ok(mut e) = world.get_entity_mut(entity) {
            e.insert(MeshMaterial3d(handle));
        }
    }
}

/// Draw one child mesh per bound `GeomSubset`.
///
/// USD binds these to a set of *faces*, which Bevy has no notion of — one mesh
/// carries one material — so the faces have to be split into meshes of their
/// own. Whatever the subsets don't claim stays on the prim's own mesh; when
/// they partition it (the usual case) that mesh goes away, or every face would
/// be drawn a second time in the placeholder white.
fn split_subsets(ctx: &RouteCtx, world: &mut World, entity: Entity) {
    if !has_subsets(ctx.stage, ctx.path) {
        return;
    }
    let Ok(Some(read)) = read_mesh(ctx.stage, ctx.path) else {
        return;
    };
    let bound: Vec<_> = read
        .subsets
        .iter()
        .filter(|s| s.material_binding.is_some() && !s.indices.is_empty())
        .collect();
    if bound.is_empty() {
        return;
    }

    for subset in &bound {
        let mesh = intern_mesh(world, mesh_from_usd_subset(&read, Some(&subset.indices)));
        let binding = subset.material_binding.as_ref().expect("filtered above");
        let material =
            material(ctx.stage, binding, read.double_sided, world).unwrap_or_else(|| {
                usd_bevy::route::cache::placeholder_material(world, read.double_sided)
            });
        world.spawn((
            Mesh3d(mesh),
            MeshMaterial3d(material),
            SubsetPiece,
            ChildOf(entity),
        ));
    }

    let claimed: HashSet<i32> = bound
        .iter()
        .flat_map(|s| s.indices.iter().copied())
        .collect();
    let rest: Vec<i32> = (0..read.face_vertex_counts.len() as i32)
        .filter(|face| !claimed.contains(face))
        .collect();
    let leftover =
        (!rest.is_empty()).then(|| intern_mesh(world, mesh_from_usd_subset(&read, Some(&rest))));
    let Ok(mut e) = world.get_entity_mut(entity) else {
        return;
    };
    match leftover {
        Some(mesh) => {
            e.insert(Mesh3d(mesh));
        }
        None => {
            e.remove::<Mesh3d>();
        }
    }
}

/// Despawn the subset meshes a previous projection of this prim spawned.
fn clear_subsets(world: &mut World, entity: Entity) {
    let children: Vec<Entity> = world
        .get::<Children>(entity)
        .map(|c| c.iter().collect())
        .unwrap_or_default();
    for child in children {
        if world.get::<SubsetPiece>(child).is_some() {
            world.entity_mut(child).despawn();
        }
    }
}

/// Decode a bound Material into a [`StandardMaterial`] asset.
fn material(
    stage: &Stage,
    binding: &SdfPath,
    double_sided: bool,
    world: &mut World,
) -> Option<Handle<StandardMaterial>> {
    let read = read_preview_material(stage, binding).ok().flatten()?;
    let server = world.get_resource::<AssetServer>().cloned();
    let mut material =
        to_standard_material(&read, &resolved_paths(stage, binding), server.as_ref());
    // Bevy back-face culls by default, so a double-sided surface has to turn
    // the cull mode off as well as set the flag. One-way: USD's default
    // matches Bevy's, so an absent opinion must not stomp the material.
    if double_sided {
        material.double_sided = true;
        material.cull_mode = None;
    }
    Some(
        world
            .resource_mut::<Assets<StandardMaterial>>()
            .add(material),
    )
}

fn to_standard_material(
    read: &ReadPreviewMaterial,
    resolved: &HashMap<String, String>,
    server: Option<&AssetServer>,
) -> StandardMaterial {
    let mut m = StandardMaterial::default();
    if let Some(c) = read.diffuse_color {
        m.base_color = Color::srgba(c[0], c[1], c[2], read.opacity.unwrap_or(1.0));
    } else if let Some(a) = read.opacity {
        m.base_color.set_alpha(a);
    }
    if read.opacity.is_some_and(|a| a < 1.0) {
        m.alpha_mode = AlphaMode::Blend;
    }
    if let Some(r) = read.roughness {
        m.perceptual_roughness = r;
    }
    if let Some(v) = read.metallic {
        m.metallic = v;
    }
    if let Some(e) = read.emissive_color {
        m.emissive = LinearRgba::rgb(e[0], e[1], e[2]);
    }
    if let Some(ior) = read.ior {
        m.ior = ior;
    }
    // USD applies `st' = rotate(scale·st) + translation`, which is glam's T·R·S.
    if let Some(uv) = &read.uv_transform {
        m.uv_transform = bevy::math::Affine2::from_scale_angle_translation(
            Vec2::from(uv.scale),
            uv.rotation_deg.to_radians(),
            Vec2::from(uv.translation),
        );
    }
    if let Some(server) = server {
        let load = |authored: &Option<String>| {
            let authored = authored.as_ref()?;
            let path = resolved.get(authored).unwrap_or(authored);
            Some(server.load(path.clone()))
        };
        m.base_color_texture = load(&read.diffuse_texture);
        m.normal_map_texture = load(&read.normal_texture);
        m.metallic_roughness_texture = load(&read.metallic_texture);
        m.emissive_texture = load(&read.emissive_texture);
        m.occlusion_texture = load(&read.occlusion_texture);
    }
    m
}

/// Every asset path in a Material's shader network, as authored → as resolved.
///
/// `read_preview_material` reports the authored string, so this walks the
/// network a second time for the anchored path openusd already worked out.
fn resolved_paths(stage: &Stage, material: &SdfPath) -> HashMap<String, String> {
    let mut out = HashMap::new();
    let mut stack = vec![material.clone()];
    while let Some(path) = stack.pop() {
        let prim = stage.prim(path.clone());
        for name in prim.property_names().unwrap_or_default() {
            if let Ok(Some(Value::AssetPath(asset))) = prim.attribute(name.as_str()).get::<Value>()
                && let Some(resolved) = asset.resolved_path()
            {
                out.insert(asset.authored_path.clone(), resolved.to_string());
            }
        }
        for child in prim.child_names().unwrap_or_default() {
            if let Ok(child) = path.append_path(child.as_str()) {
                stack.push(child);
            }
        }
    }
    out
}

fn binding_of(stage: &Stage, prim: &SdfPath) -> Option<SdfPath> {
    read_material_binding(stage, prim).ok().flatten()
}

/// Whether the prim has any `GeomSubset` child, without parsing its mesh —
/// [`read_mesh`] decodes every point and index, too much to spend on the
/// question of whether there is anything to split.
fn has_subsets(stage: &Stage, prim: &SdfPath) -> bool {
    stage
        .prim(prim.clone())
        .child_names()
        .unwrap_or_default()
        .iter()
        .any(|name| {
            prim.append_path(name.as_str()).is_ok_and(|child| {
                stage.prim(child).type_name().ok().flatten().as_deref() == Some("GeomSubset")
            })
        })
}

#[cfg(test)]
mod tests {
    use bevy::asset::{AssetPlugin, UnapprovedPathMode};
    use bevy::image::Image;

    use super::*;

    /// An app with somewhere to put bakes and load textures from; no window.
    fn headless() -> App {
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
        .insert_resource(registry())
        .add_plugins(usd_bevy::UsdPlugin);
        app
    }

    /// The whole point of [`resolved_paths`]: the `AssetServer` is handed the
    /// path openusd anchored, not the one the shader authored.
    #[test]
    fn a_texture_loads_from_the_path_usd_resolved_it_to() {
        let app = headless();
        let server = app.world().resource::<AssetServer>().clone();
        let read = ReadPreviewMaterial {
            diffuse_texture: Some("./textures/wood.png".into()),
            ..Default::default()
        };
        let resolved = HashMap::from([(
            "./textures/wood.png".to_string(),
            "/assets/oak/textures/wood.png".to_string(),
        )]);

        let material = to_standard_material(&read, &resolved, Some(&server));

        let handle = material.base_color_texture.expect("a base colour texture");
        let path = server.get_path(handle.id()).expect("a texture path");
        assert_eq!(path.path().to_str(), Some("/assets/oak/textures/wood.png"));
    }

    /// A material that resolved nothing keeps the authored path, which is what
    /// a texture named by an absolute path or a URL wants anyway.
    #[test]
    fn an_unresolved_texture_keeps_its_authored_path() {
        let app = headless();
        let server = app.world().resource::<AssetServer>().clone();
        let read = ReadPreviewMaterial {
            diffuse_texture: Some("/already/absolute.png".into()),
            ..Default::default()
        };

        let material = to_standard_material(&read, &HashMap::new(), Some(&server));

        let handle = material.base_color_texture.expect("a base colour texture");
        let path = server.get_path(handle.id()).expect("a texture path");
        assert_eq!(path.path().to_str(), Some("/already/absolute.png"));
    }

    /// Isaac's Franka links carry one material per face subset. Splitting them
    /// is the difference between a shaded robot and a white one, so check a
    /// real link ends up as several meshes that do not all look the same.
    ///
    /// Ignored because it needs the robot USD Arena composes on the fly; run
    /// `arena-resolve-layout environments/tasks/banana_in_bowl.yaml` first.
    #[test]
    #[ignore = "needs a generated robot USD in the isaaclab_arena cache"]
    fn geom_subsets_become_meshes_of_their_own() {
        let dir = format!(
            "{}/.cache/isaaclab_arena/usd/robot_on_stand",
            std::env::var("HOME").unwrap()
        );
        let robot = std::fs::read_dir(&dir)
            .ok()
            .and_then(|entries| {
                entries
                    .filter_map(Result::ok)
                    .map(|e| e.path())
                    .find(|p| p.extension().is_some_and(|e| e == "usd"))
            })
            .unwrap_or_else(|| panic!("no generated robot USD under {dir}"));

        let mut app = headless();
        let stage = Stage::open(robot.to_str().unwrap()).expect("opening the robot");
        let world = app.world_mut();
        let placement = world.spawn(Transform::default()).id();
        crate::spawn::spawn_asset(world, &stage, placement);

        let pieces: Vec<_> = world
            .query_filtered::<(&ChildOf, &MeshMaterial3d<StandardMaterial>), With<SubsetPiece>>()
            .iter(world)
            .map(|(parent, material)| (parent.parent(), material.0.clone()))
            .collect();
        assert!(pieces.len() > 1, "no mesh was split by its subsets");

        // The split is only worth anything if the pieces shade differently.
        let materials = world.resource::<Assets<StandardMaterial>>();
        let colours: HashSet<String> = pieces
            .iter()
            .filter_map(|(_, handle)| materials.get(handle))
            .map(|m| format!("{:?}", m.base_color))
            .collect();
        assert!(
            colours.len() > 1,
            "every subset came out the same colour: {colours:?}"
        );

        // And the faces they claim must not be drawn a second time underneath.
        let split_parents: HashSet<Entity> = pieces.iter().map(|(parent, _)| *parent).collect();
        for parent in split_parents {
            assert!(
                world.get::<Mesh3d>(parent).is_none(),
                "{parent} kept its whole mesh as well as its subsets"
            );
        }
    }
}
