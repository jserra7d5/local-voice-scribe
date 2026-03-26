#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/setup.sh [--yes] [--doctor]

Modes:
  --yes     Skip the confirmation prompt for install/update mode
  --doctor  Validate the current install without changing it

Install/update mode will:
  - install Homebrew if needed
  - install ffmpeg, cmake, and Hammerspoon
  - download and verify the large-v3-turbo model
  - download and build a pinned whisper.cpp with whisper-server enabled
  - write ~/.local-voice-scribe/runtime.lua
  - install or update the managed Hammerspoon loader block
  - restart Hammerspoon and verify the expected install token is live
EOF
}

log() {
  printf '[setup] %s\n' "$*"
}

die() {
  printf '[setup] ERROR: %s\n' "$*" >&2
  exit 1
}

confirm() {
  local prompt="${1:-Proceed? [Y/n] }"
  local reply
  read -r -p "$prompt" reply
  case "${reply:-y}" in
    y|Y|yes|YES) ;;
    *) exit 0 ;;
  esac
}

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    die "Neither shasum nor sha256sum is available."
  fi
}

lua_string_or_nil() {
  if [ -n "${1:-}" ]; then
    printf '[[%s]]' "$1"
  else
    printf 'nil'
  fi
}

find_brew_bin() {
  if [ -n "${BREW_BIN_OVERRIDE:-}" ]; then
    printf '%s\n' "$BREW_BIN_OVERRIDE"
    return 0
  fi

  if command -v brew >/dev/null 2>&1; then
    command -v brew
    return 0
  fi

  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

wait_for_http() {
  local url="$1"
  local timeout="${2:-30}"
  local attempt=1
  while [ "$attempt" -le "$timeout" ]; do
    if "$CURL_BIN" -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done
  return 1
}

wait_for_state_token() {
  local token="$1"
  local timeout="${2:-60}"
  local attempt=1
  local state_json
  while [ "$attempt" -le "$timeout" ]; do
    state_json="$("$CURL_BIN" -fsS --max-time 2 "$STATUS_URL" 2>/dev/null || true)"
    if [ -n "$state_json" ] && printf '%s' "$state_json" | grep -F "\"install_token\":\"$token\"" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done
  return 1
}

read_runtime_value() {
  local key="$1"
  local line
  [ -f "$RUNTIME_FILE" ] || return 1
  line="$(sed -n "s/^[[:space:]]*$key = \\[\\[\\(.*\\)\\]\\],$/\\1/p" "$RUNTIME_FILE" | head -n 1)"
  [ -n "$line" ] || return 1
  printf '%s\n' "$line"
}

download_with_sha() {
  local url="$1"
  local dest="$2"
  local expected_sha="$3"
  local dest_dir tmp_file actual_sha

  dest_dir="$(dirname "$dest")"
  mkdir -p "$dest_dir"
  tmp_file="$(mktemp "$dest_dir/.download.XXXXXX")"

  cleanup_tmp_download() {
    rm -f "$tmp_file"
  }
  trap cleanup_tmp_download RETURN

  log "Downloading $(basename "$dest")"
  "$CURL_BIN" -fL --progress-bar "$url" -o "$tmp_file"

  actual_sha="$(sha256_file "$tmp_file")"
  [ "$actual_sha" = "$expected_sha" ] || die "Checksum mismatch for $(basename "$dest"): expected $expected_sha, got $actual_sha"

  mv "$tmp_file" "$dest"
  trap - RETURN
}

require_macos() {
  if [ "${LVS_ALLOW_NON_DARWIN:-0}" = "1" ]; then
    return
  fi
  [ "$(uname -s)" = "Darwin" ] || die "This setup script only supports macOS."
}

ensure_brew() {
  BREW_BIN="$(find_brew_bin || true)"
  if [ -z "$BREW_BIN" ]; then
    log "Homebrew not found. Installing Homebrew."
    NONINTERACTIVE=1 /bin/bash -c "$("$CURL_BIN" -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    BREW_BIN="$(find_brew_bin || true)"
  fi
  [ -n "$BREW_BIN" ] || die "Homebrew installation failed."
  eval "$("$BREW_BIN" shellenv)"
}

install_host_dependencies() {
  log "Installing host dependencies with Homebrew."
  "$BREW_BIN" install ffmpeg cmake
  if [ -d "/Applications/Hammerspoon.app" ] || "$BREW_BIN" list --cask hammerspoon >/dev/null 2>&1; then
    log "Hammerspoon cask already installed."
  else
    "$BREW_BIN" install --cask hammerspoon
  fi
}

generate_install_token() {
  local epoch rand
  epoch="$(date +%s)"
  rand="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
  printf 'install-%s-%s\n' "$epoch" "$rand"
}

render_managed_block() {
  cat <<EOF
$MARKER_START
dofile([[$REPO_INIT]])
$MARKER_END
EOF
}

print_manual_merge_instructions() {
  cat <<EOF >&2

Hammerspoon already has an unmanaged ~/.hammerspoon/init.lua.
This hardened installer will not edit arbitrary existing configs.

Add this block manually at the point you want Local Voice Scribe loaded:

$(render_managed_block)
EOF
}

install_hammerspoon_loader() {
  mkdir -p "$HAMMERSPOON_DIR"

  if [ -f "$HAMMERSPOON_INIT" ]; then
    if grep -F -- "$MARKER_START" "$HAMMERSPOON_INIT" >/dev/null 2>&1; then
      local tmp_file
      tmp_file="$(mktemp)"
      awk -v start="$MARKER_START" -v end="$MARKER_END" '
        $0 == start { skip = 1; next }
        $0 == end { skip = 0; next }
        !skip { print }
      ' "$HAMMERSPOON_INIT" > "$tmp_file"
      if [ -s "$tmp_file" ]; then
        printf '\n' >> "$tmp_file"
      fi
      render_managed_block >> "$tmp_file"
      mv "$tmp_file" "$HAMMERSPOON_INIT"
      return
    fi

    if grep -q '[^[:space:]]' "$HAMMERSPOON_INIT"; then
      print_manual_merge_instructions
      die "Refusing to modify unmanaged ~/.hammerspoon/init.lua"
    fi
  fi

  render_managed_block > "$HAMMERSPOON_INIT"
}

quit_hammerspoon() {
  if pgrep -x Hammerspoon >/dev/null 2>&1; then
    "$OSASCRIPT_BIN" -e 'tell application "Hammerspoon" to quit' >/dev/null 2>&1 || true
    local attempt=1
    while [ "$attempt" -le 15 ]; do
      if ! pgrep -x Hammerspoon >/dev/null 2>&1; then
        return
      fi
      sleep 1
      attempt=$((attempt + 1))
    done
    pkill -TERM -x Hammerspoon >/dev/null 2>&1 || true
    sleep 2
  fi
}

launch_hammerspoon() {
  "$OPEN_BIN" -a Hammerspoon
}

build_whisper_server() {
  local source_archive source_dir build_dir temp_root source_root built_server

  mkdir -p "$CACHE_DIR"
  source_archive="$CACHE_DIR/whisper.cpp-$WHISPER_VERSION.tar.gz"
  if [ ! -f "$source_archive" ] || [ "$(sha256_file "$source_archive")" != "$WHISPER_SOURCE_SHA256" ]; then
    download_with_sha "$WHISPER_SOURCE_URL" "$source_archive" "$WHISPER_SOURCE_SHA256"
  else
    log "Using cached whisper.cpp source archive."
  fi

  temp_root="$(mktemp -d)"
  tar -xzf "$source_archive" -C "$temp_root"
  source_root="$(find "$temp_root" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -n "$source_root" ] || die "Failed to unpack whisper.cpp source archive."

  build_dir="$temp_root/build"
  "$CMAKE_BIN" -S "$source_root" -B "$build_dir" \
    -DCMAKE_BUILD_TYPE=Release \
    -DWHISPER_BUILD_SERVER=ON \
    -DWHISPER_BUILD_EXAMPLES=ON \
    -DWHISPER_BUILD_TESTS=OFF \
    -DGGML_METAL=ON \
    -DGGML_METAL_EMBED_LIBRARY=ON

  "$CMAKE_BIN" --build "$build_dir" --config Release -j "$BUILD_JOBS"

  built_server="$build_dir/bin/whisper-server"
  [ -x "$built_server" ] || die "whisper-server build failed."

  mkdir -p "$WHISPER_HOME/bin"
  cp "$built_server" "$WHISPER_SERVER_DEST"
  if [ -d "$source_root/examples/server/public" ]; then
    rm -rf "$WHISPER_PUBLIC_DIR"
    mkdir -p "$WHISPER_PUBLIC_DIR"
    cp -R "$source_root/examples/server/public/." "$WHISPER_PUBLIC_DIR/"
  else
    rm -rf "$WHISPER_PUBLIC_DIR"
  fi
}

ensure_model() {
  mkdir -p "$MODEL_DIR"
  if [ -f "$MODEL_PATH" ]; then
    local actual_sha
    actual_sha="$(sha256_file "$MODEL_PATH")"
    [ "$actual_sha" = "$MODEL_SHA256" ] || die "Existing model checksum mismatch at $MODEL_PATH. Remove it and rerun setup."
    log "Model already present and verified."
    return
  fi
  download_with_sha "$MODEL_URL" "$MODEL_PATH" "$MODEL_SHA256"
}

write_runtime_file() {
  local install_token="$1"
  mkdir -p "$CONFIG_DIR"
  cat > "$RUNTIME_FILE" <<EOF
return {
    ffmpeg_path = $(lua_string_or_nil "$FFMPEG_PATH"),
    whisper_server_path = $(lua_string_or_nil "$WHISPER_SERVER_DEST"),
    whisper_public_path = $(lua_string_or_nil "$([ -d "$WHISPER_PUBLIC_DIR" ] && printf '%s' "$WHISPER_PUBLIC_DIR")"),
    model_path = $(lua_string_or_nil "$MODEL_PATH"),
    ggml_metal_path_resources = nil,
    install_token = $(lua_string_or_nil "$install_token"),
    repo_root = $(lua_string_or_nil "$REPO_ROOT"),
}
EOF
}

doctor() {
  local install_token state_json runtime_ffmpeg runtime_server runtime_public runtime_model

  [ -f "$RUNTIME_FILE" ] || die "Missing runtime config at $RUNTIME_FILE"

  runtime_ffmpeg="$(read_runtime_value ffmpeg_path || true)"
  runtime_server="$(read_runtime_value whisper_server_path || true)"
  runtime_public="$(read_runtime_value whisper_public_path || true)"
  runtime_model="$(read_runtime_value model_path || true)"
  [ -x "$runtime_ffmpeg" ] || die "ffmpeg not found at $runtime_ffmpeg"
  [ -x "$runtime_server" ] || die "Missing whisper-server at $runtime_server"
  if [ -n "$runtime_public" ]; then
    [ -d "$runtime_public" ] || die "Missing whisper public assets at $runtime_public"
  fi
  [ -f "$runtime_model" ] || die "Missing model at $runtime_model"
  [ "$(sha256_file "$runtime_model")" = "$MODEL_SHA256" ] || die "Model checksum mismatch at $runtime_model"
  [ -f "$HAMMERSPOON_INIT" ] || die "Missing ~/.hammerspoon/init.lua"
  grep -F -- "$MARKER_START" "$HAMMERSPOON_INIT" >/dev/null 2>&1 || die "Managed Hammerspoon block not installed"

  install_token="$(read_runtime_value install_token || true)"
  [ -n "$install_token" ] || die "Missing install_token in $RUNTIME_FILE"

  state_json="$("$CURL_BIN" -fsS --max-time 2 "$STATUS_URL" 2>/dev/null || true)"
  [ -n "$state_json" ] || die "Status API is not reachable at $STATUS_URL"
  printf '%s' "$state_json" | grep -F "\"install_token\":\"$install_token\"" >/dev/null 2>&1 || die "Running Hammerspoon instance does not match install token $install_token"

  cat <<EOF
Local Voice Scribe doctor check passed.
  - status API matches install token: $install_token
  - model checksum matches expected value
EOF
}

main() {
  local auto_yes=0
  local doctor_mode=0
  local install_token

  while [ $# -gt 0 ]; do
    case "$1" in
      --yes)
        auto_yes=1
        ;;
      --doctor)
        doctor_mode=1
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        usage
        die "Unknown argument: $1"
        ;;
    esac
    shift
  done

  require_macos

  if [ "$doctor_mode" -eq 1 ]; then
    doctor
    exit 0
  fi

  if [ "$auto_yes" -ne 1 ]; then
    log "Repo: $REPO_ROOT"
    confirm "Install or update Local Voice Scribe on this Mac? [Y/n] "
  fi

  ensure_brew
  install_host_dependencies
  ensure_model
  build_whisper_server

  install_token="$(generate_install_token)"
  FFMPEG_PATH="$(command -v ffmpeg || true)"
  [ -n "$FFMPEG_PATH" ] || die "ffmpeg was installed but is not on PATH."
  write_runtime_file "$install_token"
  install_hammerspoon_loader

  log "Restarting Hammerspoon."
  quit_hammerspoon
  launch_hammerspoon

  log "Waiting for Local Voice Scribe status server."
  wait_for_state_token "$install_token" 60 || die "Status API did not expose the expected install token. Hammerspoon did not load the new config."

  log "Waiting for whisper-server."
  wait_for_http "$WHISPER_URL" 90 || die "whisper-server did not come up. Check /tmp/whisper_debug.log and rerun setup."

  cat <<EOF

Local Voice Scribe is installed.

Verification:
  - Status API: $STATUS_URL
  - Whisper server: $WHISPER_URL
  - Install token: $install_token

Remaining manual approvals:
  - On first recording attempt, approve the macOS microphone prompt if shown.
  - If you use ducking, approve Hammerspoon automation prompts for Music/Spotify if shown.

For a non-mutating verification pass later:
  ./scripts/setup.sh --doctor
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_INIT="$REPO_ROOT/init.lua"

WHISPER_VERSION="${WHISPER_VERSION:-v1.8.4}"
WHISPER_SOURCE_URL="${WHISPER_SOURCE_URL:-https://github.com/ggml-org/whisper.cpp/archive/refs/tags/$WHISPER_VERSION.tar.gz}"
WHISPER_SOURCE_SHA256="${WHISPER_SOURCE_SHA256:-b26f30e52c095ccb75da40b168437736605eb280de57381887bf9e2b65f31e66}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin}"
MODEL_SHA256="${MODEL_SHA256:-1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69}"

CONFIG_DIR="${LVS_CONFIG_DIR:-$HOME/.local-voice-scribe}"
MODEL_DIR="$CONFIG_DIR/models"
MODEL_PATH="$MODEL_DIR/ggml-large-v3-turbo.bin"
RUNTIME_FILE="$CONFIG_DIR/runtime.lua"
CACHE_DIR="$CONFIG_DIR/cache"
WHISPER_HOME="$CONFIG_DIR/whisper"
WHISPER_SERVER_DEST="$WHISPER_HOME/bin/whisper-server"
WHISPER_PUBLIC_DIR="$WHISPER_HOME/public"

HAMMERSPOON_DIR="${LVS_HAMMERSPOON_DIR:-$HOME/.hammerspoon}"
HAMMERSPOON_INIT="$HAMMERSPOON_DIR/init.lua"
MARKER_START="-- local-voice-scribe:start"
MARKER_END="-- local-voice-scribe:end"

STATUS_URL="${STATUS_URL:-http://127.0.0.1:8989/state}"
WHISPER_URL="${WHISPER_URL:-http://127.0.0.1:8178/}"

OSASCRIPT_BIN="${OSASCRIPT_BIN:-osascript}"
OPEN_BIN="${OPEN_BIN:-open}"
CURL_BIN="${CURL_BIN:-curl}"
CMAKE_BIN="${CMAKE_BIN:-cmake}"
BUILD_JOBS="${BUILD_JOBS:-$(sysctl -n hw.ncpu 2>/dev/null || echo 4)}"
FFMPEG_PATH="${FFMPEG_PATH:-$(command -v ffmpeg || true)}"

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
