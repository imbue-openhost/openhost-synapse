"""Tests for the post-SSO landing path (default to the community space).

_community_landing_path decides where the SSO bootstrap sends the web client
after sign-in: the community space's lobby when the owner has joined it, else
the app root. The path must match Cinny's space route: /<spaceIdOrAlias>/lobby/
with the id/alias percent-encoded as a single path segment.

Run: python3 -m unittest discover -s tests
"""

import importlib.util
import unittest
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_admin():
    # admin.py imports flask; skip cleanly if it isn't installed in this env.
    spec = importlib.util.spec_from_file_location("synapse_admin", REPO / "admin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    admin = _load_admin()
    LANDING = admin._community_landing_path
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - env without flask
    admin = None
    LANDING = None
    IMPORT_ERROR = exc

ROOM_ID = "!wzOIiQirmkOwqxQPYv:matrix.openhost.imbue.com"
ALIAS = "#openhost-community:matrix.openhost.imbue.com"


@unittest.skipIf(LANDING is None, f"admin.py not importable: {IMPORT_ERROR}")
class TestCommunityLandingPath(unittest.TestCase):
    def test_not_joined_lands_on_root(self):
        self.assertEqual(LANDING({"community_joined": False}), "/")

    def test_joined_without_reference_lands_on_root(self):
        # Guard against a bad state: joined flag set but no id/alias.
        self.assertEqual(
            LANDING({"community_joined": True, "community_room_id": "", "community_room_alias": ""}),
            "/",
        )

    def test_joined_prefers_room_id(self):
        path = LANDING({
            "community_joined": True,
            "community_room_id": ROOM_ID,
            "community_room_alias": ALIAS,
        })
        self.assertEqual(path, "/" + urllib.parse.quote(ROOM_ID, safe="") + "/lobby/")
        # room id must win over alias
        self.assertIn(urllib.parse.quote(ROOM_ID, safe=""), path)

    def test_joined_falls_back_to_alias(self):
        path = LANDING({"community_joined": True, "community_room_alias": ALIAS})
        self.assertEqual(path, "/" + urllib.parse.quote(ALIAS, safe="") + "/lobby/")

    def test_reference_is_single_encoded_segment(self):
        # The whole id/alias must be one encoded segment: no bare '!' '#' ':' '/'
        # that would split the path or break Cinny's :spaceIdOrAlias param.
        path = LANDING({"community_joined": True, "community_room_id": ROOM_ID})
        middle = path[len("/"):-len("/lobby/")]
        self.assertNotIn("/", middle)
        self.assertNotIn("!", middle)
        self.assertNotIn("#", middle)
        self.assertNotIn(":", middle)
        # And it round-trips back to the original room id.
        self.assertEqual(urllib.parse.unquote(middle), ROOM_ID)

    def test_path_shape_is_space_lobby(self):
        path = LANDING({"community_joined": True, "community_room_id": ROOM_ID})
        self.assertTrue(path.startswith("/"))
        self.assertTrue(path.endswith("/lobby/"))


@unittest.skipIf(admin is None, f"admin.py not importable: {IMPORT_ERROR}")
class TestDefaultsIncludeRoomId(unittest.TestCase):
    def test_defaults_have_community_room_id(self):
        self.assertIn("community_room_id", admin.DEFAULTS)
        self.assertEqual(admin.DEFAULTS["community_room_id"], "")


if __name__ == "__main__":
    unittest.main()
