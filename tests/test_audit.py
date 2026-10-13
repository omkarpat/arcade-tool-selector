"""Audit sinks: a JSONL file, or OpenTelemetry over OTLP."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from arcade_tool_selector.audit import Audit


def test_file_sink(tmp_path):
    """Without otel, events are appended to the JSONL file."""
    audit = Audit(tmp_path / "audit.jsonl")
    audit.write("catalog_changed", added=["Gmail_ListEmails"])
    assert json.loads((tmp_path / "audit.jsonl").read_text())["added"] == ["Gmail_ListEmails"]


def test_otel_sink_exports_over_otlp(tmp_path, monkeypatch):
    """With otel, events are exported over OTLP to the configured endpoint and the file is never written."""
    received: list[tuple[str, bytes]] = []

    class Collector(BaseHTTPRequestHandler):
        """A stand-in OTLP collector that records each request."""

        def do_POST(self):
            """Record the request's path and body."""
            received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            """Keep the test output quiet."""
            pass

    server = HTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}")

    audit = Audit(tmp_path / "audit.jsonl", otel=True)
    audit.write("prompt_seen", session_id="s1", prompt_id=None, chars=12)
    audit.close()
    server.shutdown()

    assert not (tmp_path / "audit.jsonl").exists(), "otel replaces the file"
    path, body = received[0]
    assert path == "/v1/logs"
    assert b"ats.prompt_seen" in body and b"s1" in body and b"arcade-tool-selector" in body
