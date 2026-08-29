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
swift build

# Launch as a real macOS application bundle. SwiftPM's bare executable has no
# bundle identifier, so accessibility automation and macOS window management
# cannot reliably discover it. The bundle is generated in .build and is not a
# distributable artifact; it simply wraps the locally built binary/resources.
configuration="debug"
arch=$(uname -m)
build_dir="$script_dir/swiftui/.build/${arch}-apple-macosx/${configuration}"
bundle_dir="$script_dir/swiftui/.build/SEECODERDesktop.app"
rm -rf "$bundle_dir"
mkdir -p "$bundle_dir/Contents/MacOS" "$bundle_dir/Contents/Resources"
cp "$build_dir/SEECODERDesktop" "$bundle_dir/Contents/MacOS/SEECODERDesktop"
cp -R "$build_dir/SEECODERDesktop_SEECODERDesktop.bundle" "$bundle_dir/Contents/Resources/"
cp "$script_dir/swiftui/Resources/Info.plist" "$bundle_dir/Contents/Info.plist"
open -n "$bundle_dir"
