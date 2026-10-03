//! Putting a layout in a world, and pointing a camera at it.
//!
//! Shared by the two front ends: the windowed viewer and the one-shot renderer
//! build the same scene the same way, and differ only in what they do with the
//! frames that come out.

use bevy::camera::ScalingMode;
use bevy::prelude::*;

use crate::scene::{Asset, Scene, arena_to_bevy};
use crate::spawn::spawn_geometry;

/// Parent of everything in the layout; holds the Z-up → Y-up correction so
/// every asset below it keeps the pose the solver reported, unaltered.
#[derive(Component)]
pub struct ArenaRoot;

/// The spec node a placement came from.
#[derive(Component)]
pub struct AssetId(pub String);

/// The layout on screen: every asset of the scene last spawned, drawable or
/// not. An object reference draws nothing of its own but is what an `on`
/// stands on, so anything that reasons about the layout wants it here too.
#[derive(Resource, Default)]
pub struct Placed(pub Vec<Asset>);

/// Leaves a fitted view this much room around the layout.
const MARGIN: f32 = 1.1;

/// Smallest extent a view is fitted to. A layout thinner than this along one
/// axis — a flat tabletop seen from the front — would otherwise be magnified
/// until nothing else fits in the frame.
const MIN_EXTENT: f32 = 0.5;

/// Spawn a layout under a fresh [`ArenaRoot`] and return it.
pub fn spawn_scene(world: &mut World, scene: &Scene) -> Entity {
    world.insert_resource(Placed(scene.assets().to_vec()));
    let root = world
        .spawn((
            ArenaRoot,
            Transform::from_rotation(arena_to_bevy()),
            Visibility::default(),
        ))
        .id();

    for asset in scene.assets() {
        let nodes = scene.geometry_of(asset);
        if nodes.is_empty() {
            continue;
        }
        let placement = world
            .spawn((
                AssetId(asset.id.clone()),
                asset.transform(),
                Visibility::default(),
                ChildOf(root),
            ))
            .id();
        spawn_geometry(world, nodes, placement);
    }
    root
}

/// Stand in for the dome light Arena injects at build time when neither the
/// spec nor the background USD supplies one, and for a light the spec lists as
/// an object, which is not drawn either. `payload.build_payload` sets the flag
/// for both.
///
/// Only those. Lights authored inside a USD are not extracted, so a background
/// that carries its own is drawn by whatever the assets carry — which is
/// nothing, today. It lights whatever is drawn, so a reload leaves it alone.
pub fn spawn_lighting(world: &mut World, scene: &Scene) {
    if scene.lighting.auto_injected_dome_light {
        world.spawn((
            DirectionalLight {
                illuminance: 6_000.0,
                ..default()
            },
            Transform::default().looking_to(Vec3::new(-0.4, -1.0, -0.6), Vec3::Y),
        ));
    }
}

/// Fill light, lifting the shadowed side of an asset enough to read its
/// silhouette. Per-camera in Bevy 0.19, not a resource, so every camera that
/// wants it has to carry its own.
pub fn ambient() -> AmbientLight {
    AmbientLight {
        brightness: 400.0,
        ..default()
    }
}

/// A named camera placement.
///
/// The axis views are orthographic on purpose. What they are asked is "does the
/// banana rest on the table or float above it" and "do these two overlap" —
/// under perspective both answers depend on where the object sits in the frame,
/// and under a parallel projection a gap is a gap.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum View {
    /// Perspective: the background's suggested framing, else a three-quarter
    /// view of the whole layout. What the window opens on.
    Default,
    /// Orthographic, straight down Arena's +Z. Answers placement in XY.
    Top,
    /// Orthographic, from Arena's -Y. Answers heights and gaps.
    Front,
    /// Orthographic, from Arena's +X. Answers heights and gaps.
    Side,
}

impl std::str::FromStr for View {
    type Err = String;

    fn from_str(name: &str) -> Result<Self, Self::Err> {
        match name {
            "default" => Ok(Self::Default),
            "top" => Ok(Self::Top),
            "front" => Ok(Self::Front),
            "side" => Ok(Self::Side),
            other => Err(format!(
                "unknown view {other:?}, expected default, top, front or side"
            )),
        }
    }
}

/// Where a camera goes for a view of a scene.
pub struct Framing {
    pub transform: Transform,
    pub projection: Projection,
    /// What the camera looks at, kept for the viewer's orbit pivot.
    pub focus: Vec3,
}

/// Frame `scene` for `view`, in Bevy's frame.
pub fn framing(view: View, scene: &Scene) -> Framing {
    // A scene with nothing drawable still has to produce a camera somewhere.
    let (min, max) = bounds(scene).unwrap_or((Vec3::splat(-0.5), Vec3::splat(0.5)));
    let focus = (min + max) / 2.0;
    let size = max - min;

    if view == View::Default {
        return perspective(scene, focus, size);
    }

    // The bounding sphere's diameter: far enough along any axis that the layout
    // sits wholly in front of the camera, so nothing is cut by the near plane.
    let distance = size.length().max(MIN_EXTENT);
    // Arena's +Z is Bevy's +Y and its +Y is Bevy's -Z, so a top view puts
    // Arena's +Y up the image and Arena's +X to the right.
    let (direction, up, extent) = match view {
        View::Top => (Vec3::Y, Vec3::NEG_Z, Vec2::new(size.x, size.z)),
        View::Front => (Vec3::Z, Vec3::Y, Vec2::new(size.x, size.y)),
        View::Side => (Vec3::X, Vec3::Y, Vec2::new(size.z, size.y)),
        View::Default => unreachable!("handled above"),
    };

    Framing {
        transform: Transform::from_translation(focus + direction * distance).looking_at(focus, up),
        projection: Projection::Orthographic(OrthographicProjection {
            // AutoMin fits both extents whatever the image's aspect ratio, and
            // never stretches the picture to do it.
            scaling_mode: ScalingMode::AutoMin {
                min_width: (extent.x * MARGIN).max(MIN_EXTENT),
                min_height: (extent.y * MARGIN).max(MIN_EXTENT),
            },
            // The camera stands `distance` off; twice that clears the layout
            // behind the focus as well.
            far: distance * 2.0,
            ..OrthographicProjection::default_3d()
        }),
        focus,
    }
}

/// The perspective view: what the background suggests, else a three-quarter
/// view sized to the layout.
fn perspective(scene: &Scene, focus: Vec3, size: Vec3) -> Framing {
    if let Some(viewer) = &scene.viewer {
        let to_bevy = arena_to_bevy();
        let (eye, lookat) = (
            to_bevy * Vec3::from(viewer.eye),
            to_bevy * Vec3::from(viewer.lookat),
        );
        return Framing {
            transform: Transform::from_translation(eye).looking_at(lookat, Vec3::Y),
            projection: Projection::default(),
            focus: lookat,
        };
    }
    // 1.6x the diagonal keeps the whole layout inside a default 45° FOV with
    // room to spare, and never collapses onto a single-point scene.
    let distance = size.length().max(MIN_EXTENT) * 1.6;
    let eye = focus + Vec3::new(0.7, 0.55, 1.0).normalize() * distance;
    Framing {
        transform: Transform::from_translation(eye).looking_at(focus, Vec3::Y),
        projection: Projection::default(),
        focus,
    }
}

/// The layout's world-space bounds in Bevy's frame, over the assets that are
/// drawn. `None` when nothing is.
pub fn bounds(scene: &Scene) -> Option<(Vec3, Vec3)> {
    let to_bevy = arena_to_bevy();
    let mut min = Vec3::splat(f32::INFINITY);
    let mut max = Vec3::splat(f32::NEG_INFINITY);
    let mut any = false;
    for asset in scene
        .assets()
        .iter()
        .filter(|asset| !scene.geometry_of(asset).is_empty())
    {
        let (lo, hi) = world_aabb(asset);
        // The frame change only permutes and flips axes, so a box stays a box
        // and its two corners are enough — sorted back into min and max.
        for point in [to_bevy * lo, to_bevy * hi] {
            min = min.min(point);
            max = max.max(point);
        }
        any = true;
    }
    any.then_some((min, max))
}

/// An asset's world-space bounds in Arena's frame: the reported local box
/// under the solved pose, refitted to the axes — the box the placement solver
/// constrained.
///
/// The pose's `scale` is left out on purpose. Arena bakes `Object.scale` into
/// the box it reports, so applying it here would size the box twice.
pub fn world_aabb(asset: &Asset) -> (Vec3, Vec3) {
    let pose = Transform {
        scale: Vec3::ONE,
        ..asset.transform()
    };
    let mut min = Vec3::splat(f32::INFINITY);
    let mut max = Vec3::splat(f32::NEG_INFINITY);
    for corner in corners((Vec3::from(asset.bbox_min), Vec3::from(asset.bbox_max))) {
        let point = pose.transform_point(corner);
        min = min.min(point);
        max = max.max(point);
    }
    (min, max)
}

/// The eight corners of a box.
pub fn corners((lo, hi): (Vec3, Vec3)) -> impl Iterator<Item = Vec3> {
    (0..8).map(move |i| {
        Vec3::new(
            if i & 1 == 0 { lo.x } else { hi.x },
            if i & 2 == 0 { lo.y } else { hi.y },
            if i & 4 == 0 { lo.z } else { hi.z },
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scene::{Node as GeometryNode, Rotation, Scale};
    use std::f32::consts::FRAC_1_SQRT_2;

    /// The extents an orthographic framing was fitted to. `ScalingMode` carries
    /// no `PartialEq`, hence the unpacking.
    fn fitted(projection: &Projection) -> Vec2 {
        let Projection::Orthographic(orthographic) = projection else {
            panic!("not an orthographic projection");
        };
        let ScalingMode::AutoMin {
            min_width,
            min_height,
        } = orthographic.scaling_mode
        else {
            panic!("not fitted to the layout");
        };
        Vec2::new(min_width, min_height)
    }

    /// One asset at `position` with the given local bounds. `drawable` decides
    /// whether it points at a geometry entry — the node itself is degenerate,
    /// since none of these tests upload a mesh.
    pub fn scene(position: [f32; 3], bbox: ([f32; 3], [f32; 3]), drawable: bool) -> Scene {
        Scene {
            env_name: "t".into(),
            assets: Some(vec![Asset {
                id: "a".into(),
                role: "object".into(),
                position_xyz: position,
                bbox_min: bbox.0,
                bbox_max: bbox.1,
                geometry: drawable.then_some(0),
                ..default()
            }]),
            geometries: vec![vec![GeometryNode::default()]],
            ..default()
        }
    }

    /// A 1 m cube sitting 1 m up in Arena's frame is 1 m up in Bevy's too,
    /// and the camera looks at its centre rather than the world origin.
    #[test]
    fn framing_centres_on_the_layout() {
        let framed = framing(View::Default, &scene([0.0, 0.0, 1.0], ([-0.5; 3], [0.5; 3]), true));
        assert!(framed.focus.abs_diff_eq(Vec3::Y, 1e-5), "{}", framed.focus);
        assert!(
            framed.transform.translation.distance(framed.focus) > 1.0,
            "camera sits off the subject"
        );
    }

    /// An asset with nothing to draw must not drag the framing towards the
    /// origin — an empty scene falls back to a fixed view instead.
    #[test]
    fn undrawable_assets_are_left_out_of_the_bounds() {
        assert!(bounds(&scene([10.0; 3], ([0.0; 3], [1.0; 3]), false)).is_none());
    }

    /// The box the solver constrained: the local bounds turned by the pose and
    /// refitted, and not scaled again — Arena reports them scaled already.
    #[test]
    fn world_bounds_follow_the_pose_but_not_the_scale() {
        let mut scene = scene([1.0, 0.0, 0.0], ([0.0; 3], [2.0, 1.0, 1.0]), true);
        let asset = &mut scene.assets.as_mut().unwrap()[0];
        // A quarter turn about Z takes the box's +X extent onto +Y.
        asset.rotation_xyzw = Rotation([0.0, 0.0, FRAC_1_SQRT_2, FRAC_1_SQRT_2]);
        asset.scale = Scale([2.0; 3]);
        let (min, max) = world_aabb(asset);
        assert!(min.abs_diff_eq(Vec3::ZERO, 1e-5), "{min}");
        assert!(max.abs_diff_eq(Vec3::new(1.0, 2.0, 1.0), 1e-5), "{max}");
    }

    /// Every axis view looks at the layout from outside it, along its own axis,
    /// and does so without perspective.
    #[test]
    fn axis_views_look_down_their_axis() {
        // A 2 m cube centred 1 m up in Arena's frame, i.e. on Bevy's +Y.
        let scene = scene([0.0, 0.0, 1.0], ([-1.0; 3], [1.0; 3]), true);
        for (view, axis) in [
            (View::Top, Vec3::Y),
            (View::Front, Vec3::Z),
            (View::Side, Vec3::X),
        ] {
            let framed = framing(view, &scene);
            let offset = framed.transform.translation - framed.focus;
            assert!(
                offset.normalize().abs_diff_eq(axis, 1e-5),
                "{view:?} sits at {offset}, not along {axis}"
            );
            // Outside the 2 m cube, so nothing is clipped by the near plane.
            assert!(offset.length() > 1.0, "{view:?} is inside the layout");
            assert!(
                framed.transform.forward().abs_diff_eq(-axis, 1e-5),
                "{view:?} is not looking back down its axis"
            );
            // Fitted to the 2 m cube, with the margin on top.
            let fitted = fitted(&framed.projection);
            assert!(
                fitted.abs_diff_eq(Vec2::splat(2.0 * MARGIN), 1e-5),
                "{view:?} fitted to {fitted}"
            );
        }
    }

    /// A layout flat along one axis still gets a usable frame rather than one
    /// magnified until nothing fits.
    #[test]
    fn a_flat_layout_is_not_magnified() {
        // A tabletop: 2 m square, no thickness. Seen from the front it is a line.
        let framed = framing(
            View::Front,
            &scene([0.0; 3], ([-1.0, -1.0, 0.0], [1.0, 1.0, 0.0]), true),
        );
        let fitted = fitted(&framed.projection);
        assert!(
            fitted.abs_diff_eq(Vec2::new(2.0 * MARGIN, MIN_EXTENT), 1e-5),
            "fitted to {fitted}"
        );
    }

    /// View names come in from Python as strings, and a typo has to say so.
    #[test]
    fn view_names_parse() {
        assert_eq!("top".parse(), Ok(View::Top));
        assert_eq!("default".parse(), Ok(View::Default));
        assert!("topdown".parse::<View>().is_err());
    }
}
