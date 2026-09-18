import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from servora.ai import MockAIProvider, OpenAICompatibleProvider, create_container_plan, AIPlanError


def test_mock_plan():
    result = create_container_plan(MockAIProvider(), "make nginx")
    assert result["image"] == "nginx:alpine"


def test_prompt_limit():
    with pytest.raises(AIPlanError):
        create_container_plan(MockAIProvider(), "x" * 16385)


def test_openai_compatible_json_response():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": '{"action":"create_container","name":"x","image":"nginx"}'}}]}).encode())
        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        p = OpenAICompatibleProvider(f"http://127.0.0.1:{server.server_port}/v1", "test")
        result = create_container_plan(p, "create nginx")
        assert result["name"] == "x"
    finally:
        server.shutdown()
        thread.join()
