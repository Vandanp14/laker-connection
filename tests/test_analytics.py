"""
Quiet usage analytics client. In the private repo the module lives at
public_only/analytics.py (never imported by the app); in the public repo it
sits at the root as analytics.py. Load it by path from whichever exists.
"""
import importlib.util
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANDIDATES = [os.path.join(ROOT, "public_only", "analytics.py"), os.path.join(ROOT, "analytics.py")]
MODULE_PATH = next((p for p in _CANDIDATES if os.path.exists(p)), _CANDIDATES[0])


def _load():
    spec = importlib.util.spec_from_file_location("analytics_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server():
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            bodies.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/exec", bodies
    httpd.shutdown()


@pytest.fixture
def analytics(tmp_path, monkeypatch, server):
    url, _ = server
    data = tmp_path / "LakerConnection" / "data"
    data.mkdir(parents=True)
    monkeypatch.setenv("LAKER_DATA_DIR", str(data))
    monkeypatch.delenv("LAKER_NO_ANALYTICS", raising=False)
    monkeypatch.delenv("LAKER_DEBUG", raising=False)
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    module = _load()
    monkeypatch.setattr(module, "ENDPOINT", url)
    return module


def _events(bodies):
    return [b["event"] for b in bodies]


def test_payload_has_only_allowed_keys(analytics):
    assert set(analytics.payload("launch")) == {"token", "event", "install_id", "version"}
    submit = analytics.payload("submit", submitted=1, failed=0, uncertain=0, skipped=0,
                               crashed=0, seconds=3)
    assert set(submit) == {"token", "event", "install_id", "version", "submitted", "failed",
                           "uncertain", "skipped", "crashed", "seconds"}


def test_track_sends_json(analytics, server):
    _, bodies = server
    analytics.track("launch").join(5)
    assert len(bodies) == 1
    assert bodies[0]["event"] == "launch" and bodies[0]["token"] == "lc1"


def test_install_fires_once(analytics, server):
    _, bodies = server
    for _ in range(2):
        analytics.on_launch()
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon and t.name.startswith("laker-"):
                t.join(5)
    assert _events(bodies) == ["install", "launch", "launch"]
    assert len({b["install_id"] for b in bodies}) == 1


def test_unwritable_data_dir_skips_install(analytics, server, tmp_path):
    _, bodies = server
    data = tmp_path / "LakerConnection" / "data"
    os.chmod(data, 0o500)
    try:
        analytics.on_launch()
        time.sleep(0.5)
    finally:
        os.chmod(data, 0o700)
    assert _events(bodies) == ["launch"]


def test_off_switch_env(analytics, server, monkeypatch):
    monkeypatch.setenv("LAKER_NO_ANALYTICS", "1")
    assert analytics.track("launch") is None
    assert server[1] == []


def test_off_switch_file(analytics, server, tmp_path):
    (tmp_path / "LakerConnection" / "no-analytics").write_text("")
    assert analytics.track("launch") is None
    assert server[1] == []


def test_empty_endpoint_disabled(analytics, server, monkeypatch):
    monkeypatch.setattr(analytics, "ENDPOINT", "")
    assert analytics.track("launch") is None
    assert server[1] == []


def test_unreachable_endpoint_is_fast_and_silent(analytics, monkeypatch, capsys):
    monkeypatch.setattr(analytics, "ENDPOINT", "http://10.255.255.1/x")
    start = time.monotonic()
    thread = analytics.track("launch")
    assert time.monotonic() - start < 0.05
    thread.join(4)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_debug_reloader_parent_skipped(analytics, server, monkeypatch):
    monkeypatch.setenv("LAKER_DEBUG", "1")
    analytics.on_launch()
    time.sleep(0.3)
    assert server[1] == []


def test_tls_falls_back_to_macos_cert_bundle(analytics, monkeypatch):
    """Python.org / standalone Pythons on macOS can ship with an empty CA store;
    then every HTTPS send fails verification. Use the system bundle instead —
    never disable verification."""
    import ssl
    loaded = []
    real = ssl.create_default_context

    def empty_context(*args, **kwargs):
        ctx = real(*args, **kwargs)
        monkeypatch.setattr(ctx, "cert_store_stats", lambda: {"x509_ca": 0})
        monkeypatch.setattr(ctx, "load_verify_locations",
                            lambda cafile=None, **kw: loaded.append(cafile))
        return ctx

    monkeypatch.setattr(ssl, "create_default_context", empty_context)
    ctx = analytics._tls_context()
    assert loaded == ["/etc/ssl/cert.pem"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


ERROR_KEYS = {"token", "event", "install_id", "version", "stage", "kind", "student", "when",
              "start", "end", "where", "reason_locked", "reason_options", "waited_ms", "elapsed_ms"}


def test_error_payload_keys_exact(analytics):
    diag = {"stage": "reason", "kind": "timeout", "student": "Doe, Jane", "when": "01/15/2026",
            "start_time": "1:00 pm", "end_time": "", "where": "In- Residence Halls",
            "reason_disabled": True, "reason_options": -1, "waited_ms": 45000, "elapsed_ms": 61000}
    body = analytics.payload("error", **analytics.error_fields(diag))
    assert set(body) == ERROR_KEYS
    assert body["stage"] == "reason" and body["kind"] == "timeout"
    assert (body["student"], body["when"], body["start"], body["end"], body["where"]) == (1, 1, 1, 0, 1)
    assert body["reason_locked"] == 1 and body["reason_options"] == -1


def test_error_fields_never_carry_text(analytics):
    diag = {"stage": "Jane Doe", "kind": "Jane Doe", "where": "Out of Residence Halls",
            "student": "Doe, Jane", "url_path": "/jane", "error": "No match for Jane Doe"}
    body = analytics.payload("error", **analytics.error_fields(diag))
    assert body["stage"] == "other" and body["kind"] == "other"
    assert body["where"] == 1 and body["student"] == 1
    for key, value in body.items():
        if key in ("token", "event", "install_id", "version"):
            continue
        assert "jane" not in str(value).lower()
        assert isinstance(value, int) or value in analytics.ERROR_STAGES + analytics.ERROR_KINDS


def test_error_fields_clamp_numbers(analytics):
    fields = analytics.error_fields({"waited_ms": 10**9, "elapsed_ms": -5, "reason_disabled": None,
                                     "reason_options": 9999})
    assert fields["waited_ms"] == 600000 and fields["elapsed_ms"] == 0
    assert fields["reason_locked"] == 2 and fields["reason_options"] == 200
    assert analytics.error_fields({})["reason_options"] == -1


def test_error_kind_allows_exception_class_names(analytics):
    assert analytics.error_fields({"kind": "TimeoutError"})["kind"] == "TimeoutError"
    assert analytics.error_fields({"kind": "Weird-Name"})["kind"] == "other"


def test_error_kind_allows_playwright_bare_error(analytics):
    assert analytics.error_fields({"kind": "Error"})["kind"] == "Error"
    assert analytics.error_fields({"kind": "Jane"})["kind"] == "other"


def test_track_batch_sends_in_order_on_one_thread(analytics, server):
    _, bodies = server
    thread = analytics.track_batch([
        ("submit", {"submitted": 0, "failed": 2, "uncertain": 0, "skipped": 0, "crashed": 0,
                    "seconds": 9}),
        ("error", analytics.error_fields({"stage": "reason", "kind": "timeout"})),
        ("error", analytics.error_fields({"stage": "where", "kind": "mismatch"})),
    ])
    thread.join(10)
    assert [b["event"] for b in bodies] == ["submit", "error", "error"]
    assert [b.get("stage") for b in bodies[1:]] == ["reason", "where"]


def test_track_batch_disabled_returns_none(analytics, server, monkeypatch):
    monkeypatch.setenv("LAKER_NO_ANALYTICS", "1")
    assert analytics.track_batch([("launch", {})]) is None
    assert server[1] == []
