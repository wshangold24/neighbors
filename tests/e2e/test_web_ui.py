"""Spec tests for the web app, driven through a real (headless) browser.
Each test cites its requirement in docs/SPEC.md. WEB-05 (no JS errors) is
asserted for every test by the fixtures in conftest.py."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

CARDS = "#grid .card"


def open_app(page: Page, app) -> None:
    page.goto(app.url)
    expect(page.locator(CARDS).first).to_be_visible()


def stat(page: Page, label: str) -> int:
    """Read a header counter like '<b>12</b> available'."""
    text = page.locator("#stat-store").inner_text()
    m = re.search(rf"(\d+)\s+{label}", text)
    assert m, f"no '{label}' counter in header: {text!r}"
    return int(m.group(1))


def card_ids(page: Page) -> list[str]:
    return page.locator(CARDS).evaluate_all("els => els.map(e => e.dataset.id)")


def like_first_card(page: Page) -> str:
    card = page.locator(CARDS).first
    listing_id = card.get_attribute("data-id")
    card.locator('button[data-act="like"]').click()
    expect(page.locator("#btn-compare")).to_be_visible()
    return listing_id


def write_prefs(app, **changes) -> None:
    path = app.app_dir / "preferences.json"
    prefs = json.loads(path.read_text())
    prefs.update(changes)
    path.write_text(json.dumps(prefs))


# ---------- page & layout ----------

class TestPage:
    def test_title_and_brand(self, live_app, page):
        """WEB-01"""
        open_app(page, live_app)
        expect(page).to_have_title("Neighbors")
        expect(page.locator("header h1")).to_have_text("Neighbors")

    def test_cards_show_core_facts(self, live_app, page):
        """WEB-02"""
        open_app(page, live_app)
        assert page.locator(CARDS).count() >= 5
        for i in range(3):
            card = page.locator(CARDS).nth(i)
            expect(card.locator(".price")).to_have_text(re.compile(r"^\$[\d,]+$"))
            expect(card.locator(".match")).to_have_text(re.compile(r"^\d{1,3}%$"))
            expect(card.locator(".facts")).to_contain_text(re.compile(r"(Studio|\d+ bd) · [\d.]+ ba"))
            expect(card.locator(".hood")).not_to_be_empty()
            expect(card.locator(".addr")).to_contain_text("—")

    def test_header_counters(self, live_app, page):
        """WEB-03"""
        open_app(page, live_app)
        assert stat(page, "available") > 0
        assert stat(page, "verified") == 0
        assert stat(page, "liked") == 0

    def test_strong_matches_first_then_stretch(self, live_app, page):
        """WEB-04"""
        write_prefs(live_app, k=40)   # enough results to include weaker matches
        open_app(page, live_app)
        groups = page.locator("#grid").evaluate("""grid => {
            const out = {strong: [], stretch: []}; let bucket = out.strong;
            for (const el of grid.children) {
                if (el.classList.contains('section-label')) bucket = out.stretch;
                else if (el.classList.contains('card'))
                    bucket.push(parseInt(el.querySelector('.match').textContent));
            }
            return out; }""")
        assert groups["strong"], "expected at least one strong match"
        assert all(m >= 60 for m in groups["strong"])
        assert all(m < 60 for m in groups["stretch"])
        if groups["stretch"]:
            expect(page.locator("#grid .section-label")).to_have_text("stretch options")

    def test_list_view_is_local_only(self, live_app, page):
        """WEB-06"""
        open_app(page, live_app)
        page.wait_for_load_state("networkidle")
        assert live_app.external_requests == []

    def test_no_horizontal_scroll_on_phone(self, live_app, page):
        """WEB-07"""
        page.set_viewport_size({"width": 375, "height": 812})
        open_app(page, live_app)
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 0, f"page is {overflow}px wider than a 375px phone"


# ---------- feedback ----------

class TestFeedback:
    def test_like_counts_and_persists(self, live_app, page):
        """WEB-10"""
        open_app(page, live_app)
        like_first_card(page)
        expect(page.locator("#btn-compare")).to_contain_text("Liked · 1")
        assert stat(page, "liked") == 1
        page.reload()
        expect(page.locator("#btn-compare")).to_contain_text("Liked · 1")
        assert stat(page, "liked") == 1

    def test_unlike_from_compare(self, live_app, page):
        """WEB-11"""
        open_app(page, live_app)
        like_first_card(page)
        page.locator("#btn-compare").click()
        page.locator("#cmp-body .cmp-unlike").first.click()
        expect(page.locator("#cmp-body .cmp-unlike")).to_have_count(0)
        page.locator("#cmp-close").click()
        expect(page.locator("#btn-compare")).to_be_hidden()
        assert stat(page, "liked") == 0

    def test_hide_removes_card_for_good(self, live_app, page):
        """WEB-12"""
        open_app(page, live_app)
        card = page.locator(CARDS).first
        hidden_id = card.get_attribute("data-id")
        card.locator('button[data-act="hide"]').click()
        expect(page.locator(f'{CARDS}[data-id="{hidden_id}"]')).to_have_count(0)
        expect(page.locator("#toast")).to_contain_text("hidden")
        page.reload()
        expect(page.locator(CARDS).first).to_be_visible()
        assert hidden_id not in card_ids(page)

    def test_compare_lists_every_liked(self, live_app, page):
        """WEB-13"""
        open_app(page, live_app)
        first = like_first_card(page)
        expect(page.locator("#btn-compare")).to_contain_text("Liked · 1")
        second = page.locator(f'{CARDS}:not([data-id="{first}"])').first
        second_id = second.get_attribute("data-id")
        second.locator('button[data-act="like"]').click()
        expect(page.locator("#btn-compare")).to_contain_text("Liked · 2")
        page.locator("#btn-compare").click()
        expect(page.locator("#compare-dialog")).to_be_visible()
        ids = page.locator("#cmp-body .cmp-unlike").evaluate_all(
            "els => els.map(e => e.dataset.id)")
        assert set(ids) == {first, second_id}


# ---------- ranking & exploration ----------

class TestRankingAndExploration:
    def test_blend_modes_need_a_like(self, live_app, page):
        """WEB-20"""
        open_app(page, live_app)
        expect(page.locator('#modes button[data-alpha="1.0"]')).to_be_enabled()
        expect(page.locator('#modes button[data-alpha="0.6"]')).to_be_disabled()
        expect(page.locator('#modes button[data-alpha="0.3"]')).to_be_disabled()
        like_first_card(page)
        expect(page.locator('#modes button[data-alpha="0.6"]')).to_be_enabled()
        expect(page.locator('#modes button[data-alpha="0.3"]')).to_be_enabled()

    def test_mode_switch_reranks_without_fetching(self, live_app, page):
        """WEB-21"""
        open_app(page, live_app)
        like_first_card(page)
        with page.expect_response(lambda r: "/api/results?alpha=0.3" in r.url):
            page.locator('#modes button[data-alpha="0.3"]').click()
        expect(page.locator('#modes button[data-alpha="0.3"]')).to_have_class(re.compile("active"))
        expect(page.locator('#modes button[data-alpha="0.6"]')).not_to_have_class(re.compile("active"))
        assert live_app.fetch_calls == 0

    def test_similar_view_with_breadcrumb(self, live_app, page):
        """WEB-22"""
        open_app(page, live_app)
        seed = page.locator(CARDS).first
        seed_id = seed.get_attribute("data-id")
        street = seed.locator(".addr").inner_text().split("— ", 1)[1].split(",")[0]
        seed.locator('button[data-act="similar"]').click()
        expect(page.locator('#banner button[data-crumb="-1"]')).to_have_text("search")
        expect(page.locator("#banner .here")).to_contain_text(street)
        expect(page.locator(CARDS).first).to_be_visible()
        assert seed_id not in card_ids(page)

    def test_chained_similar_and_back_to_search(self, live_app, page):
        """WEB-23"""
        open_app(page, live_app)
        page.locator(CARDS).first.locator('button[data-act="similar"]').click()
        expect(page.locator("#banner .here")).to_be_visible()
        page.locator(CARDS).first.locator('button[data-act="similar"]').click()
        expect(page.locator('#banner button[data-crumb="0"]')).to_be_visible()
        expect(page.locator("#banner .sep")).to_have_count(2)
        page.locator('#banner button[data-crumb="-1"]').click()
        expect(page.locator("#banner")).to_be_empty()
        expect(page.locator(CARDS).first).to_be_visible()

    def test_details_dialog(self, live_app, page):
        """WEB-24"""
        open_app(page, live_app)
        card = page.locator(CARDS).first
        address = card.locator(".addr").inner_text().split("— ", 1)[1]
        card.locator('button[data-act="details"]').click()
        dialog = page.locator("#detail-dialog")
        expect(dialog).to_be_visible()
        expect(page.locator("#detail-title")).to_contain_text(address)
        page.locator("#detail-close").click()
        expect(dialog).to_be_hidden()
        # clicking the card body (not a button) opens it too
        card.locator(".addr").click()
        expect(dialog).to_be_visible()

    def test_list_map_toggle(self, live_app, page):
        """WEB-25 (map tiles/library are blocked offline; the view still switches)"""
        open_app(page, live_app)
        page.locator('#viewtoggle button[data-view="map"]').click()
        expect(page.locator("#map-wrap")).to_be_visible()
        expect(page.locator("#grid")).to_be_hidden()
        page.locator('#viewtoggle button[data-view="list"]').click()
        expect(page.locator("#grid")).to_be_visible()
        expect(page.locator("#map-wrap")).to_be_hidden()


# ---------- preferences ----------

class TestPreferences:
    def test_dialog_prefilled(self, live_app, page):
        """WEB-30"""
        open_app(page, live_app)
        page.locator("#btn-prefs").click()
        expect(page.locator("#prefs-dialog")).to_be_visible()
        expect(page.locator('#prefs-form input[name="max_price"]')).to_have_value("3500")
        expect(page.locator('#prefs-form input[name="beds"]')).to_have_value("1")

    def test_save_persists_and_offers_fetch(self, live_app, page):
        """WEB-31"""
        open_app(page, live_app)
        page.locator("#btn-prefs").click()
        page.locator('#prefs-form input[name="max_price"]').fill("4200")
        page.locator("#btn-save").click()
        expect(page.locator("#toast")).to_contain_text("preferences saved")
        expect(page.locator("#prefs-dialog")).to_be_hidden()
        expect(page.locator("#fetch-prompt")).to_be_visible()
        saved = json.loads((live_app.app_dir / "preferences.json").read_text())
        assert saved["max_price"] == 4200
        page.reload()
        page.locator("#btn-prefs").click()
        expect(page.locator('#prefs-form input[name="max_price"]')).to_have_value("4200")

    def test_cancel_discards(self, live_app, page):
        """WEB-32"""
        open_app(page, live_app)
        before = (live_app.app_dir / "preferences.json").read_text()
        page.locator("#btn-prefs").click()
        page.locator('#prefs-form input[name="max_price"]').fill("9999")
        page.locator('#prefs-form button[value="cancel"]').click()
        expect(page.locator("#prefs-dialog")).to_be_hidden()
        assert (live_app.app_dir / "preferences.json").read_text() == before
        page.locator("#btn-prefs").click()
        expect(page.locator('#prefs-form input[name="max_price"]')).to_have_value("3500")


# ---------- updating data ----------

class TestUpdating:
    def test_update_menu(self, live_app, page):
        """WEB-40"""
        open_app(page, live_app)
        menu = page.locator("#update-menu")
        expect(menu).to_be_hidden()
        page.locator("#btn-update").click()
        expect(menu).to_be_visible()
        expect(menu.locator(".mi-title")).to_have_text(
            ["Refresh", re.compile(r"^Verify all"), "Fetch new listings"])
        page.locator("main").click(position={"x": 5, "y": 5})
        expect(menu).to_be_hidden()

    def test_fetch_new_listings(self, live_app, page):
        """WEB-41"""
        open_app(page, live_app)
        available_before = stat(page, "available")
        page.locator("#btn-update").click()
        page.locator('.menu-item[data-action="fetch"]').click()
        expect(page.locator("#toast")).to_contain_text(
            re.compile(r"fetched 40 · 40 new · 0 drops · \d+ verified"))
        expect(page.locator("#btn-update")).to_be_enabled()
        assert live_app.fetch_calls == 1
        expect(page.locator("#stat-store")).to_contain_text(f"{available_before + 40} available")

    def test_empty_store_points_to_fetch(self, empty_app, page):
        """WEB-42"""
        page.goto(empty_app.url)
        empty = page.locator("#empty")
        expect(empty).to_be_visible()
        expect(empty).to_contain_text("Fetch new listings")
        expect(page.locator(CARDS)).to_have_count(0)
        # and following the pointer actually fills the page
        page.locator("#btn-update").click()
        page.locator('.menu-item[data-action="fetch"]').click()
        expect(page.locator(CARDS).first).to_be_visible()
        expect(empty).to_be_hidden()

    def test_filters_that_exclude_everything_are_explained(self, live_app, page):
        """WEB-43"""
        write_prefs(live_app, max_price=500, price_stretch=0)
        page.goto(live_app.url)
        empty = page.locator("#empty")
        expect(empty).to_be_visible()
        expect(empty).to_contain_text("rule them all out")
        expect(empty).to_contain_text("over your price cap")
        empty.locator("#empty-prefs").click()
        expect(page.locator("#prefs-dialog")).to_be_visible()
