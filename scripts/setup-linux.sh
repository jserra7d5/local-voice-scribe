#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/setup-linux.sh [--yes] [--doctor]

Modes:
  --yes     Skip all confirmation prompts
  --doctor  Validate the current install without changing it

Install/update mode will:
  - install required system packages (ffmpeg, cmake, curl, etc.)
  - download and verify the large-v3-turbo whisper model (~1.6 GB)
  - download and build whisper.cpp with CUDA GPU acceleration
  - create a Python virtual environment with pynput and PyQt6
  - detect audio input devices (Focusrite Scarlett if connected)
  - write ~/.local-voice-scribe/runtime.json
  - create desktop launcher and autostart entries

Prerequisites:
  - Ubuntu/Debian-based Linux (apt)
  - NVIDIA GPU with proprietary drivers installed
  - CUDA toolkit (installed automatically if missing)
  - Python 3.10+ with venv support
EOF
}

log() {
  printf '[setup] %s\n' "$*"
}

warn() {
  printf '[setup] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[setup] ERROR: %s\n' "$*" >&2
  exit 1
}

confirm() {
  if [ "$AUTO_YES" -eq 1 ]; then return; fi
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

  log "Downloading $(basename "$dest")..."
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
  # Fallback: common architectures
  # 75 = Turing (RTX 20xx), 86 = Ampere (RTX 30xx), 89 = Ada (RTX 40xx)
  warn "Could not detect GPU compute capability, defaulting to 89 (RTX 40xx)"
  printf '89\n'
}

detect_focusrite() {
  if command -v pactl >/dev/null 2>&1; then
    # Match input sources only (alsa_input.*), not output monitors (alsa_output.*.monitor)
    pactl list sources short 2>/dev/null | grep -i 'scarlett\|focusrite' | grep 'alsa_input\.' | awk '{print $2}' | head -1
  fi
}

# ─── System package management ───

check_nvidia_driver() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    die "NVIDIA driver not found. Install your GPU's proprietary driver first:
  Ubuntu:  sudo ubuntu-drivers install
  Manual:  https://www.nvidia.com/Download/index.aspx

Then rerun this script."
  fi
  local driver_version
  driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
  log "NVIDIA driver: $driver_version"
}

install_system_packages() {
  # Packages we need and their apt names
  local -a needed=()
  local -A pkg_for_cmd=(
    [ffmpeg]=ffmpeg
    [cmake]=cmake
    [curl]=curl
    [xclip]=xclip
    [xdotool]=xdotool
    [notify-send]=libnotify-bin
    [pactl]=pulseaudio-utils
    [lsof]=lsof
    [xdg-open]=xdg-utils
    [g++]=g++
    [pkg-config]=pkg-config
  )

  for cmd in "${!pkg_for_cmd[@]}"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      needed+=("${pkg_for_cmd[$cmd]}")
    fi
  done

  # CUDA toolkit
  if ! command -v nvcc >/dev/null 2>&1; then
    needed+=(nvidia-cuda-toolkit)
  fi

  if [ ${#needed[@]} -eq 0 ]; then
    log "All required system packages found."
    return
  fi

  log "Missing packages: ${needed[*]}"
  log "Installing with apt..."
  confirm "Run 'sudo apt install ${needed[*]}'? [Y/n] "
  sudo apt update -qq
  sudo apt install -y "${needed[@]}"
  log "System packages installed."
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

  # Check version >= 3.10 (needed for X | Y union syntax)
  local ver
  ver="$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")"
  local major minor
  major="${ver%%.*}"
  minor="${ver##*.}"
  if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
    die "Python >= 3.10 required, found $ver"
  fi

  # Check venv module is available
  if ! "$py" -m venv --help >/dev/null 2>&1; then
    log "Python venv module missing. Installing python3-venv..."
    confirm "Run 'sudo apt install python3-venv'? [Y/n] "
    sudo apt install -y python3-venv
  fi

  PYTHON_BIN="$py"
  log "Using Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
}

# ─── Build and install ───

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
  # Skip rebuild if binary already exists and is executable
  if [ -x "$WHISPER_SERVER_DEST" ]; then
    log "whisper-server already built at $WHISPER_SERVER_DEST"
    confirm "Rebuild whisper-server? [y/N] "
  fi

  local source_archive temp_root source_root build_dir cuda_arch built_server

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
  [ -x "$built_server" ] || die "whisper-server build failed. Check cmake output above."

  mkdir -p "$WHISPER_HOME/bin"
  cp "$built_server" "$WHISPER_SERVER_DEST"
  log "whisper-server installed to $WHISPER_SERVER_DEST"

  rm -rf "$temp_root"
}

setup_python_venv() {
  log "Setting up Python virtual environment..."
  if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
  # Use public PyPI only and suppress interactive prompts from private indexes
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip \
    --index-url https://pypi.org/simple/ --no-input 2>/dev/null
  "$VENV_DIR/bin/pip" install --quiet \
    --index-url https://pypi.org/simple/ --no-input \
    -r "$REPO_ROOT/linux/requirements.txt"
  log "Python venv ready at $VENV_DIR"
}

write_runtime_config() {
  local install_token="$1"
  local audio_device="$2"
  mkdir -p "$CONFIG_DIR"
  # Use Python to generate valid JSON (handles special chars in paths safely)
  "$PYTHON_BIN" -c "
import json, sys
data = {
    'ffmpeg_path': sys.argv[1],
    'whisper_server_path': sys.argv[2],
    'model_path': sys.argv[3],
    'install_token': sys.argv[4],
    'repo_root': sys.argv[5],
    'audio_device': sys.argv[6] if sys.argv[6] else None,
}
with open(sys.argv[7], 'w') as f:
    json.dump(data, f, indent=4)
" \
    "$(command -v ffmpeg)" \
    "$WHISPER_SERVER_DEST" \
    "$MODEL_PATH" \
    "$install_token" \
    "$REPO_ROOT" \
    "${audio_device:-}" \
    "$RUNTIME_JSON"
  log "Runtime config written to $RUNTIME_JSON"
}

create_launcher_script() {
  cat > "$LAUNCHER_SCRIPT" <<EOF
#!/usr/bin/env bash
# Local Voice Scribe - Linux launcher
cd "$REPO_ROOT" || exit 1
exec "$VENV_DIR/bin/python3" -m linux "\$@"
EOF
  chmod +x "$LAUNCHER_SCRIPT"
  log "Launcher script: $LAUNCHER_SCRIPT"
}

create_desktop_entry() {
  mkdir -p "$(dirname "$DESKTOP_FILE")"
  local escaped_launcher
  escaped_launcher="$(printf '%s' "$LAUNCHER_SCRIPT" | sed 's/ /\\ /g')"
  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Local Voice Scribe
Comment=Local voice recording and transcription
Exec=${escaped_launcher}
Terminal=false
Categories=Audio;Utility;
StartupNotify=false
EOF
  log "Desktop entry: $DESKTOP_FILE"
}

create_autostart_entry() {
  mkdir -p "$(dirname "$AUTOSTART_FILE")"
  local escaped_launcher
  escaped_launcher="$(printf '%s' "$LAUNCHER_SCRIPT" | sed 's/ /\\ /g')"
  cat > "$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Local Voice Scribe
Comment=Local voice recording and transcription
Exec=${escaped_launcher}
Terminal=false
Categories=Audio;Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
EOF
  log "Autostart entry: $AUTOSTART_FILE"
}

# ─── Doctor ───

doctor() {
  local install_token audio_device

  [ -f "$RUNTIME_JSON" ] || die "Missing runtime config at $RUNTIME_JSON"

  local runtime_ffmpeg runtime_server runtime_model
  runtime_ffmpeg="$(python3 -c "import json; print(json.load(open('$RUNTIME_JSON')).get('ffmpeg_path',''))" 2>/dev/null || true)"
  runtime_server="$(python3 -c "import json; print(json.load(open('$RUNTIME_JSON')).get('whisper_server_path',''))" 2>/dev/null || true)"
  runtime_model="$(python3 -c "import json; print(json.load(open('$RUNTIME_JSON')).get('model_path',''))" 2>/dev/null || true)"

  [ -x "$runtime_ffmpeg" ] || die "ffmpeg not found at $runtime_ffmpeg"
  [ -x "$runtime_server" ] || die "Missing whisper-server at $runtime_server"
  [ -f "$runtime_model" ] || die "Missing model at $runtime_model"
  [ "$(sha256_file "$runtime_model")" = "$MODEL_SHA256" ] || die "Model checksum mismatch at $runtime_model"

  [ -d "$VENV_DIR" ] || die "Python venv missing at $VENV_DIR"
  "$VENV_DIR/bin/python3" -c "from Xlib import X" 2>/dev/null || warn "python-xlib not installed in venv (hotkeys will fall back to pynput which may leak keystrokes)"
  "$VENV_DIR/bin/python3" -c "import pynput" 2>/dev/null || warn "pynput not installed in venv (no fallback hotkey backend)"
  "$VENV_DIR/bin/python3" -c "import PyQt6" 2>/dev/null || warn "PyQt6 not installed in venv (border overlay will be disabled)"

  command -v nvidia-smi >/dev/null 2>&1 || warn "nvidia-smi not found (GPU acceleration may not work)"

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
  - model checksum: OK
  - Focusrite Scarlett: $audio_status
  - Python venv: $VENV_DIR
  - whisper-server: $runtime_server
  - launcher: $LAUNCHER_SCRIPT
EOF
}

# ─── Main ───

main() {
  local doctor_mode=0
  local install_token audio_device

  while [ $# -gt 0 ]; do
    case "$1" in
      --yes)
        AUTO_YES=1
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

  if [ "$AUTO_YES" -ne 1 ]; then
    log "Repo: $REPO_ROOT"
    confirm "Install or update Local Voice Scribe on this Linux machine? [Y/n] "
  fi

  check_nvidia_driver
  install_system_packages
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

===================================
  Local Voice Scribe is installed!
===================================

Start now:
  $LAUNCHER_SCRIPT

Or launch "Local Voice Scribe" from your application menu.
It will auto-start on next login.

Configuration:
  Runtime config:  $RUNTIME_JSON
  User overrides:  $CONFIG_DIR/config.json (create to customize)
  Dictionary:      $CONFIG_DIR/dictionary.txt

Hotkeys (defaults):
  Super+Alt+R  Toggle recording
  Super+Alt+C  Open settings window
  Super+Alt+T  Open transcript folder

Verify install:
  ./scripts/setup-linux.sh --doctor
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AUTO_YES=0

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
