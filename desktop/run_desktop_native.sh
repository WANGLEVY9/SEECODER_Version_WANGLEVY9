#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$script_dir/swiftui"
# A SwiftPM executable has no bundle identifier, so macOS will not enforce a
# single-instance policy for us. Close stale development instances first so
# the newly compiled window is the only window that can receive keyboard focus.
existing_pids=$(pgrep -f '/SEECODERDesktop$' || true)
if [ -n "$existing_pids" ]; then
  kill $existing_pids || true
fi
exec swift run SEECODERDesktop
