"""Guards the Piper install/smoke script against the health-check regression.

piper1-gpl 1.4.2 serves synthesis at POST / (so GET / answers 405) and exposes
GET /voices as the readiness endpoint. If _wait_health ever probes bare / again,
`install-piper.sh smoke` would fail every time with 405 — this test catches that
by running the real bash function against a mock that mimics both responses.
"""

import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "ojn" / "piper" / "install-piper.sh"

requires_bash = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("curl")),
    reason="needs bash and curl on PATH",
)


class _PiperMock(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/voices":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"[]")
        else:  # bare / is POST-only on the real server
            self.send_response(405)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"RIFF\x00\x00\x00\x00WAVE")

    def log_message(self, *_):  # silence the test output
        pass


@pytest.fixture
def piper_mock():
    server = HTTPServer(("127.0.0.1", 0), _PiperMock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _wait_health(port: int) -> subprocess.CompletedProcess:
    # Source the script (dispatch is guarded, so no subcommand runs) and call
    # the real _wait_health against the mock.
    snippet = f'source "{SCRIPT}"; _wait_health {port}'
    return subprocess.run(["bash", "-c", snippet], capture_output=True, timeout=30)


@requires_bash
def test_health_check_hits_voices_not_root(piper_mock):
    # /voices -> 200, / -> 405: succeeds only because the probe targets /voices.
    result = _wait_health(piper_mock)
    assert result.returncode == 0, result.stderr.decode()
