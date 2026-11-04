// The silhouette of what the mask camera drew: a band `width` pixels wide
// around it in `color`, and a darker rim outside that, so it reads on a
// surface of its own colour. The mask is the window's size, so a fragment's
// position is its pixel in the mask.

import bevy_ui_render::ui_vertex_output::UiVertexOutput;

@group(1) @binding(0) var<uniform> color: vec4<f32>;
// In x.
@group(1) @binding(1) var<uniform> width: vec4<f32>;
@group(1) @binding(2) var mask: texture_2d<f32>;

fn covered(at: vec2<i32>, size: vec2<i32>) -> bool {
    return textureLoad(mask, clamp(at, vec2<i32>(0), size - 1), 0).a > 0.0;
}

@fragment
fn fragment(in: UiVertexOutput) -> @location(0) vec4<f32> {
    let size = vec2<i32>(textureDimensions(mask));
    let at = vec2<i32>(in.position.xy);
    if covered(at, size) {
        discard;
    }
    // ponytail: a brute-force search of the disc around each pixel, about a
    // hundred loads at 2.5 px; a jump flood if the band ever has to be wide.
    let band = width.x;
    let reach = i32(ceil(band + 1.5));
    var nearest = 1e6;
    for (var y = -reach; y <= reach; y++) {
        for (var x = -reach; x <= reach; x++) {
            let distance = length(vec2<f32>(f32(x), f32(y)));
            if distance < nearest && covered(at + vec2<i32>(x, y), size) {
                nearest = distance;
            }
        }
    }
    let inner = clamp(band + 0.5 - nearest, 0.0, 1.0);
    let rim = clamp(band + 1.5 - nearest, 0.0, 1.0) * 0.6;
    let alpha = max(inner, rim);
    if alpha <= 0.0 {
        discard;
    }
    return vec4<f32>(color.rgb * inner / alpha, alpha);
}
