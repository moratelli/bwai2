#!/usr/bin/env bash
set -euo pipefail

base_url="${APP_URL:-http://127.0.0.1:8080}"
python_bin="${PYTHON_BIN:-python3}"
work_dir="${LIVE_PROVIDER_WORK_DIR:-.tmp/live-providers}"
mkdir -p "$work_dir"

"$python_bin" scripts/generate_sample_pdf.py "$work_dir/sample.pdf" >/dev/null

for _ in {1..60}; do
  if curl --fail --silent "$base_url/health/ready" >"$work_dir/readiness.json"; then
    break
  fi
  sleep 2
done

if ! curl --fail --silent "$base_url/health/ready" >"$work_dir/readiness.json"; then
  echo "Stack did not become ready with live providers" >&2
  exit 1
fi

run_provider() {
  local parser="$1"
  local accepted_file="$work_dir/${parser}-accepted.json"
  local result_file="$work_dir/${parser}-result.json"
  local status_code
  local document_id
  local state

  status_code=$(curl --silent --show-error \
    --output "$accepted_file" \
    --write-out "%{http_code}" \
    --form "parser=$parser" \
    --form "files=@$work_dir/sample.pdf;type=application/pdf" \
    "$base_url/api/v1/documents")

  if [[ "$status_code" != "202" ]]; then
    echo "$parser submission failed with HTTP $status_code" >&2
    exit 1
  fi

  document_id=$("$python_bin" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["documents"][0]["id"])' \
    "$accepted_file")

  for _ in {1..120}; do
    curl --fail --silent \
      "$base_url/api/v1/documents/$document_id" >"$result_file"
    state=$("$python_bin" -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["status"])' \
      "$result_file")
    case "$state" in
      completed)
        "$python_bin" - "$result_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    result = json.load(source)
assert result["status"] == "completed"
assert result["summary"].strip()
assert result["pages"]
assert [page["index"] for page in result["pages"]] == list(range(len(result["pages"])))
assert any(page["markdown"].strip() for page in result["pages"])
PY
        echo "$parser live check completed for document $document_id"
        return 0
        ;;
      failed)
        error_code=$("$python_bin" -c \
          'import json,sys; print(json.load(open(sys.argv[1]))["error"]["code"])' \
          "$result_file")
        echo "$parser live check failed with safe error code: $error_code" >&2
        return 1
        ;;
    esac
    sleep 2
  done

  echo "$parser live check timed out" >&2
  return 1
}

run_provider gemini
run_provider mistral
