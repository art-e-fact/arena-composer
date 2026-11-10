//! The relation marks: what a relation asks of its subject, laid on the
//! surfaces the objects stand on rather than drawn as lines in the air. Each
//! mark has one job:
//!
//! - a fill, rimmed, is where the subject may go, and a side the loss leaves
//!   open fades out;
//! - a wall fading upward stands on a bound the loss holds;
//! - chevrons march the way the relation reads, brightening as they arrive;
//! - a thin line traces a mark through whatever stands on it: the shapes are
//!   depth-tested, the lines drawn over everything.
//!
//! The subject is lit and ringed in its orange, and the reference tinted
//! lightly in the region's aqua. A kind with no marks of its own keeps its
//! lines.

use std::sync::OnceLock;

use bevy::color::palettes::css;
use bevy::prelude::*;

use super::super::shapes::{Primitive, Shape};
use super::{Aabb, Args, Draw, FORBIDDEN, REGION, SUBJECT, at, center, extent, facing, fits, next_to_target, reach, side, support};
use crate::layout::{Placed, world_aabb};
use crate::scene::Asset;

/// How far a fill lies over the surface under it, and a rim over that, so
/// neither fights the surface for depth.
const LIFT: f32 = 0.002;
const RIM_LIFT: f32 = 0.003;
/// A fill's alpha, a large one's, and a rim's.
const FILL: f32 = 0.16;
const FILL_LARGE: f32 = 0.1;
const RIM: f32 = 0.85;
const RIM_WIDTH: f32 = 0.006;
/// A wall's alpha at its foot.
const WALL: f32 = 0.4;
/// A mark's width: a chevron's stroke, a dash. Aqua on a light table needs it
/// wide to read.
const STROKE: f32 = 0.008;
/// How far an open side fades out over.
const FADE: f32 = 0.12;
/// How fast chevrons march, in metres a second.
const MARCH: f32 = 0.04;
/// How strongly the reference is tinted: lightly, so a red bowl stays red. The
/// reticle or the glow already says which it is.
const REFERENCE_TINT: f32 = 0.5;

/// A fill light enough not to tint a whole table.
fn fill_for(min: Vec2, max: Vec2) -> f32 {
    if (max - min).element_product() > 0.3 { FILL_LARGE } else { FILL }
}

/// The subject, lit as the pointer lights it, and a ring on the ground around
/// it: notched where it faces if the relation turns it, and with a dot under
/// its origin and a cross through it where the relation holds the origin.
pub fn subject(draw: &mut Draw, subject: &Asset, bounds: Aabb, heading: bool, holds_origin: bool) {
    draw.painter.light(subject);
    let ground = center(bounds).with_z(bounds.0.z + RIM_LIFT);
    // Snug on a round footprint, which the diagonal overshoots; a box's
    // corners may poke out.
    let inner = (bounds.1 - bounds.0).truncate().max_element() * 0.56 + 0.006;
    let outer = inner + 0.007;
    draw.ring(ground, inner, outer, SUBJECT.with_alpha(0.9));
    let facing = facing(subject).with_z(0.0).normalize_or_zero();
    if heading && facing != Vec3::ZERO {
        draw.triangle(ground + facing * (outer - 0.001), facing, 0.016, 0.02, SUBJECT.with_alpha(0.9));
    }
    if holds_origin {
        // The dot where it can be seen, and a cross through whatever hides it.
        let origin = Vec3::from(subject.position_xyz);
        draw.disc(origin.with_z(ground.z + 0.0005), 0.007, SUBJECT.with_alpha(0.95));
        draw.cross(origin, 0.012, SUBJECT);
    }
}

/// Draw `kind`'s marks, and tint what it refers to — false for a kind with no
/// marks of its own.
pub fn relation(
    draw: &mut Draw,
    args: &Args,
    kind: &str,
    subject: &Asset,
    bounds: Aabb,
    reference: Option<&Asset>,
    placed: &Placed,
) -> bool {
    if let Some(reference) = reference {
        draw.painter.highlight.tint(reference, REGION.with_alpha(REFERENCE_TINT));
    }
    let origin = Vec3::from(subject.position_xyz);
    match (kind, reference) {
        ("on", Some(parent)) => surface(draw, args, world_aabb(parent), Some(bounds)),
        ("next_to", Some(parent)) => next_to(draw, args, origin, bounds, world_aabb(parent)),
        ("face_to", Some(target)) => face_to(draw, subject, bounds, target),
        ("on" | "next_to" | "face_to", None) => {}
        // `position_limits` is Arena's older name for the same relation.
        ("position_limits_box" | "position_limits", _) => limits_box(draw, args, bounds, extent(placed)),
        ("position_limits_cylindrical", _) => limits_cylinder(draw, args, origin, bounds),
        _ => return false,
    }
    true
}

/// `on`'s support over `parent`: its top inset by the margin, and the lip the
/// margin keeps clear. Red where nothing can stand on it: the margin has eaten
/// the whole top, or the subject, where there is one yet, is wider than what
/// is left. A drop shows it too, before there is.
pub fn surface(draw: &mut Draw, args: &Args, parent: Aabb, subject: Option<Aabb>) {
    let (top_min, top_max) = (parent.0.truncate(), parent.1.truncate());
    let Some(support) = support(args, parent) else {
        draw.rect(top_min, top_max, parent.1.z + LIFT, FORBIDDEN.with_alpha(0.2));
        draw.rim(top_min, top_max, parent.1.z + RIM_LIFT, RIM_WIDTH, [false; 4], FORBIDDEN.with_alpha(RIM));
        return;
    };
    let (lo, hi, z) = (support.0.truncate(), support.1.truncate(), support.0.z);
    let (color, fill) = if subject.is_none_or(|subject| fits(args, support, subject)) {
        (REGION, fill_for(lo, hi))
    } else {
        (FORBIDDEN, 0.2)
    };
    draw.rect(lo, hi, z + LIFT, color.with_alpha(fill));
    draw.rim(lo, hi, z + RIM_LIFT, RIM_WIDTH, [false; 4], color.with_alpha(RIM));
    let lip = css::GRAY.with_alpha(0.14);
    let top = parent.1.z + LIFT;
    draw.rect(top_min, Vec2::new(top_max.x, lo.y), top, lip);
    draw.rect(Vec2::new(top_min.x, hi.y), top_max, top, lip);
    draw.rect(Vec2::new(top_min.x, lo.y), Vec2::new(lo.x, hi.y), top, lip);
    draw.rect(Vec2::new(hi.x, lo.y), Vec2::new(top_max.x, hi.y), top, lip);
}

/// `next_to`. Which side: the parent's face there glowing. How far: chevrons
/// marching out of it across the gap. Where: the slot the subject is asked
/// into, rimmed, and traced through whatever stands in it — or, where no
/// `cross_position_ratio` pins it, the whole bay along the side, since the
/// loss lets it anywhere there.
fn next_to(draw: &mut Draw, args: &Args, origin: Vec3, bounds: Aabb, parent: Aabb) {
    let Some([line_from, _, gap_from, _]) = next_to_target(args, origin, bounds, parent) else { return };
    let Some((a, dir, c)) = args.text("side").and_then(side) else { return };
    let floor = bounds.0.z;
    let (face, edge, across) = (line_from[a], gap_from[a], gap_from[c]);
    let depth = bounds.1[a] - bounds.0[a];
    let half = (bounds.1[c] - bounds.0[c]) / 2.0;
    let mut outward = Vec3::ZERO;
    outward[a] = dir;

    let mut base = at(a, edge + dir * 0.001, c, (parent.0[c] + parent.1[c]) / 2.0, parent.0.z);
    base.z = parent.0.z.max(floor);
    // A fridge's side would be a sheet across the view: a glow, not the whole
    // face. None where the subject stands above the parent's top.
    let height = (parent.1.z - base.z).min(0.15);
    if height > 0.0 {
        draw.fade(base, Vec3::Z, outward, height, parent.1[c] - parent.0[c], REGION.with_alpha(0.45));
    }

    let (from, to) = match args.number("cross_position_ratio") {
        Some(_) => (across - half, across + half),
        None => (parent.0[c], parent.1[c]),
    };
    let (p, q) = (at(a, face, c, from, floor), at(a, face + dir * depth, c, to, floor));
    let (lo, hi) = (p.min(q), p.max(q));
    draw.rect(lo.truncate(), hi.truncate(), floor + LIFT, REGION.with_alpha(0.18));
    draw.rim(lo.truncate(), hi.truncate(), floor + RIM_LIFT, 0.004, [false; 4], REGION.with_alpha(0.7));
    draw.face((lo, hi), 2, floor + RIM_LIFT, REGION.with_alpha(0.35));

    let gap = (at(a, edge, c, across, floor + RIM_LIFT), at(a, face, c, across, floor + RIM_LIFT));
    draw.chevrons(gap.0, gap.1, 0.025, 0.03, REGION);
}

/// `face_to` turns the subject's origin to the target's — origins, not
/// boxes: Arena's `yaw_toward_positions`. Chevrons march from one to the
/// other, and a reticle rings the target's origin, which for a robot on its
/// stand is far from its box's middle. No cone either side of the way: the
/// yaw is exact, with no tolerance to draw.
fn face_to(draw: &mut Draw, subject: &Asset, bounds: Aabb, target: &Asset) {
    let origin = Vec3::from(subject.position_xyz);
    let aim = Vec3::from(target.position_xyz);
    let toward = (aim - origin).with_z(0.0);
    let distance = toward.length();
    if distance < 1e-4 {
        return;
    }
    let toward = toward / distance;
    let target_bounds = world_aabb(target);
    // Capped, so a big target's ring stays a mark.
    let ring = reach(target_bounds).min(0.15) + 0.01;
    // None where the subject stands within the ring: no way is left to point along.
    let (start, end) = (reach(bounds) + 0.02, distance - ring - 0.012);
    if end > start {
        let base = origin.with_z(bounds.0.z + RIM_LIFT);
        draw.chevrons(base + toward * start, base + toward * end, 0.05, 0.03, REGION);
    }
    draw.reticle(aim.with_z(target_bounds.0.z + RIM_LIFT), ring, REGION.with_alpha(RIM));
}

/// The box the origin must stay in, filled on its floor: rimmed and walled
/// where a bound is set, and fading out where a side is open — at the layout's
/// edge, or, where no X or Y bound is set at all, just past the subject, so a
/// Z limit alone does not read as a shelf across the layout. A set Z bound is
/// traced at its height.
fn limits_box(draw: &mut Draw, args: &Args, bounds: Aabb, (mut min, mut max): Aabb) {
    let mut set = [[false; 2]; 3];
    for (axis, name) in ["x_limits", "y_limits", "z_limits"].into_iter().enumerate() {
        let [low, high] = args.range(name);
        if let Some(value) = low {
            min[axis] = value;
            set[axis][0] = true;
        }
        if let Some(value) = high {
            max[axis] = value;
            set[axis][1] = true;
        }
    }
    if !set[2][0] {
        min.z = bounds.0.z;
    }
    if !set[2][1] {
        max.z = max.z.min(bounds.1.z + 0.05);
    }
    // A bound past the layout's edge, as a drag sets before the solve: the open
    // side runs on past it. Set the wrong way round, it is no box at all.
    for axis in 0..3 {
        if min[axis] > max[axis] {
            let room = if axis < 2 { 2.0 * FADE } else { 0.0 };
            match set[axis] {
                [true, false] => max[axis] = min[axis] + room,
                [false, true] => min[axis] = max[axis] - room,
                _ => return,
            }
        }
    }
    if set[0] == [false; 2] && set[1] == [false; 2] {
        let past = Vec3::new(FADE + 0.03, FADE + 0.03, 0.0);
        (min, max) = ((bounds.0 - past).with_z(min.z), (bounds.1 + past).with_z(max.z));
    }
    // -x, +x, -y, +y
    let open = [!set[0][0], !set[0][1], !set[1][0], !set[1][1]];
    let floor = min.z;
    let (lo, hi) = draw.fading_rect(min.truncate(), max.truncate(), floor + LIFT, open, REGION.with_alpha(0.13));
    draw.rim(lo, hi, floor + RIM_LIFT, RIM_WIDTH, open, REGION.with_alpha(RIM));
    // To a set top; open, a low glow whose fade says there is none.
    let height = if set[2][1] { max.z - floor } else { 0.06 };
    draw.fence(lo, hi, floor, height, open, REGION.with_alpha(WALL));
    for (side, value) in [(0, min.z), (1, max.z)] {
        if set[2][side] {
            draw.face((min, max), 2, value, REGION);
        }
    }
}

/// The radii the origin must stay between, on the ground the subject stands
/// on, since the relation says nothing about Z: the band filled, each radius
/// rimmed and walled, the centre marked, and the distance the loss measures
/// dashed from the centre to the origin.
fn limits_cylinder(draw: &mut Draw, args: &Args, origin: Vec3, bounds: Aabb) {
    let (Some(x), Some(y)) = (args.number("center_x"), args.number("center_y")) else { return };
    let (inner, outer) = (args.number("radius_min"), args.number("radius_max"));
    let floor = bounds.0.z;
    let middle = Vec3::new(x, y, floor + LIFT);
    match (inner, outer) {
        (Some(inner), Some(outer)) => draw.ring(middle, inner, outer, REGION.with_alpha(FILL)),
        (None, Some(outer)) => draw.disc(middle, outer, REGION.with_alpha(FILL)),
        (Some(inner), None) => draw.disc(middle, inner, FORBIDDEN.with_alpha(FILL)),
        (None, None) => {}
    }
    let rim = middle.with_z(floor + RIM_LIFT);
    for radius in [inner, outer].into_iter().flatten() {
        draw.ring(rim, radius - 0.003, radius + 0.003, REGION.with_alpha(RIM));
        draw.fade_wall(middle, radius, 0.05, REGION.with_alpha(WALL));
    }
    let mark = 0.015;
    draw.bar(rim - Vec3::X * mark, rim + Vec3::X * mark, 0.004, REGION.with_alpha(RIM));
    draw.bar(rim - Vec3::Y * mark, rim + Vec3::Y * mark, 0.004, REGION.with_alpha(RIM));
    draw.dashes(rim, origin.with_z(rim.z), 0.015, 0.01, STROKE * 0.75, SUBJECT.with_alpha(0.9));
}

/// The seconds chevrons march by: the app's, but held still for a screenshot
/// (`ARENA_VIEWER_SCREENSHOT`), which would catch them wherever the frames it
/// waits through left them.
fn clock(time: &Time) -> f32 {
    static STILL: OnceLock<bool> = OnceLock::new();
    if *STILL.get_or_init(|| std::env::var_os("ARENA_VIEWER_SCREENSHOT").is_some()) { 0.3 } else { time.elapsed_secs() }
}

/// Where chevrons `spacing` apart stand along a way `length` long at time
/// `secs`, and how bright: marching at [`MARCH`], brighter the further along,
/// faded in and out at the ends. A dozen at most, however long the way; one
/// way too short to march along holds a single one still in its middle. The
/// alphas come in eighths, so a fade reuses a few materials rather than
/// minting one a frame.
fn marching(length: f32, spacing: f32, secs: f32) -> Vec<(f32, f32)> {
    let spacing = spacing.max(length / 12.0);
    if length < spacing {
        return vec![(length / 2.0, 1.0)];
    }
    let ramp = spacing * 0.3;
    let mut stops = Vec::new();
    let mut travelled = (secs * MARCH).rem_euclid(spacing);
    while travelled <= length {
        let ends = (travelled.min(length - travelled) / ramp).clamp(0.0, 1.0);
        stops.push((travelled, (ends * (0.5 + 0.5 * travelled / length) * 8.0).round() / 8.0));
        travelled += spacing;
    }
    stops
}

/// Flat shapes in Arena's frame, as [`Draw`]'s lines are.
impl Draw<'_, '_, '_> {
    fn shape(&mut self, primitive: Primitive, transform: Transform, color: Srgba) {
        let transform = Transform {
            translation: self.to_bevy * transform.translation,
            rotation: self.to_bevy * transform.rotation,
            scale: transform.scale,
        };
        self.painter.shapes.add(Shape { primitive, transform, color });
    }

    /// A flat rectangle at height `z`.
    fn rect(&mut self, min: Vec2, max: Vec2, z: f32, color: Srgba) {
        let size = max - min;
        if size.min_element() > 1e-5 {
            let transform = Transform::from_translation(((min + max) / 2.0).extend(z)).with_scale(size.extend(1.0));
            self.shape(Primitive::Square, transform, color);
        }
    }

    /// A flat bar from `from` to `to`, at `from`'s height.
    fn bar(&mut self, from: Vec3, to: Vec3, width: f32, color: Srgba) {
        let along = (to - from).truncate();
        let length = along.length();
        if length > 1e-5 {
            let transform = Transform {
                translation: ((from + to) / 2.0).with_z(from.z),
                rotation: Quat::from_rotation_z(along.y.atan2(along.x)),
                scale: Vec3::new(length, width, 1.0),
            };
            self.shape(Primitive::Square, transform, color);
        }
    }

    /// A rectangle's edge, `width` wide inside it, but on the sides `open`
    /// leaves out: -x, +x, -y, +y.
    fn rim(&mut self, min: Vec2, max: Vec2, z: f32, width: f32, open: [bool; 4], color: Srgba) {
        let width = width.min((max - min).min_element() / 2.0);
        let half = width / 2.0;
        let (x0, x1) = (if open[0] { min.x } else { min.x + width }, if open[1] { max.x } else { max.x - width });
        if !open[0] {
            self.bar(Vec3::new(min.x + half, min.y, z), Vec3::new(min.x + half, max.y, z), width, color);
        }
        if !open[1] {
            self.bar(Vec3::new(max.x - half, min.y, z), Vec3::new(max.x - half, max.y, z), width, color);
        }
        if !open[2] {
            self.bar(Vec3::new(x0, min.y + half, z), Vec3::new(x1, min.y + half, z), width, color);
        }
        if !open[3] {
            self.bar(Vec3::new(x0, max.y - half, z), Vec3::new(x1, max.y - half, z), width, color);
        }
    }

    /// A flat rectangle whose `open` sides fade out over their last [`FADE`],
    /// and the part of it that does not fade.
    fn fading_rect(&mut self, min: Vec2, max: Vec2, z: f32, open: [bool; 4], color: Srgba) -> (Vec2, Vec2) {
        let fade = FADE.min((max - min).min_element() / 3.0);
        let lo = Vec2::new(if open[0] { min.x + fade } else { min.x }, if open[2] { min.y + fade } else { min.y });
        let hi = Vec2::new(if open[1] { max.x - fade } else { max.x }, if open[3] { max.y - fade } else { max.y });
        self.rect(lo, hi, z, color);
        let middle = (lo + hi) / 2.0;
        let edges = [
            (Vec3::new(lo.x, middle.y, z), Vec3::NEG_X, hi.y - lo.y),
            (Vec3::new(hi.x, middle.y, z), Vec3::X, hi.y - lo.y),
            (Vec3::new(middle.x, lo.y, z), Vec3::NEG_Y, hi.x - lo.x),
            (Vec3::new(middle.x, hi.y, z), Vec3::Y, hi.x - lo.x),
        ];
        for ((base, outward, width), open) in edges.into_iter().zip(open) {
            if open {
                self.fade(base, outward, Vec3::Z, fade, width, color);
            }
        }
        (lo, hi)
    }

    /// Walls `height` tall on a rectangle's sides, but those `open` leaves
    /// out, fading upward.
    fn fence(&mut self, min: Vec2, max: Vec2, z: f32, height: f32, open: [bool; 4], color: Srgba) {
        let middle = (min + max) / 2.0;
        let sides = [
            (Vec3::new(min.x, middle.y, z), Vec3::NEG_X, max.y - min.y),
            (Vec3::new(max.x, middle.y, z), Vec3::X, max.y - min.y),
            (Vec3::new(middle.x, min.y, z), Vec3::NEG_Y, max.x - min.x),
            (Vec3::new(middle.x, max.y, z), Vec3::Y, max.x - min.x),
        ];
        for ((base, outward, width), open) in sides.into_iter().zip(open) {
            if !open {
                self.fade(base, Vec3::Z, outward, height, width, color);
            }
        }
    }

    /// A square `width` across and `length` along `along` from `base`, opaque
    /// there and clear at its far end, facing `normal`.
    fn fade(&mut self, base: Vec3, along: Vec3, normal: Vec3, length: f32, width: f32, color: Srgba) {
        let rotation = Quat::from_mat3(&Mat3::from_cols(along.cross(normal), along, normal));
        let transform = Transform { translation: base, rotation, scale: Vec3::new(width, length, 1.0) };
        self.shape(Primitive::Fade, transform, color);
    }

    fn disc(&mut self, center: Vec3, radius: f32, color: Srgba) {
        let transform = Transform::from_translation(center).with_scale(Vec3::new(radius, radius, 1.0));
        self.shape(Primitive::Disc, transform, color);
    }

    /// A flat ring between two radii.
    fn ring(&mut self, center: Vec3, inner: f32, outer: f32, color: Srgba) {
        if outer > inner && inner >= 0.0 {
            let ratio = (inner / outer * 1000.0).round().clamp(0.0, 999.0) as u16;
            let transform = Transform::from_translation(center).with_scale(Vec3::new(outer, outer, 1.0));
            self.shape(Primitive::Ring(ratio), transform, color);
        }
    }

    /// A ring with four ticks pointing in at it: a target.
    fn reticle(&mut self, center: Vec3, radius: f32, color: Srgba) {
        self.ring(center, radius, radius + STROKE, color);
        for direction in [Vec3::X, Vec3::Y, Vec3::NEG_X, Vec3::NEG_Y] {
            self.bar(center + direction * (radius - 0.018), center + direction * (radius + 0.004), STROKE, color);
        }
    }

    /// A wall standing on a circle, fading upward.
    fn fade_wall(&mut self, center: Vec3, radius: f32, height: f32, color: Srgba) {
        let transform = Transform::from_translation(center).with_scale(Vec3::new(radius, radius, height));
        self.shape(Primitive::FadeWall, transform, color);
    }

    /// A flat triangle, its base's middle at `base`, pointing along `toward`.
    fn triangle(&mut self, base: Vec3, toward: Vec3, length: f32, width: f32, color: Srgba) {
        let transform = Transform {
            translation: base,
            rotation: Quat::from_rotation_z(toward.y.atan2(toward.x)),
            scale: Vec3::new(length, width, 1.0),
        };
        self.shape(Primitive::Triangle, transform, color);
    }

    /// A flat chevron, its tip at `tip`, pointing along `toward`.
    fn chevron(&mut self, tip: Vec3, toward: Vec3, size: f32, stroke: f32, color: Srgba) {
        let across = Vec3::new(-toward.y, toward.x, 0.0);
        for side in [1.0, -1.0] {
            self.bar(tip - toward * size * 0.5 + across * side * size * 0.5, tip, stroke, color);
        }
    }

    /// Chevrons `spacing` apart from `from` to `to`, at `from`'s height,
    /// marching toward `to` as [`marching`] has them. Each lies on a dark
    /// stroke, so aqua on an aqua fill, or on a light table, still reads; and
    /// a thin line traces it through whatever stands on it.
    fn chevrons(&mut self, from: Vec3, to: Vec3, spacing: f32, size: f32, color: Srgba) {
        let along = (to - from).with_z(0.0);
        let length = along.length() - size * 0.5;
        if length <= 0.0 {
            return;
        }
        let toward = along.normalize();
        let across = Vec3::new(-toward.y, toward.x, 0.0);
        for (travelled, alpha) in marching(length, spacing, clock(&self.painter.time)) {
            let color = color.with_alpha(color.alpha * alpha);
            let tip = from + toward * (size * 0.5 + travelled);
            // The stroke a hair lower, so it sorts behind.
            let shadow = Srgba::BLACK.with_alpha(0.4 * color.alpha);
            self.chevron(tip - Vec3::Z * 0.0005, toward, size, STROKE * 2.2, shadow);
            self.chevron(tip, toward, size, STROKE, color);
            for side in [1.0, -1.0] {
                self.line(tip - toward * size * 0.5 + across * side * size * 0.5, tip, color.with_alpha(color.alpha * 0.6));
            }
        }
    }

    /// A flat dashed line, at `from`'s height.
    fn dashes(&mut self, from: Vec3, to: Vec3, dash: f32, gap: f32, width: f32, color: Srgba) {
        let along = (to - from).with_z(0.0);
        let length = along.length();
        let toward = along.normalize_or_zero();
        let mut start = 0.0;
        while start < length {
            let end = (start + dash).min(length);
            self.bar(from + toward * start, from + toward * end, width, color);
            start = end + gap;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Chevrons stay on the way, march with time, fade in eighths, cap at a
    /// dozen and more on a long way, and a way too short to march holds one.
    #[test]
    fn chevrons_march_along_the_way_and_no_further() {
        for secs in [0.0, 0.3, 1.7, 1000.25] {
            let stops = marching(0.2, 0.05, secs);
            assert!((4..=5).contains(&stops.len()), "{stops:?}");
            for &(at, alpha) in &stops {
                assert!((0.0..=0.2).contains(&at), "{at}");
                assert!((0.0..=1.0).contains(&alpha) && (alpha * 8.0).fract() == 0.0, "{alpha}");
            }
        }
        let (now, later) = (marching(0.2, 0.05, 0.0)[0].0, marching(0.2, 0.05, 0.25)[0].0);
        assert!((later - now - 0.25 * MARCH).abs() < 1e-6, "{now} → {later}");
        assert!(marching(50.0, 0.05, 0.0).len() <= 13);
        assert_eq!(marching(0.02, 0.05, 7.0), vec![(0.01, 1.0)]);
    }
}
