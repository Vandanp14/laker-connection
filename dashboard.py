from flask import Flask, render_template, request, redirect, url_for, send_from_directory
import asyncio
from datetime import datetime
import hashlib
import logging
import os
import csv
import time
from typing import Dict

import data
import browser
import csv_import
import datadir
import paths

try:
    import analytics  # only present in the public app (see public_only/)
except ImportError:
    analytics = None

app = Flask(__name__)
# Goes to stderr, which launch.sh tees into logs/runs/run-<time>.log.
log = logging.getLogger('laker')
app.config['MAX_CONTENT_LENGTH'] = 1_000_000  # uploads: a roster/check-in CSV is a few KB

# DASH_ROOT holds the user's data files (follows LAKER_DATA_DIR); APP_ROOT
# holds the shipped config.json. Tests monkeypatch both.
DASH_ROOT = paths.data_dir()
APP_ROOT = paths.APP_ROOT
CSV_TEMPLATE_DIR = os.path.join(paths.APP_ROOT, 'templates', 'csv')
CSV_TEMPLATES = ('residents_template.csv', 'checkins_template.csv')


def _load_config() -> dict:
    """Shipped config.json with this user's settings.json (locations) on top."""
    return data.load_effective_config(os.path.join(APP_ROOT, 'config.json'),
                                      os.path.join(DASH_ROOT, 'settings.json'))


def _build_info() -> str:
    """'<sha> · <date>' written by the publish script; empty in a dev checkout."""
    try:
        with open(os.path.join(APP_ROOT, 'BUILD_INFO'), encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        return ''


def _import_notice() -> str:
    """One-line confirmation after an import redirect (?imported=…&added=…&skipped=…)."""
    kind = {'residents': 'resident', 'checkins': 'check-in'}.get(request.args.get('imported', ''))
    if not kind:
        return ''
    added = request.args.get('added', type=int, default=0)
    skipped = request.args.get('skipped', type=int, default=0)
    notice = f"Imported {added} {kind}{'s' if added != 1 else ''}."
    if skipped:
        notice += f" Skipped {skipped} already on file."
    return notice


def _candidate_key(row: Dict) -> str:
    """Stable identity key for a resolved row, safe to round-trip through an HTML form value."""
    return f"{row['email_or_id']}|{row['date']}|{row['start_time']}"


def _note_digest(notes: str) -> str:
    """Stable edit-version token that safely handles multiline note text."""
    return hashlib.sha256((notes or '').encode('utf-8')).hexdigest()


def _error_page(message: str, details=None, status: int = 400):
    if status >= 500:
        # Called from inside an except block: keep the traceback for the run log.
        log.error(message, exc_info=True)
    return render_template('error.html', message=message, details=details or []), status


def _build_history_rows(checkins, resolved_checkins, submission_log, submission_log_details):
    """Build the single status model shared by Dashboard and History."""
    resolved_by_original_index = {r['_original_index']: r for r in resolved_checkins}
    details_by_key = {
        (d['email_or_id'], d['date'], d['start_time']): d for d in submission_log_details
    }
    rows = []
    for raw_row in checkins:
        resolved = resolved_by_original_index.get(raw_row['_original_index'])
        if resolved is None:
            rows.append({
                'email_or_id': '',
                'name': raw_row['full_name'] or '(blank name)',
                'date': raw_row['date'] or '(blank date)',
                'time': raw_row['start_time'] or '—',
                'location': raw_row['location'],
                'notes': raw_row['notes'],
                'status': 'error',
                'reason': '',
                'original_row_index': raw_row['_original_index'],
                'key': '',
                'sort_date': raw_row['date'],
                'sort_time': raw_row['start_time'],
            })
            continue

        key = (resolved['email_or_id'], resolved['date'], resolved['start_time'])
        detail = details_by_key.get(key)
        rows.append({
            'email_or_id': resolved['email_or_id'],
            'name': resolved['full_name'],
            'date': resolved['date'],
            'time': resolved['start_time'],
            'location': resolved['location'],
            'notes': resolved['notes'],
            'status': 'submitted' if key in submission_log else 'pending',
            'reason': detail['reason'] if detail else '',
            'original_row_index': resolved['_original_index'],
            'key': _candidate_key(resolved),
            'sort_date': resolved['date'],
            'sort_time': resolved['start_time'],
        })

    def sort_key(row):
        try:
            return datetime.strptime(
                f"{row['sort_date']} {row['sort_time']}", "%m-%d-%Y %H:%M")
        except (ValueError, TypeError):
            return datetime.min

    rows.sort(key=sort_key, reverse=True)
    return rows


def _status_counts(rows):
    """Counts used by both dashboard tiles and History filters."""
    return {
        'recorded': len(rows),
        'pending': sum(row['status'] == 'pending' for row in rows),
        'submitted': sum(row['status'] == 'submitted' for row in rows),
        'error': sum(row['status'] == 'error' for row in rows),
    }


@app.route('/')
def index():
    try:
        config = _load_config()
        residents = data.load_residents(os.path.join(DASH_ROOT, 'residents.csv'))
        checkins = data.load_checkins(os.path.join(DASH_ROOT, 'checkins.csv'))
        submission_log = data.load_submission_log(os.path.join(DASH_ROOT, 'submission_log.csv'))

        # Resolve and validate
        resolved_checkins, resolution_errors = data.resolve_checkins(
            checkins, residents, submission_log, config)
        validation_errors = data.validate(resolved_checkins, residents, config, submission_log)
        errors = resolution_errors + validation_errors

        candidates = data.compute_candidates(resolved_checkins, submission_log)
        submission_log_details = data.load_submission_log_details(
            os.path.join(DASH_ROOT, 'submission_log.csv'))
        progress = data.compute_progress(residents, submission_log_details, config)
        history_rows = _build_history_rows(
            checkins, resolved_checkins, submission_log, submission_log_details)
        status_counts = _status_counts(history_rows)
    except Exception as e:
        return _error_page(f"Couldn't load data: {e}", status=500)

    # Format residents for template (sorted by name)
    template_residents = sorted(
        [{'name': name, 'id': eid} for eid, name in residents.items()],
        key=lambda r: r['name']
    )

    # Format candidates (only actual candidates — not-yet-submitted rows)
    template_candidates = []
    for row in candidates:
        template_candidates.append({
            'key': _candidate_key(row),
            'email_or_id': row['email_or_id'],
            'name': row['full_name'],
            'date': row['date'],
            'time': row['start_time'],
            'location': row['location'],
            'notes': row['notes'],
            'original_row_index': row['_original_index']
        })

    # Format progress (sorted by name, same as the resident picker, so rows
    # don't appear in whatever order residents.csv happens to be in)
    template_progress = sorted(progress, key=lambda p: p['full_name'].lower())

    return render_template('index.html',
        residents=template_residents,
        candidates=template_candidates,
        progress=template_progress,
        errors=errors or [],
        status_counts=status_counts,
        config=config,
        import_notice=_import_notice(),
        build_info=_build_info(),
        current_datetime=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    )

@app.route('/history')
def history():
    """
    Review everything in checkins.csv — every note ever logged, newest first,
    with its submit status. Pending notes can be edited; submitted rows remain
    read-only because changing the local CSV would not change Starfish.
    """
    try:
        config = _load_config()
        residents = data.load_residents(os.path.join(DASH_ROOT, 'residents.csv'))
        checkins = data.load_checkins(os.path.join(DASH_ROOT, 'checkins.csv'))
        submission_log = data.load_submission_log(os.path.join(DASH_ROOT, 'submission_log.csv'))
        submission_log_details = data.load_submission_log_details(
            os.path.join(DASH_ROOT, 'submission_log.csv'))

        resolved_checkins, _resolution_errors = data.resolve_checkins(
            checkins, residents, submission_log, config)
        all_rows = _build_history_rows(
            checkins, resolved_checkins, submission_log, submission_log_details)
        status_counts = _status_counts(all_rows)

        active_status = request.args.get('status', 'all').strip().lower()
        if active_status not in {'all', 'pending', 'submitted', 'error'}:
            active_status = 'all'
        active_resident = request.args.get('resident', '').strip()
        active_reason = request.args.get('reason', '').strip()

        rows = all_rows
        if active_status != 'all':
            rows = [row for row in rows if row['status'] == active_status]
        if active_resident:
            rows = [row for row in rows if row['email_or_id'] == active_resident]
        if active_reason:
            rows = [row for row in rows if row['reason'] == active_reason]
    except Exception as e:
        return _error_page(f"Couldn't load history: {e}", status=500)

    return render_template('history.html',
        history=rows,
        status_counts=status_counts,
        active_status=active_status,
        active_resident=active_resident,
        active_reason=active_reason,
        note_updated=request.args.get('updated') == 'note',
        current_datetime=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    )


def _note_edit_return_url(updated: bool = False) -> str:
    """Return only to a known dashboard view; never redirect to user-supplied URLs."""
    if request.values.get('return_to') == 'dashboard':
        return url_for('index')

    status = request.values.get('status', 'pending').strip().lower()
    if status not in {'all', 'pending', 'submitted', 'error'}:
        status = 'pending'
    args = {'status': status}
    resident = request.values.get('resident', '').strip()
    reason = request.values.get('reason', '').strip()
    if resident:
        args['resident'] = resident
    if reason:
        args['reason'] = reason
    if updated:
        args['updated'] = 'note'
    return url_for('history', **args)


@app.route('/check-ins/<int:original_index>/edit-note', methods=['GET', 'POST'])
def edit_checkin_note(original_index):
    """Edit a pending check-in note without allowing submitted rows to drift."""
    try:
        config = _load_config()
        residents = data.load_residents(os.path.join(DASH_ROOT, 'residents.csv'))
        checkins_path = os.path.join(DASH_ROOT, 'checkins.csv')
        checkins = data.load_checkins(checkins_path)
        submission_log = data.load_submission_log(
            os.path.join(DASH_ROOT, 'submission_log.csv'))

        if original_index < 0 or original_index >= len(checkins):
            return _error_page(
                "Couldn't edit note",
                ["This check-in no longer exists. Return to History and choose it again."],
                status=404,
            )

        resolved_checkins, resolution_errors = data.resolve_checkins(
            checkins, residents, submission_log, config)
        resolved = next(
            (row for row in resolved_checkins
             if row['_original_index'] == original_index),
            None,
        )
        if resolved is None:
            details = resolution_errors or ["This check-in cannot be resolved to a resident."]
            return _error_page("Couldn't edit note", details, status=409)

        key = (resolved['email_or_id'], resolved['date'], resolved['start_time'])
        if key in submission_log:
            return _error_page(
                "Couldn't edit note",
                ["This check-in was already submitted. Edit it in Starfish if a correction is required."],
                status=409,
            )

        raw_row = checkins[original_index]
        if request.method == 'POST':
            expected_key = request.form.get('checkin_key', '')
            expected_note_digest = request.form.get('original_note_digest', '')
            if (expected_key != _candidate_key(resolved)
                    or expected_note_digest != _note_digest(raw_row['notes'])):
                return _error_page(
                    "Couldn't edit note",
                    ["This check-in changed after the edit page opened. Return to History and try again."],
                    status=409,
                )

            expected_identity = tuple(
                raw_row[field] for field in data.CHECKINS_FIELDNAMES[:-1])
            data.update_checkin_note(
                checkins_path,
                original_index,
                request.form.get('notes', ''),
                expected_identity=expected_identity,
            )
            return redirect(_note_edit_return_url(updated=True))

        return render_template(
            'edit_note.html',
            checkin=resolved,
            checkin_key=_candidate_key(resolved),
            original_note_digest=_note_digest(raw_row['notes']),
            original_notes=raw_row['notes'],
            cancel_url=_note_edit_return_url(),
            return_to=request.args.get('return_to', 'history'),
            active_status=request.args.get('status', 'pending'),
            active_resident=request.args.get('resident', ''),
            active_reason=request.args.get('reason', ''),
        )
    except Exception as e:
        return _error_page(f"Couldn't edit note: {e}", status=500)


@app.route('/failure-screenshots/<path:filename>')
def failure_screenshot(filename):
    """Serve only captured automation failures from the dedicated log directory."""
    return send_from_directory(os.path.join(DASH_ROOT, 'logs', 'failures'), filename)

@app.route('/add-resident', methods=['POST'])
def add_resident():
    try:
        full_name = request.form.get('full_name', '').strip()
        email_or_id = request.form.get('email_or_id', '').strip()

        if not full_name or not email_or_id:
            return _error_page("Couldn't add resident", ["Missing full name or email/ID"])

        residents_path = os.path.join(DASH_ROOT, 'residents.csv')
        residents = data.load_residents(residents_path)
        if email_or_id in residents:
            return _error_page(
                "Couldn't add resident",
                [f'"{email_or_id}" is already on the roster (as "{residents[email_or_id]}")'])
        _, duplicate_names = data.build_name_index({**residents, email_or_id: full_name})
        if data._name_key(full_name) in duplicate_names:
            return _error_page(
                "Couldn't add resident",
                [f'"{full_name}" collides with an existing resident\'s name '
                 f'(case/whitespace-insensitive) — full_name must be unique'])

        with open(residents_path, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow([full_name, email_or_id])

        return redirect(url_for('index'))
    except Exception as e:
        return _error_page(f"Couldn't add resident: {e}", status=500)

@app.route('/add-checkin', methods=['POST'])
def add_checkin():
    try:
        full_name = request.form.get('full_name', '').strip()
        date_iso = request.form.get('date', '').strip()
        start_time = request.form.get('start_time', '').strip()
        location = request.form.get('location', '').strip()
        notes = request.form.get('notes', '').strip()

        if not full_name or not date_iso:
            return _error_page("Couldn't add check-in", ["Missing resident or date"])

        # <input type="date"> always posts YYYY-MM-DD; checkins.csv uses MM-DD-YYYY.
        try:
            date = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%m-%d-%Y")
        except ValueError:
            return _error_page("Couldn't add check-in", [f'Date "{date_iso}" is not a valid date'])

        checkins_path = os.path.join(DASH_ROOT, 'checkins.csv')
        residents_path = os.path.join(DASH_ROOT, 'residents.csv')
        submission_log_path = os.path.join(DASH_ROOT, 'submission_log.csv')

        config = _load_config()
        residents = data.load_residents(residents_path)
        submission_log = data.load_submission_log(submission_log_path)
        existing_checkins = data.load_checkins(checkins_path)
        new_row = {
            'full_name': full_name,
            'date': date,
            'start_time': start_time,
            'location': location,
            'notes': notes,
            '_original_index': len(existing_checkins),
        }

        # Resolve + validate the WOULD-BE file (existing rows + this new one) before
        # writing anything, so a bad row never lands in checkins.csv in the first place.
        candidate_checkins = existing_checkins + [new_row]
        resolved, resolution_errors = data.resolve_checkins(
            candidate_checkins, residents, submission_log, config)
        validation_errors = data.validate(resolved, residents, config, submission_log)
        errors = resolution_errors + validation_errors
        if errors:
            return _error_page("Couldn't add check-in", errors)

        with open(checkins_path, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow([full_name, date, start_time, location, notes])

        return redirect(url_for('index'))
    except Exception as e:
        return _error_page(f"Couldn't add check-in: {e}", status=500)

@app.route('/add-checkins-batch', methods=['POST'])
def add_checkins_batch():
    """
    Log check-ins for MULTIPLE residents in one sitting — the actual real-world
    workflow (a run of back-to-back conversations on one day). One shared date
    and location; the first checked resident anchors the start time, everyone
    else auto-chains +slot_duration_minutes via resolve_checkins, same as
    hand-editing checkins.csv would.
    """
    try:
        date_iso = request.form.get('date', '').strip()
        first_start_time = request.form.get('first_start_time', '').strip()
        location = request.form.get('location', '').strip()

        if not date_iso:
            return _error_page("Couldn't log check-ins", ["Missing date"])
        try:
            date = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%m-%d-%Y")
        except ValueError:
            return _error_page("Couldn't log check-ins", [f'Date "{date_iso}" is not a valid date'])

        residents_path = os.path.join(DASH_ROOT, 'residents.csv')
        checkins_path = os.path.join(DASH_ROOT, 'checkins.csv')
        submission_log_path = os.path.join(DASH_ROOT, 'submission_log.csv')

        config = _load_config()
        residents = data.load_residents(residents_path)
        submission_log = data.load_submission_log(submission_log_path)
        existing_checkins = data.load_checkins(checkins_path)

        # Residents are offered to the form sorted by name (see index()) — walk
        # them in that same order so "first checked" reliably means "first
        # anchor" the same way the form visually lists them.
        ordered = sorted(residents.items(), key=lambda kv: kv[1])
        new_rows = []
        for email_or_id, full_name in ordered:
            if request.form.get(f'checked_{email_or_id}') != 'on':
                continue
            notes = request.form.get(f'notes_{email_or_id}', '').strip()
            start_time = first_start_time if not new_rows else ''  # only the first row gets the explicit anchor
            new_rows.append({
                'full_name': full_name,
                'date': date,
                'start_time': start_time,
                'location': location,
                'notes': notes,
                '_original_index': len(existing_checkins) + len(new_rows),
            })

        if not new_rows:
            return _error_page("Couldn't log check-ins", ["No residents selected"])

        candidate_checkins = existing_checkins + new_rows
        resolved, resolution_errors = data.resolve_checkins(
            candidate_checkins, residents, submission_log, config)
        validation_errors = data.validate(resolved, residents, config, submission_log)
        errors = resolution_errors + validation_errors
        if errors:
            return _error_page("Couldn't log check-ins", errors)

        with open(checkins_path, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            for row in new_rows:
                writer.writerow([row['full_name'], row['date'], row['start_time'], row['location'], row['notes']])

        return redirect(url_for('index'))
    except Exception as e:
        return _error_page(f"Couldn't log check-ins: {e}", status=500)

@app.route('/add-checkins-bulk', methods=['POST'])
def add_checkins_bulk():
    """
    Paste-many-at-once entry point for catching up a backlog spanning different
    residents and dates in one go. Each non-blank line:
    "Full Name | MM-DD-YYYY | HH:MM or blank | notes". One shared location
    applies to every parsed line — asking for it per line in a paste box is
    needless friction, same tradeoff add_checkins_batch already makes for
    same-day entry.

    Two error phases, both block ALL writes (never a partial paste):
    1. Format errors (wrong field count, blank name) — reported per textarea
       line number, since that's what the user is looking at.
    2. Resolution/validation errors (unknown resident, overlap, bad date) —
       reported by data.py in terms of the resulting checkins.csv row number,
       same as every other add route.
    """
    try:
        raw_text = request.form.get('bulk_text', '')
        location = request.form.get('location', '').strip()

        if not location:
            return _error_page("Couldn't add check-ins", ["Missing location"])

        residents_path = os.path.join(DASH_ROOT, 'residents.csv')
        checkins_path = os.path.join(DASH_ROOT, 'checkins.csv')
        submission_log_path = os.path.join(DASH_ROOT, 'submission_log.csv')

        config = _load_config()
        residents = data.load_residents(residents_path)
        submission_log = data.load_submission_log(submission_log_path)
        existing_checkins = data.load_checkins(checkins_path)

        parse_errors = []
        new_rows = []
        for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            # maxsplit=3 so a stray "|" inside free-text notes stays in the
            # notes field instead of splitting it further.
            parts = [p.strip() for p in line.split('|', 3)]
            if len(parts) != 4:
                parse_errors.append(
                    f'Line {line_no}: expected 4 fields separated by "|" '
                    f'(Name | MM-DD-YYYY | HH:MM or blank | notes), got {len(parts)}')
                continue
            line_name, line_date, line_start_time, line_notes = parts
            if not line_name:
                parse_errors.append(f"Line {line_no}: name is blank")
                continue
            if not line_date:
                parse_errors.append(f"Line {line_no}: date is blank")
                continue
            new_rows.append({
                'full_name': line_name,
                'date': line_date,
                'start_time': line_start_time,
                'location': location,
                'notes': line_notes,
                '_original_index': len(existing_checkins) + len(new_rows),
            })

        if parse_errors:
            return _error_page("Couldn't add check-ins — fix these lines and paste again", parse_errors)

        if not new_rows:
            return _error_page("Couldn't add check-ins", ["No check-in lines found"])

        candidate_checkins = existing_checkins + new_rows
        resolved, resolution_errors = data.resolve_checkins(
            candidate_checkins, residents, submission_log, config)
        validation_errors = data.validate(resolved, residents, config, submission_log)
        errors = resolution_errors + validation_errors
        if errors:
            return _error_page("Couldn't add check-ins", errors)

        with open(checkins_path, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            for row in new_rows:
                writer.writerow([row['full_name'], row['date'], row['start_time'], row['location'], row['notes']])

        return redirect(url_for('index'))
    except Exception as e:
        return _error_page(f"Couldn't add check-ins: {e}", status=500)

@app.route('/submit-batch', methods=['POST'])
def submit_batch():
    try:
        # Parse form data — selected rows are identified by stable
        # "email_or_id|date|start_time" keys, not list position (position can
        # shift between page-load and submit if checkins.csv changes).
        # Preserve the queue's visible order. Turning these into a set made
        # execution order arbitrary, which made a failed row appear to jump
        # around between runs and complicated recovery diagnostics.
        selected_keys = list(dict.fromkeys(request.form.getlist('selected_keys')))
        run_reason = request.form.get('reason', '').strip()
        default_loc = request.form.get('default_location', '').strip()
        dry_run = request.form.get('dry_run', '').lower() in {'on', 'true', '1'}

        if not run_reason:
            return _error_page("Couldn't submit", ["Batch reason is required"])
        if not selected_keys:
            return _error_page("Couldn't submit", ["No rows selected"])

        # Load data
        config = _load_config()
        residents = data.load_residents(os.path.join(DASH_ROOT, 'residents.csv'))
        checkins = data.load_checkins(os.path.join(DASH_ROOT, 'checkins.csv'))
        submission_log = data.load_submission_log(os.path.join(DASH_ROOT, 'submission_log.csv'))

        # Resolve and validate
        resolved_checkins, resolution_errors = data.resolve_checkins(
            checkins, residents, submission_log, config)
        validation_errors = data.validate(resolved_checkins, residents, config, submission_log)
        errors = resolution_errors + validation_errors

        if errors:
            return _error_page("Couldn't submit", errors)

        by_key = {_candidate_key(row): row for row in resolved_checkins}
        rows_to_submit = []
        for key in selected_keys:
            row = by_key.get(key)
            if row is None:
                return _error_page(
                    "Couldn't submit",
                    [f"Selected row {key} no longer matches a candidate — "
                     f"checkins.csv may have changed since this page loaded, reload and retry"])
            rows_to_submit.append(row)

        # Apply location overrides: form field name is "location_<key>"
        for row in rows_to_submit:
            key = _candidate_key(row)
            location_override = request.form.get(f'location_{key}', '').strip()
            if location_override:
                row['effective_location'] = location_override
            elif default_loc:
                row['effective_location'] = default_loc
            # else keep the row's original location (browser.py falls back to row['location'])

        # SpeedNotes: batch topics, replaced per row when that row's
        # "sn_override_<key>" flag is on (an override with no topics means
        # explicitly none). Every name is validated before anything runs.
        if data.speednote_topics(config):
            batch_topics, sn_errors = data.normalize_speednotes(
                request.form.getlist('speednotes'), config)
            uses_batch = False
            for row in rows_to_submit:
                key = _candidate_key(row)
                if request.form.get(f'sn_override_{key}') == 'on':
                    row_topics, row_errors = data.normalize_speednotes(
                        request.form.getlist(f'speednotes_{key}'), config)
                    sn_errors.extend(f"{row['full_name']}: {e}" for e in row_errors)
                    row['effective_speednotes'] = row_topics
                else:
                    uses_batch = True
                    row['effective_speednotes'] = batch_topics
            if uses_batch and not batch_topics:
                sn_errors.append(
                    "Pick at least one SpeedNote for this batch, or give those rows their own topics")
            if sn_errors:
                return _error_page("Couldn't submit", sn_errors)

        # Run the submission asynchronously
        started = time.monotonic()
        try:
            # Since Flask is sync by default, run async code in a new event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            submitted, failed = loop.run_until_complete(
                browser.run_submission(rows_to_submit, run_reason, config, dry_run=dry_run)
            )
            loop.close()
        except Exception as e:
            if analytics and not dry_run:
                analytics.track('submit', submitted=0, failed=0, uncertain=0,
                                skipped=len(rows_to_submit), crashed=1,
                                seconds=round(time.monotonic() - started))
            return _error_page(f"Submission error: {e}", status=500)
        if analytics and not dry_run:
            O = browser.SubmitOutcome
            outcomes = [f['outcome'] for f in failed]
            analytics.track('submit', submitted=len(submitted),
                            failed=sum(o in (O.NOT_FOUND, O.AMBIGUOUS, O.SUBMIT_FAILED)
                                       for o in outcomes),
                            uncertain=outcomes.count(O.SUBMIT_UNCERTAIN),
                            skipped=outcomes.count(O.NOT_ATTEMPTED),
                            crashed=0, seconds=round(time.monotonic() - started))

        log.info("%s: %d row(s), reason %r", 'dry run' if dry_run else 'real submit',
                 len(rows_to_submit), run_reason)
        for r in submitted:
            log.info("  SUBMITTED %s %s %s", r['email_or_id'], r['date'], r['start_time'])
        for f in failed:
            log.warning("  %s %s %s %s: %s", f['outcome'], f['email_or_id'], f['date'],
                        f['start_time'], f.get('error'))

        # For success, append to submission_log (only if not dry run)
        if not dry_run:
            for row in submitted:
                data.append_submission_log(os.path.join(DASH_ROOT, 'submission_log.csv'), row)

        # Prepare summary
        summary = {
            'total_selected': len(rows_to_submit),
            'submitted': len(submitted),
            'failed': len(failed),
            'dry_run': dry_run,
            'not_found': len([f for f in failed if f['outcome'] == browser.SubmitOutcome.NOT_FOUND]),
            'ambiguous': len([f for f in failed if f['outcome'] == browser.SubmitOutcome.AMBIGUOUS]),
            'submit_failures': len([f for f in failed if f['outcome'] == browser.SubmitOutcome.SUBMIT_FAILED]),
            'uncertain': len([f for f in failed if f['outcome'] == browser.SubmitOutcome.SUBMIT_UNCERTAIN]),
            'not_attempted': len([f for f in failed if f['outcome'] == browser.SubmitOutcome.NOT_ATTEMPTED]),
            'results': failed
        }

        return render_template('summary.html', summary=summary)
    except Exception as e:
        return _error_page(f"Unexpected error: {e}", status=500)

def _unsubmitted_location_counts(config: dict) -> Dict[str, int]:
    """How many not-yet-submitted check-ins use each location."""
    residents = data.load_residents(os.path.join(DASH_ROOT, 'residents.csv'))
    checkins = data.load_checkins(os.path.join(DASH_ROOT, 'checkins.csv'))
    submission_log = data.load_submission_log(os.path.join(DASH_ROOT, 'submission_log.csv'))
    resolved, _ = data.resolve_checkins(checkins, residents, submission_log, config)
    counts: Dict[str, int] = {}
    for row in data.compute_candidates(resolved, submission_log):
        counts[row['location']] = counts.get(row['location'], 0) + 1
    return counts


def _settings_page(config: dict, errors=None, saved: bool = False, status: int = 200,
                   locations_text=None):
    if locations_text is None:
        locations_text = '\n'.join(config.get('valid_locations') or [])
    return render_template('settings.html', config=config, errors=errors or [], saved=saved,
                           locations_text=locations_text), status


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    try:
        config = _load_config()
        if request.method == 'GET':
            return _settings_page(config, saved=request.args.get('saved') == '1')

        locations_text = request.form.get('locations', '')
        locations = [line.strip() for line in locations_text.splitlines() if line.strip()]
        default_location = request.form.get('default_location', '').strip()
        if default_location not in locations and locations:
            # Default dropdown reflects the old list; fall back to the first new entry.
            default_location = locations[0]

        errors = []
        for loc, count in sorted(_unsubmitted_location_counts(config).items()):
            if loc not in locations:
                errors.append(f'"{loc}" is still used by {count} check-in(s) not yet submitted. '
                              f'Change those first.')
        if not errors:
            try:
                data.save_location_settings(os.path.join(DASH_ROOT, 'settings.json'),
                                            locations, default_location)
            except ValueError as e:
                errors.append(str(e))
        if errors:
            return _settings_page(config, errors=errors, status=400, locations_text=locations_text)
        return redirect(url_for('settings', saved=1))
    except Exception as e:
        return _error_page(f"Couldn't save settings: {e}", status=500)


@app.route('/settings/reset', methods=['POST'])
def settings_reset():
    try:
        settings_path = os.path.join(DASH_ROOT, 'settings.json')
        if os.path.exists(settings_path):
            os.remove(settings_path)
        return redirect(url_for('settings', saved=1))
    except Exception as e:
        return _error_page(f"Couldn't reset settings: {e}", status=500)


def _append_csv_rows(path: str, rows) -> None:
    """Append rows, first adding a newline if the file doesn't end with one
    (spreadsheet exports often don't — appending would glue two rows together)."""
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        needs_newline = False
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) not in (b'\n', b'\r')
    with open(path, 'a', newline='', encoding='utf-8') as f:
        if needs_newline:
            f.write('\n')
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


def _read_uploaded_file(required):
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        raise csv_import.ImportError_(["Choose a file to import."])
    if not upload.filename.lower().endswith('.csv'):
        raise csv_import.ImportError_([
            f'"{upload.filename}" isn\'t a CSV file. In your spreadsheet app, use '
            f'File → Download/Export → CSV, then import that file.'])
    return csv_import.read_upload(upload.read(), required)


def _import_rejected(messages):
    return _error_page("Nothing was imported",
                       messages + ["Fix these lines in your file and import it again."])


@app.route('/templates/<name>')
def csv_template(name):
    if name not in CSV_TEMPLATES:
        return _error_page("No such template", status=404)
    return send_from_directory(CSV_TEMPLATE_DIR, name, as_attachment=True)


@app.route('/import/residents', methods=['POST'])
def import_residents():
    try:
        rows = _read_uploaded_file(['full_name', 'email_or_id'])
        residents_path = os.path.join(DASH_ROOT, 'residents.csv')
        new_pairs, skipped = csv_import.merge_residents(data.load_residents(residents_path), rows)
        _append_csv_rows(residents_path, new_pairs)
        return redirect(url_for('index', imported='residents', added=len(new_pairs), skipped=skipped))
    except csv_import.ImportError_ as e:
        return _import_rejected(e.messages)
    except Exception as e:
        return _error_page(f"Couldn't import residents: {e}", status=500)


@app.route('/import/checkins', methods=['POST'])
def import_checkins():
    try:
        rows = _read_uploaded_file(list(data.CHECKINS_FIELDNAMES))
        checkins_path = os.path.join(DASH_ROOT, 'checkins.csv')
        new_rows, skipped = csv_import.merge_checkins(
            data.load_checkins(checkins_path),
            rows,
            data.load_residents(os.path.join(DASH_ROOT, 'residents.csv')),
            data.load_submission_log(os.path.join(DASH_ROOT, 'submission_log.csv')),
            _load_config(),
        )
        _append_csv_rows(checkins_path, [[r[c] for c in data.CHECKINS_FIELDNAMES] for r in new_rows])
        return redirect(url_for('index', imported='checkins', added=len(new_rows), skipped=skipped))
    except csv_import.ImportError_ as e:
        return _import_rejected(e.messages)
    except Exception as e:
        return _error_page(f"Couldn't import check-ins: {e}", status=500)


if __name__ == '__main__':
    import sys
    try:
        for note in datadir.prepare(DASH_ROOT):
            print(note)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    if analytics:
        analytics.on_launch()
    # Debug mode (auto-reload, in-browser tracebacks) only when asked for:
    # LAKER_DEBUG=1 python3 dashboard.py
    # Port 5000 collides with macOS's AirPlay Receiver, which squats on it by
    # default — 5050 avoids that without asking you to change a Mac setting.
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')
    debug = os.environ.get('LAKER_DEBUG') == '1'
    if not debug:
        # Friends see this Terminal window: hide Flask's per-request log and its
        # red "development server" banner, which read as errors to non-developers.
        import logging
        import flask.cli
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        flask.cli.show_server_banner = lambda *args, **kwargs: None
    port = int(os.environ.get('LAKER_PORT', '5050'))
    log.info("Dashboard on port %d (data: %s)", port, DASH_ROOT)
    app.run(debug=debug, port=port)
