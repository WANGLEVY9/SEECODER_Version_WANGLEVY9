#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
electron_dir="$script_dir/electron"
electron_binary="$electron_dir/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"

if [ ! -x "$electron_binary" ]; then
    printf '%s\n' "SEECODER Desktop is missing. With Node.js 22.12+, run: cd desktop/electron && npm install" >&2
    exit 2
fi

exec "$electron_binary" "$electron_dir"
