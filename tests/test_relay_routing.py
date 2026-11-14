"""Controller side of the runner relay (docs/SPEC-relay-routing.md 1, 3): loopback-only workers are routed through the
runner's /v1/chat/completions with a relay-scoped token. Fakes only. Run:
uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_relay_routing.py
"""
import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import unittest
from unittest import mock

from tests.test_controller import W

from fastapi.testclient import TestClient

import controller
import events
import tasks
from store import db, init_db

LOOPBACK_FW = {"checked_at": 1000.0, "age_s": 12.0, "status": "pass", "backend": "macos-pf",
               "runner_tailnet_only": True, "engine_loopback_only": True, "ipv6_wildcard": False, "details": []}
NODE_TOKEN = "node-secret-token-" + "q" * 24
GLOBAL_TOKEN = "global-secret-token-" + "g" * 24


def relay_of(token):
    return hmac.new(token.encode(), b"relay-chat-v1", hashlib.sha256).hexdigest()


class FakeLiteLLM:
    """Stands in for httpx.AsyncClient against LiteLLM's /model/* API."""
    models: dict = {}
    posts: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, path):
        data = [{"model_info": dict(v["model_info"]), "litellm_params": dict(v["litellm_params"])}
                for v in FakeLiteLLM.models.values()]
        return mock.Mock(raise_for_status=lambda: None, json=lambda: {"data": data})

    async def post(self, path, json=None):
        FakeLiteLLM.posts.append((path, json))
        if path == "/model/delete":
            FakeLiteLLM.models.pop(json["id"], None)
        else:
            FakeLiteLLM.models[json["model_info"]["id"]] = json
        return mock.Mock(raise_for_status=lambda: None)


class RelayBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        controller._loopback_warned.clear()
        FakeLiteLLM.models, FakeLiteLLM.posts = {}, []
        with db() as c:
            c.execute("DELETE FROM assignments WHERE node LIKE 'rl%'")
            c.execute("DELETE FROM nodes WHERE id LIKE 'rl%'")

    def seed(self, node_id="rl1", runner=None, token=NODE_TOKEN, params=None, catalog_id=None, role="coder",
             engine="ollama", firewall=None, relay_chat=True, model="m:1"):
        runner_doc = {"firewall": firewall if firewall is not None else LOOPBACK_FW}
        if relay_chat is not None:
            runner_doc["relay_chat"] = relay_chat
        runner_doc.update(runner or {})
        with db() as c:
            c.execute("INSERT INTO nodes (id,name,ip,engine,port,models,status,runner,runner_token,fails) "
                      "VALUES (?,?,?,'ollama',11434,?,'approved',?,?,0)",
                      (node_id, node_id, "100.9.9.7", json.dumps([model, "fleet-cat1"]), json.dumps(runner_doc), token))
            c.execute("INSERT INTO assignments (role,node,model,engine,state,catalog_id,params) "
                      "VALUES (?,?,?,?,'active',?,?)",
                      (role, node_id, model, engine, catalog_id, json.dumps(params or {})))

    def deployment(self, node_id="rl1"):
        found = [d for k, d in controller.desired_deployments().items() if f"--{node_id}--" in k]
        return found[0] if found else None

    def reconcile(self):
        with mock.patch.object(controller, "MASTER_KEY", "k"), mock.patch.object(controller.httpx, "AsyncClient", FakeLiteLLM):
            return asyncio.run(controller.reconcile())

    def assignment(self, node_id="rl1"):
        with db() as c:
            return dict(c.execute("SELECT * FROM assignments WHERE node=?", (node_id,)).fetchone())


class RelayDeployment(RelayBase):
    def test_relay_eligible_node_gets_an_openai_deployment_through_the_runner(self):
        self.seed()
        d = self.deployment()
        params = d["litellm_params"]
        self.assertEqual(params["model"], "openai/m:1")
        self.assertEqual(params["api_base"], f"http://100.9.9.7:{tasks.RUNNER_PORT}/v1")
        self.assertEqual(params["api_key"], relay_of(NODE_TOKEN))
        self.assertEqual(params["timeout"], 600)
        self.assertNotIn("extra_headers", params)
        self.assertNotIn(NODE_TOKEN, json.dumps(d))
        self.assertEqual(d["model_info"]["relay_fp"], hashlib.sha256(relay_of(NODE_TOKEN).encode()).hexdigest()[:12])

    def test_no_relay_chat_is_skipped_with_engine_loopback_only(self):
        self.seed(relay_chat=None)
        controller._loopback_warned.discard("rl1")
        with mock.patch.object(events, "emit") as emit:
            self.assertIsNone(self.deployment())
        self.assertEqual(emit.call_args.args[1], "deployment.skipped")
        self.assertEqual(controller._route_ineligible(self.assignment(), self.node_row()), "engine loopback-only")

    def node_row(self, node_id="rl1"):
        with db() as c:
            return c.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()

    def test_global_token_only_node_is_skipped_with_no_node_token(self):
        self.seed(token=None)
        with mock.patch.object(tasks, "RUNNER_TOKEN", GLOBAL_TOKEN), mock.patch.object(events, "emit"):
            self.assertIsNone(self.deployment())
            self.assertEqual(controller._route_ineligible(self.assignment(), self.node_row()), "no node token")

    def test_reachable_node_is_unchanged(self):
        self.seed(firewall=dict(LOOPBACK_FW, engine_loopback_only=False))
        params = self.deployment()["litellm_params"]
        self.assertEqual((params["model"], params["api_base"]), ("ollama_chat/m:1", "http://100.9.9.7:11434"))
        self.assertNotIn("api_key", params)

    def test_master_with_a_loopback_engine_is_unchanged(self):
        with db() as c:
            c.execute("DELETE FROM assignments WHERE node=?", (tasks.MASTER_ID,))
            c.execute("INSERT OR IGNORE INTO nodes (id,name,ip,engine,port,status,fails) "
                      "VALUES (?, 'master','127.0.0.1','ollama',11434,'approved',0)", (tasks.MASTER_ID,))
            c.execute("UPDATE nodes SET models=?, runner=?, runner_token=?, fails=0, status='approved', engine='ollama' WHERE id=?",
                      (json.dumps(["m:1"]), json.dumps({"firewall": LOOPBACK_FW, "relay_chat": True}), NODE_TOKEN,
                       tasks.MASTER_ID))
            c.execute("INSERT INTO assignments (role,node,model,engine,state,params) VALUES ('rl-m',?,'m:1','ollama','active','{}')",
                      (tasks.MASTER_ID,))
        try:
            with mock.patch.object(controller, "MASTER_MODEL_HOST", "host.docker.internal"):
                found = [d for k, d in controller.desired_deployments().items() if "rl-m--master" in k]
            self.assertEqual(found[0]["litellm_params"]["model"], "ollama_chat/m:1")
            self.assertEqual(found[0]["litellm_params"]["api_base"], "http://host.docker.internal:11434")
        finally:
            with db() as c:
                c.execute("DELETE FROM assignments WHERE role='rl-m'")

    def test_dmr_assignment_is_unchanged(self):
        self.seed(engine="dmr")
        self.assertIsNone(self.deployment())
        self.assertEqual(controller._route_ineligible(self.assignment(), self.node_row()), "runner-only engine")

    def test_route_ineligible_returns_none_for_a_relay_node(self):
        self.seed()
        self.assertIsNone(controller._route_ineligible(self.assignment(), self.node_row()))


class RelayParams(RelayBase):
    def test_think_and_num_ctx_with_a_catalog_id_use_the_derived_model_and_a_note(self):
        self.seed(params={"think": False, "num_ctx": 8192, "temperature": 0.1}, catalog_id="cat1")
        params = self.deployment()["litellm_params"]
        self.assertEqual(params["model"], "openai/fleet-cat1")
        self.assertEqual(params["temperature"], 0.1)
        for key in ("think", "num_ctx", "keep_alive"):
            self.assertNotIn(key, params)
        self.reconcile()
        row = self.assignment()
        self.assertEqual(row["routed"], 1)
        self.assertEqual(row["route_reason"], "relay: think ignored")

    def test_num_ctx_without_a_catalog_id_is_left_out_and_named(self):
        self.seed(params={"num_ctx": 4096, "seed": 7, "top_p": 0.9})
        params = self.deployment()["litellm_params"]
        self.assertNotIn("num_ctx", params)
        self.assertEqual((params["seed"], params["top_p"]), (7, 0.9))
        self.reconcile()
        row = self.assignment()
        self.assertEqual(row["routed"], 1)
        self.assertIn("num_ctx", row["route_reason"])
        self.assertTrue(row["route_reason"].startswith("relay: "))

    def test_relay_route_without_ignored_params_has_no_note(self):
        self.seed()
        self.reconcile()
        row = self.assignment()
        self.assertEqual((row["routed"], row["route_reason"]), (1, None))


class RelayReconcile(RelayBase):
    @staticmethod
    def mine():
        """This node's calls only: other test files in the same process leave their own nodes in the shared db."""
        out = []
        for path, body in FakeLiteLLM.posts:
            mid = body.get("id") or (body.get("model_info") or {}).get("id", "")
            if "--rl1--" in mid:
                out.append((path, body))
        return out

    def test_token_rotation_deletes_and_recreates_the_deployment(self):
        self.seed()
        self.reconcile()
        self.assertEqual([p for p, _ in self.mine()], ["/model/new"])
        FakeLiteLLM.posts.clear()
        self.reconcile()
        self.assertEqual(self.mine(), [], "unchanged: nothing to do")
        with db() as c:
            c.execute("UPDATE nodes SET runner_token=? WHERE id='rl1'", ("rotated-" + NODE_TOKEN,))
        self.reconcile()
        self.assertEqual([p for p, _ in self.mine()], ["/model/delete", "/model/new"])
        created = self.mine()[-1][1]
        self.assertEqual(created["litellm_params"]["api_key"], relay_of("rotated-" + NODE_TOKEN))


class NoTokenLeaks(RelayBase):
    def test_neither_token_appears_in_api_fleet_or_controller_logs(self):
        self.seed(params={"think": True})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.deployment()
            self.reconcile()
        with TestClient(controller.app, base_url="http://127.0.0.1") as client:
            fleet = client.get("/api/fleet", headers=W)
        self.assertEqual(fleet.status_code, 200)
        for secret in (NODE_TOKEN, relay_of(NODE_TOKEN)):
            self.assertNotIn(secret, fleet.text)
            self.assertNotIn(secret, out.getvalue())


class RelayHealth(unittest.TestCase):
    def test_relay_chat_survives_clean_health_on_the_poll_path(self):
        self.assertIs(tasks.clean_health({"ok": True, "relay_chat": True, "junk": 1})["relay_chat"], True)

    def test_relay_chat_survives_the_heartbeat_merge(self):
        merged = tasks.merge_health({}, {"ok": True, "relay_chat": True})
        self.assertIs(merged["relay_chat"], True)

    def test_a_heartbeat_without_relay_chat_clears_a_stored_true(self):
        merged = tasks.merge_health({"relay_chat": True, "ok": True}, {"ok": True, "version": "2"})
        self.assertIs(merged["relay_chat"], False)


class EventsClosedLoop(unittest.TestCase):
    def test_emit_with_a_closed_bound_loop_does_not_raise(self):
        loop = asyncio.new_event_loop()
        loop.close()
        previous = events._loop
        events.bind_loop(loop)
        try:
            events._wake()
            events.emit(None, "relay.test", {})
        finally:
            events._loop = previous


if __name__ == "__main__":
    unittest.main()
