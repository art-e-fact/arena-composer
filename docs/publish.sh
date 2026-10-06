#!/usr/bin/env bash
# Puts the README's pictures and video on GitHub and points the README at them.
# GitHub plays only video it hosts, and pictures kept out of git leave every
# retake out of the clone: `gh` attaches the files to a comment on the pull
# request, where its reviewers see them too, and each URL GitHub gives replaces
# the one on the README line under the file's marker, `<!-- graph.png -->`.
#
#   docs/publish.sh docs/screenshots/*.png target/video/arena-composer.mp4
#
# PR=<number> names the pull request, the current branch's by default. Needs gh
# 2.102 or later, for `--attach`, and push access. It leaves README.md edited,
# for the commit that changed the files. How the files are made and checked:
# .claude/skills/readme-screenshots and .claude/skills/readme-video.
set -euo pipefail
(($#)) || { echo "usage: docs/publish.sh FILE..." >&2; exit 1; }
files=()
for file in "$@"; do files+=("$(realpath "$file")"); done
repo=$(cd "$(dirname "$0")/.." && pwd)
readme=$repo/README.md
# gh reads the repository, and the branch's pull request, from the checkout.
cd "$repo"
pr=${PR:-$(gh pr view --json number --jq .number)}
url='https://github[.]com/user-attachments/assets/[0-9a-f-]+'

# Every line checked before anything goes up.
for file in "${files[@]}"; do
    marker="<!-- $(basename "$file") -->"
    grep -A1 -F "$marker" "$readme" | tail -n +2 | grep -qE "$url" ||
        { echo "README.md has no URL on the line under $marker" >&2; exit 1; }
done

# A reference each, its paragraph alone so a video plays rather than links. gh
# rewrites them to the uploads in place, so the URLs come back in this order.
body=$(mktemp)
trap 'rm -f "$body"' EXIT
attach=()
{
    echo 'The README'"'"'s pictures and video, for `docs/publish.sh`:'
    for file in "${files[@]}"; do
        printf '\n![](%s)\n' "$file"
        attach+=(--attach "$file")
    done
} >"$body"
comment=$(gh pr comment "$pr" --body-file "$body" "${attach[@]}")
mapfile -t urls < <(gh api "repos/{owner}/{repo}/issues/comments/${comment##*-}" --jq .body | grep -oE "$url")
((${#urls[@]} == ${#files[@]})) || { echo "$comment has ${#urls[@]} URLs for ${#files[@]} files" >&2; exit 1; }

for i in "${!files[@]}"; do
    edited=$(awk -v marker="<!-- $(basename "${files[i]}") -->" -v url="${urls[i]}" -v pattern="$url" \
        '{ if (under) gsub(pattern, url); under = index($0, marker) > 0; print }' "$readme")
    printf '%s\n' "$edited" >"$readme"
    echo "$(basename "${files[i]}"): ${urls[i]}"
done
echo "from $comment"
