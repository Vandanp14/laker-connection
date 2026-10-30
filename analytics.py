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
import re
import ssl
import threading
import urllib.request
import uuid

import paths

ENDPOINT = "https://script.google.com/macros/s/AKfycbwWZG4R7T4-6xPvYKZ1qpsZBCEEQ83Y_tfLC2vfopEfrGOxTI6bsK7RCjxTmUHeeNJP/exec"
TOKEN = "lc1"
TIMEOUT_SECONDS = 3
SUBMIT_FIELDS = ("submitted", "failed", "uncertain", "skipped", "crashed", "seconds")
ERROR_STAGES = ("dialog", "student", "when", "start_time", "end_time", "where", "reason",
                "sharing", "notes", "outcomes", "speednotes", "close_dry_run", "submit", "other")
ERROR_KINDS = ("timeout", "not_found", "ambiguous", "mismatch", "guard", "uncertain",
               "not_attempted", "other")
ERROR_FIELDS = ("stage", "kind", "student", "when", "start", "end", "where", "reason_locked",
                "reason_options", "waited_ms", "elapsed_ms")
# Exception class names are allowed as a kind (e.g. TimeoutError), but only
# ones shaped like an error class — never anything that could be a name.
_ERROR_CLASS = re.compile(r"^[A-Za-z_]{0,30}(Error|Exception)$")

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


def _clamp(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def error_fields(diag: dict) -> dict:
    """The only things a failed row reports: fixed labels, 0/1 'field had a
    value' flags and clamped numbers. Field values themselves never leave."""
    stage = diag.get("stage")
    kind = diag.get("kind")
    disabled = diag.get("reason_disabled")
    return {
        "stage": stage if stage in ERROR_STAGES else "other",
        "kind": kind if kind in ERROR_KINDS or (isinstance(kind, str) and _ERROR_CLASS.match(kind))
                else "other",
        "student": int(bool(diag.get("student"))),
        "when": int(bool(diag.get("when"))),
        "start": int(bool(diag.get("start_time"))),
        "end": int(bool(diag.get("end_time"))),
        "where": int(bool(diag.get("where"))),
        "reason_locked": 2 if disabled is None else int(bool(disabled)),
        "reason_options": _clamp(diag.get("reason_options"), -1, 200, -1),
        "waited_ms": _clamp(diag.get("waited_ms"), 0, 600000, 0),
        "elapsed_ms": _clamp(diag.get("elapsed_ms"), 0, 600000, 0),
    }


def payload(event: str, **counts) -> dict:
    body = {"token": TOKEN, "event": event, "install_id": install_id()[0], "version": _version()}
    if event == "submit":
        body.update({key: int(counts.get(key, 0)) for key in SUBMIT_FIELDS})
    elif event == "error":
        body.update(error_fields_passthrough(counts))
    return body


# (low, high, default) for each numeric error field.
_ERROR_BOUNDS = {"student": (0, 1, 0), "when": (0, 1, 0), "start": (0, 1, 0), "end": (0, 1, 0),
                 "where": (0, 1, 0), "reason_locked": (0, 2, 2), "reason_options": (-1, 200, -1),
                 "waited_ms": (0, 600000, 0), "elapsed_ms": (0, 600000, 0)}


def error_fields_passthrough(fields: dict) -> dict:
    """Re-check already-built error fields so payload() alone can't send text."""
    stage, kind = fields.get("stage"), fields.get("kind")
    checked = {
        "stage": stage if stage in ERROR_STAGES else "other",
        "kind": kind if kind in ERROR_KINDS or (isinstance(kind, str) and _ERROR_CLASS.match(kind))
                else "other",
    }
    for key, (low, high, default) in _ERROR_BOUNDS.items():
        checked[key] = _clamp(fields.get(key), low, high, default)
    return checked


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


def track_batch(events):
    """Send several events in order on one thread: [(event, fields), ...].
    One request at a time keeps the endpoint's lock from timing some out."""
    try:
        if not enabled():
            return None
        return _start([payload(event, **fields) for event, fields in events])
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
