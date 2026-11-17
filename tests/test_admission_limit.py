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


class RequeuedResumeTests(unittest.TestCase):
    """A task requeued from admission (runs >= 1) resumes through try_start once not_before has passed."""

    def start(self, not_before):
        import capacity
        task = {"id": "t-x", "state": "queued", "agent": "claude", "model": "m", "mode": "auto-edit", "gate": None,
                "policy": {}, "runs": 1, "not_before": not_before, "budget_wait": "limit", "error": "x"}
        restart = mock.AsyncMock(return_value=True)
        with mock.patch.object(tasks, "node_of", return_value={}), \
             mock.patch.object(tasks, "eligible", return_value=None), \
             mock.patch.object(tasks, "temporary_reason", return_value=None), \
             mock.patch.object(tasks, "update"), \
             mock.patch.object(tasks.budget, "start_wait", return_value=(None, None)), \
             mock.patch.object(capacity, "restart_requeued", restart):
            result = asyncio.run(tasks.try_start(task))
        return result, restart

    def test_picked_once_not_before_has_passed(self):
        result, restart = self.start(time.time() - 1)
        self.assertTrue(result)
        restart.assert_awaited_once()

    def test_not_picked_before_the_reset(self):
        result, restart = self.start(time.time() + 3600)
        self.assertFalse(result)
        restart.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
