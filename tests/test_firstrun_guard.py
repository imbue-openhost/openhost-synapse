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


def run_guard(path: str, token: str | None) -> str | None:
    """Execute the guard JS in Node with a stubbed window/localStorage and return
    the URL it redirected to (via location.replace), or None if it did not."""
    token_js = "null" if token is None else repr(token).replace("'", '"')
    harness = f"""
    let redirectedTo = null;
    const localStorage = {{
      _d: {{ {"" if token is None else f'"cinny_access_token": {token_js}'} }},
      getItem(k) {{ return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; }},
    }};
    const location = {{
      pathname: {path!r},
      replace(u) {{ redirectedTo = u; }},
    }};
    // The guard references bare `localStorage` and `location` (globals in a
    // browser). Evaluate it with those in scope.
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
