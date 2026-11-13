import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from long.llm.base import BackendError
from long.llm.openai_compat import OpenAICompatBackend


def serve(handler_fn):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            code, payload = handler_fn(self.path, dict(self.headers), body)
            data = json.dumps(payload).encode()
            self.send_response(code); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_tool_call_roundtrip_and_auth_header(monkeypatch):
    seen = {}
    def fn(path, headers, body):
        seen.update(path=path, auth=headers.get("Authorization"), body=body)
        return 200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "git", "arguments": "{\"args\":[\"status\"]}"}}]}}]}
    srv = serve(fn)
    monkeypatch.setenv("MY_KEY", "sekret")
    b = OpenAICompatBackend(f"http://127.0.0.1:{srv.server_port}/v1", "m", api_key_env="MY_KEY")
    turn = b.chat([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "git"}}])
    srv.shutdown()
    assert seen["path"] == "/v1/chat/completions" and seen["auth"] == "Bearer sekret"
    assert seen["body"]["model"] == "m" and seen["body"]["tools"]
    assert turn.tool_calls[0].name == "git" and json.loads(turn.tool_calls[0].arguments) == {"args": ["status"]}


def test_http_error_raises_backend_error():
    srv = serve(lambda *a: (500, {"error": "boom"}))
    b = OpenAICompatBackend(f"http://127.0.0.1:{srv.server_port}/v1", "m")
    with pytest.raises(BackendError, match="500"):
        b.chat([], [])
    srv.shutdown()


def test_connection_refused_raises_backend_error():
    with pytest.raises(BackendError):
        OpenAICompatBackend("http://127.0.0.1:1/v1", "m").chat([], [])


def serve_models(models_payload, models_code=200):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self._send(404, {"error": {"message": "model 'm' not found"}})
        def do_GET(self):
            self._send(models_code, models_payload)
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_404_model_not_found_lists_available_models():
    srv = serve_models({"object": "list", "data": [{"id": "a:7b"}, {"id": "b:3b"}]})
    b = OpenAICompatBackend(f"http://127.0.0.1:{srv.server_port}/v1", "m")
    with pytest.raises(BackendError) as e:
        b.chat([], [])
    srv.shutdown()
    assert "available models: a:7b, b:3b" in str(e.value) and "not found" in str(e.value)


def test_404_hint_failure_is_ignored():
    srv = serve_models({"oops": 1}, models_code=500)
    b = OpenAICompatBackend(f"http://127.0.0.1:{srv.server_port}/v1", "m")
    with pytest.raises(BackendError) as e:
        b.chat([], [])
    srv.shutdown()
    assert "404" in str(e.value) and "available models" not in str(e.value)


def test_list_and_set_model():
    srv = serve_models({"data": [{"id": "z"}, {"id": "a"}]})
    b = OpenAICompatBackend(f"http://127.0.0.1:{srv.server_port}/v1", "a")
    assert b.list_models() == ["a", "z"]
    b.set_model("z")
    srv.shutdown()
    assert b.model == "z" and b.name.startswith("openai-compat:z@")


def test_list_models_error():
    with pytest.raises(BackendError):
        OpenAICompatBackend("http://127.0.0.1:1/v1", "m").list_models()
