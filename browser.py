import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeoutError

import data
import paths


# None = resolve at call time via paths.failure_dir() (follows LAKER_DATA_DIR).
# Tests override this with a fixed directory.
FAILURE_SCREENSHOT_DIR = None
DIALOG_CLOSE_TIMEOUT_MS = 5000
FIELD_TIMEOUT_MS = 10000
# Live 2026-10-20: the Reason combobox opened disabled and took 5.5s-31s to
# unlock — past Playwright's 30s default click timeout in the slowest case.
REASON_ENABLE_TIMEOUT_MS = 45000

# Real element roles/names confirmed live against the site (see
# docs/starfish-add-appointment-flow.md and the 2026-08-17 live discovery
# session notes below). The site is ExtJS-based with auto-generated
# ext-comp-*/ext-gen* ids — CONFIRMED UNSTABLE across sessions in practice
# (the Outcomes tab id drifted between two sessions minutes apart), so
# role/label-based locators are used everywhere one is available. The
# current When input has a normal <label for="generated-id"> association,
# so get_by_label() follows the live generated id instead of pinning one.
ROLE_NAMES = {
    "appointment_button": "Appointment",
    "student_combobox": "Student",
    "start_time_combobox": "Start Time",
    "end_time_combobox": "End Time",
    "where_combobox": "Where",
    "reason_combobox": "Reason",
    "outcomes_tab": "Outcomes",
    "speednotes_tab": "SpeedNotes",
}

def dialog(page: Page):
    """
    The Add Appointment modal's dialog container. All field lookups are
    scoped to this rather than the whole page — the page header has its own
    global "Search for Students" combobox whose accessible name also
    contains "Student", which caused a real strict-mode collision when
    field lookups weren't scoped to the dialog.
    """
    return page.get_by_role("dialog")


def when_input(page: Page):
    """
    Resolve the date control structurally, never by its generated ExtJS id.

    The input has its own calendar aria-label, so Playwright's get_by_label()
    does not follow the visible "When" label even though that label's `for`
    attribute is correct. The first input following the exact label is stable
    across the live ExtJS wrapper and generated-id churn.
    """
    label = dialog(page).locator("label").filter(has_text=re.compile(r"^\s*When\s*$"))
    return label.locator("xpath=following::input[1]")


def submit_button(page: Page):
    """
    Confirmed live: an ExtJS button rendered as a table cell, not a real
    <button>, no matching ARIA role. The page renders a duplicate top+bottom
    toolbar (both "Submit"/"Never Mind" pairs are in the DOM at once) —
    `.first` picks the functional one; scoped to the dialog so it can never
    match the page header.
    """
    return dialog(page).get_by_text("Submit", exact=True).first


def never_mind_button(page: Page):
    """
    Confirmed live 2026-09-01. The dialog renders duplicate top and bottom
    toolbar actions, so use the first exact visible-text match, mirroring
    submit_button().
    """
    return dialog(page).get_by_text("Never Mind", exact=True).first


async def dialog_is_open(page: Page) -> bool:
    """Return True when any appointment dialog is currently visible."""
    dialogs = dialog(page)
    for index in range(await dialogs.count()):
        try:
            if await dialogs.nth(index).is_visible():
                return True
        except Exception:
            continue
    return False


async def dismiss_open_dialog(
    page: Page,
    timeout: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Cancel an open appointment safely and verify that it actually closed.

    The forced-click fallback is still scoped to the dialog's exact
    "Never Mind" action. If cancellation cannot be confirmed, callers must
    stop the batch rather than letting the open modal poison the next row.
    """
    timeout = timeout or DIALOG_CLOSE_TIMEOUT_MS
    if not await dialog_is_open(page):
        return True, None

    first_error = None
    try:
        await never_mind_button(page).click(timeout=min(timeout, 3000))
        await dialog(page).wait_for(state="hidden", timeout=timeout)
        return True, None
    except Exception as exc:
        first_error = exc

    try:
        await never_mind_button(page).click(force=True, timeout=min(timeout, 2000))
        await dialog(page).wait_for(state="hidden", timeout=timeout)
        return True, None
    except Exception as exc:
        return False, f"Could not close the appointment dialog: {exc or first_error}"


async def type_and_select_role(page: Page, combobox_name: str, text: str, timeout: int = 3000):
    """
    Click a role="combobox" field within the modal dialog (matched by
    accessible name substring), type `text`, and return its role="option"
    suggestions locator after waiting for at least one to appear (or timing
    out with zero).

    Uses page.keyboard.type() rather than Locator.fill() — confirmed live
    that fill() sets the value in one shot and fires a single 'input' event,
    which never triggers Starfish's ExtJS remote combobox (it keys its AJAX
    lookup off real per-keystroke 'keyup' events). fill() reliably produced
    zero suggestions for every query; per-character typing reliably found
    the match.
    """
    combobox = dialog(page).get_by_role("combobox", name=combobox_name)
    await combobox.click()
    await page.keyboard.type(text, delay=100)
    options = page.get_by_role("option")
    try:
        await options.first.wait_for(timeout=timeout)
    except PlaywrightTimeoutError:
        pass
    return options


async def click_and_select_role(page: Page, combobox_name: str, option_text: str, timeout: int = 3000) -> bool:
    """
    Where/Reason pattern: click to open the combobox within the modal dialog
    (no typing), then click the role="option" matching `option_text`
    exactly. Never leaves the field unselected. Returns False if the option
    doesn't appear.

    Matches by visible text, not get_by_role's `name` (accessible name) —
    confirmed live that Starfish renders every option's visible text inside
    a `<span aria-hidden="true">`, so ARIA accessible-name computation
    (which excludes aria-hidden content) always returns an empty string for
    these options. A `name=` filter therefore matches zero options
    regardless of what's typed; this reliably reproduced as a false
    "not found in dropdown options" for both Where and Reason. Filtering by
    text content (which does not consult aria-hidden) is unaffected.
    """
    combobox = dialog(page).get_by_role("combobox", name=combobox_name)
    await combobox.click()
    option = page.get_by_role("option").filter(has_text=re.compile(rf"^{re.escape(option_text)}$"))
    try:
        await option.wait_for(timeout=timeout)
    except PlaywrightTimeoutError:
        return False
    await option.click()
    return True


# Every visible checkbox in the dialog with its <label for=...> text. Only
# the active tab's panel is visible, so after clicking the SpeedNotes tab
# this is exactly the SpeedNotes list — including any box Starfish added or
# pre-ticked that config doesn't know about (confirmed live 2026-10-20:
# each SpeedNote is a real <input type="checkbox" id="activity_NNNN"> with
# <label class="x-form-cb-label" for="activity_NNNN">).
_VISIBLE_CHECKBOXES_JS = """
(dlg) => [...dlg.querySelectorAll('input[type="checkbox"]')]
  .filter(i => i.getClientRects().length > 0 && getComputedStyle(i).visibility !== 'hidden')
  .map(i => {
    const label = i.id ? dlg.querySelector(`label[for="${CSS.escape(i.id)}"]`) : null;
    return {label: label ? label.textContent.trim() : '', checked: i.checked};
  })
"""


async def apply_speednotes(page: Page, topics: List[str]) -> Optional[str]:
    """
    Tick exactly `topics` on the SpeedNotes tab, then read the tab back.

    Must run after Reason is set and Reason must not change afterwards —
    Starfish: "Updating the Reason will reset all SpeedNotes." Runs for an
    empty list too, so a box nobody chose (pre-ticked default, leftover
    state) is still caught. Each topic is matched by its exact label text;
    a topic that isn't on the page fails rather than being skipped.
    Returns an error message, or None when the checked set equals `topics`.
    """
    await dialog(page).get_by_role("tab", name=ROLE_NAMES["speednotes_tab"]).click()
    for topic in topics:
        box = dialog(page).get_by_label(topic, exact=True)
        count = await box.count()
        if count != 1:
            return f"SpeedNote '{topic}' not found in Starfish ({count} matches)"
        await box.check(timeout=FIELD_TIMEOUT_MS)

    visible = await dialog(page).evaluate(_VISIBLE_CHECKBOXES_JS)
    if not visible:
        return "SpeedNotes tab showed no checkboxes"
    checked = sorted(b["label"] for b in visible if b["checked"])
    if checked != sorted(topics):
        return f"SpeedNotes mismatch: expected {sorted(topics)}, Starfish shows {checked}"
    return None


async def check_sharing_shared(page: Page) -> Optional[str]:
    """Sharing must still be on its default, Shared. Never clicked."""
    shared = dialog(page).get_by_label("Shared", exact=True)
    if await shared.count() != 1 or not await shared.is_checked():
        return "Sharing is not set to Shared"
    return None


async def check_outcome_boxes_off(page: Page) -> Optional[str]:
    """
    The two Outcomes-tab checkboxes must stay unticked: ticking "Send a
    copy of note to student" would email the student, and "Student missed
    appointment" would mark a conversation that happened as a no-show.
    Never clicked. A box that isn't on the page is fine.
    """
    for label in ("Student missed appointment", "Send a copy of note to student"):
        box = dialog(page).get_by_label(label, exact=True)
        if await box.count() and await box.first.is_checked():
            return f"'{label}' is checked"
    return None


async def _wait_until_enabled(page: Page, locator, timeout_ms: int) -> None:
    """Poll until `locator` is enabled; raises PlaywrightTimeoutError after timeout_ms."""
    waited = 0
    while not await locator.is_enabled():
        if waited >= timeout_ms:
            raise PlaywrightTimeoutError(f"still disabled after {timeout_ms}ms")
        await page.wait_for_timeout(250)
        waited += 250


def to_mdy_slash(date_str: str) -> str:
    """Convert checkins.csv's MM-DD-YYYY to the When field's confirmed accepted format, MM/DD/YYYY."""
    return date_str.replace("-", "/")


def to_12h(time_str: str) -> str:
    """Convert 24h 'HH:MM' (checkins.csv format) to Starfish's 12h combobox format, e.g. '1:00 pm'."""
    dt = datetime.strptime(time_str, "%H:%M")
    return dt.strftime("%I:%M %p").lstrip("0").lower()


# Outcome enum for submit_one
class SubmitOutcome:
    SUBMITTED = "submitted"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    SUBMIT_FAILED = "submit_failed"
    SUBMIT_UNCERTAIN = "submit_uncertain"
    NOT_ATTEMPTED = "not_attempted"

async def launch_context(config: dict) -> BrowserContext:
    """
    Attach to a real, already-running Google Chrome via CDP instead of
    launching Playwright's own bundled Chromium. Chrome must already be
    running with --remote-debugging-port and the target
    --profile-directory *before* this is called (Chrome only reads those
    flags on its first process launch — a second invocation just messages
    the existing process and the flags are ignored). See README/CLAUDE.md
    for the exact command.
    """
    playwright = await async_playwright().start()
    cdp_url = config.get("cdp_url", "http://localhost:9222")
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
    except Exception as e:
        await playwright.stop()
        raise Exception(
            f"Could not connect to Chrome at {cdp_url}. Make sure Chrome is fully quit "
            f"(all profiles/windows) and relaunched with --remote-debugging-port and "
            f"--profile-directory=\"LakerConnection\" before running this. ({e})"
        )
    # connect_over_cdp attaches to the real, already-open profile context —
    # do not close it (closing it would close all the user's tabs in that
    # profile). Only the tab we open ourselves gets closed in run_submission.
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    context._playwright = playwright
    return context

async def verify_logged_in(page: Page, config: dict) -> bool:
    """
    Check if we are logged in. Primary check is the "Open navigation menu"
    button, confirmed present post-login via codegen. Falls back to
    config.logged_in_indicator (a raw CSS selector) if that's set and the
    role-based check doesn't find anything, for flexibility if the real
    page structure differs.
    """
    try:
        await page.get_by_role("button", name="Open navigation menu").wait_for(timeout=5000)
        return True
    except PlaywrightTimeoutError:
        pass
    indicator = config.get("logged_in_indicator")
    if not indicator:
        return False
    try:
        await page.wait_for_selector(indicator, timeout=2000)
        return True
    except PlaywrightTimeoutError:
        return False

async def submit_one(
    page: Page,
    row: Dict,
    run_reason: str,
    dry_run: bool = False,
    speednotes_enabled: bool = False,
) -> Tuple[str, Optional[str]]:
    """
    Process a single checkin row.
    Returns (outcome, error_message)
    outcome: one of SubmitOutcome constants
    error_message: optional details for logging
    """
    stage = "opening the appointment dialog"
    try:
        # 1. Open Appointment modal
        # Scoped to real <button> elements with exact text: calendar cards for
        # already-booked appointments are also role="button" with aria-labels
        # like "Appointment" / "Appointment with Doe, Jane at 12:00 pm", so a
        # plain get_by_role(name=...) hits a strict-mode violation as soon as
        # the visible week has any appointments in it.
        await page.locator("button").filter(
            has_text=re.compile(rf"^\s*{ROLE_NAMES['appointment_button']}\s*$")
        ).first.click()
        await dialog(page).wait_for(state="visible", timeout=FIELD_TIMEOUT_MS)

        # 2. Student search (search by full_name per PRD FR11)
        stage = "finding the student"
        student_options = await type_and_select_role(page, ROLE_NAMES["student_combobox"], row["full_name"])
        count = await student_options.count()
        if count == 0:
            return SubmitOutcome.NOT_FOUND, f"No match for full_name '{row['full_name']}' ({row['email_or_id']})"
        elif count >= 2:
            # Disambiguate by email_or_id among the suggestions' text
            # (option text format confirmed as "Lastname, Firstname username").
            matched = None
            for i in range(count):
                text = await student_options.nth(i).text_content()
                if text and row["email_or_id"].lower() in text.lower():
                    if matched is not None:
                        matched = None
                        break
                    matched = i
            if matched is None:
                return SubmitOutcome.AMBIGUOUS, f"Ambiguous match for '{row['full_name']}' ({row['email_or_id']}, {count} suggestions)"
            await student_options.nth(matched).click()
        else:
            await student_options.first.click()

        # 3. When / Start Time / End Time
        # Confirmed live: the visible "When" label points at the current
        # generated input id. Following that association survives ExtJS id
        # churn while preserving direct MM/DD/YYYY entry.
        stage = "filling the When date"
        await when_input(page).fill(to_mdy_slash(row["date"]), timeout=FIELD_TIMEOUT_MS)
        start_dt = data.parse_datetime(row["date"], row["start_time"])
        end_dt = start_dt + timedelta(minutes=row["duration_minutes"])
        end_time_12h = to_12h(end_dt.strftime("%H:%M"))
        start_time_12h = to_12h(row["start_time"])

        stage = "selecting the start time"
        start_options = await type_and_select_role(page, ROLE_NAMES["start_time_combobox"], start_time_12h)
        if await start_options.count() == 0:
            return SubmitOutcome.SUBMIT_FAILED, f"Start Time '{start_time_12h}' not found in dropdown suggestions"
        await start_options.first.click()

        stage = "selecting the end time"
        end_options = await type_and_select_role(page, ROLE_NAMES["end_time_combobox"], end_time_12h)
        if await end_options.count() == 0:
            return SubmitOutcome.SUBMIT_FAILED, f"End Time '{end_time_12h}' not found in dropdown suggestions"
        await end_options.first.click()

        # 4. Where (location) — click-to-open dropdown, no typing; must select from options
        stage = "selecting the location"
        location = row.get("effective_location", row["location"])  # fallback to original location if not set
        if not await click_and_select_role(page, ROLE_NAMES["where_combobox"], location):
            return SubmitOutcome.SUBMIT_FAILED, f"Where value '{location}' not found in dropdown options"

        # 5. Reason (from batch prompt) — click-to-open dropdown, no typing; must select from options
        stage = "selecting the reason"
        reason_box = dialog(page).get_by_role("combobox", name=ROLE_NAMES["reason_combobox"])
        await reason_box.wait_for(state="visible", timeout=FIELD_TIMEOUT_MS)
        await _wait_until_enabled(page, reason_box, REASON_ENABLE_TIMEOUT_MS)
        if not await click_and_select_role(page, ROLE_NAMES["reason_combobox"], run_reason):
            return SubmitOutcome.SUBMIT_FAILED, f"Reason value '{run_reason}' not found in dropdown options"

        # 6. Course: leave blank, never touched
        # 7. Sharing: must still be on its default (Shared), never clicked
        stage = "checking Sharing"
        sharing_error = await check_sharing_shared(page)
        if sharing_error:
            return SubmitOutcome.SUBMIT_FAILED, sharing_error

        # 8. Switch to Outcomes tab and fill notes.
        #
        # Confirmed live 2026-09-01: the notes field ("Detailed
        # Description") is now an ExtJS HtmlEditor — a design-mode iframe
        # (document.designMode = "on"), not a plain <textarea> as confirmed
        # 2026-08-17 and not a `contenteditable`-attribute element either
        # (designMode makes the whole document editable without setting
        # that attribute, so a `[contenteditable]` search finds nothing).
        # Starfish appears to have shipped this change sometime between
        # 2026-08-17 and today — see docs/starfish-add-appointment-flow.md.
        # There is exactly one iframe in the dialog; its own id/name are
        # ExtJS-autogenerated and churn session-to-session same as every
        # other raw id here, so it's matched structurally
        # ('[role="dialog"] iframe') rather than by name. fill() does not
        # work on it (not a form control) — click to focus, then real
        # keystrokes, same pattern as the typeahead fields above.
        stage = "filling the notes"
        await dialog(page).get_by_role("tab", name=ROLE_NAMES["outcomes_tab"]).click()
        notes_body = page.frame_locator('[role="dialog"] iframe').locator("body")
        await notes_body.click()
        await page.keyboard.type(row["notes"], delay=20)

        stage = "checking the Outcomes checkboxes"
        outcome_error = await check_outcome_boxes_off(page)
        if outcome_error:
            return SubmitOutcome.SUBMIT_FAILED, outcome_error

        # 9. SpeedNotes — after Reason (changing Reason resets them), and for
        # an empty selection too, so an unexpected pre-ticked box is caught.
        if speednotes_enabled:
            stage = "ticking SpeedNotes"
            speednotes_error = await apply_speednotes(page, row.get("effective_speednotes") or [])
            if speednotes_error:
                return SubmitOutcome.SUBMIT_FAILED, speednotes_error

        # 10. Submit or Never Mind (dry run)
        if dry_run:
            stage = "closing the dry run"
            closed, close_error = await dismiss_open_dialog(page)
            if not closed:
                return SubmitOutcome.SUBMIT_FAILED, close_error
            return SubmitOutcome.SUBMITTED, None
        else:
            stage = "submitting the appointment"
            await submit_button(page).click()
            # No error/success banner selector has been confirmed live (never
            # triggered a real error to observe one, by design — see PRD).
            # Instead: the modal closing is the success signal — standard
            # behavior for this kind of form, and confirmable without ever
            # needing to force a real error. If the dialog is still open
            # after a few seconds, something went wrong; grab whatever text
            # is visible in it as the best available error message.
            try:
                await dialog(page).wait_for(state="hidden", timeout=5000)
                return SubmitOutcome.SUBMITTED, None
            except PlaywrightTimeoutError:
                return (
                    SubmitOutcome.SUBMIT_UNCERTAIN,
                    "Submit was clicked, but the dialog did not close. The appointment may or may not "
                    "have been created; verify it in Starfish before retrying.",
                )
    except Exception as e:
        return SubmitOutcome.SUBMIT_FAILED, f"Failed while {stage}: {str(e)[:900]}"


async def _capture_failure_screenshot(page: Page, row: Dict) -> Optional[str]:
    """Capture the still-open failure state and return a safe filename."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_eid = re.sub(r"[^A-Za-z0-9_-]+", "_", row["email_or_id"])
    safe_date = re.sub(r"[^0-9-]+", "_", row["date"])
    safe_time = re.sub(r"[^0-9-]+", "_", row["start_time"])
    filename = f"{timestamp}_{safe_eid}_{safe_date}_{safe_time}.png"
    failure_dir = FAILURE_SCREENSHOT_DIR or paths.failure_dir()
    os.makedirs(failure_dir, exist_ok=True)
    try:
        await page.screenshot(path=os.path.join(failure_dir, filename))
        return filename
    except Exception:
        return None


def _not_attempted(row: Dict, reason: str) -> Dict:
    return {**row, "outcome": SubmitOutcome.NOT_ATTEMPTED, "error": reason, "screenshot": None}


async def run_rows(
    page: Page,
    selected_rows: List[Dict],
    run_reason: str,
    dry_run: bool = False,
    on_row=None,
    speednotes_enabled: bool = False,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Process rows on an already-open appointments page.

    Every row starts and ends with a closed-dialog invariant. A pre-submit
    failure is screenshotted, cancelled, and the next row may proceed. If
    cancellation fails—or Submit was clicked but its result is uncertain—the
    batch stops and remaining rows are explicitly marked not attempted.
    """
    submitted = []
    failed = []
    total = len(selected_rows)

    for offset, row in enumerate(selected_rows):
        idx = offset + 1
        if await dialog_is_open(page):
            clean, clean_error = await dismiss_open_dialog(page)
            if not clean:
                reason = (
                    "Not attempted because an existing appointment dialog could not be closed. "
                    f"{clean_error or ''}"
                ).strip()
                for remaining_index, remaining in enumerate(selected_rows[offset:], start=idx):
                    entry = _not_attempted(remaining, reason)
                    failed.append(entry)
                    if on_row:
                        on_row("failed", remaining_index, total, remaining,
                               outcome=entry["outcome"], error=entry["error"])
                break

        if on_row:
            on_row("start", idx, total, row)

        outcome, error = await submit_one(
            page, row, run_reason, dry_run=dry_run, speednotes_enabled=speednotes_enabled)
        if outcome == SubmitOutcome.SUBMITTED:
            submitted.append({**row, "outcome": outcome, "error": error, "reason": run_reason})
            if on_row:
                on_row("success", idx, total, row)
            continue

        entry = {**row, "outcome": outcome, "error": error}
        entry["screenshot"] = await _capture_failure_screenshot(page, row)
        failed.append(entry)
        if on_row:
            on_row("failed", idx, total, row, outcome=outcome, error=error)

        if outcome == SubmitOutcome.SUBMIT_UNCERTAIN:
            reason = "Not attempted because the previous Submit result must be verified in Starfish first."
            for remaining_index, remaining in enumerate(selected_rows[offset + 1:], start=idx + 1):
                remaining_entry = _not_attempted(remaining, reason)
                failed.append(remaining_entry)
                if on_row:
                    on_row("failed", remaining_index, total, remaining,
                           outcome=remaining_entry["outcome"], error=remaining_entry["error"])
            break

        clean, clean_error = await dismiss_open_dialog(page)
        if not clean:
            entry["error"] = (
                f"{entry['error']} Recovery also failed: {clean_error}. "
                "The rest of the batch was not attempted."
            )
            reason = "Not attempted because the previous appointment dialog could not be closed safely."
            for remaining_index, remaining in enumerate(selected_rows[offset + 1:], start=idx + 1):
                remaining_entry = _not_attempted(remaining, reason)
                failed.append(remaining_entry)
                if on_row:
                    on_row("failed", remaining_index, total, remaining,
                           outcome=remaining_entry["outcome"], error=remaining_entry["error"])
            break

    return submitted, failed


async def run_submission(
    selected_rows: List[Dict],
    run_reason: str,
    config: dict,
    dry_run: bool = False,
    on_row=None,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Launch browser, verify login, process each selected row.
    Returns (submitted_rows, failed_rows) where each element is a dict with row and outcome.

    `on_row`, if given, is called as on_row(event, idx, total, row, outcome=None, error=None)
    with event in {"start", "success", "failed"} — lets a caller (e.g. main.py's CLI)
    print live per-row progress without duplicating this loop.
    """
    context = await launch_context(config)
    page = None
    try:
        page = await context.new_page()
        await page.goto(config["appointments_url"])
        await page.wait_for_load_state("domcontentloaded")
        # NOTE: was "networkidle" — confirmed live this Starfish app never
        # goes network-idle (ExtJS background polling keeps requests in
        # flight), which reliably ate the full 30s default timeout and
        # surfaced as an opaque "Timeout 30000ms exceeded." with no further
        # detail. domcontentloaded plus verify_logged_in's own explicit
        # element wait below is the real readiness signal.

        # Starfish sometimes redirects a fresh navigation to an "Advisor
        # Wizard" onboarding interstitial instead of landing on the
        # appointments page directly — confirmed live 2026-08-17. Dismiss it
        # and re-navigate if so.
        if "advisorWizard" in page.url:
            close_btn = page.get_by_role("button", name="Close")
            if await close_btn.count() > 0:
                await close_btn.first.click()
                await page.wait_for_timeout(1000)
            await page.goto(config["appointments_url"])
            await page.wait_for_load_state("domcontentloaded")

        # Verify logged in
        if not await verify_logged_in(page, config):
            raise Exception("Not logged in to Starfish. Please log in manually via the LakerConnection Chrome profile.")

        return await run_rows(
            page,
            selected_rows,
            run_reason,
            dry_run=dry_run,
            on_row=on_row,
            speednotes_enabled=bool(data.speednote_topics(config)),
        )
    finally:
        # Only close the tab we opened — context is the user's real, live
        # Chrome profile (see launch_context); closing it would close every
        # tab they have open in that profile.
        if page is not None:
            await page.close()
        await context._playwright.stop()


if __name__ == "__main__":
    # Simple test: launch and verify login (requires config and logged-in profile)
    import sys
    import json
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    with open(config_path) as f:
        config = json.load(f)
    import asyncio
    async def main():
        context = await launch_context(config)
        page = await context.new_page()
        await page.goto(config["appointments_url"])
        logged_in = await verify_logged_in(page, config)
        print("Logged in:", logged_in)
        await page.close()
        await context._playwright.stop()
    asyncio.run(main())
