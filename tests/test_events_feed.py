"""Part F requirement 7: /api/events/list is newest first with before= paging; after= stays ascending
(docs/SPEC-dashboard-truth.md). Run:
uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_events_feed.py
"""
import unittest

from tests.test_live import LiveBase

import events


class EventFeedTests(LiveBase):
    def emit_n(self, n, kind="feed.test"):
        return [events.emit(None, kind, {"i": i}) for i in range(n)]

    def feed(self, query):
        """Our events only: background loops in LiveBase may emit others between calls."""
        return [r for r in self.c.get(f"/api/events/list?{query}").json() if r["type"] == "feed.test"]

    def test_new_event_is_first(self):
        self.emit_n(3)
        eid = events.emit(None, "feed.newest", {})
        rows = self.c.get("/api/events/list").json()
        self.assertEqual(rows[0]["id"], eid)
        self.assertEqual(rows[0]["type"], "feed.newest")
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_limit_returns_newest_n(self):
        ids = self.emit_n(12)
        rows = self.feed("limit=1000")
        self.assertEqual([r["id"] for r in rows][:5], ids[::-1][:5])

    def test_before_pages_newest_first(self):
        ids = self.emit_n(10)
        page1 = self.feed("limit=1000")
        self.assertEqual([r["id"] for r in page1][:4], ids[::-1][:4])
        page2 = self.feed(f"limit=1000&before={ids[::-1][3]}")
        self.assertEqual([r["id"] for r in page2][:4], ids[::-1][4:8])
        small = self.c.get(f"/api/events/list?limit=2&before={ids[5]}").json()
        self.assertEqual(len(small), 2)
        self.assertTrue(all(r["id"] < ids[5] for r in small))

    def test_before_with_after_is_newest_first_window(self):
        ids = self.emit_n(6)
        rows = self.feed(f"after={ids[1]}&before={ids[5]}")
        self.assertEqual([r["id"] for r in rows], [ids[4], ids[3], ids[2]])

    def test_after_unchanged_ascending(self):
        ids = self.emit_n(6)
        rows = self.feed(f"after={ids[1]}&limit=1000")
        self.assertEqual([r["id"] for r in rows][:4], ids[2:6])
        self.assertEqual(len(self.c.get(f"/api/events/list?after={ids[1]}&limit=3").json()), 3)

    def test_after_zero_is_ascending_oldest_first(self):
        self.emit_n(3)
        ids = [r["id"] for r in self.c.get("/api/events/list?after=0&limit=1000").json()]
        self.assertEqual(ids, sorted(ids))

    def test_shape_unchanged(self):
        self.emit_n(1)
        row = self.c.get("/api/events/list?limit=1").json()[0]
        self.assertEqual(set(row), {"id", "ts", "task_id", "type", "data"})


if __name__ == "__main__":
    unittest.main()
