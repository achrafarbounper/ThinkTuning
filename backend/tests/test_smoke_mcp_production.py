from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from scripts.smoke_mcp_production import main


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request["method"] == "initialize":
            result = {"capabilities": {"sampling": {}}}
        else:
            result = {"tools": [{"name": "orchestrate"}]}
        body = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result})}\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args):
        return


def test_production_smoke_checks_initialize_and_tools(monkeypatch, capsys):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        "sys.argv",
        ["smoke_mcp_production.py", "--url", f"http://127.0.0.1:{server.server_port}", "--api-key", "test"],
    )
    try:
        assert main() == 0
    finally:
        server.shutdown()
        thread.join()
    assert json.loads(capsys.readouterr().out) == {
        "sampling": True,
        "status": "ok",
        "tool_count": 1,
    }
