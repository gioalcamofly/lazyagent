#!/bin/sh
# smoke-wheel.sh WHEEL binary|none
# Install WHEEL as a uv tool in a fresh place, with no Rust on PATH, and check
# `lazyagent desktop` finds the app inside it (binary) or explains how to get
# one (none: the pure wheel).
set -eu
wheel=$(realpath "$1")
expect=$2
uv=$(command -v uv)
tmp=$(mktemp -d)
export UV_TOOL_DIR="$tmp/tools" UV_TOOL_BIN_DIR="$tmp/bin"
"$uv" tool install --python 3.12 "$wheel"
PATH="$UV_TOOL_BIN_DIR:/usr/bin:/bin"
if command -v cargo; then echo "cargo is on PATH: not a clean box" >&2; exit 1; fi
set -x
help=$(lazyagent desktop --help)
echo "$help"
case $expect in
binary)
    app=$(echo "$help" | sed -n 's/^app: //p')
    case $app in "$UV_TOOL_DIR"/lazyagent/*/lazyagent/bin/lazyagent-desktop) ;; *) exit 1 ;; esac
    ldd "$app"
    "$app" --help | grep -q '^usage: lazyagent-desktop'
    # Through `lazyagent desktop`'s exec, args passed on to the app.
    lazyagent desktop --theme dark --help | grep -q '^usage: lazyagent-desktop'
    ;;
none)
    echo "$help" | grep -q '^app: not built'
    if lazyagent desktop 2>"$tmp/err"; then exit 1; fi
    cat "$tmp/err"
    grep -q -- '--git https://github.com/gioalcamofly/lazyagent --tag v' "$tmp/err"
    ;;
*) exit 2 ;;
esac
echo "smoke ok: $(basename "$wheel")"
