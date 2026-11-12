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
        rows = self.c.get("/api/events/list?limit=5").json()
        self.assertEqual([r["id"] for r in rows], ids[::-1][:5])

    def test_before_pages_newest_first(self):
        ids = self.emit_n(10)
        page1 = self.c.get("/api/events/list?limit=4").json()
        self.assertEqual([r["id"] for r in page1], ids[::-1][:4])
        page2 = self.c.get(f"/api/events/list?limit=4&before={page1[-1]['id']}").json()
        self.assertEqual([r["id"] for r in page2], ids[::-1][4:8])

    def test_before_with_after_is_newest_first_window(self):
        ids = self.emit_n(6)
        rows = self.c.get(f"/api/events/list?after={ids[1]}&before={ids[5]}").json()
        self.assertEqual([r["id"] for r in rows], [ids[4], ids[3], ids[2]])

    def test_after_unchanged_ascending(self):
        ids = self.emit_n(6)
        rows = self.c.get(f"/api/events/list?after={ids[1]}&limit=3").json()
        self.assertEqual([r["id"] for r in rows], ids[2:5])

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
