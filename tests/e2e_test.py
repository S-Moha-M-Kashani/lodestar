"""End-to-end verification of Question Board.

Launches the real SQLite-backed server, drives the app in headless Chrome, and
exercises every button and flow — including the in-app confirm dialogs and that
the board actually persists to (and deletes from) the database.

    uv run --with playwright python tests/e2e_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("TEST_PORT", "8799"))
URL = f"http://localhost:{PORT}"
DB_PATH = os.path.join(tempfile.mkdtemp(prefix="qboard-test-"), "board.db")

ARTIFACTS = os.path.join(os.path.dirname(__file__), "artifacts")
os.makedirs(ARTIFACTS, exist_ok=True)
shot = lambda name: os.path.join(ARTIFACTS, name)
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def start_server():
    proc = subprocess.Popen(
        ["node", "server.js"],
        cwd=ROOT,
        env={**os.environ, "PORT": str(PORT), "BOARD_DB": DB_PATH, "NODE_NO_WARNINGS": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            urllib.request.urlopen(URL + "/api/state", timeout=1)
            return proc
        except Exception:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("server did not become ready")


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def api_state():
    with urllib.request.urlopen(URL + "/api/state", timeout=3) as r:
        return json.loads(r.read())


def launch_browser(p):
    # Prefer the system Chrome locally; fall back to bundled Chromium (CI).
    try:
        return p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        return p.chromium.launch(headless=True)


server = start_server()
browser = None
try:
    with sync_playwright() as p:
        browser = launch_browser(p)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, accept_downloads=True
        )
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=URL)
        # Keep the run network-free and deterministic: force the Overview map onto
        # its offline keyword-overlap layout instead of downloading the HuggingFace
        # model. The semantic upgrade is a progressive enhancement exercised by hand.
        context.add_init_script("window.QBOARD_DISABLE_SEMANTIC = true;")
        page = context.new_page()

        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        # Native confirm/alert should never be used anymore — record any that fire.
        native_dialogs = []
        page.on("dialog", lambda d: (native_dialogs.append(d.message), d.dismiss()))

        page.goto(URL)
        page.wait_for_selector(".card")

        # ---- Seed + first save to the database ------------------------------
        check("seed: 4 cards on first run", page.locator(".card").count() == 4)
        page.wait_for_timeout(600)  # let the initial push reach the server
        check("server: seed board auto-saved to the database",
              len(api_state().get("cards", [])) == 4)

        # ---- Quick add ------------------------------------------------------
        page.fill(".quick-add input", "What is speculative decoding?")
        page.press(".quick-add input", "Enter")
        first = page.locator('[data-col="inbox"] .card .card-title').first
        check("quick-add: new question at top of Inbox",
              first.inner_text() == "What is speculative decoding?")

        # ---- Edit modal -----------------------------------------------------
        page.locator('[data-col="inbox"] .card').first.click()
        page.wait_for_selector("#card-dialog[open]")
        page.fill("#card-notes", "Draft tokens from a small model, verify with the big one.")
        page.locator('.priority-picker label:has(input[value="high"])').click()
        page.fill("#card-tags", "inference, decoding")
        page.click('#card-form button[type="submit"]')
        card = page.locator('[data-col="inbox"] .card').first
        check("modal: priority badge updated to high", card.locator(".badge.high").count() == 1)
        check("modal: tags rendered", card.locator(".card-tag").count() == 2)
        check("modal: notes indicator shown", card.locator(".notes-dot").count() == 1)

        # Cancel must not change anything
        card.click()
        page.wait_for_selector("#card-dialog[open]")
        page.fill("#card-title", "THROWAWAY EDIT")
        page.click("#cancel-dialog")
        page.wait_for_timeout(100)
        check("modal: Cancel discards edits",
              page.locator('[data-col="inbox"] .card', has_text="THROWAWAY EDIT").count() == 0)

        # ---- Keyboard moves -------------------------------------------------
        card.focus()
        page.keyboard.press("]")
        page.wait_for_timeout(100)
        moved = page.locator('[data-col="to-research"] .card', has_text="speculative decoding")
        check("keyboard: ] moved card to To Research", moved.count() == 1)
        check("keyboard: focus restored after move",
              page.evaluate("document.activeElement?.classList.contains('card')"))

        page.keyboard.press("Alt+ArrowUp")
        page.wait_for_timeout(100)
        titles = page.locator('[data-col="to-research"] .card-title').all_inner_texts()
        check("keyboard: Alt+Up reordered within column",
              len(titles) == 3 and "speculative decoding" in titles[1])

        page.keyboard.press("[")
        page.wait_for_timeout(100)
        check("keyboard: [ moved card back to Inbox",
              page.locator('[data-col="inbox"] .card', has_text="speculative decoding").count() == 1)

        # ---- Sort -----------------------------------------------------------
        page.locator('[data-col="inbox"] .card', has_text="speculative decoding").first.press("]")
        page.wait_for_timeout(100)
        page.locator('[data-col="to-research"] .sort-btn').click()
        page.wait_for_timeout(100)
        first_badge = page.locator('[data-col="to-research"] .card .badge').first
        check("sort: high-priority card first after sort", first_badge.inner_text().lower() == "high")

        # ---- Filters --------------------------------------------------------
        page.fill("#search", "runway")
        page.wait_for_timeout(100)
        check("filter: search narrows to 1 card", page.locator(".card").count() == 1)
        page.fill("#search", "")
        page.wait_for_timeout(50)
        check("filter: clearing search restores the board", page.locator(".card").count() == 5)

        page.select_option("#priority-filter", "high")
        page.wait_for_timeout(100)
        check("filter: priority=high shows only high badges",
              page.locator(".card").count() == page.locator(".card .badge.high").count()
              and page.locator(".card").count() == 2)
        page.select_option("#priority-filter", "")

        page.locator('.tag-chip:has-text("planning")').first.click()
        page.wait_for_timeout(100)
        check("filter: tag chip 'planning' shows 2 cards", page.locator(".card").count() == 2)
        page.locator('.tag-chip:has-text("planning")').first.click()

        # ---- Drag & drop ----------------------------------------------------
        src = page.locator('[data-col="inbox"] .card').first
        dragged_title = src.locator(".card-title").inner_text()
        src.drag_to(page.locator('.cards[data-col="answered"]'))
        page.wait_for_timeout(100)
        check("drag & drop: card landed in Answered",
              page.locator('[data-col="answered"] .card', has_text=dragged_title[:30]).count() == 1)

        # ---- Reload persistence (same browser) ------------------------------
        count_before = page.locator(".card").count()
        page.wait_for_timeout(400)
        page.reload()
        page.wait_for_selector(".card")
        check("persistence: board intact after reload", page.locator(".card").count() == count_before)

        # ---- Export ---------------------------------------------------------
        page.click("#export-btn")
        page.wait_for_selector("#export-dialog[open]")
        ta = page.locator("#export-json").input_value()
        check("export: dialog shows the whole board as JSON",
              json.loads(ta).get("version") == 1 and len(json.loads(ta)["cards"]) == count_before)
        with page.expect_download() as dl:
            page.click("#download-export")
        data = json.loads(open(dl.value.path()).read())
        check("export: download is valid JSON with all cards",
              data.get("version") == 1 and len(data.get("cards", [])) == count_before)
        check("export: dialog closes after download", page.locator("#export-dialog[open]").count() == 0)
        page.click("#export-btn")
        page.wait_for_selector("#export-dialog[open]")
        page.click("#copy-export")
        copied = page.evaluate("navigator.clipboard.readText()")
        check("export: Copy JSON puts the whole board on the clipboard",
              json.loads(copied).get("version") == 1 and len(json.loads(copied)["cards"]) == count_before)
        page.click("#cancel-export")
        check("export: Cancel closes the dialog", page.locator("#export-dialog[open]").count() == 0)

        # ---- Themes ---------------------------------------------------------
        for theme in ("white", "sepia", "dark", "light"):
            page.select_option("#theme-select", theme)
            page.wait_for_timeout(40)
            check(f"theme: '{theme}' mode applied",
                  page.evaluate("document.documentElement.dataset.theme") == theme)
        page.select_option("#theme-select", "white")
        page.reload()
        page.wait_for_selector(".card")
        check("theme: choice persisted across reload",
              page.evaluate("document.documentElement.dataset.theme") == "white")
        check("theme: Day mode uses a plain white background",
              page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)")
        page.select_option("#theme-select", "light")

        # ---- Delete (in-app confirm dialog) ---------------------------------
        target = page.locator('[data-col="answered"] .card').first
        target.click()
        page.wait_for_selector("#card-dialog[open]")
        page.click("#delete-card")
        page.wait_for_selector("#confirm-dialog[open]")
        check("delete: opens the in-app confirm dialog (not a native one)",
              page.locator("#confirm-dialog[open]").count() == 1)
        page.click("#confirm-cancel")
        page.wait_for_timeout(100)
        check("delete: Cancel keeps the card",
              page.locator('[data-col="answered"] .card').count() == 1)

        page.locator('[data-col="answered"] .card').first.click()
        page.wait_for_selector("#card-dialog[open]")
        page.click("#delete-card")
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        page.wait_for_timeout(150)
        check("delete: confirming removes the card",
              page.locator('[data-col="answered"] .card').count() == 0)

        page.click("#undo-btn")
        page.wait_for_timeout(150)
        check("undo: deleted card restored to Answered",
              page.locator('[data-col="answered"] .card').count() == 1)

        # ---- History --------------------------------------------------------
        page.click("#history-btn")
        page.wait_for_selector("#history-dialog[open]")
        check("history: log records many actions", page.locator(".history-row").count() >= 10)
        check("history: exactly one entry marked current",
              page.locator(".history-row.current").count() == 1)
        check("history: every entry carries a timestamp and an action",
              page.locator(".history-row .history-time").count()
              == page.locator(".history-row .history-action").count()
              == page.locator(".history-row").count())
        page.screenshot(path=shot("history-dialog.png"))
        page.locator(".history-row .history-restore").last.click()
        page.wait_for_timeout(150)
        check("history: restored the opening state (4 seeds)",
              page.locator(".card").count() == 4
              and page.locator(".card", has_text="speculative decoding").count() == 0)
        page.locator(".history-row .history-restore").first.click()
        page.wait_for_timeout(150)
        check("history: came forward again, later state intact",
              page.locator(".card", has_text="speculative decoding").count() == 1)
        page.click("#close-history")

        # ---- Responsive -----------------------------------------------------
        page.set_viewport_size({"width": 375, "height": 800})
        page.wait_for_timeout(100)
        check("responsive: board switches to flex scroll at 375px",
              page.evaluate("getComputedStyle(document.getElementById('board')).display") == "flex")
        page.screenshot(path=shot("board-mobile.png"))
        page.set_viewport_size({"width": 1440, "height": 900})

        # ---- Backlog view ---------------------------------------------------
        inbox_count = page.locator('[data-col="inbox"] .card').count()
        page.locator('.view-switch button[data-view="backlog"]').click()
        page.wait_for_selector("#board.backlog")
        check("backlog: rows match Inbox card count",
              page.locator(".backlog-row").count() == inbox_count)
        check("backlog: view button marked pressed",
              page.get_attribute('.view-switch button[data-view="backlog"]', "aria-pressed") == "true")
        page.locator(".backlog-row").first.click()
        page.wait_for_selector("#card-dialog[open]")
        check("backlog: row opens the edit dialog", page.locator("#card-dialog[open]").count() == 1)
        page.click("#cancel-dialog")
        page.screenshot(path=shot("board-backlog.png"))
        page.locator('.view-switch button[data-view="board"]').click()
        page.wait_for_selector("#board.board")
        check("backlog: switching back restores the board",
              page.locator('.column[data-col="inbox"]').count() == 1)

        # ---- Overview view (semantic map, kept offline for a network-free test) ----
        n_all = page.locator(".card").count()
        page.locator('.view-switch button[data-view="overview"]').click()
        page.wait_for_selector("#board.overview .plot-dot")
        check("overview: one dot per question", page.locator(".plot-dot").count() == n_all)
        check("overview: view button marked pressed",
              page.get_attribute('.view-switch button[data-view="overview"]', "aria-pressed") == "true")
        check("overview: layout falls back to keyword overlap when the model is offline",
              "keyword overlap" in page.locator(".plot-status").inner_text())

        page.locator(".plot-dot").first.hover()
        page.wait_for_timeout(120)
        check("overview: hovering a dot reveals its details",
              page.locator(".plot-tip").is_visible()
              and page.locator(".plot-tip .plot-tip-title").inner_text() != "")

        page.locator('.tag-chip:has-text("planning")').first.click()
        page.wait_for_timeout(100)
        k = page.locator(".plot-dot").count()
        check("overview: tag filter narrows the map", 0 < k < n_all)
        page.locator('.view-switch button[data-view="board"]').click()
        page.wait_for_selector("#board.board")
        check("overview: its tag filter matches the board's own filtering",
              page.locator(".card").count() == k)
        page.locator('.tag-chip:has-text("planning")').first.click()  # clear the filter
        page.wait_for_timeout(60)

        page.locator('.view-switch button[data-view="overview"]').click()
        page.wait_for_selector("#board.overview .plot-dot")
        page.locator(".plot-dot").first.click()
        page.wait_for_selector("#card-dialog[open]")
        check("overview: clicking a dot opens the question editor",
              page.locator("#card-dialog[open]").count() == 1)
        page.click("#cancel-dialog")
        page.screenshot(path=shot("overview.png"))

        # ---- Matrix view (Eisenhower importance × urgency) ------------------
        page.locator('.view-switch button[data-view="matrix"]').click()
        page.wait_for_selector("#board.matrix .matrix-grid")
        check("matrix: view button marked pressed",
              page.get_attribute('.view-switch button[data-view="matrix"]', "aria-pressed") == "true")
        check("matrix: four quadrants drawn", page.locator(".matrix-quad").count() == 4)
        page.wait_for_timeout(300)
        placed_expected = sum(1 for c in api_state()["cards"] if c.get("importance") and c.get("urgency"))
        check("matrix: one dot per question that has both importance and urgency",
              page.locator(".matrix-quad-dots .plot-dot").count() == placed_expected)
        page.screenshot(path=shot("matrix.png"))

        # Routing: set an unplaced question to Important + not urgent → the Schedule quadrant
        hl_before = page.locator('.matrix-quad[data-imp="high"][data-urg="low"] .plot-dot').count()
        placed_before = page.locator(".matrix-quad-dots .plot-dot").count()
        page.locator('.view-switch button[data-view="board"]').click()
        page.wait_for_selector("#board.board")
        page.locator('.card', has_text="speculative decoding").first.click()
        page.wait_for_selector("#card-dialog[open]")
        page.select_option("#card-importance", "high")
        page.select_option("#card-urgency", "low")
        page.click('#card-form button[type="submit"]')
        page.wait_for_timeout(150)
        page.locator('.view-switch button[data-view="matrix"]').click()
        page.wait_for_selector("#board.matrix .matrix-grid")
        check("matrix: setting importance & urgency routes the question to its quadrant",
              page.locator('.matrix-quad[data-imp="high"][data-urg="low"] .plot-dot').count() == hl_before + 1
              and page.locator(".matrix-quad-dots .plot-dot").count() == placed_before + 1)

        page.wait_for_timeout(450)
        spec = next((c for c in api_state()["cards"] if "speculative decoding" in c["title"]), None)
        check("matrix: importance & urgency persist to the database",
              spec is not None and spec.get("importance") == "high" and spec.get("urgency") == "low")

        page.locator('.view-switch button[data-view="board"]').click()
        page.wait_for_selector("#board.board")

        # ---- Import: schema dialog -----------------------------------------
        page.click("#import-btn")
        page.wait_for_selector("#import-dialog[open]")
        schema_text = page.locator("#import-schema").inner_text()
        check("import: dialog shows the JSON schema",
              '"version": 1' in schema_text and '"cards"' in schema_text
              and "inbox | to-research | in-progress | answered" in schema_text
              and '"importance"' in schema_text and '"urgency"' in schema_text)
        page.screenshot(path=shot("import-dialog.png"))
        page.click("#copy-schema")
        check("import: 'Copy schema' puts the schema on the clipboard",
              '"version": 1' in page.evaluate("navigator.clipboard.readText()"))

        # ---- Import: cancel the choice --------------------------------------
        import_file = shot("generated-import.json")
        with open(import_file, "w") as f:
            json.dump({"version": 1, "cards": [
                {"title": "Imported: which venue for the offsite?", "columnId": "to-research",
                 "priority": "high", "tags": ["planning"]},
                {"title": "Imported: who owns the onboarding doc?"},
            ]}, f)
        count_before_import = page.locator(".card").count()
        page.set_input_files("#import-input", import_file)
        page.wait_for_selector("#import-mode-dialog[open]")
        check("import: add-or-substitute choice offered after picking a file",
              page.locator("#import-add").count() == 1 and page.locator("#import-replace").count() == 1)
        page.click("#cancel-import-mode")
        page.wait_for_timeout(100)
        check("import: cancelling the choice leaves the board untouched",
              page.locator(".card").count() == count_before_import)

        # ---- Import: ADD ----------------------------------------------------
        page.set_input_files("#import-input", import_file)
        page.wait_for_selector("#import-mode-dialog[open]")
        page.click("#import-add")
        page.wait_for_timeout(200)
        check("import: questions added on top of the existing board",
              page.locator(".card").count() == count_before_import + 2)
        check("import: added card kept its column, defaults fill the gaps",
              page.locator('[data-col="to-research"] .card', has_text="offsite").count() == 1
              and page.locator('[data-col="inbox"] .card', has_text="onboarding doc").count() == 1)
        nums = page.locator("#board .card-num").all_inner_texts()
        check("import: ledger numbers stay unique after adding",
              len(nums) == len(set(nums)) and all(n.startswith("Q-0") for n in nums))

        # ---- Import: SUBSTITUTE asks are-you-sure (cancel, then confirm) ----
        page.set_input_files("#import-input", import_file)
        page.wait_for_selector("#import-mode-dialog[open]")
        page.click("#import-replace")
        page.wait_for_selector("#confirm-dialog[open]")
        check("import: substitute shows an 'Are you sure?' confirm",
              "are you sure" in page.locator("#confirm-title").text_content().lower())
        page.click("#confirm-cancel")
        page.wait_for_timeout(100)
        page.click("#cancel-import-mode")
        check("import: cancelling the confirm keeps the full board",
              page.locator(".card").count() == count_before_import + 2)

        page.set_input_files("#import-input", import_file)
        page.wait_for_selector("#import-mode-dialog[open]")
        page.click("#import-replace")
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        page.wait_for_timeout(200)
        check("import: confirming substitute replaces the whole board",
              page.locator(".card").count() == 2)

        page.click("#undo-btn")
        page.wait_for_timeout(150)
        check("undo: substitution rolled back",
              page.locator(".card").count() == count_before_import + 2)

        # ---- Import: invalid file -------------------------------------------
        bad_file = shot("bad-import.json")
        with open(bad_file, "w") as f:
            f.write("this is not the board format")
        page.set_input_files("#import-input", bad_file)
        page.wait_for_selector("#confirm-dialog[open]")
        check("import: invalid file shows an error dialog",
              "could not import" in page.locator("#confirm-title").text_content().lower())
        page.click("#confirm-ok")

        check("regression: no native browser dialogs were used", not native_dialogs)

        # ---- Database persistence (the whole point of the server) -----------
        page.fill(".quick-add input", "PERSIST-MARKER-alpha")
        page.press(".quick-add input", "Enter")
        page.wait_for_timeout(500)
        check("db: new question written to the database",
              any(c["title"] == "PERSIST-MARKER-alpha" for c in api_state()["cards"]))

        fresh = browser.new_context(viewport={"width": 1200, "height": 800})
        fresh_page = fresh.new_page()
        fresh_page.goto(URL)
        fresh_page.wait_for_selector(".card")
        fresh_page.wait_for_timeout(400)
        check("db: a fresh browser loads the board from the database (not localStorage)",
              fresh_page.locator(".card", has_text="PERSIST-MARKER-alpha").count() == 1)
        fresh.close()

        # Delete on the main board, and confirm it leaves the database too
        page.locator('.card', has_text="PERSIST-MARKER-alpha").first.click()
        page.wait_for_selector("#card-dialog[open]")
        page.click("#delete-card")
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        page.wait_for_timeout(500)
        check("db: deleting a question removes it from the database",
              all(c["title"] != "PERSIST-MARKER-alpha" for c in api_state()["cards"]))

        # ---- Survives a server restart (proves on-disk SQLite) --------------
        page.fill(".quick-add input", "RESTART-MARKER-beta")
        page.press(".quick-add input", "Enter")
        page.wait_for_timeout(500)
        check("db: restart marker saved before restart",
              any(c["title"] == "RESTART-MARKER-beta" for c in api_state()["cards"]))

        stop_server(server)
        server = start_server()
        check("db: data survived a full server restart",
              any(c["title"] == "RESTART-MARKER-beta" for c in api_state()["cards"]))

        after = browser.new_context(viewport={"width": 1200, "height": 800})
        after_page = after.new_page()
        after_page.goto(URL)
        after_page.wait_for_selector(".card")
        after_page.wait_for_timeout(400)
        check("db: a fresh browser after restart still shows the persisted board",
              after_page.locator(".card", has_text="RESTART-MARKER-beta").count() == 1)
        after.close()

        check("console: no JS errors during entire run", not errors)
        if errors:
            print("Errors:", errors)
        if native_dialogs:
            print("Native dialogs (should be none):", native_dialogs)

finally:
    if browser:
        try:
            browser.close()
        except Exception:
            pass
    stop_server(server)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
