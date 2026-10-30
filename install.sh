#!/bin/sh
# Installs cad-eye-mcp's private runtime into ./runtime (portable Python, Python packages,
# CADCraft and CJK fonts). Nothing is written outside this folder.
#
#   ./install.sh            install or update
#   ./install.sh --force    re-download and re-extract everything
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
RUNTIME="$ROOT/runtime"
DOWNLOADS="$RUNTIME/downloads"
LOCK="$ROOT/runtime.lock.json"
FORCE=""
[ "${1:-}" = "--force" ] && FORCE="--force"

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  *) echo "Unsupported OS: $(uname -s) (use install.ps1 on Windows)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64|amd64) arch=x64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "Unsupported CPU: $(uname -m)" >&2; exit 1 ;;
esac
PLATFORM="$os-$arch"

# Read python.assets.<platform>.<field> from the lock file without needing jq or Python.
lock_field() {
  awk -v plat="\"$PLATFORM\": {" -v field="\"$1\": \"" '
    /"python": \{/ { in_py = 1 }
    in_py && index($0, plat) { in_plat = 1 }
    in_plat && index($0, field) {
      s = substr($0, index($0, field) + length(field)); sub(/".*/, "", s); print s; exit
    }' "$LOCK"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

PY="$RUNTIME/python/bin/python3"
PY_VERSION=$(awk -F'"' '/"python": \{/ { getline; print $4; exit }' "$LOCK")
if [ -n "$FORCE" ] || [ ! -f "$RUNTIME/python/.version-$PY_VERSION" ]; then
  echo "==> Python $PY_VERSION ($PLATFORM)"
  url=$(lock_field url)
  sha=$(lock_field sha256)
  [ -n "$url" ] && [ -n "$sha" ] || { echo "No Python build pinned for $PLATFORM" >&2; exit 1; }
  mkdir -p "$DOWNLOADS"
  archive="$DOWNLOADS/python-$PY_VERSION-$PLATFORM.tar.gz"
  if [ ! -f "$archive" ] || [ "$(sha256_of "$archive")" != "$sha" ]; then
    echo "    downloading $(basename "$archive")"
    curl -fsSL "$url" -o "$archive"
  fi
  actual=$(sha256_of "$archive")
  if [ "$actual" != "$sha" ]; then
    mv "$archive" "$archive.bad"
    echo "SHA-256 mismatch for Python: expected $sha, got $actual (kept as $archive.bad)" >&2
    exit 1
  fi
  tar -xzf "$archive" -C "$RUNTIME"
  touch "$RUNTIME/python/.version-$PY_VERSION"
fi

"$PY" -I "$ROOT/scripts/install_runtime.py" $FORCE

echo
echo "Installed. Register the server with your MCP client, for example:"
echo "  claude mcp add cad-eye -- \"$ROOT/cad-eye-mcp\""
echo "or add to your client's JSON config:"
echo "  { \"mcpServers\": { \"cad-eye\": { \"command\": \"$ROOT/cad-eye-mcp\" } } }"
