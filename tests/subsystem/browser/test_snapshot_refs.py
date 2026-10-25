"""The page as the agent reads it (N12-A): a short outline whose refs stay the same.

Refs survive re-renders and new documents (Chromium renumbers its own), name the same control
when the page shows it again, and a ref that's gone says what it was. Layout noise is dropped
and the outline is cut at a size a model can afford.
"""

from __future__ import annotations

import pytest

from row_bot.browser.snapshot import RefMap, compact

pytestmark = pytest.mark.subsystem

STEP_ONE = """- document [ref=e1]:
  - generic [active] [ref=e2]:
    - heading "What's the problem?" [level=1] [ref=e3]
    - generic [ref=e4]:
      - radio "Leak" [ref=e5]
      - text: Leak
      - radio "Boiler not working" [ref=e6]
      - button "Next" [ref=e8] [cursor=pointer]
      - link "Help" [ref=e9] [cursor=pointer]:
        - /url: /help
      - generic [ref=e10] [cursor=pointer]: Chat with us
    - iframe [ref=e11]:
      - button "Accept cookies" [ref=f1e2]"""


def test_the_outline_keeps_what_the_agent_acts_on_and_drops_layout():
    text, controls = compact(STEP_ONE, RefMap())

    assert text.splitlines() == [
        '- heading "What\'s the problem?" [level=1]',
        '- radio "Leak" [ref=e1]',
        "- text: Leak",
        '- radio "Boiler not working" [ref=e2]',
        '- button "Next" [ref=e3]',
        '- link "Help" [ref=e4]:',
        "  - /url: /help",
        "- generic [ref=e5]: Chat with us",  # Clickable, so it keeps a ref.
        "- iframe:",
        '  - button "Accept cookies" [ref=e6]',
    ]
    assert controls == 6


def test_a_ref_stays_through_a_re_render_and_a_new_document():
    refs = RefMap()
    compact(STEP_ONE, refs)
    # The page re-rendered the button (Chromium gave it a new ref), then a new document renumbered frames.
    re_rendered = STEP_ONE.replace('button "Next" [ref=e8]', 'button "Next" [ref=e14]')
    text, _ = compact(re_rendered, refs)
    assert '- button "Next" [ref=e3]' in text and refs.page_ref("e3") == "e14"

    next_page = '- main [ref=f1e1]:\n  - combobox "Room" [ref=f1e4]\n  - button "Next" [ref=f1e5] [cursor=pointer]'
    text, _ = compact(next_page, refs)
    assert text.splitlines() == ["- main:", '  - combobox "Room" [ref=e7]', '  - button "Next" [ref=e3]']
    assert refs.page_ref("e3") == "f1e5"


def test_a_gone_control_is_known_by_what_it_was_and_resolves_to_nothing():
    refs = RefMap()
    compact(STEP_ONE, refs)
    compact('- button "Done" [ref=e20]', refs)

    assert refs.page_ref("e1") is None
    assert refs.describe("e1") == ("radio", "Leak")
    assert refs.page_ref("e99") is None and refs.describe("e99") is None


def test_controls_with_the_same_name_keep_their_own_refs_in_page_order():
    refs = RefMap()
    page = '- button "Add" [ref=e1]\n- button "Add" [ref=e2]'
    text, _ = compact(page, refs)
    assert text.splitlines() == ['- button "Add" [ref=e1]', '- button "Add" [ref=e2]']
    compact(page.replace("e1", "e7").replace("e2", "e8"), refs)
    assert (refs.page_ref("e1"), refs.page_ref("e2")) == ("e7", "e8")


def test_a_long_page_is_cut_with_a_way_to_read_the_rest():
    raw = "\n".join(f'- link "Result {n}" [ref=e{n}]' for n in range(1, 2000))
    text, _ = compact(raw, RefMap(), max_chars=2000)

    assert len(text) < 2200
    assert text.endswith("more lines not shown: scroll, or use browser_extract for the page's text.")
