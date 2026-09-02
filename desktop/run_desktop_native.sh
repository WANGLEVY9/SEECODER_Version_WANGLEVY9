#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$script_dir/swiftui"
# A SwiftPM executable has no bundle identifier, so macOS will not enforce a
# single-instance policy for us. Close stale development instances first so
# the newly compiled window is the only window that can receive keyboard focus.
existing_pids=$(pgrep -f '/SEECODERDesktop$' || true)
if [ -n "$existing_pids" ]; then
  kill $existing_pids || true
fi
# Keep compiler caches inside the checkout. This avoids a stale or read-only
# user cache preventing the app from being built from a normal Terminal shell.
cache_dir="$script_dir/swiftui/.build/module-cache"
mkdir -p "$cache_dir/swift" "$cache_dir/clang"
export SWIFT_MODULECACHE_PATH="$cache_dir/swift"
export CLANG_MODULE_CACHE_PATH="$cache_dir/clang"
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
# Bundle.module looks beside Bundle.main on SwiftPM executables. Keep a copy
# at the app root as well as Contents/Resources so packaged and development
# launches resolve the same logo and other resources.
cp -R "$build_dir/SEECODERDesktop_SEECODERDesktop.bundle" "$bundle_dir/"
cp "$script_dir/swiftui/Resources/Info.plist" "$bundle_dir/Contents/Info.plist"
chmod +x "$bundle_dir/Contents/MacOS/SEECODERDesktop"

# Launch through LaunchServices so macOS creates and activates the app window
# reliably. `-n` guarantees a fresh development instance and `-W` keeps this
# command attached until that instance closes. The bundle can locate the repo
# from its own path; the explicit environment is also retained for direct
# process lookups made by the app.
export SEECODER_PROJECT_ROOT="$project_root"
exec open -n -W "$bundle_dir"
