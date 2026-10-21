"""
launch.sh paths that don't need a real Chrome: syntax, "already running",
and "port taken by something else". The full Chrome + dashboard flow is
checked by hand (see the plan's Task 8 checklist).
"""
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCH_SH = os.path.join(ROOT, "launch.sh")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    """Starts a static server on a free port; yields (port, set_page)."""
    port = _free_port()
    root = tmp_path / "site"
    root.mkdir()
    proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                            cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.1)
    yield port, (lambda html: (root / "index.html").write_text(html))
    proc.terminate()
    proc.wait()


def _run(tmp_path, port, cdp_port=None):
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    record = tmp_path / "open-args"
    (stub / "open").write_text(f'#!/bin/bash\necho "$@" >> "{record}"\n')
    (stub / "open").chmod(0o755)
    env = {"PATH": f"{stub}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "LAKER_HOME": str(tmp_path / "LakerConnection"), "LAKER_PORT": str(port)}
    if cdp_port:
        env["LAKER_CDP_PORT"] = str(cdp_port)
        venv_bin = tmp_path / "LakerConnection" / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        if not (venv_bin / "python").exists():
            (venv_bin / "python").symlink_to(sys.executable)
    result = subprocess.run(["/bin/bash", LAUNCH_SH], env=env, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=30)
    opened = record.read_text() if record.exists() else ""
    return result, opened


def test_launch_reports_already_running(tmp_path, server):
    port, set_page = server
    set_page("<title>Laker Connection</title>")
    result, opened = _run(tmp_path, port)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already running" in result.stdout
    assert f"http://127.0.0.1:{port}" in opened


def test_launch_port_taken_by_other_app(tmp_path, server):
    port, set_page = server
    set_page("<title>Something else</title>")
    result, opened = _run(tmp_path, port)
    assert result.returncode == 1
    assert f"port {port}" in result.stdout + result.stderr
    assert opened == ""


def test_launch_syntax():
    assert subprocess.run(["/bin/bash", "-n", LAUNCH_SH]).returncode == 0
    if shutil.which("shellcheck"):
        assert subprocess.run(["shellcheck", "-s", "bash", LAUNCH_SH]).returncode == 0


def test_launch_refuses_foreign_process_on_cdp_port(tmp_path, server):
    """Something that isn't our Chrome listening on the CDP port must not be
    trusted: the dashboard would drive whatever is there."""
    cdp_port, set_page = server
    set_page("{}")
    result, opened = _run(tmp_path, _free_port(), cdp_port=cdp_port)
    assert result.returncode == 1
    assert f"port {cdp_port}" in result.stdout + result.stderr
    assert opened == ""
