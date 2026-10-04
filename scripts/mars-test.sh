#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
files=()
while IFS= read -r file || [[ -n "$file" ]]; do
  [[ -z "$file" || "$file" == \#* ]] && continue
  [[ "$file" == tests/test_*.py && -f "$file" ]] || { printf 'Invalid Mars test path: %s\n' "$file" >&2; exit 2; }
  files+=("$file")
done < scripts/mars-test-files.txt
[[ ${#files[@]} -gt 0 ]] || { printf 'Empty Mars test manifest\n' >&2; exit 2; }
bash scripts/test.sh "${files[@]}" --collect-only -q >/dev/null
PY=.venv/bin/python
[[ -x "$PY" ]] || PY=.venv/Scripts/python.exe
"$PY" -m compileall -q api server.py bootstrap.py mcp_server.py tests scripts
git diff --check
"$PY" -m ruff check api/anchor_activity_scene.py api/file_response_helpers.py api/route_git.py api/route_skills.py api/route_files.py api/route_memory.py api/route_wiki_helpers.py tests/test_route_git.py tests/test_route_skills.py tests/test_route_files.py tests/test_route_memory.py tests/test_route_wiki_helpers.py tests/test_live_to_final_anchor_visible_order.py
exec bash scripts/test.sh "${files[@]}" -q --timeout=60 "$@"
