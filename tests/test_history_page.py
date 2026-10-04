"""History page contracts with the API and the shared chart script.

Browser behavior is checked by hand (see the PR); these pin what the page
relies on from the server and the shared-code rule in DESIGN.md §6.6.
"""

import asyncio
import re

import pytest

from app.core.timeseries import TimeSeriesStore


@pytest.fixture
def daily_store(tmp_path, monkeypatch):
    """A file store with daily rows, served by /api/timeseries/daily."""
    import app.api.timeseries as api

    store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
    asyncio.run(store.get_daily_energy())  # create schema
    conn = store._ensure_conn()
    for i in range(1, 13):
        conn.execute(
            "INSERT INTO daily_energy (gateway_id, day, solar_kwh, updated_at) "
            "VALUES ('gw1', ?, 1.0, 0)",
            (f"2099-01-{i:02d}",),
        )
    conn.commit()
    monkeypatch.setattr(api, "get_timeseries_store", lambda: store)
    yield store
    asyncio.run(store.stop())


class TestPresetDailyQuery:
    def test_start_only_returns_every_later_day(self, client, daily_store):
        # Presets send only `start`: the gateway's current day can be ahead
        # of the browser's, so nothing after start may be cut off, and the
        # default 7-day limit must not apply
        body = client.get("/api/timeseries/daily?start=2099-01-02").json()
        days = [d["day"] for d in body["days"]]
        assert days == [f"2099-01-{i:02d}" for i in range(12, 1, -1)]

    def test_all_range_start(self, client, daily_store):
        # The All preset asks from 1970-01-01
        body = client.get("/api/timeseries/daily?start=1970-01-01").json()
        assert len(body["days"]) == 12


class TestSharedChartCode:
    def test_trend_status_text_lives_in_charts_js(self, client):
        # DESIGN.md §6.6: the Energy Trend's status line is written once
        texts = (
            "Not enough raw samples",
            "needs local time-series storage",
            "Trend data unavailable",
            "resolution \\u00b7",
        )
        charts = client.get("/static/js/charts.js").text
        assert "trendNote," in charts  # exported on window.PWCharts
        for text in texts:
            assert text in charts, text
        for page in ("/console", "/history"):
            html = client.get(page).text
            assert "trendNote(" in html, page
            for text in texts + ("resolution ·",):
                assert text not in html, (page, text)

    def test_one_time_format_in_tooltips(self, client):
        # Locale-dependent dates made the two chart types disagree
        assert "toLocaleDateString" not in client.get("/static/js/charts.js").text

    def test_errors_do_not_show_urls(self, client):
        page = client.get("/history").text
        assert "throw new Error(`HTTP ${resp.status}`)" in page
        assert "returned ${resp.status}" not in page

    def test_range_label_is_not_a_form_label(self, client):
        page = client.get("/history").text
        assert re.search(r'<span[^>]*id="range-label"', page)
        assert 'aria-describedby\', \'energy-table\'' not in page

    def test_default_range_is_24h_and_last_preset_is_remembered(self, client):
        # Plain /history opens at 24h, or the last preset picked in this
        # browser; a range in the URL still wins (bookmarks)
        page = client.get("/history").text
        assert "const DEFAULT_RANGE = '1d';" in page
        assert "storageSet(RANGE_KEY, b.dataset.range)" in page
        assert "else if (PRESETS.has(range)) applyPreset(range);" in page

    def test_console_and_history_share_the_header_menu(self, client):
        # Switching pages keeps the same menu: same links and buttons in the
        # same order, the current page marked, and the version badge on both.
        # Cards and Kiosk are buttons (role="button"), last, on both pages.
        def nav(page):
            html = client.get(page).text
            block = re.search(
                r'<nav class="header-links" aria-label="Pages">(.*?)</nav>', html, re.S
            ).group(1)
            links = re.findall(r"<a ([^>]*)>([^<]*)</a>", block)
            # Everything but the current-page mark must match exactly
            items = [(a.replace(' aria-current="page"', ""), t) for a, t in links]
            current = [t for a, t in links if 'aria-current="page"' in a]
            buttons = [t for a, t in links if 'role="button"' in a]
            return html, items, current, buttons

        console, console_items, console_current, console_buttons = nav("/console")
        history, history_items, history_current, history_buttons = nav("/history")
        assert console_items == history_items
        assert [t for _, t in console_items] == [
            "Console",
            "History",
            "Power Flow",
            "API Docs",
            "Gateways API",
            "GitHub",
            "Cards",
            "Kiosk",
        ]
        assert console_buttons == history_buttons == ["Cards", "Kiosk"]
        assert console_current == ["Console"]
        assert history_current == ["History"]
        badge = '<span id="version-badge" class="version-badge">v'
        assert badge in console and badge in history

    def test_cards_menu_and_kiosk_markup_match(self, client):
        # The Cards menu and the floating kiosk buttons are the same block
        # on both pages (page.js finds them by id)
        def chrome(page):
            html = client.get(page).text
            return re.search(
                r'(<div class="kiosk-controls" id="kiosk-controls">.*?'
                r'<button type="button" id="card-menu-reset">Show all cards</button>'
                r"\s*</div>)",
                html,
                re.S,
            ).group(1)

        assert chrome("/console") == chrome("/history")

    def test_pages_load_shared_page_chrome(self, client):
        # DESIGN.md §6.6: header, Cards menu and kiosk live in one script and
        # one stylesheet, cache-busted by version like charts.js
        from app.config import SERVER_VERSION

        for page in ("/console", "/history"):
            html = client.get(page).text
            assert f'src="/static/js/page.js?v={SERVER_VERSION}"' in html, page
            assert f'href="/static/css/page.css?v={SERVER_VERSION}"' in html, page
            assert "window.PWPage.cardsAndKiosk(" in html, page
            # Not copied into the page
            for inline in (
                ".card-menu {",
                ".kiosk-controls {",
                ".header-links a {",
                ".version-badge {",
                "function toggleMenu",
                "function setKiosk",
                # One scrollbar style: a styled WebKit scrollbar takes width
                # (overlay ones don't), so a page-only copy shifts the header
                "::-webkit-scrollbar",
            ):
                assert inline not in html, (page, inline)
        js = client.get("/static/js/page.js").text
        assert "window.PWPage" in js
        assert "function toggleMenu" in js and "function setKiosk" in js
        css = client.get("/static/css/page.css").text
        for rule in (
            ".card-menu {",
            ".kiosk-controls {",
            ".header-links a {",
            "::-webkit-scrollbar {",
            "scrollbar-gutter: stable;",
        ):
            assert rule in css, rule

    def test_card_and_kiosk_settings_names(self, client):
        # Released Console names (0.7.0) stay; History gets its own keys and
        # no card URL parameter (its hide= lists switched-off series)
        console = client.get("/console").text
        assert "hiddenKey: 'pw_console_hidden_cards'" in console
        assert "kioskKey: 'pw_console_kiosk'" in console
        assert "urlHideParam: 'hide'" in console
        history = client.get("/history").text
        assert "hiddenKey: 'pw_history_hidden_cards'" in history
        assert "kioskKey: 'pw_history_kiosk'" in history
        assert "urlHideParam" not in history
        # Signal cards are rebuilt on every load; hidden ones stay hidden
        assert "renderSignalCardsNow(); cardsUi.apply();" in history
        assert 'data-card-id="energy"' in history
        js = client.get("/static/js/page.js").text
        assert "params.has('kiosk')" in js
        assert ".card[data-card-id]" in js
