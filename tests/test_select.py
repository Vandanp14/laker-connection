import io
import sys

import pytest

import selection


CONFIG = {
    "slot_duration_minutes": 15,
    "valid_reasons": ["Test Reason"],
    "valid_locations": ["Hall", "Lounge"],
    "default_location": "Hall",
}


def _run(residents_p, checkins_p, log_p, config, inputs, monkeypatch):
    it = iter(inputs)
    monkeypatch.setattr('builtins.input', lambda _: next(it))
    saved_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        return selection.prompt_for_selection(str(residents_p), str(checkins_p), str(log_p), config)
    finally:
        sys.stdout = saved_stdout


def test_prompt_for_selection_basic(tmpdir, monkeypatch):
    residents_p = tmpdir.join("residents.csv")
    residents_p.write(
        "full_name,email_or_id\nJohn Doe,john@example.com\nJane Smith,jane@example.com\nBob Jones,bob@example.com\n"
    )

    # Different months so the monthly-cadence check doesn't reject any of these.
    checkins_p = tmpdir.join("checkins.csv")
    checkins_p.write(
        "full_name,date,start_time,location,notes\n"
        "John Doe,01-15-2026,10:00,Hall,Met John\n"
        "Jane Smith,02-15-2026,11:00,Hall,Met Jane\n"
        "Bob Jones,03-15-2026,12:00,Hall,Met Bob\n"
    )

    # john already logged
    log_p = tmpdir.join("submission_log.csv")
    log_p.write("email_or_id,date,start_time,submitted_at\njohn@example.com,01-15-2026,10:00,2026-01-15 10:30:00\n")

    inputs = [
        "Test Reason",   # reason
        "Hall",          # default location
        "Y",             # select jane (first candidate)
        "",              # keep default location
        "n",             # skip bob
        "yes",           # final confirmation
    ]
    selected, reason, default_loc = _run(residents_p, checkins_p, log_p, CONFIG, inputs, monkeypatch)

    assert reason == "Test Reason"
    assert default_loc == "Hall"
    assert len(selected) == 1
    row = selected[0]
    assert row['full_name'] == 'Jane Smith'
    assert row['email_or_id'] == 'jane@example.com'
    assert row['effective_location'] == 'Hall'


def test_prompt_for_selection_override_location(tmpdir, monkeypatch):
    residents_p = tmpdir.join("residents.csv")
    residents_p.write("full_name,email_or_id\nJohn Doe,john@example.com\n")

    checkins_p = tmpdir.join("checkins.csv")
    checkins_p.write("full_name,date,start_time,location,notes\nJohn Doe,01-15-2026,10:00,Hall,Met John\n")

    log_p = tmpdir.join("submission_log.csv")
    log_p.write("")

    inputs = [
        "Test Reason",
        "Hall",     # default location
        "Y",        # select the row
        "Lounge",   # override location
        "yes",
    ]
    selected, reason, default_loc = _run(residents_p, checkins_p, log_p, CONFIG, inputs, monkeypatch)

    assert len(selected) == 1
    row = selected[0]
    assert row['full_name'] == 'John Doe'
    assert row['effective_location'] == 'Lounge'


def test_prompt_for_selection_no_candidates(tmpdir, monkeypatch):
    residents_p = tmpdir.join("residents.csv")
    residents_p.write("full_name,email_or_id\nJohn Doe,john@example.com\n")

    checkins_p = tmpdir.join("checkins.csv")
    checkins_p.write("full_name,date,start_time,location,notes\nJohn Doe,01-15-2026,10:00,Hall,Met John\n")

    # already submitted -> zero candidates
    log_p = tmpdir.join("submission_log.csv")
    log_p.write("email_or_id,date,start_time,submitted_at\njohn@example.com,01-15-2026,10:00,2026-01-15 10:30:00\n")

    monkeypatch.setattr('builtins.input', lambda _: "")  # unused, early exit before any prompt
    saved_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        with pytest.raises(SystemExit) as exc_info:
            selection.prompt_for_selection(str(residents_p), str(checkins_p), str(log_p), CONFIG)
    finally:
        sys.stdout = saved_stdout
    assert exc_info.value.code == 0


def test_prompt_for_selection_reject_all(tmpdir, monkeypatch):
    residents_p = tmpdir.join("residents.csv")
    residents_p.write("full_name,email_or_id\nJohn Doe,john@example.com\n")

    checkins_p = tmpdir.join("checkins.csv")
    checkins_p.write("full_name,date,start_time,location,notes\nJohn Doe,01-15-2026,10:00,Hall,Met John\n")

    log_p = tmpdir.join("submission_log.csv")
    log_p.write("")

    inputs = ["Test Reason", "Hall", "n"]  # reject the only candidate -> exits before final confirm
    it = iter(inputs)
    monkeypatch.setattr('builtins.input', lambda _: next(it))
    saved_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        with pytest.raises(SystemExit) as exc_info:
            selection.prompt_for_selection(str(residents_p), str(checkins_p), str(log_p), CONFIG)
    finally:
        sys.stdout = saved_stdout
    assert exc_info.value.code == 0


def test_prompt_for_selection_resolution_error_exits(tmpdir, monkeypatch):
    """A full_name in checkins.csv that isn't in residents.csv must abort cleanly,
    not crash — this is the exact bug that a prior version of selection.py had."""
    residents_p = tmpdir.join("residents.csv")
    residents_p.write("full_name,email_or_id\nJohn Doe,john@example.com\n")

    checkins_p = tmpdir.join("checkins.csv")
    checkins_p.write("full_name,date,start_time,location,notes\nGhost Person,01-15-2026,10:00,Hall,unknown\n")

    log_p = tmpdir.join("submission_log.csv")
    log_p.write("")

    monkeypatch.setattr('builtins.input', lambda _: "")  # unused, exits before any prompt
    saved_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        with pytest.raises(SystemExit) as exc_info:
            selection.prompt_for_selection(str(residents_p), str(checkins_p), str(log_p), CONFIG)
    finally:
        sys.stdout = saved_stdout
    assert exc_info.value.code == 1
