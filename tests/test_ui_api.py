"""Console revamp routes: /api/work, /api/search, /api/ask (docs/SPEC-ui-revamp.md 5, 6a). Run in its own process:
uv run -q --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest tests/test_ui_api.py
"""
import time
import unittest

from tests.test_controller import W
from tests.test_live import LiveBase

from fastapi.testclient import TestClient

import auth
import controller
import ui_api
from store import db

ROOT, CHILD = "t-aa11bb22", "t-cc33dd44"


def put_task(task_id, parent=None, title="t", state="done", created=None, **extra):
    cols = {"id": task_id, "parent_id": parent, "title": title, "agent": "claude", "model": "m", "prompt": "p",
            "state": state, "created_at": created or time.time(), "updated_at": time.time(), "node_id": "master",
            "branch": f"fleet/{task_id}", "result": "", "kind": "build", "stage": "build-1"} | extra
    names = ",".join(cols)
    marks = ",".join("?" * len(cols))
    with db() as c:
        c.execute(f"INSERT OR REPLACE INTO tasks ({names}) VALUES ({marks})", list(cols.values()))


class _Url:
    def __init__(self, path):
        self.path = path


class _Req:
    def __init__(self, method, path):
        self.method = method
        self.url = _Url(path)


async def no_log(task):
    return ""


class UiApiTests(LiveBase):
    def setUp(self):
        super().setUp()
        with db() as c:
            c.execute("DELETE FROM tasks WHERE id IN (?,?)", (ROOT, CHILD))
            c.execute("DELETE FROM roles WHERE name='zzrole'")
        put_task(ROOT, title="Greeting revamp", created=time.time() - 10, result="root result")
        put_task(CHILD, parent=ROOT, title="Fix attempt", state="failed", kind="fix", stage="fix-1",
                 gate_result='{"passed": false, "output": "boom password=hunter2"}')
        self.saved = (ui_api.fetch_log, ui_api.chat)

    def tearDown(self):
        ui_api.fetch_log, ui_api.chat = self.saved
        super().tearDown()

    def test_work_shape_and_order(self):
        r = self.c.get(f"/api/work/{ROOT}")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["root_id"], ROOT)
        self.assertEqual([a["id"] for a in body["attempts"]], [ROOT, CHILD])
        fix = body["attempts"][1]
        for key in ("state", "kind", "stage", "agent", "model", "account", "node", "gate", "tokens"):
            self.assertIn(key, fix)
        self.assertEqual((fix["state"], fix["kind"], fix["stage"], fix["node"]), ("failed", "fix", "fix-1", "master"))
        self.assertFalse(fix["gate"]["passed"])
        self.assertNotIn("hunter2", r.text)
        self.assertIn("chain", body)
        self.assertIn("tokens", body)

    def test_work_404(self):
        self.assertEqual(self.c.get("/api/work/t-00000000").status_code, 404)
        self.assertEqual(self.c.get(f"/api/work/{CHILD}").status_code, 404)  # a child is not a work item root

    def test_search_matches_each_type(self):
        with db() as c:
            c.execute("INSERT OR REPLACE INTO roles (name,fallback,description) VALUES ('zzrole',NULL,'')")
        by_title = self.c.get("/api/search", params={"q": "GREETING"}).json()
        hit = [x for x in by_title if x["id"] == ROOT][0]
        self.assertEqual((hit["type"], hit["href"]), ("task", f"/work/{ROOT}"))
        found = self.c.get("/api/search", params={"q": f"fleet/{CHILD}"}).json()
        child = [x for x in found if x["id"] == CHILD][0]
        self.assertEqual(child["href"], f"/work/{ROOT}")  # links to the root work item
        nodes = self.c.get("/api/search", params={"q": "master"}).json()
        self.assertTrue(any(x["type"] == "node" and x["href"] == "/nodes/master" for x in nodes))
        roles = self.c.get("/api/search", params={"q": "zzrole"}).json()
        self.assertEqual(roles, [{"type": "role", "id": "zzrole", "label": "zzrole", "href": "/roles"}])
        with db() as c:
            ref = c.execute("SELECT id FROM model_catalog LIMIT 1").fetchone()
        if ref:
            models = self.c.get("/api/search", params={"q": ref["id"]}).json()
            self.assertTrue(any(x["type"] == "model" and x["href"] == "/models" for x in models))

    def test_search_empty_and_limits(self):
        self.assertEqual(self.c.get("/api/search").json(), [])
        self.assertEqual(self.c.get("/api/search", params={"q": "  "}).json(), [])
        for i in range(60):
            put_task(f"t-ee{i:06x}", title="bulk-search-item")
        try:
            def count(**params):
                return len(self.c.get("/api/search", params={"q": "bulk-search-item"} | params).json())
            self.assertEqual(count(), 20)
            self.assertEqual(count(limit=5), 5)
            self.assertEqual(count(limit=50), 50)
            self.assertEqual(self.c.get("/api/search", params={"q": "bulk", "limit": 51}).status_code, 422)
        finally:
            with db() as c:
                c.execute("DELETE FROM tasks WHERE title='bulk-search-item'")

    def test_ask_uses_fast_only_redacts_and_parses_citations(self):
        seen = {}
        log = "\n".join(f"line {i}" for i in range(1, 501)) + "\napi_key=sk-abcdefghijklmnop12345"

        async def fake_log(task):
            seen["log_task"] = task["id"]
            return log

        async def fake_chat(role, system, user):
            seen.update(role=role, user=user)
            return "It failed at [L450] and again [L450], see [L12].", "ollama/qwen-fast"

        ui_api.fetch_log, ui_api.chat = fake_log, fake_chat
        r = self.c.post("/api/ask", json={"root_id": ROOT, "question": "why did it fail?"}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"answer": "It failed at [L450] and again [L450], see [L12].",
                                    "citations": [12, 450], "model": "ollama/qwen-fast", "local": True})
        self.assertEqual((seen["role"], seen["log_task"]), ("fast", CHILD))  # latest attempt
        self.assertIn("[L501]", seen["user"])
        self.assertIn("[L102]", seen["user"])
        self.assertNotIn("[L101]", seen["user"])  # only the last 400 lines
        self.assertNotIn("sk-abcdefghijklmnop12345", seen["user"])

    def test_ask_503_when_fast_unavailable(self):
        async def down(role, system, user):
            raise ConnectionError("no gateway")

        ui_api.fetch_log, ui_api.chat = no_log, down
        r = self.c.post("/api/ask", json={"root_id": ROOT, "question": "q"}, headers=W)
        self.assertEqual(r.status_code, 503)
        self.assertIn("fast", r.json()["detail"])

    def test_ask_404_and_bad_question(self):
        r = self.c.post("/api/ask", json={"root_id": "t-00000000", "question": "q"}, headers=W)
        self.assertEqual(r.status_code, 404)
        r = self.c.post("/api/ask", json={"root_id": ROOT, "question": "  "}, headers=W)
        self.assertEqual(r.status_code, 400)

    def test_scope_enforcement(self):
        self.assertEqual(auth.required_scope(_Req("GET", "/api/work/t-1")), "view")
        self.assertEqual(auth.required_scope(_Req("GET", "/api/search")), "view")
        self.assertEqual(auth.required_scope(_Req("POST", "/api/ask")), "operate")
        remote = TestClient(controller.app, base_url="https://master.tail.ts.net:8701")
        view, operate = auth.create_token("view", "ui-v"), auth.create_token("operate", "ui-o")
        hv, ho = W | {"Authorization": f"Bearer {view}"}, W | {"Authorization": f"Bearer {operate}"}
        self.assertEqual(remote.get(f"/api/work/{ROOT}", headers=hv).status_code, 200)
        self.assertEqual(remote.get("/api/search?q=a", headers=hv).status_code, 200)
        body = {"root_id": ROOT, "question": "q"}
        self.assertEqual(remote.post("/api/ask", json=body, headers=hv).status_code, 403)

        async def ok(role, system, user):
            return "fine", "m"

        ui_api.fetch_log, ui_api.chat = no_log, ok
        self.assertEqual(remote.post("/api/ask", json=body, headers=ho).status_code, 200)


if __name__ == "__main__":
    unittest.main()
