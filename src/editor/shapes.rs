//! Filled shapes for the relation marks: asked for afresh every frame, as
//! gizmos are, and drawn by a pool of entities kept between frames, so a shape
//! that holds still costs no new mesh or material.
//!
//! A shape is one of a few unit meshes, scaled and placed by its transform, in
//! a flat colour; alpha under 1 blends it. None casts or takes a shadow, and a
//! ray cast is meant to pass through them all: see [`Piece`].

use std::collections::HashMap;

use bevy::asset::RenderAssetUsages;
use bevy::light::{NotShadowCaster, NotShadowReceiver};
use bevy::mesh::{Indices, PrimitiveTopology};
use bevy::prelude::*;
use bevy_egui::EguiPostUpdateSet;

pub fn plugin(app: &mut App) {
    // After egui's pass, where the shapes are asked for.
    app.init_resource::<Shapes>()
        .add_systems(PostUpdate, apply.after(EguiPostUpdateSet::ProcessOutput).before(TransformSystems::Propagate));
}

/// The unit meshes. Each lies in its local XY plane or stands along its local
/// Z, so that in Arena's Z-up frame it lies flat or stands up.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum Primitive {
    /// A 1 × 1 square, centred.
    Square,
    /// A disc of radius 1.
    Disc,
    /// A ring of outer radius 1, its inner radius in thousandths of that.
    Ring(u16),
    /// A triangle pointing along +X: its tip at (1, 0), its base from (0, -0.5) to (0, 0.5).
    Triangle,
    /// A square from y = 0, opaque, to y = 1, clear; x from -0.5 to 0.5.
    Fade,
    /// The side of a cylinder of radius 1, opaque at z = 0 and clear at z = 1.
    FadeWall,
}

pub struct Shape {
    pub primitive: Primitive,
    /// In Bevy's frame.
    pub transform: Transform,
    pub color: Srgba,
}

/// One of the pool's entities.
#[derive(Component)]
pub struct Piece;

#[derive(Resource, Default)]
pub struct Shapes {
    wanted: Vec<Shape>,
    pool: Vec<Entity>,
    meshes: HashMap<Primitive, Handle<Mesh>>,
    materials: HashMap<[u8; 4], Handle<StandardMaterial>>,
}

impl Shapes {
    /// Draw `shape` this frame.
    pub fn add(&mut self, shape: Shape) {
        self.wanted.push(shape);
    }
}

fn mesh(primitive: Primitive) -> Mesh {
    match primitive {
        Primitive::Square => Rectangle::new(1.0, 1.0).mesh().build(),
        Primitive::Disc => Circle::new(1.0).mesh().resolution(64).build(),
        Primitive::Ring(inner) => Annulus::new(f32::from(inner) / 1000.0, 1.0).mesh().resolution(96).build(),
        Primitive::Triangle => Triangle2d::new(Vec2::X, Vec2::new(0.0, 0.5), Vec2::new(0.0, -0.5)).mesh().build(),
        Primitive::Fade => {
            let at = [[-0.5, 0.0, 0.0], [0.5, 0.0, 0.0], [0.5, 1.0, 0.0], [-0.5, 1.0, 0.0]];
            faded(at.to_vec(), vec![Vec3::Z; 4], vec![1.0, 1.0, 0.0, 0.0], vec![0, 1, 2, 0, 2, 3])
        }
        Primitive::FadeWall => {
            const SEGMENTS: u32 = 96;
            let (mut at, mut normals, mut alpha, mut indices) = (vec![], vec![], vec![], vec![]);
            for i in 0..=SEGMENTS {
                let (sin, cos) = (i as f32 / SEGMENTS as f32 * std::f32::consts::TAU).sin_cos();
                at.extend([[cos, sin, 0.0], [cos, sin, 1.0]]);
                normals.extend([Vec3::new(cos, sin, 0.0); 2]);
                alpha.extend([1.0, 0.0]);
            }
            for i in 0..SEGMENTS * 2 {
                indices.extend(if i % 2 == 0 { [i, i + 2, i + 1] } else { [i, i + 1, i + 2] });
            }
            faded(at, normals, alpha, indices)
        }
    }
}

/// A mesh whose vertices fade by `alpha`, which multiplies the material's.
fn faded(at: Vec<[f32; 3]>, normals: Vec<Vec3>, alpha: Vec<f32>, indices: Vec<u32>) -> Mesh {
    let colors: Vec<[f32; 4]> = alpha.into_iter().map(|alpha| [1.0, 1.0, 1.0, alpha]).collect();
    let uvs = vec![[0.0, 0.0]; at.len()];
    Mesh::new(PrimitiveTopology::TriangleList, RenderAssetUsages::default())
        .with_inserted_attribute(Mesh::ATTRIBUTE_POSITION, at)
        .with_inserted_attribute(Mesh::ATTRIBUTE_NORMAL, normals)
        .with_inserted_attribute(Mesh::ATTRIBUTE_UV_0, uvs)
        .with_inserted_attribute(Mesh::ATTRIBUTE_COLOR, colors)
        .with_inserted_indices(Indices::U32(indices))
}

fn material(color: Srgba) -> StandardMaterial {
    StandardMaterial {
        base_color: color.into(),
        alpha_mode: if color.alpha < 1.0 { AlphaMode::Blend } else { AlphaMode::Opaque },
        unlit: true,
        double_sided: true,
        cull_mode: None,
        ..default()
    }
}

#[allow(clippy::type_complexity)]
fn apply(
    mut shapes: ResMut<Shapes>,
    mut meshes: ResMut<Assets<Mesh>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
    mut pieces: Query<
        (&mut Mesh3d, &mut MeshMaterial3d<StandardMaterial>, &mut Transform, &mut Visibility),
        With<Piece>,
    >,
    mut commands: Commands,
) {
    let shapes = &mut *shapes;
    let wanted = std::mem::take(&mut shapes.wanted);
    for (i, shape) in wanted.iter().enumerate() {
        let mesh = shapes.meshes.entry(shape.primitive).or_insert_with(|| meshes.add(mesh(shape.primitive)));
        let key = shape.color.to_u8_array();
        let material = shapes.materials.entry(key).or_insert_with(|| materials.add(material(shape.color)));
        if let Some((mut piece_mesh, mut piece_material, mut transform, mut visibility)) =
            shapes.pool.get(i).and_then(|&piece| pieces.get_mut(piece).ok())
        {
            piece_mesh.set_if_neq(Mesh3d(mesh.clone()));
            piece_material.set_if_neq(MeshMaterial3d(material.clone()));
            transform.set_if_neq(shape.transform);
            visibility.set_if_neq(Visibility::Inherited);
        } else {
            let piece = commands
                .spawn((
                    Piece,
                    Mesh3d(mesh.clone()),
                    MeshMaterial3d(material.clone()),
                    shape.transform,
                    NotShadowCaster,
                    NotShadowReceiver,
                ))
                .id();
            match shapes.pool.get_mut(i) {
                Some(slot) => *slot = piece,
                None => shapes.pool.push(piece),
            }
        }
    }
    for &piece in shapes.pool.iter().skip(wanted.len()) {
        if let Ok((.., mut visibility)) = pieces.get_mut(piece) {
            visibility.set_if_neq(Visibility::Hidden);
        }
    }
}
