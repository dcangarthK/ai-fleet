"""A usage limit hit while auto-approving denied tools holds the task queued until the reset; any other
NotAdmitted reason leaves it blocked. Run:
uv run -q --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_admission_limit.py
"""
import asyncio
import time
import unittest
from unittest import mock

import tasks


def make_task():
    return {"id": "t-x", "title": "x", "agent": "claude", "mode": "auto-edit", "state": "blocked",
            "auto_approvals": 0, "approved_tools": ["Read"],
            "policy": {"auto_approve": {"denied_tools": True, "agents": ["claude"]}}}


def run_denial(verdict):
    task = make_task()
    calls = {}

    def fake_transition(t, expected, state, **fields):
        calls["transition"] = (state, fields)
        return True

    with mock.patch.object(tasks, "denied_tool_names", return_value=["Bash"]), \
         mock.patch.object(tasks, "get_task", return_value=task), \
         mock.patch.object(tasks, "update"), \
         mock.patch.object(tasks, "emit"), \
         mock.patch.object(tasks, "approvable_tools", side_effect=lambda t, tools: list(dict.fromkeys(tools))), \
         mock.patch.object(tasks, "transition", side_effect=fake_transition), \
         mock.patch.object(tasks, "resume", side_effect=tasks.NotAdmitted(verdict)):
        asyncio.run(tasks.auto_approve_denials(task))
    return calls


class AdmissionLimitTests(unittest.TestCase):
    def test_usage_limit_queues_until_reset_and_keeps_tools(self):
        until = time.time() + 3600
        calls = run_denial({"outcome": "defer", "until": until, "reason": "claude account limited until 1:00 AM",
                            "wait": "limit"})
        state, fields = calls["transition"]
        self.assertEqual(state, "queued")
        self.assertEqual(fields["budget_wait"], "limit")
        self.assertEqual(fields["not_before"], until)
        self.assertEqual(fields["approved_tools"], '["Read", "Bash"]')

    def test_other_reasons_stay_blocked(self):
        for verdict in ({"outcome": "defer", "until": time.time() + 60, "reason": "slots full", "wait": "slot"},
                        {"outcome": "refuse", "until": None, "reason": "nope", "wait": None}):
            calls = run_denial(verdict)
            self.assertNotIn("transition", calls)


if __name__ == "__main__":
    unittest.main()
