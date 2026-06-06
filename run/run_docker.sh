#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

docker build -t iptv-builder .

docker run --rm -v "$PWD":/workspace -w /workspace iptv-builder bash -lc "
  python3 scripts/build_job.py cctv '高质量 → 本地源' &&
  python3 scripts/build_job.py satellite '高质量 → 本地源' &&
  python3 scripts/merge_cache.py &&
  python3 scripts/merge_state_files.py
"
