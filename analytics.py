"""
Anonymous usage counts for the shared Laker Connection app.

Sends one small event (install / launch / submit) to a Google Apps Script
endpoint: a random install ID, the app version, and — for submit — row counts
and how long the batch took. Never names, emails, notes, dates, locations or
anything else from the data files. Sending happens on a background thread
with a short timeout; any failure is ignored, so this can never slow down or
break the app.

Off when LAKER_NO_ANALYTICS=1, when a "no-analytics" file exists in the data
folder or the folder above it, or when ENDPOINT is empty.
"""
import json
import os
import ssl
import threading
import urllib.request
import uuid

import paths

ENDPOINT = "https://script.google.com/macros/s/AKfycbwWZG4R7T4-6xPvYKZ1qpsZBCEEQ83Y_tfLC2vfopEfrGOxTI6bsK7RCjxTmUHeeNJP/exec"
TOKEN = "lc1"
TIMEOUT_SECONDS = 3
SUBMIT_FIELDS = ("submitted", "failed", "uncertain", "skipped", "crashed", "seconds")

_session_id = None


def enabled() -> bool:
    if not ENDPOINT or os.environ.get("LAKER_NO_ANALYTICS") == "1":
        return False
    data = paths.data_dir()
    for folder in (data, os.path.dirname(data)):
        if os.path.exists(os.path.join(folder, "no-analytics")):
            return False
    return True


def install_id():
    """(id, True) when a new ID was just created and saved; (id, False) when
    it already existed or couldn't be saved (then it lives for this run only)."""
    global _session_id
    path = os.path.join(paths.data_dir(), ".install_id")
    try:
        with open(path) as f:
            saved = f.read().strip()
        uuid.UUID(saved)
        return saved, False
    except (OSError, ValueError):
        pass
    new_id = str(uuid.uuid4())
    try:
        with open(path, "w") as f:
            f.write(new_id + "\n")
        return new_id, True
    except OSError:
        if _session_id is None:
            _session_id = new_id
        return _session_id, False


def _version() -> str:
    try:
        with open(os.path.join(paths.APP_ROOT, "BUILD_INFO"), encoding="utf-8") as f:
            return (f.read().split() or ["dev"])[0][:40]
    except OSError:
        return "dev"


def payload(event: str, **counts) -> dict:
    body = {"token": TOKEN, "event": event, "install_id": install_id()[0], "version": _version()}
    if event == "submit":
        body.update({key: int(counts.get(key, 0)) for key in SUBMIT_FIELDS})
    return body


MACOS_CA_BUNDLE = "/etc/ssl/cert.pem"


def _tls_context():
    """Verified HTTPS. Some Python builds on macOS (python.org without its
    "Install Certificates" step, standalone builds) start with an empty CA
    store, which makes every send fail; fall back to macOS's own bundle."""
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509_ca") and os.path.exists(MACOS_CA_BUNDLE):
        ctx.load_verify_locations(cafile=MACOS_CA_BUNDLE)
    return ctx


def _send(body: dict) -> None:
    try:
        request = urllib.request.Request(
            ENDPOINT, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "text/plain;charset=utf-8"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=_tls_context()) as response:
            response.read(64)
    except Exception:
        pass


def _send_all(bodies) -> None:
    for body in bodies:
        _send(body)


def _start(bodies):
    thread = threading.Thread(target=_send_all, args=(bodies,), name="laker-analytics",
                              daemon=True)
    thread.start()
    return thread


def track(event: str, **counts):
    try:
        if not enabled():
            return None
        return _start([payload(event, **counts)])
    except Exception:
        return None


def on_launch() -> None:
    try:
        # Flask's debug reloader runs the app twice; count only the serving copy.
        if os.environ.get("LAKER_DEBUG") == "1" and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
            return
        if not enabled():
            return
        _, is_new = install_id()
        events = (["install"] if is_new else []) + ["launch"]
        _start([payload(event) for event in events])  # in order, one thread
    except Exception:
        pass
