"""Controller-to-runner harness imported only by the opt-in localhost test."""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_real_engine(base_url, model):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fleet = root / "fleet"
        fleet.mkdir()
        (fleet / "runner.json").write_text(json.dumps({
            "max_mode": "auto-edit",
            "engines": {"dmr": {"enabled": True, "base_url": base_url}},
        }))
        spec = importlib.util.spec_from_file_location("real_fleet_runner", ROOT / "nodes" / "runner.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        manager = runner.JobManager(fleet, "http://127.0.0.1:11434")
        catalog = manager.engine_catalog()["dmr"]
        if not catalog["reachable"]:
            raise unittest.SkipTest("real engine is absent: " + str(catalog.get("error")))
        available = {item["reference"] for item in catalog["models"]}
        model = next((candidate for candidate in (model,) if candidate in available), None)
        if not model:
            raise unittest.SkipTest("real models are absent: qwen2.5:3b (ollama pull qwen2.5:3b)")

        os.environ["FLEET_DB"] = str(root / "fleet.db")
        os.environ["FLEET_DIR"] = str(fleet)
        controller_dir = str(ROOT / "master" / "controller")
        sys.path.insert(0, controller_dir)
        try:
            import store
            store.init_db()
            import tasks
            now = time.time()
            health = runner.health_report(manager)
            health["agents"]["ollama"] = base_url
            health["engines"] = manager.engine_catalog(refresh=False)
            with store.db() as connection:
                connection.execute(
                    "INSERT INTO nodes (id,name,ip,engine,port,models,status,runner,runner_fails,heartbeat_at,polled_at) "
                    "VALUES ('master','master','127.0.0.1','ollama',11434,?,'approved',?,0,?,?)",
                    (json.dumps([model]), json.dumps(health), now, now),
                )
                connection.execute(
                    "INSERT INTO model_catalog (id,reference,engine,display_name,created_at) VALUES (?,?,?,?,?)",
                    ("real-dmr-model", model, "dmr", model, now),
                )
                connection.execute(
                    "INSERT INTO assignments (role,node,model,engine,state,catalog_id) "
                    "VALUES ('real-engine','master',?,'dmr','active','real-dmr-model')", (model,),
                )

            async def in_process_runner(node, method, path, body=None, timeout=20):
                if method == "POST" and path == "/jobs":
                    return manager.create(body)
                if method == "GET" and path.startswith("/jobs/"):
                    return manager.public(manager.get(path.split("/")[2]))
                raise AssertionError((method, path, body, timeout))

            tasks.runner_call = in_process_runner

            async def exercise():
                task = await tasks.create_task(tasks.TaskIn(
                    agent="ollama", role="real-engine", model=model, prompt="Reply with exactly FLEET_OK.",
                    mode="read-only", worktree=False,
                ))
                deadline = time.time() + 180
                while time.time() < deadline:
                    await tasks.poll_task(task["id"])
                    task = tasks.get_task(task["id"])
                    if task["state"] not in ("queued", "starting", "working"):
                        break
                    await asyncio.sleep(.1)
                if task["state"] == "review":
                    task = await tasks.complete(task["id"])
                return task

            return asyncio.run(exercise())
        finally:
            sys.path.remove(controller_dir)
