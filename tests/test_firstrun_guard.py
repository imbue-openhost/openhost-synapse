"""Tests for the Cinny first-run SSO guard injected by start.sh.

The guard is a small inline <script> that start.sh injects into Cinny's
index.html. Its job: if the visitor has no Matrix session yet, bounce them to
the OpenHost SSO/onboarding endpoint instead of letting Cinny show its own
(dead-end, custom-homeservers-disabled) login screen. It must fire on ANY app
path, because Cinny is a single-page app served with an index.html fallback, so
deep links and refreshes on sub-paths also boot session-less.

These tests extract the exact guard string from start.sh and execute its logic
in Node against simulated browser states, so we test real behavior (not just
text), including the regression we care about: firing on sub-paths, not only "/".

Run: python3 -m unittest discover -s tests
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
START_SH = (REPO / "start.sh").read_text()


def extract_guard_script() -> str:
    """Pull the inline JS out of the GUARD='<script ...>...</script>' line in
    start.sh (the body between the script tags)."""
    m = re.search(r"GUARD='<script id=\"openhost-firstrun-guard\">(.*?)</script>'", START_SH)
    assert m, "openhost-firstrun-guard script not found in start.sh"
    return m.group(1)


GUARD_JS = extract_guard_script()
NODE = shutil.which("node")


# A complete Cinny session in localStorage requires all three keys the client
# needs to initialise: the access token, the device id, and the homeserver URL.
FULL_SESSION = {
    "cinny_access_token": "syt_sometoken",
    "cinny_device_id": "DEVICE123",
    "cinny_hs_base_url": "https://hs.example.com",
}


def run_guard(
    path: str,
    token: str | None = None,
    landing_path: str = "/",
    session: dict | None = None,
    preseed_session_storage: dict | None = None,
) -> str | None:
    """Execute the guard JS in Node with a stubbed browser environment and return
    the URL it redirected to (via location.replace), or None if it did not.

    ``session`` is the localStorage contents to seed. For convenience, passing
    ``token`` is shorthand: a truthy ``token`` seeds a FULL, valid session; a
    ``None`` token seeds an empty localStorage. Pass ``session`` explicitly to
    test partial/corrupt states.

    ``preseed_session_storage`` seeds sessionStorage before the guard runs, used
    to simulate a repeat full-page load in the same tab (where sessionStorage
    survives). The guard must not depend on sessionStorage for its redirect
    decision.

    Stubs window.localStorage, location, sessionStorage, and a synchronous
    XMLHttpRequest (returning ``{"path": landing_path}``) so both the SSO-redirect
    and the "/"-landing branches can be exercised.
    """
    if session is None:
        session = dict(FULL_SESSION) if token else {}
    ls_json = repr(json.dumps(session))
    ss_json = repr(json.dumps(preseed_session_storage or {}))
    harness = f"""
    let redirectedTo = null;
    const localStorage = {{
      _d: JSON.parse({ls_json}),
      getItem(k) {{ return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; }},
    }};
    const sessionStorage = {{
      _d: JSON.parse({ss_json}),
      getItem(k) {{ return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; }},
      setItem(k, v) {{ this._d[k] = String(v); }},
      removeItem(k) {{ delete this._d[k]; }},
    }};
    const window = {{ localStorage: localStorage }};
    const location = {{
      pathname: {path!r},
      replace(u) {{ redirectedTo = u; }},
    }};
    // Minimal synchronous XMLHttpRequest that returns the landing path, matching
    // the /_openhost/community/landing endpoint the guard queries.
    class XMLHttpRequest {{
      open(method, url, async_) {{ this._url = url; }}
      send() {{ this.status = 200; this.responseText = JSON.stringify({{ path: {landing_path!r} }}); }}
    }}
    // The guard references these as browser globals. Evaluate it with them in scope.
    (function () {{ {GUARD_JS} }})();
    process.stdout.write(redirectedTo === null ? "" : redirectedTo);
    """
    result = subprocess.run(
        [NODE, "-e", harness], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    return out or None


@unittest.skipIf(NODE is None, "node not available to execute guard JS")
class TestGuardBehavior(unittest.TestCase):
    SSO = "/_openhost/community/login"

    def test_no_token_on_root_redirects(self):
        self.assertEqual(run_guard("/", None), self.SSO)

    def test_no_token_on_subpath_redirects(self):
        # The regression this change fixes: deep links / refreshes on sub-paths
        # must also route through SSO.
        for path in ["/inbox", "/direct", "/home", "/room/!abc:server", "/explore"]:
            with self.subTest(path=path):
                self.assertEqual(run_guard(path, None), self.SSO)

    def test_token_present_does_not_redirect(self):
        for path in ["/", "/inbox", "/room/!abc:server"]:
            with self.subTest(path=path):
                self.assertIsNone(run_guard(path, "syt_sometoken"))

    def test_deep_and_nested_paths_redirect(self):
        # Deeper nesting and trailing slashes still redirect when session-less.
        for path in [
            "/inbox/notifications/",
            "/direct/create",
            "/room/!abc:server/settings",
            "/spaces/!s:server",
            "/login",  # even Cinny's own login route should bounce to SSO
        ]:
            with self.subTest(path=path):
                self.assertEqual(run_guard(path, None), self.SSO)

    def test_token_present_on_nested_paths_does_not_redirect(self):
        for path in ["/inbox/notifications/", "/room/!abc:server/settings"]:
            with self.subTest(path=path):
                self.assertIsNone(run_guard(path, "syt_sometoken"))

    def test_session_on_root_lands_on_space(self):
        # A returning visit to "/" WITH a session lands on the community space
        # lobby (from the landing endpoint), not Cinny's Home.
        space = "/%21abc%3Amatrix.openhost.imbue.com/lobby/"
        self.assertEqual(run_guard("/", "syt_sometoken", landing_path=space), space)

    def test_session_on_root_lands_on_space_on_every_load(self):
        # Regression: opening the app on "/" must redirect to the space on EVERY
        # full-page load, not just the first one in a tab. A prior design gated
        # this on a one-shot sessionStorage flag ("oh_landed"), which meant that
        # reopening the app in the same tab (e.g. clicking the OpenHost dashboard
        # link again) skipped the redirect and dead-ended on Cinny's Home view.
        # Seed sessionStorage as if a previous load had already run and confirm we
        # still redirect to the space.
        space = "/%21abc%3Amatrix.openhost.imbue.com/lobby/"
        landed = run_guard(
            "/", "syt_sometoken", landing_path=space, preseed_session_storage={"oh_landed": "1"}
        )
        self.assertEqual(landed, space)

    def test_session_on_root_no_space_stays(self):
        # If the landing endpoint says "/" (no joined space), don't redirect.
        self.assertIsNone(run_guard("/", "syt_sometoken", landing_path="/"))

    def test_session_on_subpath_ignores_landing(self):
        # The space-landing redirect only applies to "/"; a session-carrying deep
        # link is left alone even if a space is configured.
        space = "/%21abc%3Amatrix.openhost.imbue.com/lobby/"
        self.assertIsNone(run_guard("/inbox", "syt_sometoken", landing_path=space))

    def test_partial_session_routes_through_sso(self):
        # A token WITHOUT the device id / homeserver url can't boot Cinny and
        # would dead-end on its own login screen. The guard must treat any
        # incomplete session as no session and route through SSO (which
        # repopulates all the keys). Cover each missing-key combination on both
        # "/" and a sub-path.
        partials = [
            {"cinny_access_token": "syt_x"},  # token only
            {"cinny_access_token": "syt_x", "cinny_device_id": "D"},  # no hs
            {"cinny_access_token": "syt_x", "cinny_hs_base_url": "https://h"},  # no device
            {"cinny_device_id": "D", "cinny_hs_base_url": "https://h"},  # no token
            {"cinny_access_token": ""},  # empty token
        ]
        for sess in partials:
            for path in ("/", "/inbox", "/room/!abc:server"):
                with self.subTest(session=sorted(sess), path=path):
                    self.assertEqual(run_guard(path, session=sess), self.SSO)

    def test_full_session_does_not_route_through_sso(self):
        # The complete key set is treated as a real session (no SSO redirect).
        self.assertIsNone(run_guard("/inbox", session=dict(FULL_SESSION)))


class TestGuardStatic(unittest.TestCase):
    def test_guard_checks_the_session_key(self):
        self.assertIn("cinny_access_token", GUARD_JS)

    def test_guard_targets_sso_endpoint(self):
        self.assertIn("/_openhost/community/login", GUARD_JS)

    def test_guard_fires_on_any_path(self):
        # The guard must run on every app path, so it must not gate on the
        # location being exactly "/". (Cinny is an SPA served with an
        # index.html fallback, so sub-paths boot session-less too.)
        self.assertNotIn('location.pathname==="/"', GUARD_JS.replace(" ", ""))

    def test_guard_injected_idempotently(self):
        # start.sh only injects when the marker is absent.
        self.assertIn("openhost-firstrun-guard", START_SH)
        self.assertIn('grep -q "openhost-firstrun-guard"', START_SH)

    def test_guard_inserted_into_head_before_boot(self):
        self.assertIn('html.replace("<head>", "<head>" + guard, 1)', START_SH)


if __name__ == "__main__":
    unittest.main()
