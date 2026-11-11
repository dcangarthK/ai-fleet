"""Grounded ask_model decision logic (SPEC-grounding §G4) with fake gateway replies."""
import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "master" / "mcp"))
import grounded  # noqa: E402

CONTEXT = "The controller listens on port 8700.\nThe gateway listens on port 4000."


def reply(status="ANSWER", answer="8700", quotes=("The controller listens on port 8700.",), reason="",
          tags=("ESTABLISHED",)):
    return json.dumps({"status": status, "answer": answer, "quotes": list(quotes), "tags": list(tags),
                       "reason": reason})


def tokens(text, logprob=-0.1):
    return [{"token": char, "logprob": logprob} for char in text]


class Harness:
    def __init__(self, replies, fallback=None, agents=(), spawn_error=None):
        self.replies = dict(replies)  # role -> content str | Exception | grounded.Reply
        self.fallback, self.agents, self.spawn_error = fallback, list(agents), spawn_error
        self.chats, self.reports, self.spawned = [], [], []

    def chat(self, role):
        self.chats.append(role)
        value = self.replies[role]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, grounded.Reply) else grounded.Reply(
            content=value, model=f"{role}-model", usage={"prompt_tokens": 10, "completion_tokens": 5})

    def spawn(self, agent, title, prompt):
        if self.spawn_error and agent in self.spawn_error:
            raise RuntimeError(self.spawn_error[agent])
        self.spawned.append((agent, title, prompt))
        return f"t-{agent}"

    def deps(self):
        return grounded.Deps(chat=self.chat, fallback_for=lambda role: self.fallback,
                             escalation_agents=lambda role: self.agents, spawn=self.spawn,
                             report=self.reports.append, clock=lambda: 0.0)

    def run(self, role="fast", prompt="Which port does the controller use?", context=CONTEXT):
        return grounded.run(role, prompt, context, self.deps())


class Decision(unittest.TestCase):
    def test_valid_answer_is_returned_and_reported_answered(self):
        h = Harness({"fast": reply()}, fallback="coder")
        result = h.run()
        self.assertEqual((result["status"], result["answer"], result["answered_by"]), ("ANSWER", "8700", "fast"))
        self.assertEqual(h.chats, ["fast"])
        self.assertEqual(len(h.reports), 1)
        self.assertEqual({k: h.reports[0][k] for k in ("role", "model", "ok", "outcome", "tokens_in", "tokens_out")},
                         {"role": "fast", "model": "fast-model", "ok": True, "outcome": "answered",
                          "tokens_in": 10, "tokens_out": 5})

    def test_invalid_quote_is_a_failed_check_then_fallback_answers(self):
        h = Harness({"fast": reply(quotes=["The controller listens on port 9000."]), "coder": reply()},
                    fallback="coder")
        result = h.run()
        self.assertEqual((result["status"], result["answered_by"], result["fallback_from"]), ("ANSWER", "coder", "fast"))
        self.assertEqual([r["outcome"] for r in h.reports], ["check_failed", "answered"])
        self.assertEqual(h.reports[1]["fallback_from"], "fast")

    def test_one_bad_quote_among_good_ones_fails(self):
        h = Harness({"fast": reply(quotes=["The controller listens on port 8700.", "invented line"])})
        result = h.run()
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertIn("1 of 2 quotes are not in the context", result["reason"])
        self.assertEqual(result["answer"], "")

    def test_answer_without_quotes_fails_when_context_given(self):
        h = Harness({"fast": reply(quotes=[])})
        self.assertEqual(h.run()["status"], "ABSTAIN")
        self.assertEqual(h.reports[0]["outcome"], "check_failed")

    def test_without_context_quotes_are_not_checked(self):
        h = Harness({"fast": reply(quotes=[])})
        self.assertEqual(h.run(context="")["status"], "ANSWER")

    def test_malformed_json_is_a_failed_check_never_an_answer(self):
        for content in ("8700", '{"answer": "8700"}', reply(tags=["SURE"]), reply(answer="")):
            h = Harness({"fast": content})
            result = h.run()
            self.assertEqual((result["status"], result["answer"]), ("ABSTAIN", ""), content)
            self.assertEqual(h.reports[0]["outcome"], "check_failed")

    def test_abstain_then_fallback_answers(self):
        h = Harness({"fast": reply("ABSTAIN", "", [], "not in context"), "coder": reply()}, fallback="coder")
        result = h.run()
        self.assertEqual((result["status"], result["answered_by"]), ("ANSWER", "coder"))
        self.assertEqual([r["outcome"] for r in h.reports], ["abstained", "answered"])

    def test_fallback_is_tried_only_once(self):
        h = Harness({"fast": reply("ABSTAIN", "", [], "no"), "coder": reply("ABSTAIN", "", [], "no")},
                    fallback="coder")
        h.run()
        self.assertEqual(h.chats, ["fast", "coder"])

    def test_both_fail_without_escalation_policy_abstains_with_reasons(self):
        h = Harness({"fast": reply("ABSTAIN", "", [], "port not stated"),
                     "coder": reply(quotes=["made up"])}, fallback="coder")
        result = h.run()
        self.assertEqual((result["status"], result["answer"]), ("ABSTAIN", ""))
        self.assertIn("fast abstained: port not stated", result["reason"])
        self.assertIn("coder failed the check", result["reason"])
        self.assertEqual(h.spawned, [])
        self.assertEqual([r["outcome"] for r in h.reports], ["abstained", "check_failed"])

    def test_both_fail_with_escalation_policy_spawns_read_only_task(self):
        h = Harness({"fast": reply("ABSTAIN", "", [], "port not stated"),
                     "coder": reply(quotes=["made up"])}, fallback="coder", agents=["claude", "codex"])
        result = h.run()
        self.assertEqual((result["status"], result["task_id"], result["agent"], result["answer"]),
                         ("ESCALATED", "t-claude", "claude", ""))
        agent, _, prompt = h.spawned[0]
        self.assertEqual(agent, "claude")
        self.assertIn("Which port does the controller use?", prompt)
        self.assertIn(CONTEXT, prompt)
        self.assertIn("port not stated", prompt)
        self.assertEqual([r["outcome"] for r in h.reports], ["abstained", "escalated"])

    def test_escalation_tries_next_agent_and_reports_failures(self):
        h = Harness({"fast": reply("ABSTAIN", "", [], "no")}, agents=["claude", "codex"],
                    spawn_error={"claude": "node busy"})
        result = h.run()
        self.assertEqual((result["status"], result["agent"]), ("ESCALATED", "codex"))
        h = Harness({"fast": reply("ABSTAIN", "", [], "no")}, agents=["claude"],
                    spawn_error={"claude": "node busy"})
        result = h.run()
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertIn("escalation failed: claude: node busy", result["reason"])
        self.assertEqual(h.reports[0]["outcome"], "abstained")

    def test_unavailable_policy_means_no_escalation_and_says_so(self):
        h = Harness({"fast": reply("ABSTAIN", "", [], "no")})
        deps = h.deps()
        deps.escalation_agents = lambda role: (_ for _ in ()).throw(OSError("down"))
        result = grounded.run("fast", "q", CONTEXT, deps)
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertIn("policy unavailable", result["reason"])

    def test_transport_errors_fall_back_and_are_reported(self):
        h = Harness({"fast": ConnectionError("refused"), "coder": reply()}, fallback="coder")
        result = h.run()
        self.assertEqual(result["answered_by"], "coder")
        self.assertEqual(h.reports[0]["ok"], False)
        self.assertNotIn("outcome", h.reports[0])  # a transport failure is not a failed check

    def test_escalation_prompt_does_not_mention_a_repository(self):
        self.assertNotIn("repository", grounded.escalation_prompt("fast", "q", CONTEXT, "why"))

    def test_report_failures_never_break_the_answer(self):
        h = Harness({"fast": reply()})
        deps = h.deps()
        deps.report = lambda body: (_ for _ in ()).throw(OSError("controller down"))
        self.assertEqual(grounded.run("fast", "q", CONTEXT, deps)["status"], "ANSWER")

    def test_logprobs_are_recorded_but_never_gate(self):
        content = reply()
        h = Harness({"fast": grounded.Reply(content=content, model="m", logprobs=tokens(content, math.log(0.01)))})
        result = h.run()
        self.assertEqual(result["status"], "ANSWER")  # very low confidence still answers: telemetry only
        self.assertAlmostEqual(result["logprob"]["min"], 0.01)
        self.assertAlmostEqual(h.reports[0]["logprob_mean"], 0.01)


class Request(unittest.TestCase):
    def test_schema_and_prompt(self):
        fmt = grounded.response_format("code-review")
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(fmt["json_schema"]["name"], "grounded_code_review")
        schema = fmt["json_schema"]["schema"]
        self.assertEqual(set(schema["required"]), {"status", "answer", "quotes", "tags", "reason"})
        self.assertEqual(schema["properties"]["status"]["enum"], ["ANSWER", "ABSTAIN"])
        self.assertIn("CONTEXT:\nctx", grounded.user_message("q", "ctx"))
        self.assertEqual(grounded.user_message("q", ""), "q")

    def test_role_temperature(self):
        rows = [{"role": "fast", "params": {"temperature": 0.3}}, {"role": "fast", "params": {"temperature": 0.0}},
                {"role": "coder", "params": {"temperature": 0.7}}, {"role": "fast", "params": {}}]
        self.assertEqual(grounded.role_temperature(rows, "fast"), 0.0)
        self.assertEqual(grounded.role_temperature(rows, "reviewer"), grounded.DEFAULT_TEMPERATURE)
        self.assertEqual(grounded.role_temperature(None, "fast"), 0.1)


class EscalationPolicy(unittest.TestCase):
    def rows(self, **layers):
        return [{"scope": scope, "scope_id": scope_id, "body": body}
                for (scope, scope_id), body in layers.values()]

    def test_default_is_none(self):
        self.assertEqual(grounded.escalation_agents([], "fast"), [])
        self.assertEqual(grounded.escalation_agents(self.rows(f=(("fleet", "*"), {"mode_ceiling": "full"})),
                                                    "fast"), [])

    def test_layers_intersect(self):
        rows = self.rows(f=(("fleet", "*"), {"escalation": {"agents": ["claude", "codex"]}}),
                         r=(("role", "fast"), {"escalation": {"agents": ["codex"]}}))
        self.assertEqual(grounded.escalation_agents(rows, "fast"), ["codex"])
        self.assertEqual(grounded.escalation_agents(rows, "coder"), ["claude", "codex"])
        rows += self.rows(a=(("agent", "codex"), {"escalation": {"agents": []}}))
        self.assertEqual(grounded.escalation_agents(rows, "fast"), [])

    def test_invalid_stored_layer_never_widens(self):
        rows = self.rows(f=(("fleet", "*"), {"escalation": {"agents": ["claude"]}}),
                         r=(("role", "fast"), {"escalation": "yes"}))
        self.assertEqual(grounded.escalation_agents(rows, "fast"), [])


class ServerRedaction(unittest.TestCase):
    """ask_model(grounded=True): the quote check runs against the redacted context the model saw."""

    def run_ask(self, quote):
        import json as _json
        from unittest import mock
        try:
            import server
        except ImportError as exc:  # plain python3 has no httpx/mcp; the gate's uv step runs this with them
            self.skipTest(f"server deps missing: {exc}")
        seen = []

        def fake_request(role, system, user, timeout, extra):
            seen.append(user)
            content = _json.dumps({"status": "ANSWER", "answer": "x", "quotes": [quote], "tags": ["ESTABLISHED"],
                                   "reason": ""})
            return {"choices": [{"message": {"content": content}}], "model": "m"}

        with mock.patch.object(server, "_chat_request", fake_request), \
                mock.patch.object(server, "_fleet_json", lambda path: {}), \
                mock.patch.object(server, "_fallback_for", lambda role: None), \
                mock.patch.object(server, "_report_body", lambda body: None):
            result = _json.loads(server.ask_model("fast", "what?", "The note says password: hunter2 here.", True))
        return result, seen

    def test_quote_of_the_redacted_text_passes_and_model_never_sees_the_secret(self):
        result, seen = self.run_ask("The note says [REDACTED] here.")
        self.assertEqual(result["status"], "ANSWER")
        self.assertNotIn("hunter2", seen[0])

    def test_quote_of_the_original_secret_fails_the_check(self):
        result, _ = self.run_ask("The note says password: hunter2 here.")
        self.assertEqual(result["status"], "ABSTAIN")


if __name__ == "__main__":
    unittest.main()
