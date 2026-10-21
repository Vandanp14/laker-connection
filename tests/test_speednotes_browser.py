"""
SpeedNotes step and Starfish-modal guards, driven against
mocks/speednotes_dialog.html (shaped after the live DOM captured
2026-10-20). Never touches the real site.
"""
import os
from urllib.parse import urlencode

import pytest
import pytest_asyncio

import browser


MOCK_HTML = os.path.join(os.path.dirname(__file__), "..", "mocks", "speednotes_dialog.html")


def mock_url(**params):
    query = urlencode(params, doseq=True)
    return f"file://{os.path.abspath(MOCK_HTML)}" + (f"?{query}" if query else "")


@pytest_asyncio.fixture
async def open_page():
    playwright = await browser.async_playwright().start()
    chromium = await playwright.chromium.launch(headless=True)
    context = await chromium.new_context()
    page = await context.new_page()

    async def _open(**params):
        await page.goto(mock_url(**params))
        return page

    yield _open
    await context.close()
    await chromium.close()
    await playwright.stop()


async def checked_topics(page):
    return await page.evaluate(
        "() => [...document.querySelectorAll('#speednote-list input:checked')]"
        ".map(i => document.querySelector(`label[for='${i.id}']`).textContent)"
    )


@pytest.mark.asyncio
async def test_apply_ticks_exact_set(open_page):
    page = await open_page()
    chosen = ["Wellbeing - Kudos/Check-in", "Roommate Relationship"]
    error = await browser.apply_speednotes(page, chosen)
    assert error is None
    assert sorted(await checked_topics(page)) == sorted(chosen)


@pytest.mark.asyncio
async def test_apply_empty_verifies_none_ticked(open_page):
    page = await open_page()
    assert await browser.apply_speednotes(page, []) is None
    assert await checked_topics(page) == []


@pytest.mark.asyncio
async def test_apply_empty_catches_pre_ticked_box(open_page):
    page = await open_page(preticked="Break Plans")
    error = await browser.apply_speednotes(page, [])
    assert error is not None and "Break Plans" in error


@pytest.mark.asyncio
async def test_apply_pre_ticked_extra_fails(open_page):
    page = await open_page(preticked="Break Plans")
    error = await browser.apply_speednotes(page, ["Room Selection"])
    assert error is not None
    assert "Break Plans" in error


@pytest.mark.asyncio
async def test_apply_unknown_topic_fails(open_page):
    page = await open_page(drop="Room Selection")
    error = await browser.apply_speednotes(page, ["Room Selection"])
    assert error is not None and "Room Selection" in error
    assert await checked_topics(page) == []


@pytest.mark.asyncio
async def test_apply_already_ticked_chosen_box_stays_ticked(open_page):
    page = await open_page(preticked="Room Selection")
    assert await browser.apply_speednotes(page, ["Room Selection"]) is None
    assert await checked_topics(page) == ["Room Selection"]


@pytest.mark.asyncio
async def test_guard_shared_ok(open_page):
    page = await open_page()
    assert await browser.check_sharing_shared(page) is None


@pytest.mark.asyncio
async def test_guard_private_fails(open_page):
    page = await open_page(shared="0")
    error = await browser.check_sharing_shared(page)
    assert error is not None and "Shared" in error


@pytest.mark.asyncio
async def test_outcome_boxes_unchecked_ok(open_page):
    page = await open_page()
    assert await browser.check_outcome_boxes_off(page) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("param,label", [
    ("missed", "Student missed appointment"),
    ("sendcopy", "Send a copy of note to student"),
])
async def test_outcome_box_checked_fails(open_page, param, label):
    page = await open_page(**{param: "1"})
    error = await browser.check_outcome_boxes_off(page)
    assert error is not None and label in error
