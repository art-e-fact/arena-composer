#!/usr/bin/env bash
# Retakes the README's screenshots of the visual editor, one `shoot` line each
# at the foot. How to change what they show: .claude/skills/readme-screenshots.
#
# Needs Xvfb, Mesa's lavapipe and cargo; and to pack a scene, Arena's venv
# and the network. A packed scene (`*.msgpack`, ~90 MB) names files in this
# machine's asset cache, so it stays out of git, repacked when its spec is newer.
set -euo pipefail
cd "$(dirname "$0")"
cargo build --quiet --bin arena-viewer

# An X server of its own, drawn in memory, so no window opens on the desktop
# and the screen has the same size and scale on every machine. (Xephyr would
# be one more window on the desktop.) It says which display it took once it
# accepts connections.
command -v Xvfb >/dev/null || { echo "shoot.sh needs Xvfb: sudo apt install xvfb" >&2; exit 1; }
logs=$(mktemp -d)
Xvfb -displayfd 3 -screen 0 1600x1000x24 -ac -noreset 3>"$logs/display" 2>"$logs/xvfb.log" &
xvfb=$!
trap 'kill $xvfb 2>/dev/null || true; rm -rf "$logs"' EXIT
until [[ -s $logs/display ]]; do
    kill -0 $xvfb 2>/dev/null || { cat "$logs/xvfb.log" >&2; exit 1; }
    sleep 0.1
done
display=:$(<"$logs/display")

# shoot <png> <scene> [VAR=value ...]: open <scene>.yaml in that state, and
# write the window to <png>.
shoot() {
    local png=$1 scene=$2
    shift 2
    if [[ ! $scene.msgpack -nt $scene.yaml ]]; then
        # This checkout's packer, not whichever one Arena's venv has installed;
        # as many samples as arena-view solves, for its header to page through.
        PYTHONPATH=../../python python -m arena_composer.resolve_layout "$scene.yaml" --format text \
            --samples 5 --payload "$scene.msgpack"
    fi
    rm -f "$png"
    if ! env DISPLAY="$display" VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.json \
        ARENA_VIEWER_SIZE=1440x900 ARENA_VIEWER_SCREENSHOT="$PWD/$png" "$@" \
        timeout 120 cargo run --quiet --bin arena-viewer -- "$scene.msgpack" 2>"$logs/$png.log" || [[ ! -s $png ]]; then
        tail -20 "$logs/$png.log" >&2
        exit 1
    fi
    echo "$png"
}

shoot assets.png kitchen_island ARENA_VIEWER_SEARCH="kitchenware mug" ARENA_VIEWER_PICK=wooden_bowl
# From 1.25 m off and 50° above the island's objects, rather than the kitchen's own eye-level
# camera, which sees the marks lying on the table almost edge-on.
shoot graph.png kitchen_island ARENA_VIEWER_DOCK=graph ARENA_VIEWER_DOCK_HEIGHT=400 ARENA_VIEWER_PICK=10 ARENA_VIEWER_SHOW_RELATIONS=7,10,12 \
    ARENA_VIEWER_CAMERA=2.95,-4.62,1.89,2.95,-3.82,0.93
