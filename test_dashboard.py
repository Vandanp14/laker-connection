"""
Tests for the Laker Connection Dashboard.

Every test runs against an isolated tmp_path fixture set (via monkeypatching
dashboard.DASH_ROOT) — never against the real residents.csv/checkins.csv/
submission_log.csv in the repo. A prior version of this file wrote directly
to the real data files and left fabricated duplicate rows in production
checkins.csv; do not reintroduce that.
"""
import os
import pytest

import dashboard


@pytest.fixture
def isolated_dashboard(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        '{"slot_duration_minutes": 15, "appointments_url": "http://example.test", '
        '"logged_in_indicator": ".x", '
        '"valid_locations": ["In- Residence Halls", "Out of Residence Halls"], '
        '"default_location": "In- Residence Halls", '
        '"valid_reasons": ["Laker Connection #1 > FALL", "Laker Connection #2 > FALL"]}'
    )
    (tmp_path / "residents.csv").write_text(
        "full_name,email_or_id\nJane Doe,jdoe@oswego.edu\n"
    )
    (tmp_path / "checkins.csv").write_text(
        "full_name,date,start_time,location,notes\n"
        "Jane Doe,08-16-2026,13:00,In- Residence Halls,talked with jane about spring break\n"
    )
    (tmp_path / "submission_log.csv").write_text(
        "email_or_id,date,start_time,reason,submitted_at\n"
    )
    monkeypatch.setattr(dashboard, "DASH_ROOT", str(tmp_path))
    monkeypatch.setattr(dashboard, "APP_ROOT", str(tmp_path))
    return tmp_path


def test_index_route(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.get('/')
        assert response.status_code == 200
        assert b'Laker Connection Dashboard' in response.data
        assert b'data-status="recorded"' in response.data
        assert b'data-status="pending"' in response.data
        assert b'data-status="submitted"' in response.data


def test_add_checkin_get_rejected(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.get('/add-checkin')
        assert response.status_code != 200


def test_add_checkin_post_valid(isolated_dashboard):
    with dashboard.app.test_client() as client:
        # Different month than the existing 08-16-2026 row — same resident can only
        # have one check-in per calendar month (cadence rule enforced by validate()).
        response = client.post('/add-checkin', data={
            'full_name': 'Jane Doe',
            'date': '2026-09-17',  # <input type="date"> posts ISO format
            'start_time': '14:00',
            'location': 'In- Residence Halls',
            'notes': 'Test check-in',
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b'Laker Connection Dashboard' in response.data

        lines = (isolated_dashboard / "checkins.csv").read_text().splitlines()
        assert len(lines) == 3  # header + original + new
        # date must be converted from ISO (2026-09-17) to MM-DD-YYYY (09-17-2026)
        assert lines[-1] == "Jane Doe,09-17-2026,14:00,In- Residence Halls,Test check-in"


def test_add_checkin_post_invalid_resident(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/add-checkin', data={
            'full_name': 'Nonexistent Person',
            'date': '2026-08-17',
            'start_time': '14:00',
            'location': 'In- Residence Halls',
            'notes': 'Test',
        })
        assert response.status_code == 400
        assert b'not found' in response.data.lower()
        # must NOT have been written
        lines = (isolated_dashboard / "checkins.csv").read_text().splitlines()
        assert len(lines) == 2  # header + original only


def test_add_checkin_post_overlap_rejected(isolated_dashboard):
    """A row that would overlap an existing one is rejected before being written."""
    with dashboard.app.test_client() as client:
        response = client.post('/add-checkin', data={
            'full_name': 'Jane Doe',
            'date': '2026-08-16',
            'start_time': '13:00',  # exact same slot as the existing row
            'location': 'In- Residence Halls',
            'notes': 'Duplicate slot',
        })
        assert response.status_code == 400
        lines = (isolated_dashboard / "checkins.csv").read_text().splitlines()
        assert len(lines) == 2  # header + original only, bad row never written


def test_submit_batch_no_selection(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/submit-batch', data={
            'reason': 'Laker Connection #2 > FALL',
            'default_location': 'In- Residence Halls',
        })
        assert response.status_code == 400
        assert b'No rows selected' in response.data


def test_submit_batch_missing_reason(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/submit-batch', data={
            'selected_keys': ['jdoe@oswego.edu|08-16-2026|13:00'],
            'default_location': 'In- Residence Halls',
        })
        assert response.status_code == 400
        assert b'Batch reason is required' in response.data


def test_submit_batch_stale_key_rejected(isolated_dashboard):
    """A selected_keys value that no longer matches any candidate must be rejected,
    not silently ignored or matched to the wrong row."""
    with dashboard.app.test_client() as client:
        response = client.post('/submit-batch', data={
            'selected_keys': ['nonexistent@oswego.edu|01-01-2026|09:00'],
            'reason': 'Laker Connection #2 > FALL',
            'default_location': 'In- Residence Halls',
        })
        assert response.status_code == 400
        assert b'no longer matches' in response.data


def test_add_resident_valid(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/add-resident', data={
            'full_name': 'Bob Jones',
            'email_or_id': 'bjones@oswego.edu',
        }, follow_redirects=True)
        assert response.status_code == 200
        lines = (isolated_dashboard / "residents.csv").read_text().splitlines()
        assert lines[-1] == "Bob Jones,bjones@oswego.edu"


def test_add_resident_duplicate_email_rejected(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/add-resident', data={
            'full_name': 'Jane Duplicate',
            'email_or_id': 'jdoe@oswego.edu',  # already on the roster
        })
        assert response.status_code == 400
        assert b'already on the roster' in response.data
        lines = (isolated_dashboard / "residents.csv").read_text().splitlines()
        assert len(lines) == 2  # header + original only


def test_add_resident_duplicate_name_rejected(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/add-resident', data={
            'full_name': 'jane   doe',  # collides with existing name, case/whitespace-insensitive
            'email_or_id': 'different_email@oswego.edu',
        })
        assert response.status_code == 400
        lines = (isolated_dashboard / "residents.csv").read_text().splitlines()
        assert len(lines) == 2


def test_add_checkins_batch_multiple_residents_chain(isolated_dashboard):
    with dashboard.app.test_client() as client:
        # Add a second resident so the batch has 2 people to check off.
        client.post('/add-resident', data={'full_name': 'Bob Jones', 'email_or_id': 'bjones@oswego.edu'})

        response = client.post('/add-checkins-batch', data={
            'date': '2026-09-20',  # different month than the existing 08-16-2026 row (cadence rule)
            'first_start_time': '09:00',
            'location': 'In- Residence Halls',
            'checked_jdoe@oswego.edu': 'on',
            'notes_jdoe@oswego.edu': 'Talked about classes',
            'checked_bjones@oswego.edu': 'on',
            'notes_bjones@oswego.edu': 'Talked about roommates',
        }, follow_redirects=True)
        assert response.status_code == 200

        lines = (isolated_dashboard / "checkins.csv").read_text().splitlines()
        assert len(lines) == 4  # header + original + 2 new
        # Bob Jones sorts before Jane Doe alphabetically -> he's the anchor at 09:00,
        # written explicitly; Jane's row is written with a BLANK start_time on disk (same as
        # hand-editing) and only resolves to 09:15 when loaded — verify that resolution here.
        assert "Bob Jones,09-20-2026,09:00,In- Residence Halls,Talked about roommates" in lines
        assert "Jane Doe,09-20-2026,,In- Residence Halls,Talked about classes" in lines

        import data as data_module
        config = data_module.load_config(str(isolated_dashboard / "config.json"))
        residents = data_module.load_residents(str(isolated_dashboard / "residents.csv"))
        checkins = data_module.load_checkins(str(isolated_dashboard / "checkins.csv"))
        submission_log = data_module.load_submission_log(str(isolated_dashboard / "submission_log.csv"))
        resolved, errors = data_module.resolve_checkins(checkins, residents, submission_log, config)
        assert errors == []
        jane_row = next(r for r in resolved if r['full_name'] == 'Jane Doe' and r['date'] == '09-20-2026')
        assert jane_row['start_time'] == '09:15'


def test_add_checkins_batch_no_selection_rejected(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/add-checkins-batch', data={
            'date': '2026-09-20',
            'first_start_time': '09:00',
            'location': 'In- Residence Halls',
        })
        assert response.status_code == 400
        assert b'No residents selected' in response.data
        lines = (isolated_dashboard / "checkins.csv").read_text().splitlines()
        assert len(lines) == 2  # unchanged


def test_history_status_filters_share_dashboard_counts(isolated_dashboard):
    log_path = isolated_dashboard / "submission_log.csv"
    log_path.write_text(
        "email_or_id,date,start_time,reason,submitted_at\n"
        "jdoe@oswego.edu,08-16-2026,13:00,Laker Connection #1 > FALL,2026-09-01T12:00:00\n"
    )

    with dashboard.app.test_client() as client:
        dashboard_response = client.get('/')
        assert dashboard_response.status_code == 200
        assert b'<strong class="connection-value">1</strong>' in dashboard_response.data
        assert b'reason-tile reason-done' in dashboard_response.data
        assert b'resident=jdoe@oswego.edu' in dashboard_response.data

        submitted = client.get('/history?status=submitted')
        assert submitted.status_code == 200
        assert b'Jane Doe' in submitted.data
        assert b'Submitted' in submitted.data
        assert b'1 shown' in submitted.data

        pending = client.get('/history?status=pending')
        assert pending.status_code == 200
        assert b'Nothing is waiting for Starfish' in pending.data
        assert b'0 shown' in pending.data


def test_history_pending_row_is_labeled_ready(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.get('/history?status=pending')
        assert response.status_code == 200
        assert b'Jane Doe' in response.data
        assert b'>Ready<' in response.data
        assert b'Edit note' in response.data
        assert b'/check-ins/0/edit-note' in response.data


def test_pending_note_can_be_edited(isolated_dashboard):
    with dashboard.app.test_client() as client:
        edit_page = client.get('/check-ins/0/edit-note?status=pending')
        assert edit_page.status_code == 200
        assert b'Edit pending note' in edit_page.data
        assert b'talked with jane about spring break' in edit_page.data

        response = client.post('/check-ins/0/edit-note', data={
            'checkin_key': 'jdoe@oswego.edu|08-16-2026|13:00',
            'original_note_digest': dashboard._note_digest('talked with jane about spring break'),
            'notes': 'Updated note, including a comma and clearer details.',
            'return_to': 'history',
            'status': 'pending',
        }, follow_redirects=True)

        assert response.status_code == 200
        assert b'Note saved.' in response.data
        assert b'Updated note, including a comma and clearer details.' in response.data

    import data as data_module
    rows = data_module.load_checkins(str(isolated_dashboard / 'checkins.csv'))
    assert len(rows) == 1
    assert rows[0]['notes'] == 'Updated note, including a comma and clearer details.'
    assert rows[0]['full_name'] == 'Jane Doe'
    assert rows[0]['date'] == '08-16-2026'


def test_submitted_note_cannot_be_edited(isolated_dashboard):
    (isolated_dashboard / 'submission_log.csv').write_text(
        'email_or_id,date,start_time,reason,submitted_at\n'
        'jdoe@oswego.edu,08-16-2026,13:00,Laker Connection #1 > FALL,2026-09-01T12:00:00\n'
    )

    with dashboard.app.test_client() as client:
        response = client.get('/check-ins/0/edit-note')
        assert response.status_code == 409
        assert b'already submitted' in response.data

        response = client.post('/check-ins/0/edit-note', data={
            'checkin_key': 'jdoe@oswego.edu|08-16-2026|13:00',
            'original_note_digest': dashboard._note_digest('talked with jane about spring break'),
            'notes': 'This must not be saved',
        })
        assert response.status_code == 409

    assert 'This must not be saved' not in (isolated_dashboard / 'checkins.csv').read_text()


def test_note_edit_rejects_stale_form(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/check-ins/0/edit-note', data={
            'checkin_key': 'someone-else@example.com|08-16-2026|13:00',
            'original_note_digest': dashboard._note_digest('talked with jane about spring break'),
            'notes': 'This must not be saved',
        })

    assert response.status_code == 409
    assert b'changed after the edit page opened' in response.data
    assert 'This must not be saved' not in (isolated_dashboard / 'checkins.csv').read_text()


def test_summary_wires_not_attempted_and_dry_run(monkeypatch, isolated_dashboard):
    captured = {}

    async def fake_run_submission(rows, reason, config, dry_run=False):
        captured['dry_run'] = dry_run
        failed = [{
            **rows[0],
            'outcome': dashboard.browser.SubmitOutcome.NOT_ATTEMPTED,
            'error': 'Not attempted because recovery failed.',
            'screenshot': None,
        }]
        return [], failed

    monkeypatch.setattr(dashboard.browser, 'run_submission', fake_run_submission)

    with dashboard.app.test_client() as client:
        response = client.post('/submit-batch', data={
            'selected_keys': ['jdoe@oswego.edu|08-16-2026|13:00'],
            'reason': 'Laker Connection #1 > FALL',
            'default_location': 'In- Residence Halls',
            'dry_run': 'on',
        })

    assert response.status_code == 200
    assert captured['dry_run'] is True
    assert b'Passed dry test' in response.data
    assert b'Not attempted' in response.data


def test_failure_screenshot_route(isolated_dashboard):
    failures = isolated_dashboard / 'logs' / 'failures'
    failures.mkdir(parents=True)
    (failures / 'example.png').write_bytes(b'fake-png')

    with dashboard.app.test_client() as client:
        response = client.get('/failure-screenshots/example.png')
        assert response.status_code == 200
        assert response.data == b'fake-png'


# ---------- SpeedNotes ----------

import json

KEY = 'jdoe@oswego.edu|08-16-2026|13:00'
SN_GROUPS = [
    {"name": "Wellbeing", "topics": ["Wellbeing - Kudos/Check-in", "Transition/Adjustment"]},
    {"name": "Housing", "topics": ["Room Selection", "Break Plans"]},
]


@pytest.fixture
def sn_dashboard(isolated_dashboard, monkeypatch):
    config_path = isolated_dashboard / "config.json"
    config = json.loads(config_path.read_text())
    config["speednote_groups"] = SN_GROUPS
    config_path.write_text(json.dumps(config))
    captured = {'calls': 0}

    async def fake_run_submission(rows, reason, config, dry_run=False):
        captured['calls'] += 1
        captured['rows'] = rows
        return [{**r, 'outcome': dashboard.browser.SubmitOutcome.SUBMITTED, 'error': None} for r in rows], []

    monkeypatch.setattr(dashboard.browser, 'run_submission', fake_run_submission)
    return captured


def _post(extra):
    base = {
        'selected_keys': [KEY],
        'reason': 'Laker Connection #1 > FALL',
        'default_location': 'In- Residence Halls',
        'dry_run': 'on',
    }
    base.update(extra)
    with dashboard.app.test_client() as client:
        return client.post('/submit-batch', data=base)


def test_submit_applies_batch_speednotes(sn_dashboard):
    response = _post({'speednotes': ['Room Selection', 'Transition/Adjustment']})
    assert response.status_code == 200
    assert sn_dashboard['rows'][0]['effective_speednotes'] == ['Room Selection', 'Transition/Adjustment']


def test_submit_override_replaces_batch(sn_dashboard):
    response = _post({
        'speednotes': ['Room Selection'],
        f'sn_override_{KEY}': 'on',
        f'speednotes_{KEY}': ['Break Plans'],
    })
    assert response.status_code == 200
    assert sn_dashboard['rows'][0]['effective_speednotes'] == ['Break Plans']


def test_submit_override_empty_is_none(sn_dashboard):
    response = _post({'speednotes': ['Room Selection'], f'sn_override_{KEY}': 'on'})
    assert response.status_code == 200
    assert sn_dashboard['rows'][0]['effective_speednotes'] == []


def test_submit_override_without_flag_is_ignored(sn_dashboard):
    response = _post({'speednotes': ['Room Selection'], f'speednotes_{KEY}': ['Break Plans']})
    assert response.status_code == 200
    assert sn_dashboard['rows'][0]['effective_speednotes'] == ['Room Selection']


def test_submit_unknown_speednote_rejected_runner_not_called(sn_dashboard):
    response = _post({'speednotes': ['Room Selection', 'Made Up Topic']})
    assert response.status_code == 400
    assert b'Made Up Topic' in response.data
    assert sn_dashboard['calls'] == 0


def test_submit_unknown_override_speednote_rejected(sn_dashboard):
    response = _post({
        'speednotes': ['Room Selection'],
        f'sn_override_{KEY}': 'on',
        f'speednotes_{KEY}': ['wellbeing - kudos/check-in'],
    })
    assert response.status_code == 400
    assert sn_dashboard['calls'] == 0


def test_submit_empty_batch_rejected_when_row_uses_batch(sn_dashboard):
    response = _post({})
    assert response.status_code == 400
    assert b'Pick at least one SpeedNote' in response.data
    assert sn_dashboard['calls'] == 0


def test_submit_empty_batch_ok_when_all_overridden(sn_dashboard):
    response = _post({f'sn_override_{KEY}': 'on', f'speednotes_{KEY}': ['Break Plans']})
    assert response.status_code == 200
    assert sn_dashboard['rows'][0]['effective_speednotes'] == ['Break Plans']


def test_submit_dedupes_topics(sn_dashboard):
    response = _post({'speednotes': ['Room Selection', 'Room Selection']})
    assert response.status_code == 200
    assert sn_dashboard['rows'][0]['effective_speednotes'] == ['Room Selection']


def test_submit_feature_off_needs_no_speednotes(monkeypatch, isolated_dashboard):
    captured = {}

    async def fake_run_submission(rows, reason, config, dry_run=False):
        captured['rows'] = rows
        return [], []

    monkeypatch.setattr(dashboard.browser, 'run_submission', fake_run_submission)
    response = _post({})
    assert response.status_code == 200
    assert captured['rows'][0].get('effective_speednotes', []) == []


def test_index_renders_speednote_picker(sn_dashboard):
    with dashboard.app.test_client() as client:
        html = client.get('/').data.decode()
    assert html.count('name="speednotes"') == 4
    assert f'name="speednotes_{KEY}"' in html
    assert f'name="sn_override_{KEY}"' in html
    assert 'Room Selection' in html


def test_index_no_picker_when_feature_off(isolated_dashboard):
    with dashboard.app.test_client() as client:
        html = client.get('/').data.decode()
    assert 'name="speednotes"' not in html


def test_settings_save_and_use(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/settings', data={
            'locations': 'Lobby\nIn- Residence Halls',
            'default_location': 'Lobby',
        })
        assert response.status_code == 302
        page = client.get('/').data
        assert b'Lobby' in page


def test_settings_blocks_removing_in_use_location(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/settings', data={
            'locations': 'Lobby',
            'default_location': 'Lobby',
        })
        assert response.status_code == 400
        assert b'still used by 1 check-in' in response.data
    assert not (isolated_dashboard / 'settings.json').exists()


def test_settings_allows_removing_location_only_used_by_submitted_rows(isolated_dashboard):
    (isolated_dashboard / 'submission_log.csv').write_text(
        "email_or_id,date,start_time,reason,submitted_at\n"
        "jdoe@oswego.edu,08-16-2026,13:00,Laker Connection #1 > FALL,2026-08-16T13:20:00\n"
    )
    with dashboard.app.test_client() as client:
        response = client.post('/settings', data={
            'locations': 'Lobby',
            'default_location': 'Lobby',
        })
        assert response.status_code == 302
    assert (isolated_dashboard / 'settings.json').exists()


def test_settings_reset(isolated_dashboard):
    (isolated_dashboard / 'settings.json').write_text(
        '{"valid_locations": ["Lobby", "In- Residence Halls"], "default_location": "Lobby"}')
    with dashboard.app.test_client() as client:
        assert client.post('/settings/reset').status_code == 302
        assert b'Lobby' not in client.get('/settings').data


def _upload(client, url, content: bytes, filename='data.csv'):
    import io
    return client.post(url, data={'file': (io.BytesIO(content), filename)},
                       content_type='multipart/form-data')


def test_import_residents_route_appends(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = _upload(client, '/import/residents',
                           b'Full Name,Email or ID\r\nAlex Rivera,arivera@oswego.edu\r\n')
        assert response.status_code == 302
        assert 'added=1' in response.headers['Location']
        assert b'Imported 1' in client.get(response.headers['Location']).data
    lines = (isolated_dashboard / 'residents.csv').read_text().splitlines()
    assert lines[-1] == 'Alex Rivera,arivera@oswego.edu'


def test_import_residents_into_file_without_trailing_newline(isolated_dashboard):
    (isolated_dashboard / 'residents.csv').write_text(
        "full_name,email_or_id\nJane Doe,jdoe@oswego.edu")
    with dashboard.app.test_client() as client:
        _upload(client, '/import/residents', b'full_name,email_or_id\nAlex Rivera,arivera@oswego.edu\n')
    lines = (isolated_dashboard / 'residents.csv').read_text().splitlines()
    assert lines[-2:] == ['Jane Doe,jdoe@oswego.edu', 'Alex Rivera,arivera@oswego.edu']


def test_import_checkins_route_all_or_nothing(isolated_dashboard):
    before = (isolated_dashboard / 'checkins.csv').read_bytes()
    with dashboard.app.test_client() as client:
        response = _upload(client, '/import/checkins', (
            b'full_name,date,start_time,location,notes\n'
            b'Jane Doe,09-10-2026,10:00,In- Residence Halls,ok\n'
            b'Nobody Here,09-10-2026,11:00,In- Residence Halls,bad\n'))
        assert response.status_code == 400
        assert b'Nothing was imported' in response.data
        assert b'Line 3' in response.data
    assert (isolated_dashboard / 'checkins.csv').read_bytes() == before


def test_import_checkins_route_appends(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = _upload(client, '/import/checkins', (
            b'full_name,date,start_time,location,notes\n'
            b'Jane Doe,09-10-2026,10:00,In- Residence Halls,"notes, with comma"\n'))
        assert response.status_code == 302
    rows = dashboard.data.load_checkins(str(isolated_dashboard / 'checkins.csv'))
    assert rows[-1]['notes'] == 'notes, with comma'


def test_import_rejects_non_csv_extension(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = _upload(client, '/import/residents',
                           b'full_name,email_or_id\nA,b\n', filename='roster.xlsx')
        assert response.status_code == 400
        assert b'CSV' in response.data


def test_template_download(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.get('/templates/checkins_template.csv')
        assert response.status_code == 200
        assert response.data.startswith(b'full_name,date,start_time,location,notes')
        assert client.get('/templates/config.json').status_code == 404
        assert client.get('/templates/../config.json').status_code == 404


def test_footer_shows_build_info(isolated_dashboard):
    with dashboard.app.test_client() as client:
        assert b'Laker Connection \xc2\xb7' not in client.get('/').data
        (isolated_dashboard / 'BUILD_INFO').write_text('a1b2c3d · 2026-10-25\n')
        assert 'Laker Connection · a1b2c3d · 2026-10-25'.encode() in client.get('/').data


def test_server_error_is_logged_with_traceback(isolated_dashboard, monkeypatch, caplog):
    def boom(path):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(dashboard.data, 'load_residents', boom)
    with caplog.at_level('ERROR', logger='laker'):
        with dashboard.app.test_client() as client:
            assert client.get('/').status_code == 500
    record = next(r for r in caplog.records if r.name == 'laker')
    assert "Couldn't load data" in record.getMessage()
    assert record.exc_info and 'disk on fire' in str(record.exc_info[1])


def test_submit_outcomes_are_logged(sn_dashboard, caplog):
    with caplog.at_level('INFO', logger='laker'):
        _post({'speednotes': ['Room Selection']})
    messages = [r.getMessage() for r in caplog.records if r.name == 'laker']
    assert any('dry run' in m and '1 row' in m for m in messages)
    assert any('SUBMITTED' in m and 'jdoe@oswego.edu' in m for m in messages)


def test_sidebar_lists_left_this_month_before_done(isolated_dashboard):
    from datetime import datetime
    this_month = datetime.now().strftime('%m-01-%Y')
    (isolated_dashboard / 'residents.csv').write_text(
        "full_name,email_or_id\nJane Doe,jdoe@oswego.edu\nBob Jones,bjones@oswego.edu\n")
    (isolated_dashboard / 'submission_log.csv').write_text(
        "email_or_id,date,start_time,reason,submitted_at\n"
        f"jdoe@oswego.edu,{this_month},09:00,Laker Connection #1 > FALL,2026-01-01T00:00:00\n")
    with dashboard.app.test_client() as client:
        page = client.get('/').data.decode()
    left, done = page.index('Left this month (1)'), page.index('Done this month (1)')
    assert left < page.index('Bob Jones', left) < done < page.index('Jane Doe', done)
    # the sidebar comes after the queue in the page's column layout wrapper
    assert page.index('class="dash-sidebar"') > page.index('Ready to submit')


def test_sidebar_everyone_done(isolated_dashboard):
    from datetime import datetime
    this_month = datetime.now().strftime('%m-01-%Y')
    (isolated_dashboard / 'submission_log.csv').write_text(
        "email_or_id,date,start_time,reason,submitted_at\n"
        f"jdoe@oswego.edu,{this_month},09:00,Laker Connection #1 > FALL,2026-01-01T00:00:00\n")
    with dashboard.app.test_client() as client:
        assert b"Everyone's done for this month." in client.get('/').data


class FakeAnalytics:
    def __init__(self):
        self.calls = []

    def track(self, event, **counts):
        self.calls.append((event, counts))


def _outcome_rows(rows, outcomes):
    template = rows[0]
    return [{**template, 'outcome': o, 'error': None} for o in outcomes]


def test_real_submit_tracked_with_counts(sn_dashboard, monkeypatch):
    fake = FakeAnalytics()
    monkeypatch.setattr(dashboard, 'analytics', fake)
    O = dashboard.browser.SubmitOutcome

    async def fake_run(rows, reason, config, dry_run=False):
        return (_outcome_rows(rows, [O.SUBMITTED, O.SUBMITTED]),
                _outcome_rows(rows, [O.NOT_FOUND, O.AMBIGUOUS, O.SUBMIT_FAILED,
                                     O.SUBMIT_UNCERTAIN, O.NOT_ATTEMPTED]))
    monkeypatch.setattr(dashboard.browser, 'run_submission', fake_run)
    _post({'speednotes': ['Room Selection'], 'dry_run': ''})
    assert len(fake.calls) == 1
    event, counts = fake.calls[0]
    seconds = counts.pop('seconds')
    assert event == 'submit' and isinstance(seconds, int) and seconds >= 0
    assert counts == {'submitted': 2, 'failed': 3, 'uncertain': 1, 'skipped': 1, 'crashed': 0}


def test_dry_run_not_tracked(sn_dashboard, monkeypatch):
    fake = FakeAnalytics()
    monkeypatch.setattr(dashboard, 'analytics', fake)
    _post({'speednotes': ['Room Selection']})
    assert fake.calls == []


def test_crashed_batch_tracked(sn_dashboard, monkeypatch):
    fake = FakeAnalytics()
    monkeypatch.setattr(dashboard, 'analytics', fake)

    async def boom(rows, reason, config, dry_run=False):
        raise RuntimeError("chrome went away")
    monkeypatch.setattr(dashboard.browser, 'run_submission', boom)
    response = _post({'speednotes': ['Room Selection'], 'dry_run': ''})
    assert response.status_code == 500 and b'Submission error' in response.data
    assert len(fake.calls) == 1
    event, counts = fake.calls[0]
    counts.pop('seconds')
    assert event == 'submit'
    assert counts == {'submitted': 0, 'failed': 0, 'uncertain': 0, 'skipped': 1, 'crashed': 1}


def test_no_analytics_module_is_harmless(sn_dashboard, monkeypatch):
    monkeypatch.setattr(dashboard, 'analytics', None, raising=False)
    response = _post({'speednotes': ['Room Selection'], 'dry_run': ''})
    assert response.status_code == 200
    assert sn_dashboard['calls'] == 1


# --- Form errors keep the user on the dashboard with their input intact ---

def test_add_checkin_error_rerenders_dashboard_with_notes(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/add-checkin', data={
            'full_name': 'Jane Doe',
            'date': '2026-08-16',
            'start_time': '13:00',  # overlaps the existing row
            'location': 'In- Residence Halls',
            'notes': 'long note I do not want to retype',
        })
    assert response.status_code == 400
    body = response.data.decode()
    assert 'Laker Connection Dashboard' in body
    assert 'long note I do not want to retype' in body
    assert 'value="2026-08-16"' in body
    assert 'value="13:00"' in body
    assert 'data-form-error="quick"' in body


def test_add_checkin_missing_location_keeps_notes(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/add-checkin', data={
            'full_name': 'Jane Doe',
            'date': '2026-09-17',
            'start_time': '14:00',
            'notes': 'note survives missing location',
        })
    assert response.status_code == 400
    body = response.data.decode()
    assert 'note survives missing location' in body
    assert '<option value="Jane Doe" selected>' in body
    lines = (isolated_dashboard / "checkins.csv").read_text().splitlines()
    assert len(lines) == 2


def test_add_checkins_batch_error_keeps_checked_and_notes(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/add-checkins-batch', data={
            'date': '2026-08-16',  # same month as Jane's existing row: cadence error
            'first_start_time': '15:00',
            'location': 'In- Residence Halls',
            'checked_jdoe@oswego.edu': 'on',
            'notes_jdoe@oswego.edu': 'batch note kept',
        })
    assert response.status_code == 400
    body = response.data.decode()
    assert 'data-form-error="batch"' in body
    assert 'batch note kept' in body
    assert 'name="checked_jdoe@oswego.edu" checked' in body
    assert 'data-active-tab="batch"' in body


def test_add_checkins_bulk_error_keeps_pasted_text(isolated_dashboard):
    with dashboard.app.test_client() as client:
        response = client.post('/add-checkins-bulk', data={
            'location': 'In- Residence Halls',
            'bulk_text': 'Nobody Here | 09-01-2026 | 10:00 | pasted text kept',
        })
    assert response.status_code == 400
    body = response.data.decode()
    assert 'data-form-error="bulk"' in body
    assert 'pasted text kept' in body


def test_mandatory_fields_marked_required(isolated_dashboard):
    with dashboard.app.test_client() as client:
        body = client.get('/').data.decode()
    assert '<select name="full_name" id="full_name" required>' in body
    assert '<select name="location" id="location" required>' in body
    assert '<select name="location" id="batch_location" required>' in body
    assert '<select name="location" id="bulk_location" required>' in body
    assert '<select name="reason" id="reason" required>' in body
    assert 'class="req"' in body
