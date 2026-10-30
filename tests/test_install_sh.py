"""
Drives install.sh end to end against a local tarball (file:// URL), with
dependency install, the Chrome check and the launch step switched off.
"""
import os
import shutil
import subprocess
import tarfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SH = os.path.join(ROOT, "install.sh")


def _make_tarball(tmp_path, dashboard_body="print('v1')\n"):
    src = tmp_path / "src" / "laker-connection-main"
    if src.exists():
        shutil.rmtree(src)
    src.mkdir(parents=True)
    (src / "dashboard.py").write_text(dashboard_body)
    (src / "requirements.txt").write_text("flask==3.0.3\n")
    (src / "UPDATE_SOURCE").write_text("Vandanp14/laker-connection main\n")
    (src / "launch.sh").write_text("#!/bin/bash\necho launched\n")
    shutil.copy(INSTALL_SH, src / "install.sh")
    tarball = tmp_path / "app.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(src, arcname="laker-connection-main")
    return tarball


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "Home With Space"
    home.mkdir()
    tarball = _make_tarball(tmp_path)
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "LAKER_HOME": str(home / "LakerConnection"),
        "LAKER_TARBALL_URL": f"file://{tarball}",
        "LAKER_SKIP_DEPS": "1",
        "LAKER_SKIP_CHROME_CHECK": "1",
        "LAKER_NO_LAUNCH": "1",
        # A free port, so a real dashboard running on 5050 doesn't make the
        # installer exit early with "already running".
        "LAKER_PORT": str(_free_port()),
    }


def _run(env):
    return subprocess.run(["/bin/bash", INSTALL_SH], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)


def test_fresh_install_layout(env):
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    home = env["LAKER_HOME"]
    assert os.path.isfile(os.path.join(home, "app", "dashboard.py"))
    assert os.path.isdir(os.path.join(home, "data"))
    launcher = os.path.join(home, "Laker Connection.command")
    assert os.access(launcher, os.X_OK)
    assert home in open(launcher).read()


def test_reinstall_preserves_data_bytes(env, tmp_path):
    assert _run(env).returncode == 0
    data_dir = os.path.join(env["LAKER_HOME"], "data")
    files = {"residents.csv": b"full_name,email_or_id\r\nJane Doe,jdoe@oswego.edu",
             "settings.json": b'{"valid_locations": ["Lobby"]}'}
    for name, body in files.items():
        with open(os.path.join(data_dir, name), "wb") as f:
            f.write(body)
    _make_tarball(tmp_path, dashboard_body="print('v2')\n")

    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert open(os.path.join(env["LAKER_HOME"], "app", "dashboard.py")).read() == "print('v2')\n"
    for name, body in files.items():
        assert open(os.path.join(data_dir, name), "rb").read() == body
    assert not os.path.exists(os.path.join(env["LAKER_HOME"], "app.old"))


def test_offline_with_existing_app_continues(env):
    assert _run(env).returncode == 0
    offline = dict(env, LAKER_TARBALL_URL="file:///nonexistent/app.tar.gz")
    result = _run(offline)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Starting the version you already have" in result.stdout
    assert open(os.path.join(env["LAKER_HOME"], "app", "dashboard.py")).read() == "print('v1')\n"


def test_offline_first_install_fails_cleanly(env):
    offline = dict(env, LAKER_TARBALL_URL="file:///nonexistent/app.tar.gz")
    result = _run(offline)
    assert result.returncode == 1
    assert "Check your internet connection" in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_shell_syntax():
    assert subprocess.run(["/bin/bash", "-n", INSTALL_SH]).returncode == 0
    if shutil.which("shellcheck"):
        result = subprocess.run(["shellcheck", "-s", "bash", INSTALL_SH],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stdout


def test_already_running_skips_update(env, tmp_path):
    """A second double-click while the dashboard is up must not swap app/
    under the running app — just open the existing dashboard."""
    import socket
    import sys
    import time
    assert _run(env).returncode == 0
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>Laker Connection</h1>")
    server = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                              cwd=site, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.1)
        stub = tmp_path / "bin"
        stub.mkdir()
        (stub / "open").write_text("#!/bin/bash\nexit 0\n")
        (stub / "open").chmod(0o755)
        _make_tarball(tmp_path, dashboard_body="print('v2')\n")
        result = _run(dict(env, LAKER_PORT=str(port), PATH=f"{stub}:{env['PATH']}"))
    finally:
        server.terminate()
        server.wait()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already running" in result.stdout
    assert open(os.path.join(env["LAKER_HOME"], "app", "dashboard.py")).read() == "print('v1')\n"


def test_second_start_while_first_is_starting(env):
    """Two quick double-clicks: the second backs off instead of racing."""
    os.makedirs(env["LAKER_HOME"], exist_ok=True)
    lock = os.path.join(env["LAKER_HOME"], ".lock")
    os.mkdir(lock)
    with open(os.path.join(lock, "pid"), "w") as f:
        f.write(str(os.getpid()))  # a live process holds it
    result = _run(env)
    assert result.returncode == 0
    assert "already starting" in result.stdout
    assert not os.path.exists(os.path.join(env["LAKER_HOME"], "app"))


def test_stale_lock_is_taken_over(env):
    os.makedirs(os.path.join(env["LAKER_HOME"], ".lock"))
    with open(os.path.join(env["LAKER_HOME"], ".lock", "pid"), "w") as f:
        f.write("999999")  # no such process
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert os.path.isfile(os.path.join(env["LAKER_HOME"], "app", "dashboard.py"))
    assert not os.path.exists(os.path.join(env["LAKER_HOME"], ".lock"))


def test_desktop_copy_failure_is_not_fatal(env):
    desktop = os.path.join(env["HOME"], "Desktop")
    os.mkdir(desktop)
    os.chmod(desktop, 0o500)
    try:
        result = _run(env)
    finally:
        os.chmod(desktop, 0o700)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Laker Connection.command" in result.stdout
    assert os.access(os.path.join(env["LAKER_HOME"], "Laker Connection.command"), os.X_OK)
