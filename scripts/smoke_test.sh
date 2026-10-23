#!/usr/bin/env bash
set -euo pipefail

base_url="${APP_URL:-http://127.0.0.1:8080}"
python_bin="${PYTHON_BIN:-python3}"
work_dir="${SMOKE_WORK_DIR:-.tmp/smoke}"
mkdir -p "$work_dir"

"$python_bin" scripts/generate_sample_pdf.py "$work_dir/sample.pdf" >/dev/null

for _ in {1..60}; do
  if curl --fail --silent "$base_url/health/ready" >"$work_dir/readiness.json"; then
    break
  fi
  sleep 2
done

if ! curl --fail --silent "$base_url/health/ready" >"$work_dir/readiness.json"; then
  echo "Stack did not become ready" >&2
  exit 1
fi

status_code=$(curl --silent --show-error \
  --output "$work_dir/accepted.json" \
  --write-out "%{http_code}" \
  --form "parser=pypdf" \
  --form "files=@$work_dir/sample.pdf;type=application/pdf" \
  "$base_url/api/v1/documents")

if [[ "$status_code" != "202" ]]; then
  echo "Upload failed with HTTP $status_code" >&2
  "$python_bin" -m json.tool "$work_dir/accepted.json" >&2 || true
  exit 1
fi

document_id=$("$python_bin" -c   'import json,sys; print(json.load(open(sys.argv[1]))["documents"][0]["id"])'   "$work_dir/accepted.json")

for _ in {1..60}; do
  curl --fail --silent     "$base_url/api/v1/documents/$document_id" >"$work_dir/result.json"
  state=$("$python_bin" -c     'import json,sys; print(json.load(open(sys.argv[1]))["status"])'     "$work_dir/result.json")
  case "$state" in
    completed)
      "$python_bin" - "$work_dir/result.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    result = json.load(source)
assert result["summary"].startswith("## Smoke-test summary")
assert "Async document processor smoke test" in result["pages"][0]["markdown"]
PY
      echo "Smoke test completed for document $document_id"
      exit 0
      ;;
    failed)
      echo "Document processing failed" >&2
      "$python_bin" -m json.tool "$work_dir/result.json" >&2
      exit 1
      ;;
  esac
  sleep 2
done

echo "Document did not reach a terminal state" >&2
exit 1
