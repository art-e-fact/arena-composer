#!/usr/bin/env bash
# Records the README's demo video of the visual editor: plays the storyboard in
# arena-view, frame by frame, and writes an mp4 GitHub takes, with a contact
# sheet beside it to check it by. How to change what it shows:
# .claude/skills/readme-video.
#
#   docs/video/record.sh [tour.json] [out.mp4]
#
# Defaults to tour.json beside this script and target/video/arena-composer.mp4.
# Needs Arena's venv active and the network, since arena-view solves every edit
# the tour makes; and cargo, Xephyr, Mesa's lavapipe and ffmpeg. Minutes: the
# window is drawn in software, at whatever pace, and the video is timed by its
# frames rather than by the clock.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
tour=$(realpath "${1:-$here/tour.json}")
out=$(realpath -m "${2:-$repo/target/video/arena-composer.mp4}")
# The README's screenshots' scene, written to show the editor off.
scene=$repo/docs/screenshots/kitchen_island.yaml
mkdir -p "$(dirname "$out")"
for tool in Xephyr ffmpeg ffprobe python; do
    command -v $tool >/dev/null || { echo "record.sh needs $tool on PATH" >&2; exit 1; }
done

# arena-view draws through the extension, so this checkout's Rust goes into
# this checkout's copy of it, as `uv pip install -e .` would put it there. Moved
# in rather than copied over, since a window open elsewhere has the old one
# mapped.
cargo build --release --quiet --lib --features extension-module --manifest-path "$repo/Cargo.toml"
core=$repo/python/arena_composer/_core.abi3.so
if ! cmp -s "$repo/target/release/libarena_composer.so" "$core"; then
    cp "$repo/target/release/libarena_composer.so" "$core.new"
    mv -f "$core.new" "$core"
fi

# The tour edits the spec, so it gets a copy.
work=$(mktemp -d)
# A nested X server, so nothing lands on the desktop's display and the window
# has the same scale on every machine. It says which display it took once it
# accepts connections.
Xephyr -displayfd 3 -screen 2560x1440 -ac -br -noreset 3>"$work/display" 2>"$work/xephyr.log" &
xephyr=$!
trap 'kill $xephyr 2>/dev/null || true; rm -rf "$work"' EXIT
until [[ -s $work/display ]]; do
    # It exits, rather than says, when there is no display to open its window on.
    kill -0 $xephyr 2>/dev/null || { cat "$work/xephyr.log" >&2; exit 1; }
    sleep 0.1
done
cp "$scene" "$work/"

# This checkout's Python, not whichever one Arena's venv has installed.
if ! PYTHONPATH=$repo/python DISPLAY=:$(<"$work/display") VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.json \
    ARENA_VIEWER_TOUR=$tour ARENA_VIEWER_RECORD=$work/raw.mp4 \
    python -m arena_composer.view "$work/$(basename "$scene")" >"$work/view.log" 2>&1; then
    tail -30 "$work/view.log" >&2
    exit 1
fi
# When each step starts, to find its frames by.
sed 's/\x1b\[[0-9;]*m//g' "$work/view.log" | grep -oE 'tour: (step|done) .*' || true

# 9 MB, as GitHub takes 10 on a free plan: two passes at the bitrate that fills
# it, however long the video. It starts playing before it has loaded.
secs=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$work/raw.mp4")
x264=(-c:v libx264 -preset slow -b:v "$(awk "BEGIN { print int(72000 / $secs) }")k" -pix_fmt yuv420p
    -passlogfile "$work/x264")
ffmpeg -y -loglevel warning -i "$work/raw.mp4" "${x264[@]}" -pass 1 -f null /dev/null
ffmpeg -y -loglevel warning -i "$work/raw.mp4" "${x264[@]}" -pass 2 -movflags +faststart "$out"
# A frame every two seconds, from the first, four to a row.
rows=$(( (${secs%.*} / 2 + 4) / 4 ))
ffmpeg -y -loglevel warning -i "$out" -vf "select='isnan(prev_selected_t)+gte(t-prev_selected_t,2)',scale=480:-2,tile=4x$rows" \
    -frames:v 1 -update 1 "${out%.*}-sheet.png"
echo "$out ($(numfmt --to=si --suffix=B "$(stat -c %s "$out")"), ${secs%.*} s), ${out%.*}-sheet.png"
