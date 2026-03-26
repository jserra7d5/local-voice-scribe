#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_TAG="local-voice-scribe-installer-tests"

docker build -t "$IMAGE_TAG" -f "$ROOT/tests/installer/Dockerfile" "$ROOT"
docker run --rm "$IMAGE_TAG"
