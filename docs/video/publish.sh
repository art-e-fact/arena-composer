#!/usr/bin/env bash
# Puts the README's demo video on GitHub and points the README at it. GitHub
# plays only video it hosts, and a comment is what `gh` can attach one to: the
# mp4 goes up in a comment on the pull request, and the URL GitHub gave it goes
# on the README's video line. Check the video first: .claude/skills/readme-video.
#
#   docs/video/publish.sh [video.mp4] [pull request]
#
# Defaults to target/video/arena-composer.mp4 and the current branch's pull
# request. Needs gh 2.102 or later, for `--attach`, and push access. It leaves
# README.md edited, for the commit that changed the video.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
video=$(realpath "${1:-$repo/target/video/arena-composer.mp4}")
# gh reads the repository, and the branch's pull request, from the checkout.
cd "$repo"
pr=${2:-$(gh pr view --json number --jq .number)}
readme=$repo/README.md
# The README's video line: the one line that is an uploaded asset alone, which
# GitHub draws as a player.
line='^https://github[.]com/user-attachments/assets/[0-9a-f-]+$'
[[ $(grep -cE "$line" "$readme") -le 1 ]] || { echo "README.md has more than one video line" >&2; exit 1; }

# The reference has to be its paragraph alone to play rather than link. gh
# rewrites it to the upload, and prints the comment's URL.
body=$(mktemp)
trap 'rm -f "$body"' EXIT
printf 'The README'"'"'s video, recorded by `docs/video/record.sh`:\n\n![](%s)\n' "$video" >"$body"
comment=$(gh pr comment "$pr" --body-file "$body" --attach "$video")
url=$(gh api "repos/{owner}/{repo}/issues/comments/${comment##*-}" --jq .body |
    grep -oE 'https://github\.com/user-attachments/assets/[0-9a-f-]+' | tail -1)
[[ -n $url ]] || { echo "no video in $comment" >&2; exit 1; }

# In place of the last one, or the first time under the screenshots' table.
if grep -qE "$line" "$readme"; then
    edited=$(awk -v url="$url" -v line="$line" '$0 ~ line { $0 = url } { print }' "$readme")
else
    edited=$(awk -v url="$url" '{ print } !done && $0 == "</table>" { print ""; print url; done = 1 }' "$readme")
fi
printf '%s\n' "$edited" >"$readme"
echo "$url in README.md, from $comment"
