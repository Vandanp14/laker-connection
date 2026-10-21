import sys
from typing import List, Tuple, Dict
import data

def prompt_for_selection(
    residents_path: str,
    checkins_path: str,
    submission_log_path: str,
    config: Dict
) -> Tuple[List[Dict], str, str]:
    """
    Interactively prompt the user to select which checkin rows to submit.
    Returns:
        selected_rows: list of checkin dicts with an added 'effective_location' key
        reason: the reason string entered by the user (applies to all selected rows)
        default_location: the batch default location entered by the user
    """
    # Load data
    residents = data.load_residents(residents_path)
    checkins = data.load_checkins(checkins_path)
    submission_log = data.load_submission_log(submission_log_path)

    checkins, resolution_errors = data.resolve_checkins(checkins, residents, submission_log, config)
    if resolution_errors:
        print("Resolution errors found:")
        for err in resolution_errors:
            print(f"  - {err}")
        sys.exit(1)

    candidates = data.compute_candidates(checkins, submission_log)

    if not candidates:
        print("No new checkins to submit.")
        sys.exit(0)

    valid_reasons = config.get('valid_reasons') or []
    valid_locations = config.get('valid_locations') or []
    default_location = config.get('default_location', '')

    # Step 1: get batch reason — must be one of the exact Starfish dropdown values
    if valid_reasons:
        print("\nValid reasons for this batch:")
        for r in valid_reasons:
            print(f"  - {r}")
        reason = input("Enter reason for this batch (must match exactly): ").strip()
        while reason not in valid_reasons:
            reason = input(f"Reason must be one of {valid_reasons}. Enter reason: ").strip()
    else:
        reason = input("Enter reason for this batch (e.g., 'Laker Connection #2 > FALL'): ").strip()
        while not reason:
            reason = input("Reason cannot be empty. Please enter reason: ").strip()

    # Step 2: get batch default location — blank keeps config's default_location
    prompt_suffix = f" (blank = default '{default_location}')" if default_location else ""
    default_loc = input(f"Enter default location for this batch{prompt_suffix}: ").strip()
    if not default_loc:
        if not default_location:
            while not default_loc:
                default_loc = input("Location cannot be empty. Please enter default location: ").strip()
        else:
            default_loc = default_location
    if valid_locations:
        while default_loc not in valid_locations:
            default_loc = input(f"Location must be one of {valid_locations}. Enter default location: ").strip()

    print("\nCandidates (new checkins not yet submitted):")
    print("Idx | Name                | Date       | Time   | Duration | Location")
    print("-" * 65)
    for i, row in enumerate(candidates):
        name = row['full_name']
        # Truncate name to 18 chars for display
        name_disp = (name[:15] + '...') if len(name) > 18 else name.ljust(18)
        print(f"{i+1:3} | {name_disp} | {row['date']} | {row['start_time']} | {row['duration_minutes']:8} | {row['location']}")

    print("\nFor each candidate, confirm selection (Y/n). If 'n', you can override location.")
    selected = []
    for i, row in enumerate(candidates):
        # Default to Y
        ans = input(f"Select row {i+1}? [{row['email_or_id']} {row['date']} {row['start_time']}] (Y/n): ").strip().lower()
        if ans == 'n':
            # Not selected
            continue
        # Selected; ask for location override
        loc_override = input(f"  Location for this row (default: '{default_loc}', enter to keep): ").strip()
        if valid_locations:
            while loc_override and loc_override not in valid_locations:
                loc_override = input(f"  Location must be one of {valid_locations} (or blank to keep default): ").strip()
        effective_loc = loc_override if loc_override else default_loc
        # Add effective location to the row dict (we'll copy to avoid mutating original?)
        selected_row = row.copy()
        selected_row['effective_location'] = effective_loc
        selected.append(selected_row)

    if not selected:
        print("No rows selected. Exiting.")
        sys.exit(0)

    # Show summary
    print("\nSummary of selected rows:")
    print("Idx | Name                | Date       | Time   | Duration | Location (eff.)")
    print("-" * 68)
    for i, row in enumerate(selected):
        name = row['full_name']
        name_disp = (name[:15] + '...') if len(name) > 18 else name.ljust(18)
        print(f"{i+1:3} | {name_disp} | {row['date']} | {row['start_time']} | {row['duration_minutes']:8} | {row['effective_location']}")

    # Final confirmation
    confirm = input(f"\nSubmit these {len(selected)} rows? (yes/no): ").strip().lower()
    if confirm not in ('yes', 'y'):
        print("Submission cancelled.")
        sys.exit(0)

    return selected, reason, default_loc


if __name__ == "__main__":
    # For testing: use default file names
    _config = data.load_config("config.json")
    selected, reason, default_loc = prompt_for_selection(
        "residents.csv",
        "checkins.csv",
        "submission_log.csv",
        _config
    )
    print("\nSelected rows:", len(selected))
    print("Reason:", reason)
    print("Default location:", default_loc)