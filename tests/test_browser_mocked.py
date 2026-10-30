import os

import pytest
import pytest_asyncio

import browser


MOCK_HTML = os.path.join(os.path.dirname(__file__), "..", "mocks", "add_appointment.html")
MOCK_URL = f"file://{os.path.abspath(MOCK_HTML)}"


def row(name="Test User", email="test@example.com", start_time="10:00"):
    return {
        "full_name": name,
        "email_or_id": email,
        "date": "01-15-2026",
        "start_time": start_time,
        "duration_minutes": 15,
        "location": "Residence Hall",
        "notes": "Test notes",
        "_original_index": 0,
    }


@pytest_asyncio.fixture
async def page():
    playwright = await browser.async_playwright().start()
    chromium = await playwright.chromium.launch(headless=True)
    context = await chromium.new_context()
    page = await context.new_page()
    await page.goto(MOCK_URL)
    yield page
    await context.close()
    await chromium.close()
    await playwright.stop()


@pytest.fixture
def fast_timeouts(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "FIELD_TIMEOUT_MS", 350)
    monkeypatch.setattr(browser, "DIALOG_CLOSE_TIMEOUT_MS", 350)
    monkeypatch.setattr(browser, "FAILURE_SCREENSHOT_DIR", str(tmp_path / "failures"))


@pytest.mark.asyncio
async def test_when_input_follows_dynamic_label_id(page):
    await page.get_by_role("button", name="Appointment").click()
    date_input = browser.when_input(page)
    assert await date_input.get_attribute("id") != "ext-comp-1068"
    await date_input.fill("01/15/2026")
    assert await date_input.input_value() == "01/15/2026"
    closed, error = await browser.dismiss_open_dialog(page)
    assert closed is True
    assert error is None


@pytest.mark.asyncio
async def test_not_found_is_cancelled_before_batch_returns(page, fast_timeouts):
    submitted, failed = await browser.run_rows(
        page,
        [row("Nonexistent Person", "none@example.com")],
        "Test Reason",
        dry_run=True,
    )
    assert submitted == []
    assert failed[0]["outcome"] == browser.SubmitOutcome.NOT_FOUND
    assert await browser.dialog_is_open(page) is False


@pytest.mark.asyncio
async def test_date_failure_does_not_poison_the_next_row(page, fast_timeouts):
    first = row("Break Date", "break@example.com")
    second = row("Test User", "test@example.com", "10:15")

    submitted, failed = await browser.run_rows(
        page,
        [first, second],
        "Test Reason",
        dry_run=True,
    )

    assert [entry["full_name"] for entry in submitted] == ["Test User"]
    assert len(failed) == 1
    assert failed[0]["full_name"] == "Break Date"
    assert failed[0]["outcome"] == browser.SubmitOutcome.SUBMIT_FAILED
    assert "When date" in failed[0]["error"]
    assert failed[0]["screenshot"]
    assert await browser.dialog_is_open(page) is False


@pytest.mark.asyncio
async def test_cleanup_failure_stops_batch_and_marks_remaining_not_attempted(page, fast_timeouts):
    await page.evaluate("window.mockCancelBroken = true")
    first = row("Break Date", "break@example.com")
    second = row("Test User", "test@example.com", "10:15")

    submitted, failed = await browser.run_rows(
        page,
        [first, second],
        "Test Reason",
        dry_run=True,
    )

    assert submitted == []
    assert failed[0]["outcome"] == browser.SubmitOutcome.SUBMIT_FAILED
    assert "Recovery also failed" in failed[0]["error"]
    assert failed[1]["outcome"] == browser.SubmitOutcome.NOT_ATTEMPTED
    assert await browser.dialog_is_open(page) is True

    await page.evaluate("window.mockCancelBroken = false")
    closed, _ = await browser.dismiss_open_dialog(page)
    assert closed is True


@pytest.mark.asyncio
async def test_dry_run_closes_without_clicking_submit(page, fast_timeouts):
    submitted, failed = await browser.run_rows(
        page,
        [row()],
        "Test Reason",
        dry_run=True,
    )
    assert len(submitted) == 1
    assert failed == []
    assert await page.locator("#appointment-modal").get_attribute("data-submit-clicked") == "false"
    assert await browser.dialog_is_open(page) is False


@pytest.mark.asyncio
async def test_uncertain_submit_stops_following_rows(page, fast_timeouts):
    await page.evaluate("window.mockSubmitStuck = true")
    first = row()
    second = row("Second User", "second@example.com", "10:15")

    submitted, failed = await browser.run_rows(
        page,
        [first, second],
        "Test Reason",
        dry_run=False,
    )

    assert submitted == []
    assert failed[0]["outcome"] == browser.SubmitOutcome.SUBMIT_UNCERTAIN
    assert failed[1]["outcome"] == browser.SubmitOutcome.NOT_ATTEMPTED
    assert "verify it in Starfish" in failed[0]["error"]

    await page.evaluate("window.mockSubmitStuck = false")
    closed, _ = await browser.dismiss_open_dialog(page)
    assert closed is True


# ---------- SpeedNotes + guards (submit_one wiring) ----------

async def speednotes_checked(page):
    return await page.evaluate(
        "() => [...document.querySelectorAll('#speednotes-panel input:checked')]"
        ".map(i => document.querySelector(`label[for='${i.id}']`).textContent)"
    )


async def submit_clicked(page):
    return await page.locator("#appointment-modal").get_attribute("data-submit-clicked")


@pytest.mark.asyncio
async def test_speednotes_ticked_then_submitted(page, fast_timeouts):
    r = {**row(), "effective_speednotes": ["Room Selection"]}
    submitted, failed = await browser.run_rows(page, [r], "Test Reason", dry_run=False, speednotes_enabled=True)
    assert failed == []
    assert len(submitted) == 1
    assert await submit_clicked(page) == "true"
    assert await speednotes_checked(page) == ["Room Selection"]


@pytest.mark.asyncio
async def test_pre_ticked_speednote_blocks_submit_even_with_no_topics(page, fast_timeouts):
    await page.evaluate("window.mockPreTick = 'Break Plans'")
    r = {**row(), "effective_speednotes": []}
    submitted, failed = await browser.run_rows(page, [r], "Test Reason", dry_run=False, speednotes_enabled=True)
    assert submitted == []
    assert failed[0]["outcome"] == browser.SubmitOutcome.SUBMIT_FAILED
    assert "Break Plans" in failed[0]["error"]
    assert await submit_clicked(page) == "false"


@pytest.mark.asyncio
async def test_unknown_speednote_blocks_submit(page, fast_timeouts):
    r = {**row(), "effective_speednotes": ["Campus Involvement"]}
    submitted, failed = await browser.run_rows(page, [r], "Test Reason", dry_run=False, speednotes_enabled=True)
    assert submitted == []
    assert "Campus Involvement" in failed[0]["error"]
    assert await submit_clicked(page) == "false"


@pytest.mark.asyncio
async def test_speednotes_feature_off_leaves_tab_alone(page, fast_timeouts):
    r = {**row(), "effective_speednotes": ["Room Selection"]}
    submitted, failed = await browser.run_rows(page, [r], "Test Reason", dry_run=False)
    assert len(submitted) == 1
    assert await speednotes_checked(page) == []


@pytest.mark.asyncio
async def test_private_sharing_blocks_submit(page, fast_timeouts):
    await page.evaluate("window.mockPrivate = true")
    submitted, failed = await browser.run_rows(page, [row()], "Test Reason", dry_run=False)
    assert submitted == []
    assert "Shared" in failed[0]["error"]
    assert await submit_clicked(page) == "false"


@pytest.mark.asyncio
async def test_send_copy_checked_blocks_submit(page, fast_timeouts):
    await page.evaluate("window.mockSendCopy = true")
    submitted, failed = await browser.run_rows(page, [row()], "Test Reason", dry_run=False)
    assert submitted == []
    assert "Send a copy of note to student" in failed[0]["error"]
    assert await submit_clicked(page) == "false"


@pytest.mark.asyncio
async def test_reason_waits_past_default_timeout_until_enabled(page, fast_timeouts, monkeypatch):
    # Live 2026-10-20: Reason took up to 31s to unlock — longer than
    # Playwright's 30s default action timeout. The explicit wait must
    # outlast the page default.
    page.set_default_timeout(400)
    monkeypatch.setattr(browser, "REASON_ENABLE_TIMEOUT_MS", 5000)
    await page.evaluate("window.mockReasonDelayMs = 1200")
    submitted, failed = await browser.run_rows(page, [row()], "Test Reason", dry_run=True)
    assert failed == []
    assert len(submitted) == 1


async def _open_filled_dialog(page, reason_delay_ms=0):
    await page.evaluate(f"window.mockReasonDelayMs = {reason_delay_ms}")
    await page.get_by_role("button", name="Appointment").click()
    await browser.when_input(page).fill("01/15/2026")
    await page.evaluate("""() => {
        document.getElementById('student-input').value = 'Test User';
        document.getElementById('start-input').value = '10:00 am';
        document.getElementById('end-input').value = '10:15 am';
        document.getElementById('where-input').value = 'Residence Hall';
    }""")


@pytest.mark.asyncio
async def test_snapshot_reads_filled_form(page):
    await _open_filled_dialog(page, reason_delay_ms=10**7)
    snap = await browser.snapshot_dialog(page)
    assert snap["dialog_open"] is True
    assert snap["student"] == "Test User"
    assert snap["when"] == "01/15/2026"
    assert snap["start_time"] == "10:00 am" and snap["end_time"] == "10:15 am"
    assert snap["where"] == "Residence Hall"
    assert snap["reason"] == ""
    assert snap["reason_disabled"] is True
    assert snap["reason_options"] == -1
    assert "?" not in snap["url_path"]


@pytest.mark.asyncio
async def test_snapshot_without_dialog_never_raises(page):
    snap = await browser.snapshot_dialog(page)
    assert snap["dialog_open"] is False
    for key in ("student", "when", "start_time", "end_time", "where", "reason"):
        assert snap[key] == ""


@pytest.mark.parametrize("exc,outcome,expected", [
    (browser.PlaywrightTimeoutError("x"), "submit_failed", "timeout"),
    (None, "not_found", "not_found"),
    (None, "ambiguous", "ambiguous"),
    (None, "submit_uncertain", "uncertain"),
    (None, "not_attempted", "not_attempted"),
    (ValueError("Jane Doe"), "submit_failed", "ValueError"),
    (type("Weird-Name", (Exception,), {})(), "submit_failed", "other"),
    (None, "submit_failed", "other"),
])
def test_kind_for_maps_outcomes_and_exceptions(exc, outcome, expected):
    assert browser.kind_for(exc, outcome) == expected


@pytest.mark.asyncio
async def test_reason_never_unlocks_diag(page, fast_timeouts, monkeypatch):
    monkeypatch.setattr(browser, "REASON_ENABLE_TIMEOUT_MS", 500)
    await page.evaluate("window.mockReasonDelayMs = 10000000")
    submitted, failed = await browser.run_rows(page, [row()], "Test Reason", dry_run=True)
    assert submitted == []
    diag = failed[0]["diag"]
    assert diag["stage"] == "reason" and diag["kind"] == "timeout"
    assert diag["reason_disabled"] is True
    assert diag["where"] == "Residence Hall"
    assert diag["student"] and diag["when"] == "01/15/2026"
    assert diag["waited_ms"] >= 500 and diag["elapsed_ms"] >= diag["waited_ms"]


@pytest.mark.asyncio
async def test_readback_mismatch_fails_at_where(page, fast_timeouts):
    await page.evaluate("window.mockWhereIgnoresClick = true")
    submitted, failed = await browser.run_rows(page, [row()], "Test Reason", dry_run=True)
    assert failed[0]["outcome"] == browser.SubmitOutcome.SUBMIT_FAILED
    assert "Where shows" in failed[0]["error"]
    assert failed[0]["diag"]["stage"] == "where" and failed[0]["diag"]["kind"] == "mismatch"


@pytest.mark.asyncio
async def test_readback_is_case_and_space_insensitive(page):
    await _open_filled_dialog(page)
    await page.evaluate("document.getElementById('start-input').value = ' 10:00 AM '")
    box = browser.dialog(page).get_by_role("combobox", name="Start Time")
    assert await browser._confirm_value(box, "Start Time", "10:00 am") is None
    assert "Start Time shows" in await browser._confirm_value(box, "Start Time", "11:00 am")


@pytest.mark.asyncio
async def test_success_records_reason_enable_ms(page, fast_timeouts):
    # Typing Student/times takes seconds, so a short mock lock has already
    # expired by the time Reason is reached; only check the timing is recorded.
    submitted, failed = await browser.run_rows(page, [row()], "Test Reason", dry_run=True)
    assert failed == []
    assert isinstance(submitted[0]["diag"]["reason_enable_ms"], int)
    assert submitted[0]["diag"]["reason_enable_ms"] >= 0


@pytest.mark.asyncio
async def test_readback_unreadable_field_does_not_fail(page):
    """If the combobox role sits on a non-input element, input_value() throws:
    read-back can't verify, so it must let the row continue, not call it a mismatch."""
    await _open_filled_dialog(page)
    not_an_input = browser.dialog(page).get_by_role("tab", name="Outcomes")
    assert await browser._confirm_value(not_an_input, "Start Time", "10:00 am") is None


@pytest.mark.asyncio
async def test_readback_waits_for_late_update(page):
    await _open_filled_dialog(page)
    await page.evaluate("""() => {
        const box = document.getElementById('start-input');
        box.value = '';
        setTimeout(() => { box.value = '10:00 am'; }, 600);
    }""")
    box = browser.dialog(page).get_by_role("combobox", name="Start Time")
    assert await browser._confirm_value(box, "Start Time", "10:00 am") is None
