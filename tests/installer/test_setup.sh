#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

pass_count=0
fail_count=0

portable_sha256() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

run_expect_success() {
  local name="$1"
  shift
  if "$@"; then
    printf 'PASS %s\n' "$name"
    pass_count=$((pass_count + 1))
  else
    printf 'FAIL %s\n' "$name" >&2
    fail_count=$((fail_count + 1))
  fi
}

run_expect_failure() {
  local name="$1"
  shift
  if "$@"; then
    printf 'FAIL %s\n' "$name" >&2
    fail_count=$((fail_count + 1))
  else
    printf 'PASS %s\n' "$name"
    pass_count=$((pass_count + 1))
  fi
}

case_unmanaged_init_aborts() {
  local sandbox
  sandbox="$(mktemp -d)"
  HOME="$sandbox/home"
  mkdir -p "$HOME/.hammerspoon"
  printf 'print("existing config")\n' > "$HOME/.hammerspoon/init.lua"
  LVS_ALLOW_NON_DARWIN=1 HOME="$HOME" LVS_CONFIG_DIR="$HOME/.local-voice-scribe" LVS_HAMMERSPOON_DIR="$HOME/.hammerspoon" \
    bash -c "source '$ROOT/scripts/setup.sh'; install_hammerspoon_loader" >/dev/null 2>&1
}

case_managed_block_updates() {
  local sandbox init_file
  sandbox="$(mktemp -d)"
  HOME="$sandbox/home"
  init_file="$HOME/.hammerspoon/init.lua"
  mkdir -p "$(dirname "$init_file")"
  cat > "$init_file" <<'EOF'
-- custom line
-- local-voice-scribe:start
dofile([[/old/path/init.lua]])
-- local-voice-scribe:end
EOF

  LVS_ALLOW_NON_DARWIN=1 HOME="$HOME" LVS_CONFIG_DIR="$HOME/.local-voice-scribe" LVS_HAMMERSPOON_DIR="$HOME/.hammerspoon" \
    bash -c "source '$ROOT/scripts/setup.sh'; install_hammerspoon_loader" >/dev/null

  grep -F -- '-- custom line' "$init_file" >/dev/null
  grep -F -- "dofile([[$ROOT/init.lua]])" "$init_file" >/dev/null
}

case_runtime_file_contains_token() {
  local sandbox
  sandbox="$(mktemp -d)"
  HOME="$sandbox/home"
  mkdir -p "$HOME/bin"
  : > "$HOME/bin/ffmpeg"
  chmod +x "$HOME/bin/ffmpeg"

  LVS_ALLOW_NON_DARWIN=1 HOME="$HOME" LVS_CONFIG_DIR="$HOME/.local-voice-scribe" LVS_HAMMERSPOON_DIR="$HOME/.hammerspoon" \
    bash -c "source '$ROOT/scripts/setup.sh'; FFMPEG_PATH='$HOME/bin/ffmpeg'; write_runtime_file test-token" >/dev/null

  grep -F 'install_token = [[test-token]],' "$HOME/.local-voice-scribe/runtime.lua" >/dev/null
  grep -F "repo_root = [[$ROOT]]," "$HOME/.local-voice-scribe/runtime.lua" >/dev/null
}

case_doctor_succeeds_with_matching_token() {
  local sandbox mockbin runtime_dir hammerspoon_dir status_url
  sandbox="$(mktemp -d)"
  HOME="$sandbox/home"
  runtime_dir="$HOME/.local-voice-scribe"
  hammerspoon_dir="$HOME/.hammerspoon"
  mockbin="$sandbox/mockbin"
  status_url="http://127.0.0.1:8989/state"

  mkdir -p "$runtime_dir/whisper/bin" "$runtime_dir/whisper/public" "$runtime_dir/models" "$hammerspoon_dir" "$mockbin"
  : > "$runtime_dir/whisper/bin/whisper-server"
  : > "$runtime_dir/models/ggml-large-v3-turbo.bin"
  : > "$mockbin/ffmpeg"
  chmod +x "$runtime_dir/whisper/bin/whisper-server" "$mockbin/ffmpeg"

  cat > "$runtime_dir/runtime.lua" <<EOF
return {
    ffmpeg_path = [[$mockbin/ffmpeg]],
    whisper_server_path = [[$runtime_dir/whisper/bin/whisper-server]],
    whisper_public_path = [[$runtime_dir/whisper/public]],
    model_path = [[$runtime_dir/models/ggml-large-v3-turbo.bin]],
    ggml_metal_path_resources = nil,
    install_token = [[test-token]],
    repo_root = [[$ROOT]],
}
EOF

  cat > "$hammerspoon_dir/init.lua" <<'EOF'
-- local-voice-scribe:start
dofile([[placeholder]])
-- local-voice-scribe:end
EOF

  cat > "$mockbin/curl" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-fsS" ] || [ "$1" = "-fsSL" ]; then
  shift
fi
if [ "$1" = "--max-time" ]; then
  shift 2
fi
printf '{"state":"idle","install_token":"test-token"}'
EOF
  chmod +x "$mockbin/curl"

  MODEL_SHA256="$(portable_sha256 "$runtime_dir/models/ggml-large-v3-turbo.bin")"

  LVS_ALLOW_NON_DARWIN=1 HOME="$HOME" LVS_CONFIG_DIR="$runtime_dir" LVS_HAMMERSPOON_DIR="$hammerspoon_dir" \
    CURL_BIN="$mockbin/curl" MODEL_SHA256="$MODEL_SHA256" \
    bash -c "source '$ROOT/scripts/setup.sh'; doctor" >/dev/null
}

run_expect_failure "unmanaged-init-aborts" case_unmanaged_init_aborts
run_expect_success "managed-block-updates" case_managed_block_updates
run_expect_success "runtime-file-contains-token" case_runtime_file_contains_token
run_expect_success "doctor-succeeds-with-matching-token" case_doctor_succeeds_with_matching_token

printf '\n%d passed, %d failed\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
