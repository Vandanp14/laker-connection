import csv
import json
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from data import load_residents, load_checkins, load_submission_log, load_config, validate, compute_candidates, compute_progress, parse_datetime, resolve_checkins


def test_load_residents(tmpdir):
    content = "full_name,email_or_id\nJohn Doe,john@example.com\nJane Smith,jane@example.com\n"
    p = tmpdir.join("residents.csv")
    p.write(content)
    residents = load_residents(str(p))
    assert residents == {"john@example.com": "John Doe", "jane@example.com": "Jane Smith"}


def test_load_checkins(tmpdir):
    content = ("full_name,date,start_time,location,notes\n"
               "John Doe,01-15-2026,10:00,Residence Hall,Met with John\n")
    p = tmpdir.join("checkins.csv")
    p.write(content)
    checkins = load_checkins(str(p))
    assert len(checkins) == 1
    row = checkins[0]
    assert row['full_name'] == 'John Doe'
    assert row['date'] == '01-15-2026'
    assert row['start_time'] == '10:00'
    assert 'email_or_id' not in row  # not resolved yet
    assert 'duration_minutes' not in row  # not resolved yet
    assert row['location'] == 'Residence Hall'
    assert row['notes'] == 'Met with John'
    assert '_original_index' in row


def test_load_checkins_multiple(tmpdir):
    content = ("full_name,date,start_time,location,notes\n"
               "John Doe,01-15-2026,10:00,Hall,Met John\n"
               "Jane Smith,01-15-2026,11:00,Lounge,Met Jane\n")
    p = tmpdir.join("checkins.csv")
    p.write(content)
    checkins = load_checkins(str(p))
    assert len(checkins) == 2
    assert checkins[0]['full_name'] == 'John Doe'
    assert checkins[1]['full_name'] == 'Jane Smith'


def test_load_submission_log(tmpdir):
    content = "full_name,email_or_id,date,start_time,submitted_at\nJohn Doe,john@example.com,01-15-2026,10:00,2026-01-15 10:30:00\n"
    p = tmpdir.join("submission_log.csv")
    p.write(content)
    logged = load_submission_log(str(p))
    assert logged == {('john@example.com', '01-15-2026', '10:00')}

    # Missing file returns empty set
    missing = tmpdir.join("missing.csv")
    assert load_submission_log(str(missing)) == set()


def test_load_config(tmpdir):
    config_dict = {"slot_duration_minutes": 15, "appointments_url": "url"}
    p = tmpdir.join("config.json")
    p.write(json.dumps(config_dict))
    config = load_config(str(p))
    assert config == config_dict


def test_load_residents_with_bom(tmpdir):
    content = "\ufefffull_name,email_or_id\nJohn Doe,john@example.com\n"
    p = tmpdir.join("residents.csv")
    p.write(content)
    residents = load_residents(str(p))
    assert "john@example.com" in residents
    assert residents["john@example.com"] == "John Doe"


def test_parse_datetime():
    dt = parse_datetime("01-15-2026", "10:30")
    assert dt == datetime(2026, 1, 15, 10, 30)


def test_validate_missing_resident_by_name(tmpdir):
    residents = {"john@example.com": "John Doe"}
    checkins = [
        {"full_name": "John Doe", "date": "01-15-2026", "start_time": "10:00",
         "location": "Residence Hall", "notes": "test"},
        {"full_name": "Unknown Person", "date": "01-15-2026", "start_time": "11:00",
         "location": "Residence Hall", "notes": "test"}
    ]
    config = {"slot_duration_minutes": 15}
    errors = validate(checkins, residents, config)
    assert any("not found in residents.csv" in e for e in errors)


def test_validate_no_overlap_adjacent(tmpdir):
    residents = {"john@example.com": "John Doe"}
    checkins = [
        {"full_name": "John Doe", "email_or_id": "john@example.com", "date": "01-15-2026", "start_time": "10:00",
         "duration_minutes": 15, "location": "Residence Hall", "notes": "test"},
        {"full_name": "John Doe", "email_or_id": "john@example.com", "date": "01-15-2026", "start_time": "10:15",
         "duration_minutes": 15, "location": "Residence Hall", "notes": "test"}
    ]
    config = {"slot_duration_minutes": 15}
    errors = validate(checkins, residents, config)
    # Adjacent (end == start) should be OK
    assert not any("Overlap detected" in e for e in errors)


def test_validate_overlap_nested(tmpdir):
    residents = {"john@example.com": "John Doe"}
    checkins = [
        {"full_name": "John Doe", "email_or_id": "john@example.com", "date": "01-15-2026", "start_time": "10:00",
         "duration_minutes": 60, "location": "Residence Hall", "notes": "test"},
        {"full_name": "John Doe", "email_or_id": "john@example.com", "date": "01-15-2026", "start_time": "10:30",
         "duration_minutes": 30, "location": "Residence Hall", "notes": "test"}
    ]
    config = {"slot_duration_minutes": 15}
    errors = validate(checkins, residents, config)
    assert any("Overlap detected" in e for e in errors)


def test_validate_overlap_same_start(tmpdir):
    residents = {"john@example.com": "John Doe"}
    checkins = [
        {"full_name": "John Doe", "email_or_id": "john@example.com", "date": "01-15-2026", "start_time": "10:00",
         "duration_minutes": 30, "location": "Residence Hall", "notes": "test"},
        {"full_name": "John Doe", "email_or_id": "john@example.com", "date": "01-15-2026", "start_time": "10:00",
         "duration_minutes": 30, "location": "Residence Hall", "notes": "test"}
    ]
    config = {"slot_duration_minutes": 15}
    errors = validate(checkins, residents, config)
    assert any("Overlap detected" in e for e in errors)


def test_validate_duplicate_name_in_residents(tmpdir):
    residents = {
        "john@example.com": "John Doe",
        "jane@example.com": "John Doe"  # Duplicate name!
    }
    checkins = [
        {"full_name": "John Doe", "date": "01-15-2026", "start_time": "10:00",
         "location": "Residence Hall", "notes": "test"}
    ]
    config = {"slot_duration_minutes": 15}
    errors = validate(checkins, residents, config)
    assert any("duplicate" in e.lower() for e in errors)


def test_compute_candidates(tmpdir):
    residents_content = "full_name,email_or_id\nJohn Doe,john@example.com\nJane Smith,jane@example.com\n"
    residents_p = tmpdir.join("residents.csv")
    residents_p.write(residents_content)

    checkins_content = ("full_name,date,start_time,location,notes\n"
                        "John Doe,01-15-2026,10:00,Residence Hall,Met John\n"
                        "Jane Smith,01-15-2026,11:00,Lounge,Met Jane\n")
    checkins_p = tmpdir.join("checkins.csv")
    checkins_p.write(checkins_content)

    log_content = "full_name,email_or_id,date,start_time,submitted_at\nJohn Doe,john@example.com,01-15-2026,10:00,2026-01-15 10:30:00\n"
    log_p = tmpdir.join("submission_log.csv")
    log_p.write(log_content)

    checkins = load_checkins(str(checkins_p))
    residents = load_residents(str(residents_p))
    logged = load_submission_log(str(log_p))
    config = {"slot_duration_minutes": 15}

    resolved, _ = resolve_checkins(checkins, residents, logged, config)
    candidates = compute_candidates(resolved, logged)
    assert len(candidates) == 1
    assert candidates[0]['full_name'] == 'Jane Smith'


def test_resolve_checkins_with_chaining(tmpdir):
    residents_content = "full_name,email_or_id\nJohn Doe,john@example.com\nJane Smith,jane@example.com\n"
    residents_p = tmpdir.join("residents.csv")
    residents_p.write(residents_content)

    # First row has explicit start_time (the anchor), subsequent rows are blank (chained)
    checkins_content = ("full_name,date,start_time,location,notes\n"
                        "John Doe,01-15-2026,10:00,Residence Hall,Met John\n"
                        "John Doe,01-15-2026,,Lounge,Met John continued\n"
                        "Jane Smith,01-15-2026,,Hall,Met Jane\n")
    checkins_p = tmpdir.join("checkins.csv")
    checkins_p.write(checkins_content)

    submission_log_content = "full_name,email_or_id,date,start_time,submitted_at\n"
    log_p = tmpdir.join("submission_log.csv")
    log_p.write(submission_log_content)

    config = {"slot_duration_minutes": 15}

    checkins = load_checkins(str(checkins_p))
    residents = load_residents(str(residents_p))
    logged = load_submission_log(str(log_p))

    resolved, errors = resolve_checkins(checkins, residents, logged, config)
    assert len(errors) == 0
    assert len(resolved) == 3

    john_rows = [r for r in resolved if r['full_name'] == 'John Doe']
    assert len(john_rows) == 2
    assert john_rows[0]['email_or_id'] == 'john@example.com'
    assert john_rows[0]['start_time'] == '10:00'  # Explicit anchor
    assert john_rows[0]['duration_minutes'] == 15
    assert john_rows[0]['location'] == 'Residence Hall'
    assert john_rows[1]['start_time'] == '10:15'  # Chained from 10:00 + 15

    jane_row = next((r for r in resolved if r['full_name'] == 'Jane Smith'), None)
    assert jane_row is not None
    assert jane_row['email_or_id'] == 'jane@example.com'
    assert jane_row['start_time'] == '10:30'  # Chained from John's last (10:15) + 15


def test_resolve_checkins_multiple_anchors_same_date(tmpdir):
    residents_content = "full_name,email_or_id\nJohn Doe,john@example.com\nJane Smith,jane@example.com\nBob Smith,bob@example.com\n"
    residents_p = tmpdir.join("residents.csv")
    residents_p.write(residents_content)

    checkins_content = ("full_name,date,start_time,location,notes\n"
                        "John Doe,01-15-2026,10:00,Hall,Morning block\n"
                        "Jane Smith,01-15-2026,12:00,Lounge,Afternoon block\n"
                        "Bob Smith,01-15-2026,14:00,Hall,Evening block\n")
    checkins_p = tmpdir.join("checkins.csv")
    checkins_p.write(checkins_content)

    submission_log_content = "full_name,email_or_id,date,start_time,submitted_at\n"
    log_p = tmpdir.join("submission_log.csv")
    log_p.write(submission_log_content)

    config = {"slot_duration_minutes": 15}

    checkins = load_checkins(str(checkins_p))
    residents = load_residents(str(residents_p))
    logged = load_submission_log(str(log_p))

    resolved, errors = resolve_checkins(checkins, residents, logged, config)
    assert len(errors) == 0
    assert len(resolved) == 3

    john_row = next((r for r in resolved if r['full_name'] == 'John Doe'), None)
    assert john_row['start_time'] == '10:00'

    jane_row = next((r for r in resolved if r['full_name'] == 'Jane Smith'), None)
    assert jane_row['start_time'] == '12:00'  # Independent anchor at noon

    bob_row = next((r for r in resolved if r['full_name'] == 'Bob Smith'), None)
    assert bob_row['start_time'] == '14:00'  # Independent anchor at 2pm


def test_resolve_checkins_insertion_safety_pin(tmpdir):
    residents_content = "full_name,email_or_id\nJohn Doe,john@example.com\nJane Smith,jane@example.com\n"
    residents_p = tmpdir.join("residents.csv")
    residents_p.write(residents_content)

    # First, create a row and submit it
    checkins_content_first = ("full_name,date,start_time,location,notes\n"
                              "John Doe,01-15-2026,10:00,Residence Hall,Met John\n")
    checkins_p = tmpdir.join("checkins.csv")
    checkins_p.write(checkins_content_first)

    submission_log_first = "full_name,email_or_id,date,start_time,submitted_at\nJohn Doe,john@example.com,01-15-2026,10:00,2026-01-15 10:30:00\n"
    log_p = tmpdir.join("submission_log.csv")
    log_p.write(submission_log_first)

    # Now add a NEW row for a different person BEFORE John's row on the same date
    # This simulates someone adding a check-in that should NOT shift John's time
    checkins_content_new = ("full_name,date,start_time,location,notes\n"
                            "Jane Smith,01-15-2026,09:45,Hall,Inserted before John\n"
                            "John Doe,01-15-2026,10:00,Residence Hall,Met John\n")
    checkins_p.write(checkins_content_new)

    checkins = load_checkins(str(checkins_p))
    residents = load_residents(str(residents_p))
    logged = load_submission_log(str(log_p))
    config = {"slot_duration_minutes": 15}

    resolved, errors = resolve_checkins(checkins, residents, logged, config)
    assert len(errors) == 0
    assert len(resolved) == 2

    john_row = next((r for r in resolved if r['full_name'] == 'John Doe'), None)
    assert john_row['start_time'] == '10:00'  # MUST stay at 10:00, not shift because we inserted in front

    jane_row = next((r for r in resolved if r['full_name'] == 'Jane Smith'), None)
    assert jane_row['start_time'] == '09:45'  # Her explicit time


def test_resolve_checkins_full_name_not_in_residents(tmpdir):
    residents_content = "full_name,email_or_id\nJohn Doe,john@example.com\n"
    residents_p = tmpdir.join("residents.csv")
    residents_p.write(residents_content)

    checkins_content = ("full_name,date,start_time,location,notes\n"
                        "John Doe,01-15-2026,10:00,Residence Hall,Met John\n"
                        "Unknown Person,01-15-2026,11:00,Hall,Met Unknown\n")
    checkins_p = tmpdir.join("checkins.csv")
    checkins_p.write(checkins_content)

    submission_log_content = "full_name,email_or_id,date,start_time,submitted_at\n"
    log_p = tmpdir.join("submission_log.csv")
    log_p.write(submission_log_content)

    config = {"slot_duration_minutes": 15}

    checkins = load_checkins(str(checkins_p))
    residents = load_residents(str(residents_p))
    logged = load_submission_log(str(log_p))

    resolved, errors = resolve_checkins(checkins, residents, logged, config)
    assert len(resolved) == 1  # Only John rows through, Unknown Person skipped
    assert any("not found in residents.csv" in e for e in errors)


def test_resolve_checkins_resident_with_multiple_checkins_same_month(tmpdir):
    residents_content = "full_name,email_or_id\nJohn Doe,john@example.com\n"
    residents_p = tmpdir.join("residents.csv")
    residents_p.write(residents_content)

    checkins_content = ("full_name,date,start_time,location,notes\n"
                        "John Doe,01-15-2026,10:00,Hall,First check-in\n"
                        "John Doe,01-15-2026,10:15,Lounge,Second check-in same day\n")
    checkins_p = tmpdir.join("checkins.csv")
    checkins_p.write(checkins_content)

    submission_log_content = "full_name,email_or_id,date,start_time,submitted_at\n"
    log_p = tmpdir.join("submission_log.csv")
    log_p.write(submission_log_content)

    config = {"slot_duration_minutes": 15}

    checkins = load_checkins(str(checkins_p))
    residents = load_residents(str(residents_p))
    logged = load_submission_log(str(log_p))

    resolved, errors = resolve_checkins(checkins, residents, logged, config)
    assert len(errors) == 0
    assert len(resolved) == 2


def test_load_checkins_with_blank_rows(tmpdir):
    content = ("full_name,date,start_time,location,notes\n"
               "John Doe,01-15-2026,10:00,Hall,Met John\n\n\n"
               "Jane Smith,01-15-2026,11:00,Lounge,Met Jane\n"
               "Bob Doe,01-15-2026,,Hall,Met Bob chained\n"
               "\n\n")
    p = tmpdir.join("checkins.csv")
    p.write(content)
    checkins = load_checkins(str(p))
    assert len(checkins) == 3  # Only 3 non-blank rows
    assert checkins[0]['full_name'] == 'John Doe'
    assert checkins[2]['full_name'] == 'Bob Doe'


def test_compute_progress_surfaces_legacy_submission_without_reason():
    residents = {"john@example.com": "John Doe"}
    log_details = [{
        "email_or_id": "john@example.com",
        "date": "01-15-2026",
        "start_time": "10:00",
        "reason": "",
        "submitted_at": "2026-01-15T10:30:00",
    }]
    config = {"valid_reasons": ["Connection #1", "Connection #2", "Connection #3"]}

    progress = compute_progress(residents, log_details, config)

    assert progress[0]["submitted"] == 1
    assert progress[0]["reasons_done"] == []
    assert progress[0]["unassigned_submissions"] == 1


# ---------- SpeedNotes ----------

import data as _data

SN_CONFIG = {
    "speednote_groups": [
        {"name": "Academics", "topics": ["Academics - Concern/Support Needed", "Academics - Kudos/Check-In"]},
        {"name": "Wellbeing", "topics": ["Wellbeing - Kudos/Check-in", "Transition/Adjustment"]},
    ]
}


def test_speednote_topics_flattens_in_order():
    assert _data.speednote_topics(SN_CONFIG) == [
        "Academics - Concern/Support Needed",
        "Academics - Kudos/Check-In",
        "Wellbeing - Kudos/Check-in",
        "Transition/Adjustment",
    ]


def test_speednote_topics_absent_is_empty():
    assert _data.speednote_topics({}) == []


def test_speednote_topics_duplicate_raises():
    config = {"speednote_groups": [
        {"name": "A", "topics": ["Break Plans"]},
        {"name": "B", "topics": ["Break Plans"]},
    ]}
    with pytest.raises(ValueError, match="Break Plans"):
        _data.speednote_topics(config)


def test_normalize_speednotes_dedupes_and_strips():
    topics, errors = _data.normalize_speednotes(
        [" Transition/Adjustment ", "", "Transition/Adjustment", "Academics - Kudos/Check-In"], SN_CONFIG)
    assert errors == []
    assert topics == ["Transition/Adjustment", "Academics - Kudos/Check-In"]


def test_normalize_speednotes_rejects_wrong_case():
    topics, errors = _data.normalize_speednotes(["Wellbeing - Kudos/Check-In"], SN_CONFIG)
    assert topics == []
    assert errors == ["SpeedNote 'Wellbeing - Kudos/Check-In' is not one of the configured topics"]


def test_normalize_speednotes_unknown():
    topics, errors = _data.normalize_speednotes(["Transition/Adjustment", "Made Up"], SN_CONFIG)
    assert topics == ["Transition/Adjustment"]
    assert len(errors) == 1 and "Made Up" in errors[0]


# --- per-user location settings (settings.json overlay) ---

from data import load_effective_config, save_location_settings


def _write_shipped_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "valid_locations": ["In- Residence Halls", "Out of Residence Halls"],
        "default_location": "In- Residence Halls",
        "valid_reasons": ["Laker Connection #1 > FALL"],
    }))
    return str(path)


def test_effective_config_overrides_locations(tmp_path):
    config_path = _write_shipped_config(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"valid_locations": ["Lobby"], "default_location": "Lobby"}))
    config = load_effective_config(config_path, str(settings))
    assert config["valid_locations"] == ["Lobby"]
    assert config["default_location"] == "Lobby"
    assert config["valid_reasons"] == ["Laker Connection #1 > FALL"]


def test_effective_config_without_settings_uses_shipped(tmp_path):
    config_path = _write_shipped_config(tmp_path)
    config = load_effective_config(config_path, str(tmp_path / "missing.json"))
    assert config["valid_locations"] == ["In- Residence Halls", "Out of Residence Halls"]


def test_effective_config_ignores_corrupt_settings(tmp_path):
    config_path = _write_shipped_config(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text("{not json")
    config = load_effective_config(config_path, str(settings))
    assert config["default_location"] == "In- Residence Halls"


def test_save_location_settings_rejects_empty(tmp_path):
    with pytest.raises(ValueError):
        save_location_settings(str(tmp_path / "settings.json"), ["  ", ""], "")
    assert not (tmp_path / "settings.json").exists()


def test_save_location_settings_rejects_default_not_in_list(tmp_path):
    with pytest.raises(ValueError):
        save_location_settings(str(tmp_path / "settings.json"), ["Lobby"], "Gym")


def test_save_location_settings_dedupes(tmp_path):
    path = tmp_path / "settings.json"
    save_location_settings(str(path), [" Lobby ", "Gym", "Lobby"], "Gym")
    assert json.loads(path.read_text()) == {"valid_locations": ["Lobby", "Gym"], "default_location": "Gym"}


def test_validate_skips_location_check_for_submitted_rows():
    residents = {"jdoe@oswego.edu": "Jane Doe"}
    row = {"full_name": "Jane Doe", "email_or_id": "jdoe@oswego.edu", "date": "10-01-2026",
           "start_time": "14:00", "location": "Old Place", "notes": "", "_original_index": 0}
    config = {"valid_locations": ["In- Residence Halls"]}
    submitted = {("jdoe@oswego.edu", "10-01-2026", "14:00")}
    assert not any("Old Place" in e for e in validate([row], residents, config, submitted))
    assert any("Old Place" in e for e in validate([row], residents, config))
