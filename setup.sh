#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-time setup on macOS (Apple Silicon or Intel).
#
#   ./setup.sh
#
# 1. Homebrew packages: python@3.12, cmake, libusb, pkg-config
# 2. ./.venv with numpy + opencv
# 3. pyrealsense2 2.59.1 built from source WITH the macOS IMU patch
#    (Intel ships no macOS wheel, and stock librealsense compiles the IMU out
#    on macOS -- the patch in patches/ turns accel + gyro back on).
#
# Takes ~5-10 minutes the first time (the build). Re-running it is safe.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
VENV_PY="$VENV/bin/python"
VERSION="2.59.1"
WORK="$ROOT/.build/librealsense-$VERSION"

step() { printf '\033[36m[setup] %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m[setup] %s\033[0m\n' "$*"; }
err()  { printf '\033[31m[setup] %s\033[0m\n' "$*" >&2; }

[ "$(uname -s)" = "Darwin" ] || { err "This is for macOS only."; exit 1; }

xcode-select -p >/dev/null 2>&1 || {
    err "Xcode command line tools are missing. Run:  xcode-select --install   then re-run ./setup.sh"
    exit 1
}

BREW="$(command -v brew || true)"
[ -z "$BREW" ] && [ -x /opt/homebrew/bin/brew ] && BREW=/opt/homebrew/bin/brew
[ -z "$BREW" ] && [ -x /usr/local/bin/brew ] && BREW=/usr/local/bin/brew
[ -n "$BREW" ] || { err "Homebrew is required: https://brew.sh"; exit 1; }
export PATH="$("$BREW" --prefix)/bin:$PATH"

for f in python@3.12 cmake libusb pkg-config; do
    if "$BREW" list --formula "$f" >/dev/null 2>&1; then ok "$f already installed"
    else step "brew install $f"; "$BREW" install "$f"; fi
done

if [ ! -x "$VENV_PY" ]; then
    step "Creating $VENV"
    "$("$BREW" --prefix python@3.12)/bin/python3.12" -m venv "$VENV"
fi
step "Installing Python packages"
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install --quiet -r "$ROOT/requirements.txt"

if "$VENV_PY" -c "import pyrealsense2, sys; sys.exit(pyrealsense2.__version__ != '$VERSION')" 2>/dev/null; then
    ok "pyrealsense2 $VERSION already built"
else
    if [ ! -d "$WORK" ]; then
        step "Downloading librealsense v$VERSION source"
        mkdir -p "$(dirname "$WORK")"
        curl -fsSL "https://github.com/realsenseai/librealsense/archive/refs/tags/v$VERSION.tar.gz" \
            | tar xz -C "$(dirname "$WORK")"
    fi
    PATCH="$ROOT/patches/librealsense-$VERSION-macos-imu.patch"
    if [ ! -f "$WORK/.imu-patched" ]; then
        step "Applying macOS IMU patch"
        patch -d "$WORK" -p1 --forward < "$PATCH"
        touch "$WORK/.imu-patched"
    fi

    step "Building pyrealsense2 $VERSION (5-10 minutes; logs in $WORK/*.log)"
    cmake -S "$WORK" -B "$WORK/build" -DCMAKE_BUILD_TYPE=Release \
        -DENABLE_CCACHE=OFF -DBUILD_WITH_OPENMP=OFF -DCMAKE_CXX_STANDARD=17 \
        -DCHECK_FOR_UPDATES=OFF -DENABLE_AI_ASSISTANT=OFF -DENABLE_STATS=OFF \
        -DBUILD_EXAMPLES=OFF -DBUILD_GRAPHICAL_EXAMPLES=OFF -DBUILD_TOOLS=OFF \
        -DBUILD_UNIT_TESTS=OFF -DBUILD_PYTHON_BINDINGS=ON \
        -DCMAKE_CXX_FLAGS="-DRS2_MACOS_MOTION" \
        -DPYTHON_EXECUTABLE="$VENV_PY" >"$WORK/cmake.log" 2>&1 \
        || { err "cmake configure failed -- see $WORK/cmake.log"; exit 1; }
    cmake --build "$WORK/build" -j"$(sysctl -n hw.ncpu)" >"$WORK/build.log" 2>&1 \
        || { err "build failed -- see $WORK/build.log"; tail -30 "$WORK/build.log"; exit 1; }

    step "Installing pyrealsense2 into .venv"
    SITE="$("$VENV_PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    PKG="$SITE/pyrealsense2"
    REL="$WORK/build/Release"
    [ -d "$REL" ] || REL="$WORK/build"
    MM="${VERSION%.*}"
    EXT="$("$VENV_PY" -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
    rm -rf "$PKG" && mkdir -p "$PKG"
    cp "$REL/pyrealsense2.$MM$EXT" "$REL/librealsense2.$MM.dylib" "$PKG/"
    ln -s "pyrealsense2.$MM$EXT" "$PKG/pyrealsense2$EXT"
    # Make the build relocatable (it bakes its own build dir in as the rpath).
    for f in "$PKG/pyrealsense2.$MM$EXT" "$PKG/librealsense2.$MM.dylib"; do
        install_name_tool -delete_rpath "$REL" "$f" 2>/dev/null || true
        install_name_tool -add_rpath @loader_path "$f"
        codesign -f -s - "$f" 2>/dev/null
    done
    cat >"$PKG/__init__.py" <<EOF
# pyrealsense2 $VERSION built from source (with macOS IMU patch) by setup.sh
from .pyrealsense2 import *
__version__ = "$VERSION"
EOF
fi

(cd / && "$VENV_PY" -c "import pyrealsense2 as rs, cv2; print('[setup] pyrealsense2', rs.__version__, '+ opencv', cv2.__version__, 'OK')")
ok "Done. Plug the camera into a USB 3 port, then:  ./record.sh"
