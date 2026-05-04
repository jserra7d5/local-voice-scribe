#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_TAG="local-voice-scribe-installer-tests"

usage() {
  cat <<'EOF'
Usage: ./scripts/test-installer.sh [--docker]

Runs installer tests locally by default.

Options:
  --docker  Run the tests inside Docker instead of the current shell
EOF
}

case "${1:-}" in
  "")
    bash "$ROOT/tests/installer/test_setup.sh"
    ;;
  --docker)
    docker build -t "$IMAGE_TAG" -f "$ROOT/tests/installer/Dockerfile" "$ROOT"
    docker run --rm "$IMAGE_TAG"
    ;;
  --help|-h)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
