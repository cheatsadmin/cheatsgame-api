#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

short_commit="$(git rev-parse --short HEAD)"
output_dir="${HOME}/Desktop"
output_file="${output_dir}/backend-cheatsgame-staging-${short_commit}.zip"

mkdir -p "$output_dir"
rm -f "$output_file"

archive_paths=(
  "."
  ":(exclude).env"
  ":(glob,exclude)**/.env"
  ":(exclude).venv"
  ":(glob,exclude).venv/**"
  ":(glob,exclude)**/.venv/**"
  ":(exclude)db.sqlite3"
  ":(glob,exclude)**/db.sqlite3"
  ":(exclude)media"
  ":(glob,exclude)media/**"
  ":(glob,exclude)**/media/**"
  ":(exclude)uploads"
  ":(glob,exclude)uploads/**"
  ":(glob,exclude)**/uploads/**"
  ":(exclude)staticfiles"
  ":(glob,exclude)staticfiles/**"
  ":(glob,exclude)**/staticfiles/**"
  ":(exclude)node_modules"
  ":(glob,exclude)node_modules/**"
  ":(glob,exclude)**/node_modules/**"
  ":(glob,exclude)HANDOFF*"
  ":(glob,exclude)**/HANDOFF*"
  ":(glob,exclude)*.patch"
  ":(glob,exclude)**/*.patch"
  ":(exclude)logs"
  ":(glob,exclude)logs/**"
  ":(glob,exclude)**/logs/**"
  ":(exclude).DS_Store"
  ":(glob,exclude)**/.DS_Store"
)

git archive --format=zip --output="$output_file" HEAD -- "${archive_paths[@]}"

required_entries=(
  "manage.py"
  "liara.json"
  "requirements/"
  "config/"
  "cheatgame/"
)

archive_listing="$(zipinfo -1 "$output_file")"

for entry in "${required_entries[@]}"; do
  if ! grep -Fxq "$entry" <<<"$archive_listing"; then
    echo "Archive validation failed: missing ${entry}" >&2
    exit 1
  fi
done

excluded_pattern='(^|/)(\.git|\.env|\.venv|db\.sqlite3|media|uploads|staticfiles|node_modules|HANDOFF[^/]*|.*\.patch|logs|\.DS_Store)(/|$)'
if grep -Eq "$excluded_pattern" <<<"$archive_listing"; then
  echo "Archive validation failed: excluded files are present" >&2
  grep -E "$excluded_pattern" <<<"$archive_listing" >&2
  exit 1
fi

echo "$output_file"
