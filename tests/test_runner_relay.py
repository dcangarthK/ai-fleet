"""Runner relay route POST /v1/chat/completions (docs/SPEC-relay-routing.md 1, 2). A real RunnerServer on loopback with a
stub manager and a fake urlopen: no Ollama, no network. Run: python3 -m unittest -v tests/test_runner_relay.py
"""
import http.client
import importlib.util
import io
import json
import socket
import sys
import threading
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_runner as base  # noqa: E402  (loopback_bind_available)

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("fleet_runner_relay", ROOT / "nodes" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

TOKEN = "node-token-" + "a" * 32
RELAY = runner.relay_token(TOKEN)
CHAT = "/v1/chat/completions"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return None


class FakeEngine:
    """Stands in for loopback_urlopen: /api/tags and /v1/chat/completions."""

    def __init__(self, models=("qwen2.5-coder:14b",), chat=None):
        self.models = list(models)
        self.chat = chat  # callable(request) -> response, else a canned JSON reply
        self.tag_calls = 0
        self.chat_requests = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        if url.endswith("/api/tags"):
            self.tag_calls += 1
            return Response(json.dumps({"models": [{"name": m} for m in self.models]}).encode())
        assert url.endswith("/v1/chat/completions"), url
        self.chat_requests.append((request, timeout))
        if self.chat:
            return self.chat(request)
        return Response(json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode())


class StubManager:
    def __init__(self, ollama_url="http://127.0.0.1:11434"):
        self.ollama_url = ollama_url
        self.request_context = threading.local()
        self.audits = []
        self.node_id = None

    def audit(self, event, detail):
        self.audits.append((event, detail))


def body(**over):
    value = {"model": "qwen2.5-coder:14b", "messages": [{"role": "user", "content": "hello"}]}
    value.update(over)
    return value


@unittest.skipUnless(base.loopback_bind_available(), "needs loopback sockets")
class RelayCase(unittest.TestCase):
    def setUp(self):
        self.manager = StubManager()
        self.server = runner.RunnerServer(("127.0.0.1", 0), runner.Handler)
        self.server.manager = self.manager
        self.server.runner_token = TOKEN
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.engine = FakeEngine()
        patch = mock.patch.object(runner, "loopback_urlopen", self.engine)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def post(self, path, payload, headers=None, raw=None):
        c = self.conn()
        data = raw if raw is not None else json.dumps(payload).encode()
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        c.request("POST", path, body=data, headers=h)
        r = c.getresponse()
        out = (r.status, r.read(), dict(r.getheaders()))
        c.close()
        return out

    def relay(self, payload, token=RELAY, **kw):
        headers = {"Authorization": "Bearer " + token} if token else {}
        return self.post(CHAT, payload, headers, **kw)

    def raw_request(self, length):
        """A connected socket with the relay request line and headers sent (Content-Length `length`), no body yet."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.sendall(("POST %s HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\n"
                      "Content-Length: %d\r\n\r\n" % (CHAT, RELAY, length)).encode())
        return sock

    def read_headers(self, sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def audits(self, event="relay_chat"):
        return [d for e, d in self.manager.audits if e == event]


class RunnerAuth(RelayCase):
    def test_no_token_is_401(self):
        self.assertEqual(self.relay(body(), token=None)[0], 401)

    def test_relay_token_on_the_relay_route_is_ok(self):
        status, data, _ = self.relay(body())
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["choices"][0]["message"]["content"], "hi")

    def test_relay_token_on_other_routes_is_401(self):
        for path, payload in (("/jobs", {}), ("/services/restart", {"service": "runner"})):
            for headers in ({"Authorization": "Bearer " + RELAY}, {"X-Runner-Token": RELAY}):
                self.assertEqual(self.post(path, payload, headers)[0], 401, (path, headers))
        c = self.conn()
        c.request("GET", "/health", headers={"Authorization": "Bearer " + RELAY})
        self.assertEqual(c.getresponse().status, 401)

    def test_full_runner_token_on_the_relay_route_is_401(self):
        for headers in ({"Authorization": "Bearer " + TOKEN}, {"X-Runner-Token": TOKEN}):
            self.assertEqual(self.post(CHAT, body(), headers)[0], 401)
        self.assertFalse(self.engine.chat_requests)

    def test_relay_token_is_derived_from_the_runner_token_and_is_not_it(self):
        self.assertNotEqual(RELAY, TOKEN)
        self.assertEqual(len(RELAY), 64)
        self.assertNotEqual(runner.relay_token("other-token"), RELAY)

    def test_runner_token_never_in_audit(self):
        self.relay(body(), token="wrong-" + RELAY)
        self.relay(body())
        self.assertNotIn(TOKEN, str(self.manager.audits))
        self.assertNotIn(RELAY, str(self.manager.audits))


class RunnerValidation(RelayCase):
    def test_bad_model_is_400(self):
        for model in ("../../etc", "", 5, None):
            self.assertEqual(self.relay(body(model=model))[0], 400, model)
        self.assertFalse(self.engine.chat_requests)

    def test_messages_must_be_a_list(self):
        self.assertEqual(self.relay(body(messages="hi"))[0], 400)

    def test_unknown_key_is_stripped_and_named_in_the_audit_but_not_its_value(self):
        status, _, _ = self.relay(body(evil_key="VALUE-XYZ", temperature=0.2, think=True))
        self.assertEqual(status, 200)
        sent = json.loads(self.engine.chat_requests[0][0].data)
        self.assertNotIn("evil_key", sent)
        self.assertNotIn("think", sent)
        self.assertEqual(sent["temperature"], 0.2)
        audit = self.audits()[-1]
        self.assertIn("evil_key", str(audit))
        self.assertNotIn("VALUE-XYZ", str(audit))

    def test_body_over_8_mib_is_413(self):
        # Raw socket: the runner answers from Content-Length alone and closes, so the body is never sent in full.
        sock = self.raw_request(8 * 1024 * 1024 + 1)
        try:
            try:
                sock.sendall(b'{"model":"x","messages":[],"pad":"' + b"a" * 4096)  # a partial body, at most
            except (BrokenPipeError, ConnectionResetError):
                pass
            status_line = self.read_headers(sock).split(b"\r\n", 1)[0]
        finally:
            sock.close()
        self.assertEqual(status_line.split()[1], b"413")
        self.assertFalse(self.engine.chat_requests)

    def framing(self, *extra, length=None):
        """Raw request with custom framing headers; returns (status, engine calls)."""
        lines = ["POST %s HTTP/1.1" % CHAT, "Host: x", "Authorization: Bearer " + RELAY] + list(extra)
        if length is not None:
            lines.append("Content-Length: %s" % length)
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            try:
                sock.sendall(json.dumps(body()).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
            head = self.read_headers(sock)
        finally:
            sock.close()
        return int(head.split(b" ", 2)[1]), self.engine.tag_calls + len(self.engine.chat_requests)

    def test_negative_content_length_is_400_and_nothing_reaches_the_engine(self):
        self.assertEqual(self.framing(length=-1), (400, 0))

    def test_invalid_or_duplicate_content_length_is_400(self):
        for length in ("abc", "1.5", "+5", "0x10"):
            self.assertEqual(self.framing(length=length), (400, 0), length)
        self.assertEqual(self.framing("Content-Length: 5", length=5), (400, 0))

    def test_transfer_encoding_is_rejected(self):
        self.assertEqual(self.framing("Transfer-Encoding: chunked", length=10), (400, 0))
        self.assertEqual(self.framing("Transfer-Encoding: chunked"), (400, 0))

    def test_missing_content_length_is_411(self):
        self.assertEqual(self.framing(), (411, 0))

    def test_non_loopback_ollama_url_is_502(self):
        self.manager.ollama_url = "http://100.101.102.103:11434"
        self.assertEqual(self.relay(body())[0], 502)
        self.assertFalse(self.engine.chat_requests)

    def test_forwards_only_to_the_loopback_engine(self):
        self.relay(body())
        self.assertEqual(self.engine.chat_requests[0][0].full_url, "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(self.engine.chat_requests[0][1], 600)

    def test_third_concurrent_request_is_429_with_an_openai_error_and_retry_after(self):
        gate, started = threading.Event(), threading.Semaphore(0)

        def slow(request):
            started.release()
            gate.wait(10)
            return Response(b"{}")
        self.engine.chat = slow
        threads = [threading.Thread(target=self.relay, args=(body(),), daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        for _ in range(2):
            self.assertTrue(started.acquire(timeout=5))
        try:
            status, data, headers = self.relay(body())
        finally:
            gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(status, 429)
        self.assertEqual(headers.get("Retry-After"), "2")
        self.assertEqual(json.loads(data), {"error": {"message": "relay busy", "type": "rate_limit_error",
                                                      "code": "rate_limit_exceeded"}})
        self.assertEqual(self.relay(body())[0], 200, "slots are released")

    def test_slots_are_taken_before_the_body_is_read_or_tags_are_queried(self):
        gate, started = threading.Event(), threading.Semaphore(0)

        def slow(request):
            started.release()
            gate.wait(10)
            return Response(b"{}")
        self.engine.chat = slow
        threads = [threading.Thread(target=self.relay, args=(body(),), daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        for _ in range(2):
            self.assertTrue(started.acquire(timeout=5))
        tags = self.engine.tag_calls
        try:
            status, _, _ = self.relay(body(model="never-checked:1b"))
            self.assertEqual(status, 429)
            self.assertEqual(self.engine.tag_calls, tags, "a rejected request must not query tags")
            self.assertEqual(len(self.engine.chat_requests), 2)
        finally:
            gate.set()
        for t in threads:
            t.join(5)

    def test_slot_is_released_after_validation_errors(self):
        for _ in range(4):
            self.assertEqual(self.relay(body(model="ghost:1b"))[0], 400)
        self.assertEqual(self.relay(body())[0], 200)

    def test_unknown_model_refreshes_tags_once_then_400(self):
        self.relay(body())  # fills the cache
        before = self.engine.tag_calls
        self.assertEqual(self.relay(body(model="ghost:1b"))[0], 400)
        self.assertEqual(self.engine.tag_calls, before + 1)

    def test_model_added_after_the_cache_was_filled_is_accepted(self):
        self.assertEqual(self.relay(body())[0], 200)
        self.engine.models.append("fleet-coder-14b")
        self.assertEqual(self.relay(body(model="fleet-coder-14b"))[0], 200)

    def test_tags_are_cached_for_a_while(self):
        self.relay(body())
        self.relay(body())
        self.assertEqual(self.engine.tag_calls, 1)


class RunnerStreaming(RelayCase):
    def test_stream_false_returns_json(self):
        status, data, headers = self.relay(body(stream=False))
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(data)["choices"][0]["message"]["content"], "hi")

    def test_upstream_http_error_is_502(self):
        def fail(request):
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)
        self.engine.chat = fail
        for stream in (False, True):
            status, data, _ = self.relay(body(stream=stream))
            self.assertEqual(status, 502)
            self.assertIn("engine returned HTTP 500", data.decode())

    def test_stream_true_relays_chunks_with_an_sse_content_type(self):
        lines = [b'data: {"n":1}\n', b"\n", b'data: {"n":2}\n', b"\n", b"data: [DONE]\n", b"\n"]
        self.engine.chat = lambda request: Response(b"".join(lines))
        status, data, headers = self.relay(body(stream=True))
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        self.assertEqual(data, b"".join(lines))

    def test_client_disconnect_closes_upstream(self):
        closed = threading.Event()

        class Endless:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

            def readline(self, *a):
                time.sleep(0.002)
                return b"data: " + b"x" * 60000 + b"\n"
            read1 = read = lambda self, *a: self.readline()

            def close(self):
                closed.set()
        self.engine.chat = lambda request: Endless()
        data = json.dumps(body(stream=True)).encode()
        sock = self.raw_request(len(data))
        sock.sendall(data)
        head = self.read_headers(sock)
        self.assertIn(b"text/event-stream", head)
        sock.recv(1024)  # the first chunk, then leave
        sock.close()
        deadline = time.time() + 10
        while not closed.is_set() and time.time() < deadline:
            closed.wait(0.05)
        self.assertTrue(closed.is_set(), "upstream was not closed after the client left")

    def test_mid_stream_error_closes_the_connection_with_nothing_appended(self):
        class Breaks:
            def __init__(self):
                self.lines = [b'data: {"n":1}\n', b"\n"]

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def readline(self, *a):
                if self.lines:
                    return self.lines.pop(0)
                raise OSError("upstream died")
            read1 = read = lambda self, *a: self.readline()

            def close(self):
                pass
        self.engine.chat = lambda request: Breaks()
        status, data, _ = self.relay(body(stream=True))
        self.assertEqual(status, 200)
        self.assertEqual(data, b'data: {"n":1}\n\n')

    def test_audit_never_contains_message_content(self):
        secret = "TOP-SECRET-PROMPT-123"
        self.engine.chat = lambda request: Response(b'data: {"c":"' + secret.encode() + b'"}\n\n')
        self.relay(body(messages=[{"role": "user", "content": secret}], stream=True))
        self.relay(body(messages=[{"role": "user", "content": secret}]))
        self.assertEqual(len(self.audits()), 2)
        self.assertNotIn(secret, str(self.manager.audits))
        detail = str(self.audits()[0])
        for field in ("qwen2.5-coder:14b", "127.0.0.1", "stream"):
            self.assertIn(field, detail)


class Health(unittest.TestCase):
    def test_health_report_advertises_relay_chat(self):
        manager = mock.MagicMock()
        manager.metrics = None
        manager.jobs = {}
        manager.jobs_report.return_value = []
        manager.ollama_url = "http://127.0.0.1:11434"
        manager.engine_catalog.return_value = {}
        with mock.patch.object(runner, "find_agents", return_value={}), \
                mock.patch.object(runner, "system_power", return_value={"on_battery": None, "battery_pct": None}), \
                mock.patch.object(runner, "posture_of", return_value={}):
            report = runner.health_report(manager)
        self.assertIs(report["relay_chat"], True)


if __name__ == "__main__":
    unittest.main()
