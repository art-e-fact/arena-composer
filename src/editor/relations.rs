//! Drawing a relation over the layout, with gizmos.
//!
//! What a relation *means* is a piece of geometry — the surface `on` lands on,
//! the side `next_to` picks, the point `at_position` pins — and the panel shows
//! it while a relation is hovered or pinned. Each shape is the one Arena's loss
//! strategy scores (`isaaclab_arena/relations/relation_loss_strategies.py`),
//! rebuilt from the poses and bounds the resolver reported, so what is drawn is
//! what the solver saw.
//!
//! Everything here is computed in Arena's frame — Z-up, metres — and [`Draw`]
//! turns each shape onto Bevy's Y-up axes as it is drawn, with the rotation the
//! scene root carries. Nothing here edits anything.

use bevy::color::palettes::css;
use bevy::prelude::*;

use crate::layout::{Placed, corners, world_aabb};
use crate::scene::{Asset, arena_to_bevy};
use crate::spec::{Kind, Params, Relation, Value};

/// The group relations draw with, so their style is their own: over the
/// geometry, since half the shapes lie flat on a surface or under an object,
/// where a depth test would hide them.
#[derive(Default, Reflect, GizmoConfigGroup)]
pub struct RelationGizmos;

/// With the bias at -1 every line lands on the near plane, so where two lines
/// coincide the one drawn *first* wins the depth test: draw what matters most
/// first. Only among plain lines, though — strips are a draw of their own.
pub fn configure(store: &mut GizmoConfigStore) {
    let (config, _) = store.config_mut::<RelationGizmos>();
    config.depth_bias = -1.0;
    config.line.width = 2.0;
}

/// The relation's subject: its bounds, its heading.
const SUBJECT: Srgba = css::ORANGE;
/// Where the relation wants the subject: a surface, a target, a region.
const REGION: Srgba = css::AQUA;
/// Where it forbids it.
const FORBIDDEN: Srgba = css::RED;
/// A dimension the relation leaves alone.
const UNSET: Srgba = css::GRAY;
/// The world axes in their usual colours. Roll, pitch and yaw take the colour
/// of the axis they turn about.
const AXES: [Srgba; 3] = [css::RED, css::LIME, css::DODGER_BLUE];

/// `NotNextToLossStrategy.margin_m`: how far the keep-out zone reaches past the
/// parent's footprint. The strategy's default, since the spec does not carry it.
const NOT_NEXT_TO_MARGIN_M: f32 = 0.1;

/// Axis-aligned bounds, `(min, max)`.
type Aabb = (Vec3, Vec3);

/// Draw `relation` over the layout — or nothing, where its subject has yet to
/// be placed: the form can be an edit ahead of the layout.
pub fn draw(relation: &Relation, kind: Option<&Kind>, placed: &Placed, gizmos: &mut Gizmos<RelationGizmos>) {
    let Some(subject) = find(placed, &relation.subject) else { return };
    let reference = relation.reference.as_deref().and_then(|id| find(placed, id));
    let args = Args { params: &relation.params, kind };
    let mut draw = Draw { gizmos, to_bevy: arena_to_bevy() };

    let bounds = world_aabb(subject);
    let origin = Vec3::from(subject.position_xyz);
    // The box the solver kept clear, and the origin that the limits and the
    // keep-out zone are about.
    draw.cuboid(bounds, SUBJECT);
    draw.cross(origin, 0.02, SUBJECT);

    match relation.kind.as_str() {
        "on" => {
            if let Some(support) = reference.map(world_aabb).and_then(|parent| support(&args, parent)) {
                draw.face(support, 2, support.0.z, REGION);
            }
        }
        "next_to" => {
            let target = reference.and_then(|parent| next_to_target(&args, origin, bounds, world_aabb(parent)));
            if let Some([line_from, line_to, gap_from, gap_to]) = target {
                draw.line(line_from, line_to, REGION);
                draw.arrow(gap_from, gap_to, REGION);
            }
        }
        "not_next_to" => {
            let zone = reference.and_then(|parent| keep_out(&args, origin, bounds, world_aabb(parent), extent(placed)));
            if let Some((min, max)) = zone {
                // Crossed out, so red reads as forbidden and not as a target.
                draw.face((min, max), 2, min.z, FORBIDDEN);
                draw.line(min, max, FORBIDDEN);
                draw.line(Vec3::new(min.x, max.y, min.z), Vec3::new(max.x, min.y, min.z), FORBIDDEN);
            }
        }
        "face_to" => {
            heading(&mut draw, subject, bounds);
            if let Some(target) = reference {
                draw.line(origin, center(world_aabb(target)), REGION);
            }
        }
        "at_position" => at_position(&mut draw, &args, origin, reach(bounds)),
        "position_limits_box" => limits_box(&mut draw, &args, extent(placed)),
        "position_limits_cylindrical" => limits_cylinder(&mut draw, &args, origin),
        "random_around_solution" => random_around(&mut draw, &args, subject, bounds),
        "rotate_around_solution" => {
            heading(&mut draw, subject, bounds);
            rotate_around(&mut draw, &args, origin, reach(bounds));
        }
        // A kind with no shape of its own here: an arrow to what it refers to,
        // or a ring under the subject where it refers to nothing.
        _ => match reference {
            Some(target) => draw.arrow(center(bounds), center(world_aabb(target)), REGION),
            None if kind.is_none_or(|kind| kind.unary != Some(false)) => ring(&mut draw, bounds, REGION),
            None => {}
        },
    }
}

fn find<'a>(placed: &'a Placed, id: &str) -> Option<&'a Asset> {
    placed.0.iter().find(|asset| asset.id == id)
}

fn center((min, max): Aabb) -> Vec3 {
    (min + max) / 2.0
}

/// Half the footprint's diagonal: a radius that clears the subject from above.
fn reach((min, max): Aabb) -> f32 {
    (max - min).truncate().length() / 2.0
}

/// The layout's bounds, in Arena's frame.
fn extent(placed: &Placed) -> Aabb {
    placed.0.iter().map(world_aabb).fold(
        (Vec3::splat(f32::INFINITY), Vec3::splat(f32::NEG_INFINITY)),
        |(lo, hi), (min, max)| (lo.min(min), hi.max(max)),
    )
}

/// Where the subject faces: its local +X.
fn facing(asset: &Asset) -> Vec3 {
    asset.transform().rotation * Vec3::X
}

/// A `Side`: the axis it lies along, which way, and the axis across it.
fn side(name: &str) -> Option<(usize, f32, usize)> {
    Some(match name {
        "positive_x" => (0, 1.0, 1),
        "negative_x" => (0, -1.0, 1),
        "positive_y" => (1, 1.0, 0),
        "negative_y" => (1, -1.0, 0),
        _ => return None,
    })
}

/// A point `along` axis `a` and `across` axis `c`, at height `z`.
fn at(a: usize, along: f32, c: usize, across: f32, z: f32) -> Vec3 {
    let mut point = Vec3::new(0.0, 0.0, z);
    point[a] = along;
    point[c] = across;
    point
}

/// A relation's parameters as the loss reads them: what the spec sets, else
/// what the kind's constructor defaults to. An explicit `null` is unset.
struct Args<'a> {
    params: &'a Params,
    kind: Option<&'a Kind>,
}

impl Args<'_> {
    fn get(&self, name: &str) -> Option<&Value> {
        self.params.get(name).or_else(|| {
            let field = self.kind?.fields.iter().find(|field| field.name == name)?;
            Some(&field.default)
        })
    }

    fn number(&self, name: &str) -> Option<f32> {
        self.get(name)?.as_number().map(|value| value as f32)
    }

    fn text(&self, name: &str) -> Option<&str> {
        self.get(name)?.as_text()
    }

    fn flag(&self, name: &str) -> bool {
        matches!(self.get(name), Some(Value::Bool(true)))
    }
}

/// The `on` support: the parent's footprint inset by the margin, at the height
/// the subject's bottom lands on. `overlap` asks for intersection rather than
/// containment, so the inset goes. `None` once the margin has eaten the whole
/// footprint, which no placement satisfies.
fn support(args: &Args, (parent_min, parent_max): Aabb) -> Option<Aabb> {
    let margin = if args.flag("overlap") { 0.0 } else { args.number("edge_margin_m").unwrap_or(0.0) };
    let z = parent_max.z + args.number("clearance_m").unwrap_or(0.0);
    let min = Vec3::new(parent_min.x + margin, parent_min.y + margin, z);
    let max = Vec3::new(parent_max.x - margin, parent_max.y - margin, z);
    (min.x <= max.x && min.y <= max.y).then_some((min, max))
}

/// Where `next_to` wants the subject: the line its near face should touch,
/// along the parent's whole side, and the gap from the parent's edge to that
/// line at the target position across — `[line from, line to, gap from, gap
/// to]`, at the subject's own height.
fn next_to_target(args: &Args, origin: Vec3, (s_min, s_max): Aabb, (p_min, p_max): Aabb) -> Option<[Vec3; 4]> {
    let (a, dir, c) = side(args.text("side")?)?;
    let edge = if dir > 0.0 { p_max[a] } else { p_min[a] };
    let face = edge + dir * args.number("distance_m").unwrap_or(0.0);
    // The loss places the origin across the side between the two positions
    // that keep the footprint inside the parent's, -1 for one end and 1 for
    // the other; the arrow sits at the footprint's centre for that origin.
    let t = (args.number("cross_position_ratio").unwrap_or(0.0) + 1.0) / 2.0;
    let (lo, hi) = (p_min[c] - (s_min[c] - origin[c]), p_max[c] - (s_max[c] - origin[c]));
    let across = lo + t * (hi - lo) + ((s_min[c] + s_max[c]) / 2.0 - origin[c]);
    let z = (s_min.z + s_max.z) / 2.0;
    Some([
        at(a, face, c, p_min[c], z),
        at(a, face, c, p_max[c], z),
        at(a, edge, c, across, z),
        at(a, face, c, across, z),
    ])
}

/// The zone `not_next_to` keeps the subject's origin out of, at the origin's
/// height: past the parent's edge less the margin, and across the side
/// between the two origins that keep the subject's footprint within the
/// parent's, widened by the margin. Out to the end of the layout, since Arena
/// leaves it unbounded. `None` when the subject is too wide for the band to
/// exist at all.
fn keep_out(args: &Args, origin: Vec3, (s_min, s_max): Aabb, (p_min, p_max): Aabb, (lo, hi): Aabb) -> Option<Aabb> {
    let (a, dir, c) = side(args.text("side")?)?;
    let margin = NOT_NEXT_TO_MARGIN_M;
    let near = if dir > 0.0 { p_max[a] - margin } else { p_min[a] + margin };
    let far = if dir > 0.0 { hi[a].max(near) + margin } else { lo[a].min(near) - margin };
    let band = (p_min[c] - (s_min[c] - origin[c]) - margin, p_max[c] - (s_max[c] - origin[c]) + margin);
    if band.0 >= band.1 {
        return None;
    }
    let mut min = Vec3::new(0.0, 0.0, origin.z);
    let mut max = min;
    (min[a], max[a]) = (near.min(far), near.max(far));
    (min[c], max[c]) = band;
    Some((min, max))
}

/// The pinned point as a translate gizmo: an arrow per axis, coloured where
/// the relation sets that coordinate and grey where it leaves it to the solver
/// — for those the point takes the subject's own position.
fn at_position(draw: &mut Draw, args: &Args, origin: Vec3, reach: f32) {
    let set = ["x", "y", "z"].map(|name| args.number(name));
    let mut point = origin;
    for (axis, value) in set.iter().enumerate() {
        if let Some(value) = value {
            point[axis] = *value;
        }
    }
    draw.cross(point, 0.02, REGION);
    for axis in 0..3 {
        let mut along = Vec3::ZERO;
        along[axis] = reach.max(0.1);
        draw.arrow(point, point + along, if set[axis].is_some() { AXES[axis] } else { UNSET });
    }
}

/// The box the origin must stay in: the set bounds as walls, and the rest as a
/// grey frame out to the edge of the layout, where an unset bound leaves it
/// open. The origin, not the bounds — an object can still poke out of it.
fn limits_box(draw: &mut Draw, args: &Args, (mut min, mut max): Aabb) {
    let names = [["x_min", "x_max"], ["y_min", "y_max"], ["z_min", "z_max"]];
    let mut walls = Vec::new();
    for (axis, [low, high]) in names.into_iter().enumerate() {
        if let Some(value) = args.number(low) {
            min[axis] = value;
            walls.push((axis, value));
        }
        if let Some(value) = args.number(high) {
            max[axis] = value;
            walls.push((axis, value));
        }
    }
    // The walls before the frame, which shares their edges: first drawn wins.
    for (axis, wall) in walls {
        draw.face((min, max), axis, wall, REGION);
    }
    draw.cuboid((min, max), UNSET);
}

/// The radii the origin must stay between, around the fixed centre, drawn at
/// the subject's height: the relation says nothing about Z.
fn limits_cylinder(draw: &mut Draw, args: &Args, origin: Vec3) {
    let (Some(x), Some(y)) = (args.number("center_x"), args.number("center_y")) else { return };
    let center = Vec3::new(x, y, origin.z);
    draw.cross(center, 0.02, REGION);
    for name in ["radius_min", "radius_max"] {
        if let Some(radius) = args.number(name) {
            draw.circle(center, radius, REGION);
        }
    }
}

/// The range a reset samples from: a box around the solved origin, and an arc
/// per randomised rotation, swung ± its half-angle about the world axis it
/// turns on — roll and pitch hung from above the object, yaw from where it
/// faces — at the reach of the object's farthest corner.
fn random_around(draw: &mut Draw, args: &Args, subject: &Asset, bounds: Aabb) {
    let origin = Vec3::from(subject.position_xyz);
    let half = Vec3::new(
        args.number("x_half_m").unwrap_or(0.0),
        args.number("y_half_m").unwrap_or(0.0),
        args.number("z_half_m").unwrap_or(0.0),
    );
    if half != Vec3::ZERO {
        draw.cuboid((origin - half, origin + half), REGION);
    }
    let radius = corners(bounds).map(|corner| corner.distance(origin)).fold(0.0, f32::max);
    let forward = facing(subject).with_z(0.0).try_normalize().unwrap_or(Vec3::X);
    let turns = [(Vec3::X, Vec3::Z, "roll_half_rad"), (Vec3::Y, Vec3::Z, "pitch_half_rad"), (Vec3::Z, forward, "yaw_half_rad")];
    for (axis, (about, from, name)) in turns.into_iter().enumerate() {
        let half = args.number(name).unwrap_or(0.0);
        if half > 0.0 {
            draw.arc(origin, about, Quat::from_axis_angle(about, -half) * from, 2.0 * half, radius, AXES[axis]);
        }
    }
}

/// The rotation applied on top of the solution: an arc per non-zero angle,
/// swung from the axis the angle turns away from — roll takes Y toward Z,
/// pitch Z toward X, yaw X toward Y.
fn rotate_around(draw: &mut Draw, args: &Args, origin: Vec3, reach: f32) {
    let turns = [(Vec3::X, Vec3::Y, "roll_rad"), (Vec3::Y, Vec3::Z, "pitch_rad"), (Vec3::Z, Vec3::X, "yaw_rad")];
    for (axis, (about, from, name)) in turns.into_iter().enumerate() {
        let angle = args.number(name).unwrap_or(0.0);
        if angle != 0.0 {
            draw.arc(origin, about, from, angle, reach, AXES[axis]);
        }
    }
}

/// An arrow from the subject's origin along its heading, clear of its bounds.
fn heading(draw: &mut Draw, subject: &Asset, bounds: Aabb) {
    let origin = Vec3::from(subject.position_xyz);
    draw.arrow(origin, origin + facing(subject) * (reach(bounds) + 0.05), SUBJECT);
}

/// A ring on the ground under the subject, around its footprint.
fn ring(draw: &mut Draw, (min, max): Aabb, color: Srgba) {
    let center = Vec3::new((min.x + max.x) / 2.0, (min.y + max.y) / 2.0, min.z);
    draw.circle(center, reach((min, max)), color);
}

/// Gizmo calls in Arena's frame: every point and rotation is turned onto
/// Bevy's axes on the way through, so the shapes above never see the flip.
struct Draw<'a, 'w, 's> {
    gizmos: &'a mut Gizmos<'w, 's, RelationGizmos>,
    to_bevy: Quat,
}

impl Draw<'_, '_, '_> {
    fn point(&self, point: Vec3) -> Vec3 {
        self.to_bevy * point
    }

    /// An isometry at `center`, axis-aligned in Arena's frame.
    fn at(&self, center: Vec3) -> Isometry3d {
        Isometry3d::new(self.to_bevy * center, self.to_bevy)
    }

    fn line(&mut self, from: Vec3, to: Vec3, color: Srgba) {
        let (from, to) = (self.point(from), self.point(to));
        self.gizmos.line(from, to, color);
    }

    fn arrow(&mut self, from: Vec3, to: Vec3, color: Srgba) {
        let (from, to) = (self.point(from), self.point(to));
        self.gizmos.arrow(from, to, color);
    }

    /// The outline of the face of a box that lies at `at` along `axis`. Plain
    /// lines rather than a strip, so it orders against [`Self::cuboid`].
    fn face(&mut self, (min, max): Aabb, axis: usize, at: f32, color: Srgba) {
        let (u, v) = ((axis + 1) % 3, (axis + 2) % 3);
        let corner = |along_u: f32, along_v: f32| {
            let mut point = Vec3::ZERO;
            point[axis] = at;
            point[u] = along_u;
            point[v] = along_v;
            point
        };
        let outline = [
            corner(min[u], min[v]),
            corner(max[u], min[v]),
            corner(max[u], max[v]),
            corner(min[u], max[v]),
        ];
        for i in 0..4 {
            self.line(outline[i], outline[(i + 1) % 4], color);
        }
    }

    /// A circle in the XY plane.
    fn circle(&mut self, center: Vec3, radius: f32, color: Srgba) {
        let at = self.at(center);
        self.gizmos.circle(at, radius, color);
    }

    /// The twelve edges of a box, as plain lines for the reason [`Self::face`] is.
    fn cuboid(&mut self, bounds: Aabb, color: Srgba) {
        let corner: Vec<Vec3> = corners(bounds).collect();
        for i in 0..8 {
            // Each corner to the three that differ from it in one axis, once.
            for bit in [1, 2, 4] {
                if i & bit == 0 {
                    self.line(corner[i], corner[i | bit], color);
                }
            }
        }
    }

    fn cross(&mut self, center: Vec3, half_size: f32, color: Srgba) {
        let at = self.at(center);
        self.gizmos.cross(at, half_size, color);
    }

    /// An arc of `angle` swung about `axis`, starting from the direction `from`.
    fn arc(&mut self, center: Vec3, axis: Vec3, from: Vec3, angle: f32, radius: f32, color: Srgba) {
        // A gizmo arc starts on its isometry's +X and turns about its +Y.
        let frame = Quat::from_mat3(&Mat3::from_cols(from, axis, from.cross(axis)));
        let at = Isometry3d::new(self.to_bevy * center, self.to_bevy * frame);
        self.gizmos.arc_3d(angle, radius, at, color);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::spec::Field;

    fn args<'a>(params: &'a Params, kind: Option<&'a Kind>) -> Args<'a> {
        Args { params, kind }
    }

    /// What the spec sets wins; what it leaves out is the kind's default; an
    /// explicit null is unset.
    #[test]
    fn a_parameter_falls_back_to_the_kinds_default() {
        let kind = Kind {
            fields: vec![Field {
                name: "edge_margin_m".into(),
                default: Value::Number(0.05),
                ..Default::default()
            }],
            ..Default::default()
        };
        let mut params = Params::new();
        assert_eq!(args(&params, Some(&kind)).number("edge_margin_m"), Some(0.05));
        params.insert("edge_margin_m".into(), Value::Number(0.1));
        assert_eq!(args(&params, Some(&kind)).number("edge_margin_m"), Some(0.1));
        params.insert("edge_margin_m".into(), Value::Null);
        assert_eq!(args(&params, Some(&kind)).number("edge_margin_m"), None);
    }

    /// The `on` support is the parent's top inset by the margin, whole when
    /// overlap is asked for, and gone once the margin has eaten it.
    #[test]
    fn the_support_is_the_parents_top_inset_by_the_margin() {
        let parent = (Vec3::ZERO, Vec3::new(1.0, 2.0, 0.8));
        let mut params = Params::from([
            ("edge_margin_m".to_owned(), Value::Number(0.1)),
            ("clearance_m".to_owned(), Value::Number(0.01)),
        ]);
        let (min, max) = support(&args(&params, None), parent).expect("a support");
        assert!(min.abs_diff_eq(Vec3::new(0.1, 0.1, 0.81), 1e-6), "{min}");
        assert!(max.abs_diff_eq(Vec3::new(0.9, 1.9, 0.81), 1e-6), "{max}");

        params.insert("overlap".into(), Value::Bool(true));
        let (min, max) = support(&args(&params, None), parent).expect("a support");
        assert_eq!((min.x, max.x), (0.0, 1.0));

        params.insert("overlap".into(), Value::Bool(false));
        params.insert("edge_margin_m".into(), Value::Number(0.6));
        assert!(support(&args(&params, None), parent).is_none());
    }

    /// `next_to` on +X at ratio 1: the near face a distance off the parent's
    /// +X edge, and the subject at the parent's +Y end, as far as its own
    /// footprint allows.
    #[test]
    fn next_to_targets_the_face_off_the_edge_and_the_position_across() {
        let parent = (Vec3::ZERO, Vec3::ONE);
        let origin = Vec3::new(5.0, 5.0, 0.5);
        let subject = (origin - Vec3::splat(0.1), origin + Vec3::splat(0.1));
        let params = Params::from([
            ("side".to_owned(), Value::text("positive_x")),
            ("distance_m".to_owned(), Value::Number(0.08)),
            ("cross_position_ratio".to_owned(), Value::Number(1.0)),
        ]);
        let [line_from, line_to, gap_from, gap_to] =
            next_to_target(&args(&params, None), origin, subject, parent).expect("a target");
        assert!(line_from.abs_diff_eq(Vec3::new(1.08, 0.0, 0.5), 1e-6), "{line_from}");
        assert!(line_to.abs_diff_eq(Vec3::new(1.08, 1.0, 0.5), 1e-6), "{line_to}");
        assert!(gap_from.abs_diff_eq(Vec3::new(1.0, 0.9, 0.5), 1e-6), "{gap_from}");
        assert!(gap_to.abs_diff_eq(Vec3::new(1.08, 0.9, 0.5), 1e-6), "{gap_to}");
    }

    /// `not_next_to` on -X keeps the origin out of a slab from just inside the
    /// parent's -X edge to the edge of the layout, narrowed across by the
    /// subject's own half-width less the margin — and there is no slab once
    /// the subject is too wide to sit within the band.
    #[test]
    fn the_keep_out_zone_is_for_the_origin_and_narrowed_by_the_subject() {
        let parent = (Vec3::ZERO, Vec3::new(1.0, 1.0, 0.2));
        let layout = (Vec3::splat(-3.0), Vec3::splat(3.0));
        let origin = Vec3::new(-1.0, 0.5, 0.1);
        let params = Params::from([("side".to_owned(), Value::text("negative_x"))]);
        let subject = (origin - Vec3::new(0.15, 0.15, 0.1), origin + Vec3::new(0.15, 0.15, 0.1));
        let (min, max) = keep_out(&args(&params, None), origin, subject, parent, layout).expect("a zone");
        assert!(min.abs_diff_eq(Vec3::new(-3.1, 0.05, 0.1), 1e-6), "{min}");
        assert!(max.abs_diff_eq(Vec3::new(0.1, 0.95, 0.1), 1e-6), "{max}");

        let wide = (origin - Vec3::new(0.6, 0.6, 0.1), origin + Vec3::new(0.6, 0.6, 0.1));
        assert!(keep_out(&args(&params, None), origin, wide, parent, layout).is_none());
    }
}
