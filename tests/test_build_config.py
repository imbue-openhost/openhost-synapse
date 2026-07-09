"""Fast, hermetic tests for the openhost-synapse build/config invariants.

These tests do NOT require network access, Docker, or a running Synapse. They
lock down the static contracts that are easy to break silently:

  * the Cinny version pin is consistent between the Dockerfile and the patch,
    and the patch applies cleanly to a pristine checkout of that pinned tag
    (only when a checkout is available; see CINNY_SRC_DIR);
  * the multi-stage Dockerfile keeps its regression guards (git apply --check,
    the post-build "Connecting..." / "Connection Lost!" grep checks) and raises
    the Node heap limit so the vite build does not OOM;
  * the Caddy sync long-poll cap rewrites only the timeouts it should, at the
    correct boundary, and leaves the initial timeout=0 sync alone;
  * start.sh renders the Cinny config and Caddyfile placeholders as expected.

Run with:  python3 -m pytest tests/  (or  python3 -m unittest discover tests)
"""

import os
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = (REPO / "Dockerfile").read_text()
PATCH = (REPO / "cinny-suppress-connecting-banner.patch").read_text()
CADDY_TEMPLATE = (REPO / "Caddyfile.template").read_text()
START_SH = (REPO / "start.sh").read_text()
WEBCLIENT_TEMPLATE = (REPO / "webclient-config.template.json").read_text()


def cinny_version_from_dockerfile() -> str:
    m = re.search(r"ARG\s+CINNY_VERSION=(\S+)", DOCKERFILE)
    assert m, "CINNY_VERSION ARG not found in Dockerfile"
    return m.group(1)


def cinny_commit_sha_from_dockerfile() -> str:
    m = re.search(r"ARG\s+CINNY_COMMIT_SHA=([0-9a-f]+)", DOCKERFILE)
    assert m, "CINNY_COMMIT_SHA ARG not found in Dockerfile"
    return m.group(1)


class TestCinnyVersionPin(unittest.TestCase):
    def test_dockerfile_pins_a_version(self):
        ver = cinny_version_from_dockerfile()
        self.assertRegex(ver, r"^v\d+\.\d+\.\d+$", "expected a vMAJOR.MINOR.PATCH tag")

    def test_single_version_arg(self):
        # Exactly one CINNY_VERSION ARG so there is a single source of truth.
        self.assertEqual(len(re.findall(r"ARG\s+CINNY_VERSION=", DOCKERFILE)), 1)

    def test_dockerfile_pins_a_commit_sha(self):
        # A tag is mutable; the commit SHA is the immutable content pin.
        sha = cinny_commit_sha_from_dockerfile()
        self.assertRegex(sha, r"^[0-9a-f]{40}$", "expected a full 40-hex commit SHA")

    def test_single_commit_sha_arg(self):
        self.assertEqual(len(re.findall(r"ARG\s+CINNY_COMMIT_SHA=", DOCKERFILE)), 1)

    def test_build_verifies_commit_sha_and_aborts_on_mismatch(self):
        # The clone step must compare HEAD against the pinned SHA and exit non-zero
        # on mismatch, so a moved upstream tag fails the build instead of silently
        # shipping different code.
        self.assertIn("git rev-parse HEAD", DOCKERFILE)
        self.assertRegex(
            DOCKERFILE,
            r'if \[ "\$HEAD_SHA" != "\$CINNY_COMMIT_SHA" \]; then',
        )
        # The mismatch branch must abort the build.
        mismatch = re.search(
            r'if \[ "\$HEAD_SHA" != "\$CINNY_COMMIT_SHA" \]; then.*?exit 1',
            DOCKERFILE,
            re.DOTALL,
        )
        self.assertIsNotNone(mismatch, "SHA mismatch must 'exit 1'")


class TestPatchIntegrity(unittest.TestCase):
    def test_patch_targets_syncstatus(self):
        self.assertIn("src/app/pages/client/SyncStatus.tsx", PATCH)

    def test_patch_removes_connecting_branch(self):
        # The patch must delete the green "Connecting..." banner text.
        removed = [l for l in PATCH.splitlines() if l.startswith("-")]
        self.assertTrue(
            any("Connecting..." in l for l in removed),
            "patch should remove the 'Connecting...' banner",
        )

    def test_patch_does_not_touch_error_banners(self):
        # We must NOT remove the useful error banners.
        removed = "\n".join(l for l in PATCH.splitlines() if l.startswith("-"))
        self.assertNotIn("Connection Lost!", removed)
        self.assertNotIn("Reconnecting", removed)

    def test_patch_is_unified_diff(self):
        self.assertTrue(PATCH.startswith("diff --git"))
        self.assertIn("@@", PATCH)


class TestDockerfileGuards(unittest.TestCase):
    def test_multistage_builder(self):
        self.assertIn("AS cinny-builder", DOCKERFILE)
        self.assertIn("FROM matrixdotorg/synapse", DOCKERFILE)

    def test_builds_from_source_not_release_tarball(self):
        # The whole point: we compile Cinny instead of downloading the release.
        self.assertIn("git clone", DOCKERFILE)
        self.assertIn("npm run build", DOCKERFILE)
        self.assertNotIn("releases/download", DOCKERFILE)

    def test_git_apply_check_guard_present(self):
        # A version bump that breaks the patch must fail the build early.
        self.assertIn("git apply --check", DOCKERFILE)

    def test_post_build_connecting_guard(self):
        # Build fails if the banner survived the patch. The guard greps the
        # built bundle for the "Connecting" string and exits non-zero if found.
        self.assertIn("dist/assets", DOCKERFILE)
        self.assertIn("still present in built Cinny", DOCKERFILE)
        guard = re.search(
            r"if grep -rq 'Connecting[^']*' dist/assets/\*\.js; then",
            DOCKERFILE,
        )
        self.assertIsNotNone(guard, "post-build 'Connecting' grep guard missing")

    def test_post_build_error_banner_guard(self):
        # Build fails if we over-patched away the error banner.
        self.assertIn("Connection Lost!", DOCKERFILE)
        self.assertIn("over-patched", DOCKERFILE)

    def test_node_heap_limit_raised(self):
        # The vite build OOMs at the ~2GB V8 default on constrained hosts.
        m = re.search(r"max-old-space-size=(\d+)", DOCKERFILE)
        self.assertIsNotNone(m, "NODE_OPTIONS max-old-space-size must be set")
        self.assertGreaterEqual(int(m.group(1)), 4096)

    def test_dist_copied_into_final_image(self):
        self.assertIn("COPY --from=cinny-builder /build/dist/", DOCKERFILE)

    def test_config_json_kept_as_default_template(self):
        # start.sh renders the live config from a default template.
        self.assertIn("webclient-config.default.json", DOCKERFILE)


class TestCaddySyncCap(unittest.TestCase):
    """Validate the sync long-poll cap that gates the banner root cause.

    The Caddyfile matches /sync requests whose client-requested timeout is
    >= 25000ms and rewrites the timeout value to 20000ms. This test reproduces
    that matcher/rewrite logic in Python and checks the boundaries, since a
    regression here would resurrect the router-504 disconnection loop (which is
    what turns the transient banner into a full-minute reconnect cycle).
    """

    def _matcher_regex(self):
        m = re.search(r"path_regexp sync (\S+)", CADDY_TEMPLATE)
        self.assertIsNotNone(m, "path_regexp for sync not found")
        return m.group(1)

    def _threshold(self):
        # Anchor on the actual Caddy expression, not prose in the comments
        # (which mention "25s"): int({...timeout}) >= <threshold>.
        m = re.search(
            r"int\(\{http\.request\.uri\.query\.timeout\}\)\s*>=\s*(\d+)",
            CADDY_TEMPLATE,
        )
        self.assertIsNotNone(m, "sync timeout threshold expression not found")
        return int(m.group(1))

    def _target(self):
        m = re.search(r"timeout=\{http\.request\.uri\.query\.timeout\} timeout=(\d+)",
                      CADDY_TEMPLATE)
        self.assertIsNotNone(m, "uri replace target not found")
        return int(m.group(1))

    def test_path_regexp_matches_sync_versions(self):
        rx = re.compile(self._matcher_regex())
        for good in ["/_matrix/client/v3/sync", "/_matrix/client/r0/sync",
                     "/_matrix/client/unstable/sync"]:
            self.assertTrue(rx.match(good), good)
        for bad in ["/_matrix/client/v3/syncx", "/_matrix/client/v1/sync",
                    "/_matrix/client/v3/sync/", "/_matrix/client/v3/rooms"]:
            self.assertIsNone(rx.match(bad), bad)

    def test_cap_target_below_router_timeout(self):
        # 20s must be comfortably under the ~30s router read timeout.
        self.assertEqual(self._target(), 20000)
        self.assertLess(self._target(), 30000)

    def test_threshold_only_rewrites_large_timeouts(self):
        threshold = self._threshold()
        target = self._target()

        def effective(timeout_str):
            # Mirror the Caddy matcher: only digits-only values >= threshold
            # are rewritten; everything else passes through untouched.
            if re.fullmatch(r"[0-9]+", timeout_str) and int(timeout_str) >= threshold:
                return target
            return timeout_str

        # Initial sync (timeout=0) is untouched -> fast initial sync preserved.
        self.assertEqual(effective("0"), "0")
        # Small long-polls pass through.
        self.assertEqual(effective("5000"), "5000")
        self.assertEqual(effective("24999"), "24999")
        # At/above threshold get capped.
        self.assertEqual(effective(str(threshold)), target)
        self.assertEqual(effective("30000"), target)
        self.assertEqual(effective("60000"), target)
        # Non-numeric values are left alone (no int() crash).
        self.assertEqual(effective(""), "")
        self.assertEqual(effective("abc"), "abc")

    def test_threshold_leaves_headroom_above_target(self):
        # Capped requests must not immediately re-trigger the matcher.
        self.assertGreater(self._threshold(), self._target())


def _render_like_start_sh(template, server_name):
    """Reproduce start.sh's webclient config render (sed substitution)."""
    out = template
    out = out.replace("SERVER_NAME_PLACEHOLDER", server_name)
    out = out.replace("COMMUNITY_SPACE_PLACEHOLDER", "")
    out = out.replace("COMMUNITY_ROOM_PLACEHOLDER", "")
    out = out.replace("COMMUNITY_SERVER_PLACEHOLDER", "")
    return out


class TestWebclientConfigRender(unittest.TestCase):
    def test_placeholders_present_in_template(self):
        self.assertIn("SERVER_NAME_PLACEHOLDER", WEBCLIENT_TEMPLATE)

    def test_render_is_valid_json_with_pinned_homeserver(self):
        import json

        rendered = _render_like_start_sh(
            WEBCLIENT_TEMPLATE, "synapse.example.host.imbue.com"
        )
        data = json.loads(rendered)
        self.assertEqual(data["homeserverList"], ["synapse.example.host.imbue.com"])
        self.assertFalse(data["allowCustomHomeservers"])
        self.assertEqual(data["defaultHomeserver"], 0)

    def test_start_sh_injects_firstrun_guard(self):
        # The SSO first-run guard must still be injected into index.html.
        self.assertIn("openhost-firstrun-guard", START_SH)
        self.assertIn("cinny_access_token", START_SH)


class TestStartShRootHandler(unittest.TestCase):
    def test_serves_webclient_when_bundled(self):
        self.assertIn("root * ${WEBROOT}", START_SH)
        self.assertIn("try_files {path} /index.html", START_SH)

    def test_bare_homeserver_fallback_exists(self):
        # If the client somehow isn't bundled, fall back to proxying Synapse.
        self.assertIn("Web client not bundled", START_SH)


class TestPatchAppliesToPinnedTag(unittest.TestCase):
    """Optionally verify the patch applies to a real Cinny checkout.

    Set CINNY_SRC_DIR to a checkout of the pinned tag to run this. In CI the
    build workflow performs the real clone + apply + build, so this stays an
    opt-in local convenience (skipped when the checkout isn't provided).
    """

    def test_patch_applies_clean(self):
        src = os.environ.get("CINNY_SRC_DIR")
        if not src or not Path(src).is_dir():
            self.skipTest("CINNY_SRC_DIR not set to a Cinny checkout")
        patch_path = REPO / "cinny-suppress-connecting-banner.patch"
        result = subprocess.run(
            ["git", "apply", "--check", str(patch_path)],
            cwd=src, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_checkout_matches_pinned_sha(self):
        src = os.environ.get("CINNY_SRC_DIR")
        if not src or not Path(src).is_dir():
            self.skipTest("CINNY_SRC_DIR not set to a Cinny checkout")
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=src, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            cinny_commit_sha_from_dockerfile(),
            "CINNY_SRC_DIR checkout does not match the pinned CINNY_COMMIT_SHA",
        )


class TestPinnedTagResolvesToSha(unittest.TestCase):
    """Network-gated: confirm the upstream tag still resolves to the pinned SHA.

    Opt in with RUN_NETWORK_TESTS=1. This catches a tag that has been moved
    upstream (the exact scenario the SHA pin defends against). Skipped by
    default so the fast/hermetic suite never depends on network access.
    """

    def test_tag_points_at_pinned_commit(self):
        if os.environ.get("RUN_NETWORK_TESTS") != "1":
            self.skipTest("set RUN_NETWORK_TESTS=1 to query upstream Cinny tags")
        version = cinny_version_from_dockerfile()
        expected = cinny_commit_sha_from_dockerfile()
        result = subprocess.run(
            ["git", "ls-remote", "--tags",
             "https://github.com/cinnyapp/cinny.git", version],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # ls-remote prints "<sha>\trefs/tags/<version>" (and possibly a "^{}"
        # peeled line for annotated tags). Accept either the tag object or its
        # peeled commit matching the pin.
        shas = {line.split("\t")[0] for line in result.stdout.strip().splitlines()}
        self.assertIn(
            expected, shas,
            f"upstream tag {version} no longer resolves to pinned {expected}; "
            f"got {shas}",
        )


if __name__ == "__main__":
    unittest.main()
