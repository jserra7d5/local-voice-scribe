#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/setup-linux.sh [--yes] [--doctor]

Modes:
  --yes     Skip the confirmation prompt for install/update mode
  --doctor  Validate the current install without changing it

Install/update mode will:
  - check for required system packages (ffmpeg, cmake, curl, etc.)
  - download and verify the large-v3-turbo model
  - download and build whisper.cpp with CUDA support
  - create a Python virtual environment with pynput and PyQt6
  - detect the Focusrite Scarlett audio device
  - write ~/.local-voice-scribe/runtime.json
  - create XDG desktop and autostart entries
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
  sha256sum "$1" | awk '{print $1}'
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
  curl -fL --progress-bar "$url" -o "$tmp_file"

  actual_sha="$(sha256_file "$tmp_file")"
  [ "$actual_sha" = "$expected_sha" ] || die "Checksum mismatch for $(basename "$dest"): expected $expected_sha, got $actual_sha"

  mv "$tmp_file" "$dest"
  trap - RETURN
}

generate_install_token() {
  local epoch rand
  epoch="$(date +%s)"
  rand="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
  printf 'install-%s-%s\n' "$epoch" "$rand"
}

detect_cuda_arch() {
  if command -v nvidia-smi >/dev/null 2>&1; then
    local cap
    cap="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '.')"
    if [ -n "$cap" ]; then
      printf '%s\n' "$cap"
      return 0
    fi
  fi
  # Default to Ada Lovelace (RTX 40xx)
  printf '89\n'
}

detect_focusrite() {
  if command -v pactl >/dev/null 2>&1; then
    pactl list sources short 2>/dev/null | grep -i 'scarlett\|focusrite' | grep -i 'analog.stereo' | awk '{print $2}' | head -1
  fi
}

check_system_packages() {
  local missing=()
  local pkg_map=(
    "ffmpeg:ffmpeg"
    "cmake:cmake"
    "curl:curl"
    "xclip:xclip"
    "xdotool:xdotool"
    "notify-send:libnotify-bin"
    "pactl:pulseaudio-utils"
    "lsof:lsof"
  )

  for entry in "${pkg_map[@]}"; do
    local cmd="${entry%%:*}"
    local pkg="${entry##*:}"
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing+=("$pkg")
    fi
  done

  # Check for CUDA toolkit
  if ! command -v nvcc >/dev/null 2>&1; then
    missing+=("nvidia-cuda-toolkit")
  fi

  if [ ${#missing[@]} -gt 0 ]; then
    log "Missing packages: ${missing[*]}"
    log "Install them with:"
    log "  sudo apt install ${missing[*]}"
    die "Required packages missing. Install them and rerun setup."
  fi
  log "All required system packages found."
}

check_python() {
  local py=""
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      py="$candidate"
      break
    fi
  done
  [ -n "$py" ] || die "Python 3 not found. Install python3."
  PYTHON_BIN="$py"
  log "Using Python: $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"
}

ensure_model() {
  mkdir -p "$MODEL_DIR"
  if [ -f "$MODEL_PATH" ]; then
    local actual_sha
    actual_sha="$(sha256_file "$MODEL_PATH")"
    if [ "$actual_sha" = "$MODEL_SHA256" ]; then
      log "Model already present and verified."
      return
    else
      die "Existing model checksum mismatch at $MODEL_PATH. Remove it and rerun setup."
    fi
  fi
  download_with_sha "$MODEL_URL" "$MODEL_PATH" "$MODEL_SHA256"
}

build_whisper_server() {
  local source_archive source_dir build_dir temp_root source_root built_server cuda_arch

  mkdir -p "$CACHE_DIR"
  source_archive="$CACHE_DIR/whisper.cpp-$WHISPER_VERSION.tar.gz"
  if [ ! -f "$source_archive" ] || [ "$(sha256_file "$source_archive")" != "$WHISPER_SOURCE_SHA256" ]; then
    download_with_sha "$WHISPER_SOURCE_URL" "$source_archive" "$WHISPER_SOURCE_SHA256"
  else
    log "Using cached whisper.cpp source archive."
  fi

  cuda_arch="$(detect_cuda_arch)"
  log "Building whisper.cpp with CUDA (compute capability: $cuda_arch)"

  temp_root="$(mktemp -d)"
  tar -xzf "$source_archive" -C "$temp_root"
  source_root="$(find "$temp_root" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -n "$source_root" ] || die "Failed to unpack whisper.cpp source archive."

  build_dir="$temp_root/build"
  cmake -S "$source_root" -B "$build_dir" \
    -DCMAKE_BUILD_TYPE=Release \
    -DWHISPER_BUILD_SERVER=ON \
    -DWHISPER_BUILD_EXAMPLES=ON \
    -DWHISPER_BUILD_TESTS=OFF \
    -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES="$cuda_arch"

  cmake --build "$build_dir" --config Release -j "$BUILD_JOBS"

  built_server="$build_dir/bin/whisper-server"
  [ -x "$built_server" ] || die "whisper-server build failed."

  mkdir -p "$WHISPER_HOME/bin"
  cp "$built_server" "$WHISPER_SERVER_DEST"
  log "whisper-server installed to $WHISPER_SERVER_DEST"

  rm -rf "$temp_root"
}

setup_python_venv() {
  log "Setting up Python virtual environment."
  if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -r "$REPO_ROOT/linux/requirements.txt"
  log "Python venv ready at $VENV_DIR"
}

write_runtime_config() {
  local install_token="$1"
  local audio_device="$2"
  mkdir -p "$CONFIG_DIR"
  cat > "$RUNTIME_JSON" <<EOF
{
    "ffmpeg_path": "$(command -v ffmpeg)",
    "whisper_server_path": "$WHISPER_SERVER_DEST",
    "model_path": "$MODEL_PATH",
    "install_token": "$install_token",
    "repo_root": "$REPO_ROOT",
    "audio_device": $([ -n "$audio_device" ] && printf '"%s"' "$audio_device" || printf 'null')
}
EOF
  log "Runtime config written to $RUNTIME_JSON"
}

create_launcher_script() {
  cat > "$LAUNCHER_SCRIPT" <<EOF
#!/usr/bin/env bash
# Local Voice Scribe - Linux launcher
exec "$VENV_DIR/bin/python3" -m linux "\$@"
EOF
  chmod +x "$LAUNCHER_SCRIPT"
  log "Launcher script: $LAUNCHER_SCRIPT"
}

create_desktop_entry() {
  mkdir -p "$(dirname "$DESKTOP_FILE")"
  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Local Voice Scribe
Comment=Local voice recording and transcription
Exec=$LAUNCHER_SCRIPT
Terminal=false
Categories=Audio;Utility;
StartupNotify=false
EOF
  log "Desktop entry: $DESKTOP_FILE"
}

create_autostart_entry() {
  mkdir -p "$(dirname "$AUTOSTART_FILE")"
  cat > "$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Local Voice Scribe
Comment=Local voice recording and transcription
Exec=$LAUNCHER_SCRIPT
Terminal=false
Categories=Audio;Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
EOF
  log "Autostart entry: $AUTOSTART_FILE"
}

doctor() {
  local install_token audio_device

  [ -f "$RUNTIME_JSON" ] || die "Missing runtime config at $RUNTIME_JSON"

  # Check runtime paths from JSON
  local runtime_ffmpeg runtime_server runtime_model
  runtime_ffmpeg="$(python3 -c "import json; print(json.load(open('$RUNTIME_JSON')).get('ffmpeg_path',''))" 2>/dev/null || true)"
  runtime_server="$(python3 -c "import json; print(json.load(open('$RUNTIME_JSON')).get('whisper_server_path',''))" 2>/dev/null || true)"
  runtime_model="$(python3 -c "import json; print(json.load(open('$RUNTIME_JSON')).get('model_path',''))" 2>/dev/null || true)"

  [ -x "$runtime_ffmpeg" ] || die "ffmpeg not found at $runtime_ffmpeg"
  [ -x "$runtime_server" ] || die "Missing whisper-server at $runtime_server"
  [ -f "$runtime_model" ] || die "Missing model at $runtime_model"
  [ "$(sha256_file "$runtime_model")" = "$MODEL_SHA256" ] || die "Model checksum mismatch at $runtime_model"

  [ -d "$VENV_DIR" ] || die "Python venv missing at $VENV_DIR"
  "$VENV_DIR/bin/python3" -c "import pynput" 2>/dev/null || die "pynput not installed in venv"

  install_token="$(python3 -c "import json; print(json.load(open('$RUNTIME_JSON')).get('install_token',''))" 2>/dev/null || true)"
  [ -n "$install_token" ] || die "Missing install_token in $RUNTIME_JSON"

  audio_device="$(detect_focusrite)"
  local audio_status
  if [ -n "$audio_device" ]; then
    audio_status="detected ($audio_device)"
  else
    audio_status="not detected (will use system default)"
  fi

  cat <<EOF
Local Voice Scribe (Linux) doctor check passed.
  - install token: $install_token
  - model checksum matches
  - Focusrite Scarlett: $audio_status
  - Python venv: $VENV_DIR
  - whisper-server: $runtime_server
  - launcher: $LAUNCHER_SCRIPT
EOF
}

main() {
  local auto_yes=0
  local doctor_mode=0
  local install_token audio_device

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

  [ "$(uname -s)" = "Linux" ] || die "This setup script only supports Linux."

  if [ "$doctor_mode" -eq 1 ]; then
    doctor
    exit 0
  fi

  if [ "$auto_yes" -ne 1 ]; then
    log "Repo: $REPO_ROOT"
    confirm "Install or update Local Voice Scribe on this Linux machine? [Y/n] "
  fi

  check_system_packages
  check_python
  ensure_model
  build_whisper_server
  setup_python_venv

  install_token="$(generate_install_token)"
  audio_device="$(detect_focusrite)"
  if [ -n "$audio_device" ]; then
    log "Focusrite Scarlett detected: $audio_device"
  else
    log "No Focusrite Scarlett found. Will use system default audio input."
  fi

  write_runtime_config "$install_token" "$audio_device"
  create_launcher_script
  create_desktop_entry
  create_autostart_entry

  # Create dictionary file if missing
  [ -f "$CONFIG_DIR/dictionary.txt" ] || touch "$CONFIG_DIR/dictionary.txt"

  cat <<EOF

Local Voice Scribe (Linux) is installed.

To start:
  $LAUNCHER_SCRIPT

Or launch "Local Voice Scribe" from your application menu.
It will auto-start on next login.

Configuration:
  Runtime: $RUNTIME_JSON
  User config: $CONFIG_DIR/config.json (create to override defaults)
  Dictionary: $CONFIG_DIR/dictionary.txt

Hotkeys (defaults):
  Super+Alt+R  - Toggle recording
  Super+Alt+C  - Open dictionary editor
  Super+Alt+T  - Open transcript folder

For a non-mutating verification pass:
  ./scripts/setup-linux.sh --doctor
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

WHISPER_VERSION="${WHISPER_VERSION:-v1.8.4}"
WHISPER_SOURCE_URL="${WHISPER_SOURCE_URL:-https://github.com/ggml-org/whisper.cpp/archive/refs/tags/$WHISPER_VERSION.tar.gz}"
WHISPER_SOURCE_SHA256="${WHISPER_SOURCE_SHA256:-b26f30e52c095ccb75da40b168437736605eb280de57381887bf9e2b65f31e66}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin}"
MODEL_SHA256="${MODEL_SHA256:-1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69}"

CONFIG_DIR="${LVS_CONFIG_DIR:-$HOME/.local-voice-scribe}"
MODEL_DIR="$CONFIG_DIR/models"
MODEL_PATH="$MODEL_DIR/ggml-large-v3-turbo.bin"
RUNTIME_JSON="$CONFIG_DIR/runtime.json"
CACHE_DIR="$CONFIG_DIR/cache"
WHISPER_HOME="$CONFIG_DIR/whisper"
WHISPER_SERVER_DEST="$WHISPER_HOME/bin/whisper-server"
VENV_DIR="$CONFIG_DIR/venv"
LAUNCHER_SCRIPT="$REPO_ROOT/local-voice-scribe-linux"
DESKTOP_FILE="$HOME/.local/share/applications/local-voice-scribe.desktop"
AUTOSTART_FILE="$HOME/.config/autostart/local-voice-scribe.desktop"
BUILD_JOBS="${BUILD_JOBS:-$(nproc 2>/dev/null || echo 4)}"

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
