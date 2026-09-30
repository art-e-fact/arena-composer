#!/usr/bin/env bash
# Retakes the README's screenshots of the visual editor, one `shoot` line each
# at the foot. How to change what they show: .claude/skills/readme-screenshots.
#
# Needs Xephyr, Mesa's lavapipe and cargo; and to pack a scene, Arena's venv
# and the network. A packed scene (`*.msgpack`, ~90 MB) names files in this
# machine's asset cache, so it stays out of git, repacked when its spec is newer.
set -euo pipefail
cd "$(dirname "$0")"
cargo build --quiet --bin arena-viewer

# A nested X server, so nothing lands on the desktop's display and the window
# has the same scale on every machine. It says which display it took once it
# accepts connections.
logs=$(mktemp -d)
Xephyr -displayfd 3 -screen 1600x1000 -ac -br -noreset 3>"$logs/display" 2>"$logs/xephyr.log" &
xephyr=$!
trap 'kill $xephyr; rm -rf "$logs"' EXIT
until [[ -s $logs/display ]]; do sleep 0.1; done
display=:$(<"$logs/display")

# shoot <png> <scene> [VAR=value ...]: open <scene>.yaml in that state, and
# write the window to <png>.
shoot() {
    local png=$1 scene=$2
    shift 2
    if [[ ! $scene.msgpack -nt $scene.yaml ]]; then
        # This checkout's packer, not whichever one Arena's venv has installed.
        PYTHONPATH=../../python python -m arena_composer.resolve_layout "$scene.yaml" --format text \
            --payload "$scene.msgpack"
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

shoot assets.png kitchen_island ARENA_VIEWER_SEARCH=kitchenware ARENA_VIEWER_PICK=wooden_bowl
shoot graph.png kitchen_island ARENA_VIEWER_DOCK=graph ARENA_VIEWER_DOCK_HEIGHT=400 ARENA_VIEWER_PICK=10 ARENA_VIEWER_SHOW_RELATIONS=7,10,12
