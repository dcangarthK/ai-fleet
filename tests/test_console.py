"""The React console is served at /app with client-side routing and no path escapes.
Run: uv run --with fastapi --with uvicorn --with httpx python -m unittest -v tests/test_console.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="fleet-console-"))
os.environ.setdefault("FLEET_DB", str(TMP / "fleet.db"))
os.environ.setdefault("TAILSCALE_BIN", "/usr/bin/false")
os.environ.setdefault("LITELLM_MASTER_KEY", "")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "master" / "controller"))
from fastapi.testclient import TestClient  # noqa: E402

import controller  # noqa: E402

DIST = Path(controller.__file__).with_name("ui") / "dist"


@unittest.skipUnless((DIST / "index.html").is_file(), "console not built")
class ConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(controller.app, base_url="http://127.0.0.1:8700")

    def test_index_and_client_routes(self):
        for path in ("/app", "/app/", "/app/tasks", "/app/nodes/master"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn('<div id="root">', r.text)
            self.assertEqual(r.headers.get("cache-control"), "no-cache")

    def test_assets_are_served_and_cached(self):
        asset = next((DIST / "assets").glob("*.js"))
        r = self.c.get(f"/app/assets/{asset.name}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("immutable", r.headers.get("cache-control", ""))
        self.assertEqual(self.c.get("/app/assets/missing.js").status_code, 404)
        self.assertEqual(self.c.get("/app/robots.txt").status_code, 404)

    def test_no_escape_from_dist(self):
        for path in ("/app/../controller.py", "/app/..%2Fcontroller.py", "/app/%2e%2e/%2e%2e/store.py"):
            r = self.c.get(path)
            self.assertNotIn("def ", r.text, path)


if __name__ == "__main__":
    unittest.main()
