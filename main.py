#!/usr/bin/env python3
"""
Main entry point for Laker Connection Starfish check-in automation.
"""
import argparse
import json
import sys

import browser
import data
import datadir
import paths
import selection

def main():
    parser = argparse.ArgumentParser(description="Automate Starfish check-in logging.")
    parser.add_argument('--dry-run', action='store_true', help="Run through the motions but do not submit.")
    args = parser.parse_args()

    residents_path = paths.data_file('residents.csv')
    checkins_path = paths.data_file('checkins.csv')
    submission_log_path = paths.data_file('submission_log.csv')
    try:
        datadir.prepare(paths.data_dir())
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Load config
    try:
        config = data.load_effective_config(paths.config_path(), paths.data_file('settings.json'))
    except FileNotFoundError:
        print("ERROR: config.json not found. Please create it from the template.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: config.json is invalid JSON: {e}")
        sys.exit(1)

    # The CLI has no SpeedNotes picker yet. With SpeedNotes configured,
    # running it would submit appointments with no topics ticked — stop.
    if data.speednote_topics(config):
        print("ERROR: SpeedNotes are configured (config.json speednote_groups), but the CLI "
              "can't pick them yet. Use the dashboard: python3 dashboard.py")
        sys.exit(1)

    # Load data
    try:
        residents = data.load_residents(residents_path)
        checkins = data.load_checkins(checkins_path)
        submission_log = data.load_submission_log(submission_log_path)
    except Exception as e:
        print(f"ERROR loading data files: {e}")
        sys.exit(1)

    # Resolve full_name -> email_or_id and auto-chain blank start_times
    checkins, resolution_errors = data.resolve_checkins(checkins, residents, submission_log, config)
    if resolution_errors:
        print("Resolution errors found:")
        for err in resolution_errors:
            print(f"  - {err}")
        sys.exit(1)

    # Validate
    validation_errors = data.validate(checkins, residents, config, submission_log)
    if validation_errors:
        print("Validation errors found:")
        for err in validation_errors:
            print(f"  - {err}")
        sys.exit(1)

    # Compute candidates
    candidates = data.compute_candidates(checkins, submission_log)
    if not candidates:
        print("INFO: No new check-ins to submit (all rows already in submission log).")
        sys.exit(0)

    # Prompt user for selection
    try:
        selected_rows, run_reason, default_loc = selection.prompt_for_selection(
            residents_path,
            checkins_path,
            submission_log_path,
            config
        )
    except SystemExit as e:
        # selection.prompt_for_selection may call sys.exit on early exit (no candidates, etc.)
        sys.exit(e.code)

    if not selected_rows:
        print("INFO: No rows selected for submission.")
        sys.exit(0)

    # Browser automation
    print(f"Starting browser automation (dry_run={args.dry_run})...")
    try:
        import asyncio

        def on_row(event, idx, total, row, outcome=None, error=None):
            if event == "start":
                print(f"Processing row {idx}/{total}: {row['email_or_id']} {row['date']} {row['start_time']} ...")
            elif event == "success":
                print("  -> SUCCESS")
            elif event == "failed":
                print(f"  -> FAILED ({outcome}): {error}")

        submitted, failed = asyncio.run(
            browser.run_submission(selected_rows, run_reason, config, dry_run=args.dry_run, on_row=on_row)
        )
        if not args.dry_run:
            for row in submitted:
                data.append_submission_log(submission_log_path, row)
    except Exception as e:
        print(f"Unexpected error during browser automation: {e}")
        sys.exit(1)

    submission_log_details = data.load_submission_log_details(submission_log_path)

    # Summary
    print("\n=== Submission Summary ===")
    print(f"Total selected:   {len(selected_rows)}")
    print(f"Submitted:        {len(submitted)}")
    print(f"Not found:        {len([f for f in failed if f['outcome'] == browser.SubmitOutcome.NOT_FOUND])}")
    print(f"Ambiguous:        {len([f for f in failed if f['outcome'] == browser.SubmitOutcome.AMBIGUOUS])}")
    print(f"Submit failed:    {len([f for f in failed if f['outcome'] == browser.SubmitOutcome.SUBMIT_FAILED])}")
    if args.dry_run:
        print("NOTE: This was a dry run; no data was submitted to Starfish.")
    else:
        print(f"Submission log updated: {submission_log_path}")

    # Progress report
    print("\n=== Resident Progress (this semester) ===")
    progress = data.compute_progress(residents, submission_log_details, config)
    for p in sorted(progress, key=lambda x: x['full_name']):
        status = "done this month" if p['done_this_month'] else "NOT done this month"
        print(f"  {p['full_name']:20} {p['submitted']}/{p['target']}  ({status})")
    overdue = [p for p in progress if not p['done_this_month']]
    print(f"\n{len(overdue)} resident(s) not yet checked in this month.")

if __name__ == "__main__":
    main()