import subprocess
import sys

import httpx


def _start_server(project_root) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "doorstop_server", "--project", str(project_root), "--port", "0", "--exit-on-stdin-eof"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def test_port_zero_reports_the_bound_port_and_serves_health(project_root):
    process = _start_server(project_root)
    try:
        line = process.stdout.readline().strip()
        assert line.startswith("DOORSTOP_PORT="), line
        port = int(line.split("=", 1)[1])
        assert port > 0
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=10)
        assert response.status_code == 200
    finally:
        process.kill()
        process.wait()


def test_server_exits_when_stdin_closes(project_root):
    # The extension host dying closes this pipe; the server must not outlive it.
    process = _start_server(project_root)
    try:
        port = int(process.stdout.readline().split("=", 1)[1])
        # The watched pipe must not hang the validation worker's start (seen on Windows).
        assert httpx.get(f"http://127.0.0.1:{port}/validate", timeout=15).status_code == 200
        process.stdin.close()
        assert process.wait(timeout=5) == 0
    finally:
        process.kill()
        process.wait()
