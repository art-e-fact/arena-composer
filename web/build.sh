#!/usr/bin/env bash
# Builds the web viewer into a folder a static server can serve as it is, and
# readies each packed scene given for it:
#
#   web/build.sh [--out DIR] scene.msgpack...    # DIR defaults to web/site
#   python -m http.server -d web/site            # then open /?payload=scene.msgpack
#
# Needs rustup's wasm32-unknown-unknown target and the wasm-bindgen CLI at the
# version Cargo.lock pins; to ready a scene, a Python with msgpack (Arena's venv).
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
out=$root/web/site
if [[ ${1-} == --out ]]; then
    out=$2
    shift 2
fi

cargo build --release --lib --target wasm32-unknown-unknown --manifest-path "$root/Cargo.toml"
wasm-bindgen --target web --no-typescript --out-dir "$out" \
    "${CARGO_TARGET_DIR:-$root/target}/wasm32-unknown-unknown/release/arena_composer.wasm"
cp "$root/web/index.html" "$out/"
if (($#)); then
    python "$root/web/export.py" "$out" "$@"
fi
