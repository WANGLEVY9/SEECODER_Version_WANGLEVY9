#!/bin/sh
# Launch SEECODER Desktop with the modern Homebrew Tk runtime.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(cd "$script_dir/.." && pwd)
gui_python=/opt/homebrew/opt/python@3.12/bin/python3.12

if [ ! -x "$gui_python" ]; then
    printf '%s\n' "SEECODER Desktop requires Homebrew Tk. Run: brew install python-tk@3.12" >&2
    exit 2
fi

exec "$gui_python" "$script_dir/seecoder_desktop.py" --project-root "$project_root" "$@"
