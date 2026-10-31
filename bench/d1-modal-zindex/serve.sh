#!/bin/sh
# Bootstrap the fixture repo on fresh clones (app/ is gitignored): clone it
# from the committed bundle, then serve the buggy revision.
DIR="$(dirname "$0")"
if [ ! -d "$DIR/app/.git" ]; then
  SHA=$(python3 -c "import json;print(json.load(open('$DIR/repro.json'))['buggy_sha'])")
  git clone -q "$DIR/app.bundle" "$DIR/app" && git -C "$DIR/app" checkout -q "$SHA"
fi
exec python3 -m http.server 8905 --directory "$DIR/app"
