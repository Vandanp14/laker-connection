import json
import os

import pytest

import csv_import
import data

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = {
    "slot_duration_minutes": 15,
    "valid_locations": ["In- Residence Halls", "Out of Residence Halls"],
}
RESIDENTS = {"jdoe@oswego.edu": "Jane Doe", "jsmith@oswego.edu": "John Smith"}


def test_read_upload_handles_bom_crlf_and_header_casing():
    raw = b'\xef\xbb\xbfFull Name , Email_or_ID\r\nJane Doe,jdoe@oswego.edu\r\n\r\n'
    rows = csv_import.read_upload(raw, ["full_name", "email_or_id"])
    assert rows == [{"full_name": "Jane Doe", "email_or_id": "jdoe@oswego.edu", "_line": 2}]


def test_read_upload_ignores_extra_empty_columns():
    raw = b'full_name,email_or_id,,\nJane Doe,jdoe@oswego.edu,,\n,,,\n'
    rows = csv_import.read_upload(raw, ["full_name", "email_or_id"])
    assert rows == [{"full_name": "Jane Doe", "email_or_id": "jdoe@oswego.edu", "_line": 2}]


def test_read_upload_cp1252_fallback():
    raw = 'full_name,email_or_id\nJos\xe9 Ruiz,jruiz@oswego.edu\n'.encode('cp1252')
    rows = csv_import.read_upload(raw, ["full_name", "email_or_id"])
    assert rows[0]["full_name"] == "Jos\xe9 Ruiz"


def test_read_upload_missing_column_raises():
    with pytest.raises(csv_import.ImportError_) as exc:
        csv_import.read_upload(b'name,email\nJane,j@x\n', ["full_name", "email_or_id"])
    assert "Missing column(s)" in exc.value.messages[0]


def test_merge_residents_appends_new_and_skips_exact_duplicates():
    rows = [
        {"full_name": "Jane Doe", "email_or_id": "jdoe@oswego.edu", "_line": 2},
        {"full_name": "Alex Rivera", "email_or_id": "arivera@oswego.edu", "_line": 3},
    ]
    new, skipped = csv_import.merge_residents(RESIDENTS, rows)
    assert new == [("Alex Rivera", "arivera@oswego.edu")]
    assert skipped == 1


def test_merge_residents_conflict_rejects_whole_file():
    rows = [
        {"full_name": "Alex Rivera", "email_or_id": "arivera@oswego.edu", "_line": 2},
        {"full_name": "Someone Else", "email_or_id": "jdoe@oswego.edu", "_line": 3},
    ]
    with pytest.raises(csv_import.ImportError_) as exc:
        csv_import.merge_residents(RESIDENTS, rows)
    assert any(m.startswith("Line 3:") for m in exc.value.messages)


def test_merge_residents_same_name_different_id_rejected():
    rows = [{"full_name": "jane  doe", "email_or_id": "other@oswego.edu", "_line": 2}]
    with pytest.raises(csv_import.ImportError_):
        csv_import.merge_residents(RESIDENTS, rows)


def test_merge_residents_duplicate_within_file_rejected():
    rows = [
        {"full_name": "Alex Rivera", "email_or_id": "arivera@oswego.edu", "_line": 2},
        {"full_name": "Alex Rivera", "email_or_id": "arivera2@oswego.edu", "_line": 3},
    ]
    with pytest.raises(csv_import.ImportError_):
        csv_import.merge_residents(RESIDENTS, rows)


def _checkin(name, date, start, line, location="In- Residence Halls", notes="n"):
    return {"full_name": name, "date": date, "start_time": start, "location": location,
            "notes": notes, "_line": line}


def test_merge_checkins_skips_existing_and_validates():
    existing = [{"full_name": "Jane Doe", "date": "10-01-2026", "start_time": "14:00",
                 "location": "In- Residence Halls", "notes": "x", "_original_index": 0}]
    rows = [
        _checkin("jane doe", "10-01-2026", "14:00", 2),
        _checkin("John Smith", "10-02-2026", "15:00", 3),
    ]
    new, skipped = csv_import.merge_checkins(existing, rows, RESIDENTS, set(), CONFIG)
    assert skipped == 1
    assert [r["full_name"] for r in new] == ["John Smith"]


def test_merge_checkins_unknown_resident_rejects_with_line_number():
    rows = [_checkin("John Smith", "10-02-2026", "15:00", 2),
            _checkin("Nobody Here", "10-02-2026", "16:00", 3)]
    with pytest.raises(csv_import.ImportError_) as exc:
        csv_import.merge_checkins([], rows, RESIDENTS, set(), CONFIG)
    assert any("Line 3" in m and "Nobody Here" in m for m in exc.value.messages)


def test_merge_checkins_cadence_violation_rejected():
    rows = [_checkin("Jane Doe", "10-02-2026", "15:00", 2),
            _checkin("Jane Doe", "10-20-2026", "15:00", 3)]
    with pytest.raises(csv_import.ImportError_) as exc:
        csv_import.merge_checkins([], rows, RESIDENTS, set(), CONFIG)
    assert any("Line 2" in m and "Line 3" in m for m in exc.value.messages)


def test_templates_parse_cleanly():
    with open(os.path.join(ROOT, "templates", "csv", "residents_template.csv"), "rb") as f:
        res_rows = csv_import.read_upload(f.read(), ["full_name", "email_or_id"])
    new_res, _ = csv_import.merge_residents({}, res_rows)
    residents = {eid: name for name, eid in new_res}
    with open(os.path.join(ROOT, "templates", "csv", "checkins_template.csv"), "rb") as f:
        rows = csv_import.read_upload(f.read(), list(data.CHECKINS_FIELDNAMES))
    config = data.load_config(os.path.join(ROOT, "config.json"))
    new, skipped = csv_import.merge_checkins([], rows, residents, set(), config)
    assert len(new) == 3 and skipped == 0


@pytest.mark.parametrize("raw,expected", [
    ("10/1/2026", "10-01-2026"), ("10/1/26", "10-01-2026"), ("10-1-2026", "10-01-2026"),
    ("10-01-2026", "10-01-2026"), (" 1/5/2027 ", "01-05-2027"), ("2026-10-01", "10-01-2026"),
    ("not a date", "not a date"),
])
def test_normalize_date(raw, expected):
    assert csv_import.normalize_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("14:00", "14:00"), ("14:00:00", "14:00"), ("9:00", "09:00"), ("2:00 PM", "14:00"),
    ("12:15 am", "00:15"), ("12:30 PM", "12:30"), ("", ""), ("25:00", "25:00"),
])
def test_normalize_time(raw, expected):
    assert csv_import.normalize_time(raw) == expected


def test_merge_checkins_accepts_excel_style_dates_and_times():
    rows = [_checkin("Jane Doe", "10/1/2026", "14:00:00", 2),
            _checkin("John Smith", "10/1/26", "2:15 PM", 3)]
    new, _ = csv_import.merge_checkins([], rows, RESIDENTS, set(), CONFIG)
    assert [(r["date"], r["start_time"]) for r in new] == [("10-01-2026", "14:00"), ("10-01-2026", "14:15")]


def test_merge_checkins_dedupes_against_normalized_existing():
    existing = [{"full_name": "Jane Doe", "date": "10-01-2026", "start_time": "09:00",
                 "location": "In- Residence Halls", "notes": "x", "_original_index": 0}]
    new, skipped = csv_import.merge_checkins(existing, [_checkin("Jane Doe", "10/1/2026", "9:00", 2)],
                                             RESIDENTS, set(), CONFIG)
    assert new == [] and skipped == 1
