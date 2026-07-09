#!/usr/bin/env bash
# End-to-end check that the bundled Cinny build behaves as intended.
#
# This mirrors exactly what the Dockerfile's cinny-builder stage does:
#   1. clone Cinny at the pinned CINNY_VERSION,
#   2. apply cinny-suppress-connecting-banner.patch,
#   3. npm ci && npm run build (with the same NODE_OPTIONS heap bump),
#   4. assert the built bundle has NO "Connecting..." banner but STILL has the
#      "Connection Lost!" error banners.
#
# It is intentionally a shell script (not part of the fast unittest suite)
# because it clones and compiles Cinny, which needs network + Node + a few
# minutes. CI runs it in a dedicated job; locally, run it directly:
#
#   bash tests/test_cinny_build.sh
#
# Requires: git, node, npm.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATCH="$REPO_ROOT/cinny-suppress-connecting-banner.patch"
DOCKERFILE="$REPO_ROOT/Dockerfile"

CINNY_VERSION="$(sed -n 's/.*ARG[[:space:]]\+CINNY_VERSION=\(v[0-9.]*\).*/\1/p' "$DOCKERFILE" | head -1)"
if [ -z "$CINNY_VERSION" ]; then
    echo "FAIL: could not read CINNY_VERSION from Dockerfile" >&2
    exit 1
fi
CINNY_COMMIT_SHA="$(sed -n 's/.*ARG[[:space:]]\+CINNY_COMMIT_SHA=\([0-9a-f]*\).*/\1/p' "$DOCKERFILE" | head -1)"
if [ -z "$CINNY_COMMIT_SHA" ]; then
    echo "FAIL: could not read CINNY_COMMIT_SHA from Dockerfile" >&2
    exit 1
fi
echo "Cinny pinned in Dockerfile: $CINNY_VERSION ($CINNY_COMMIT_SHA)"

# Match the Dockerfile's heap bump so we exercise the same build path.
HEAP="$(sed -n 's/.*max-old-space-size=\([0-9]*\).*/\1/p' "$DOCKERFILE" | head -1)"
export NODE_OPTIONS="--max-old-space-size=${HEAP:-4096}"
echo "NODE_OPTIONS=$NODE_OPTIONS"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

echo "Cloning Cinny $CINNY_VERSION ..."
git clone --depth 1 --branch "$CINNY_VERSION" https://github.com/cinnyapp/cinny.git "$WORK/cinny"
cd "$WORK/cinny"

# Verify the tag resolves to the pinned immutable commit (a tag can be moved).
HEAD_SHA="$(git rev-parse HEAD)"
if [ "$HEAD_SHA" != "$CINNY_COMMIT_SHA" ]; then
    echo "FAIL: $CINNY_VERSION resolved to $HEAD_SHA, expected $CINNY_COMMIT_SHA" >&2
    exit 1
fi
echo "PASS: pinned commit verified ($HEAD_SHA)"

echo "Checking patch applies cleanly ..."
git apply --check "$PATCH"
git apply "$PATCH"
echo "Patch applied."

echo "Installing deps and building (this takes a few minutes) ..."
npm ci
npm run build

fail=0
if grep -rq 'Connecting\.\.\.' dist/assets/*.js; then
    echo "FAIL: 'Connecting...' banner is still present in the built bundle" >&2
    fail=1
else
    echo "PASS: 'Connecting...' banner absent from built bundle"
fi

if grep -rq 'Connection Lost!' dist/assets/*.js; then
    echo "PASS: 'Connection Lost!' error banner still present"
else
    echo "FAIL: 'Connection Lost!' error banner missing (over-patched)" >&2
    fail=1
fi

# The built dist must keep the layout start.sh depends on.
for f in config.json index.html; do
    if [ -f "dist/$f" ]; then
        echo "PASS: dist/$f present"
    else
        echo "FAIL: dist/$f missing from built bundle" >&2
        fail=1
    fi
done

if [ "$fail" -ne 0 ]; then
    echo "Cinny build check FAILED" >&2
    exit 1
fi
echo "Cinny build check PASSED"
