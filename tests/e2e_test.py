"""End-to-end verification of Lodestar.

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
BRAIN_PORT = int(os.environ.get("TEST_BRAIN_PORT", "8798"))
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
        env={**os.environ, "PORT": str(PORT), "BOARD_DB": DB_PATH, "NODE_NO_WARNINGS": "1",
             "AGENT_URL": f"http://127.0.0.1:{BRAIN_PORT}"},
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


def start_brain():
    # The brain runs fully offline here: deterministic fake LLM + hash embedder.
    proc = subprocess.Popen(
        ["uv", "run", "--project", "brain", "uvicorn",
         "lodestar_brain.server:app", "--port", str(BRAIN_PORT)],
        cwd=ROOT,
        env={**os.environ, "BRAIN_LLM": "fake", "BRAIN_EMBEDDER": "hash",
             "BOARD_API_URL": f"http://127.0.0.1:{PORT}"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(300):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{BRAIN_PORT}/health", timeout=1)
            return proc
        except Exception:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("brain did not become ready")


def api_state():
    with urllib.request.urlopen(URL + "/api/state", timeout=3) as r:
        return json.loads(r.read())


def api_trash():
    with urllib.request.urlopen(URL + "/api/trash", timeout=3) as r:
        return json.loads(r.read())


def api_put(cards):
    body = json.dumps({"version": 1, "cards": cards}).encode()
    req = urllib.request.Request(
        URL + "/api/state", data=body, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def launch_browser(p):
    # Prefer the system Chrome locally; fall back to bundled Chromium (CI).
    try:
        return p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        return p.chromium.launch(headless=True)


server = start_server()
brain = start_brain()
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

        # Undo, History, Export and Import live inside the collapsed Menu button.
        def menu_click(action_sel):
            page.click("#menu-btn")
            page.click(action_sel)

        page.goto(URL)
        page.wait_for_selector(".card")

        # ---- Seed + first save to the database ------------------------------
        check("seed: 6 cards on first run", page.locator(".card").count() == 6)
        page.wait_for_timeout(600)  # let the initial push reach the server
        check("server: seed board auto-saved to the database",
              len(api_state().get("cards", [])) == 6)

        # ---- Actions menu ----------------------------------------------------
        check("menu: panel starts closed", page.locator("#menu-panel").is_hidden())
        page.click("#menu-btn")
        check("menu: click opens the panel with all four actions",
              page.locator("#menu-panel").is_visible()
              and all(page.locator(f"#menu-panel #{i}").count() == 1
                      for i in ("undo-btn", "history-btn", "export-btn", "import-btn")))
        page.keyboard.press("Escape")
        page.wait_for_timeout(50)
        check("menu: Escape closes the panel", page.locator("#menu-panel").is_hidden())

        # ---- Category rail ---------------------------------------------------
        # The rail always shows: All + every defined category + the ✎ Edit tab.
        check("categories: rail shows All + all 9 defined areas + Edit",
              page.locator(".cat-tab").count() == 11
              and page.locator(".cat-tab-all").count() == 1
              and page.locator("#edit-cats-btn").count() == 1)
        check("categories: tabs carry no count badge",
              page.locator(".cat-tab-count").count() == 0)
        check("categories: 'All' is pressed when nothing is filtered",
              page.get_attribute(".cat-tab-all", "aria-pressed") == "true")
        check("categories: cards carry their category spine",
              page.locator(".card.categorized").count() == 6)
        page.locator('.cat-tab[data-cat="love"]').click()
        page.wait_for_timeout(100)
        check("categories: clicking a tab filters the board to that life area",
              page.locator(".card").count() == 1
              and page.get_attribute('.cat-tab[data-cat="love"]', "aria-pressed") == "true"
              and page.get_attribute(".cat-tab-all", "aria-pressed") == "false")
        page.locator(".cat-tab-all").click()
        page.wait_for_timeout(100)
        check("categories: 'All' clears the filter — the whole life on one board",
              page.locator(".card").count() == 6
              and page.get_attribute(".cat-tab-all", "aria-pressed") == "true")
        page.screenshot(path=shot("board-categories.png"))

        # ---- Category management (the ✎ Edit tab) -----------------------------
        page.click("#edit-cats-btn")
        page.wait_for_selector("#cats-dialog[open]")
        check("categories: editor lists all 9 areas, each removable",
              page.locator("#cats-list .cats-row").count() == 9
              and page.locator("#cats-list .cats-remove").count() == 9)
        page.fill("#cat-add-name", "Reading")
        page.click("#cat-add-btn")
        page.wait_for_timeout(150)
        check("categories: adding 'Reading' puts it in the editor and on the rail",
              page.locator("#cats-list .cats-row").count() == 10
              and page.locator('.cat-tab[data-cat="reading"]').count() == 1)
        page.wait_for_timeout(600)  # debounced push
        check("categories: the new registry is saved to the database",
              any(c.get("id") == "reading" for c in api_state().get("categories", [])))
        page.screenshot(path=shot("cats-dialog.png"))
        page.locator('#cats-list .cats-row:has-text("Reading") .cats-remove').click()
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        page.wait_for_timeout(150)
        check("categories: removing 'Reading' takes it off the rail again",
              page.locator("#cats-list .cats-row").count() == 9
              and page.locator('.cat-tab[data-cat="reading"]').count() == 0)
        page.click("#close-cats")
        page.wait_for_timeout(600)
        check("categories: the removal is saved to the database",
              not any(c.get("id") == "reading" for c in api_state().get("categories", [])))

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
        page.locator('.type-picker label:has(input[value="problem"])').click()
        page.locator('.category-picker label:has(input[value="work"])').click()
        page.fill("#card-tags", "inference, decoding")
        page.click('#card-form button[type="submit"]')
        card = page.locator('[data-col="inbox"] .card').first
        check("modal: type stamp updated to problem", card.locator(".badge.type-problem").count() == 1)
        check("modal: category rendered on the card",
              card.locator(".card-cat").inner_text() == "Work"
              and card.evaluate("el => el.classList.contains('categorized')"))
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
        moved = page.locator('[data-col="in-progress"] .card', has_text="speculative decoding")
        check("keyboard: ] moved card to In Progress", moved.count() == 1)
        check("keyboard: focus restored after move",
              page.evaluate("document.activeElement?.classList.contains('card')"))

        page.keyboard.press("Alt+ArrowUp")
        page.wait_for_timeout(100)
        titles = page.locator('[data-col="in-progress"] .card-title').all_inner_texts()
        check("keyboard: Alt+Up reordered within column",
              len(titles) == 3 and "speculative decoding" in titles[1])

        page.keyboard.press("[")
        page.wait_for_timeout(100)
        check("keyboard: [ moved card back to Inbox",
              page.locator('[data-col="inbox"] .card', has_text="speculative decoding").count() == 1)

        # ---- Sort -----------------------------------------------------------
        page.locator('[data-col="inbox"] .card', has_text="speculative decoding").first.press("]")
        page.wait_for_timeout(100)
        page.locator('[data-col="in-progress"] .sort-btn').click()
        page.wait_for_timeout(100)
        first_badge = page.locator('[data-col="in-progress"] .card .badge').first
        check("sort: problem card sorts ahead of plans", "problem" in first_badge.inner_text().lower())

        # ---- Filters --------------------------------------------------------
        page.fill("#search", "stoic")
        page.wait_for_timeout(100)
        check("filter: search narrows to 1 card", page.locator(".card").count() == 1)
        page.fill("#search", "")
        page.wait_for_timeout(50)
        check("filter: clearing search restores the board", page.locator(".card").count() == 7)

        page.select_option("#type-filter", "problem")
        page.wait_for_timeout(100)
        check("filter: type=problem shows only problem stamps",
              page.locator(".card").count() == page.locator(".card .badge.type-problem").count()
              and page.locator(".card").count() == 2)
        page.select_option("#type-filter", "")

        page.locator('.tag-chip:has-text("planning")').first.click()
        page.wait_for_timeout(100)
        check("filter: tag chip 'planning' shows 1 card", page.locator(".card").count() == 1)
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
        menu_click("#export-btn")
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
        menu_click("#export-btn")
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
        # The body background transitions for 0.25s when the theme is applied after
        # first paint (which happens under load), so wait for it to settle instead
        # of sampling one instant.
        try:
            page.wait_for_function(
                "getComputedStyle(document.body).backgroundColor === 'rgb(255, 255, 255)'",
                timeout=3000)
            day_bg_white = True
        except Exception:
            day_bg_white = False
        check("theme: Day mode uses a plain white background", day_bg_white)
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
              page.locator('[data-col="answered"] .card').count() == 2)

        page.locator('[data-col="answered"] .card').first.click()
        page.wait_for_selector("#card-dialog[open]")
        page.click("#delete-card")
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        page.wait_for_timeout(150)
        check("delete: confirming removes the card",
              page.locator('[data-col="answered"] .card').count() == 1)

        menu_click("#undo-btn")
        page.wait_for_timeout(150)
        check("undo: deleted card restored to Answered",
              page.locator('[data-col="answered"] .card').count() == 2)

        # ---- History --------------------------------------------------------
        menu_click("#history-btn")
        page.wait_for_selector("#history-dialog[open]")
        # Scope to #history-list: the Trash panel reuses .history-row inside #trash-list.
        check("history: log records many actions", page.locator("#history-list .history-row").count() >= 10)
        check("history: exactly one entry marked current",
              page.locator("#history-list .history-row.current").count() == 1)
        check("history: every entry carries a timestamp and an action",
              page.locator("#history-list .history-row .history-time").count()
              == page.locator("#history-list .history-row .history-action").count()
              == page.locator("#history-list .history-row").count())
        page.screenshot(path=shot("history-dialog.png"))
        page.locator("#history-list .history-row .history-restore").last.click()
        page.wait_for_timeout(150)
        check("history: restored the opening state (6 seeds)",
              page.locator(".card").count() == 6
              and page.locator(".card", has_text="speculative decoding").count() == 0)
        page.locator("#history-list .history-row .history-restore").first.click()
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
        check("overview: one dot per card", page.locator(".plot-dot").count() == n_all)
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
        menu_click("#import-btn")
        page.wait_for_selector("#import-dialog[open]")
        schema_text = page.locator("#import-schema").inner_text()
        check("import: dialog shows the JSON schema",
              '"version": 1' in schema_text and '"cards"' in schema_text
              and "inbox | in-progress | answered" in schema_text
              and '"type"' in schema_text and '"category"' in schema_text
              and '"importance"' in schema_text and '"urgency"' in schema_text
              and '"categories"' in schema_text)
        page.screenshot(path=shot("import-dialog.png"))
        page.click("#copy-schema")
        check("import: 'Copy schema' puts the schema on the clipboard",
              '"version": 1' in page.evaluate("navigator.clipboard.readText()"))

        # ---- Import: cancel the choice --------------------------------------
        import_file = shot("generated-import.json")
        with open(import_file, "w") as f:
            json.dump({"version": 1, "cards": [
                {"title": "Imported: which venue for the offsite?", "columnId": "in-progress",
                 "type": "task", "category": "work", "tags": ["planning"]},
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
              page.locator('[data-col="in-progress"] .card', has_text="offsite").count() == 1
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

        menu_click("#undo-btn")
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

        # ---- Quick-add: empty input and adding inside an open drawer ---------
        count_now = page.locator(".card").count()
        page.press(".quick-add input", "Enter")
        page.wait_for_timeout(80)
        check("quick-add: empty input adds nothing and keeps focus in the input",
              page.locator(".card").count() == count_now
              and page.evaluate(
                  "document.activeElement === document.querySelector('.quick-add input')"))

        # A capture written inside an open category drawer belongs to that
        # drawer — it inherits the category and must never vanish behind the filter.
        page.locator('.cat-tab[data-cat="love"]').click()
        page.wait_for_timeout(100)
        love_before = page.locator(".card").count()
        page.fill(".quick-add input", "Plan a surprise picnic FILTER-MARKER")
        page.press(".quick-add input", "Enter")
        page.wait_for_timeout(100)
        marker = page.locator(".card", has_text="FILTER-MARKER")
        check("quick-add: a card added inside an open drawer stays visible",
              page.locator(".card").count() == love_before + 1 and marker.count() == 1)
        check("quick-add: it inherits the drawer's category",
              marker.first.locator(".card-cat").inner_text() == "Love")
        page.locator(".cat-tab-all").click()
        page.wait_for_timeout(100)

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

        # Delete on the main board: it leaves the live board but is NOT destroyed —
        # it is soft-deleted, kept in the database, and listed in the Trash.
        page.locator('.card', has_text="PERSIST-MARKER-alpha").first.click()
        page.wait_for_selector("#card-dialog[open]")
        page.click("#delete-card")
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        page.wait_for_timeout(500)
        check("db: deleting a question removes it from the live board",
              all(c["title"] != "PERSIST-MARKER-alpha" for c in api_state()["cards"]))
        check("db: a deleted question is kept in the Trash (recoverable, never destroyed)",
              any(c["title"] == "PERSIST-MARKER-alpha" for c in api_trash()["cards"]))

        # Second step of the two-step delete: permanently erase it from the Trash
        # panel in the History dialog — the ONLY action that truly destroys data.
        menu_click("#history-btn")
        page.wait_for_selector("#history-dialog[open]")
        page.wait_for_selector("#trash-section:not([hidden])")
        trash_rows = page.locator("#trash-list .history-row", has_text="PERSIST-MARKER-alpha")
        check("trash: deleted question is listed in the Trash panel",
              trash_rows.count() == 1)
        trash_rows.first.locator("button.danger").click()
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        page.wait_for_timeout(400)
        check("trash: Delete permanently erases it from the database for good",
              all(c["title"] != "PERSIST-MARKER-alpha" for c in api_trash()["cards"])
              and all(c["title"] != "PERSIST-MARKER-alpha" for c in api_state()["cards"]))
        page.click("#close-history")

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

        # ---- Soft-delete guard at the API layer -----------------------------
        # The core promise: a save that simply omits a question must archive it,
        # never hard-delete it — so a partial or buggy write can't lose data.
        base = api_state()["cards"]
        api_put(base + [{"id": "omit-guard-1", "title": "OMIT-GUARD-gamma", "columnId": "inbox"}])
        check("db: omit-guard question saved",
              any(c["title"] == "OMIT-GUARD-gamma" for c in api_state()["cards"]))
        api_put(base)  # save again WITHOUT the guard card
        check("db: a save that omits a question archives it, never hard-deletes",
              all(c["title"] != "OMIT-GUARD-gamma" for c in api_state()["cards"])
              and any(c["title"] == "OMIT-GUARD-gamma" for c in api_trash()["cards"]))

        # ---- Assistant view (brain service in offline fake mode) ------------
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        check("assistant: view opens with composer",
              page.get_attribute('.view-switch button[data-view="assistant"]', "aria-pressed") == "true")
        page.fill("#chat-input", "hello brain")
        page.click("#chat-send")
        page.wait_for_selector(".chat-msg.assistant")
        check("assistant: chat roundtrip through the Node proxy",
              "FAKE: hello brain" in page.inner_text(".chat-log"))

        page.fill("#chat-input", "add: What is Leiden clustering?")
        page.click("#chat-send")
        page.wait_for_function(
            "document.querySelectorAll('.chat-msg.assistant').length >= 2")
        check("assistant: tool chip shown for create_question",
              "create_question" in page.inner_text(".chat-log"))
        check("assistant: agent created the card via the board API",
              any(c["title"] == "What is Leiden clustering?" for c in api_state()["cards"]))
        page.locator('.view-switch button[data-view="board"]').click()
        check("assistant: created card visible on the board",
              page.locator(".card-title", has_text="What is Leiden clustering?").count() >= 1)

        # ---- Import the grown life sample (substitute) -----------------------
        sample_path = os.path.join(ROOT, "sample-overview.json")
        sample = json.load(open(sample_path))
        page.locator('.view-switch button[data-view="board"]').click()
        page.wait_for_selector("#board.board")
        page.set_input_files("#import-input", sample_path)
        page.wait_for_selector("#import-mode-dialog[open]")
        page.click("#import-replace")
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        page.wait_for_timeout(300)
        check("sample: the grown sample file imports, count matches",
              page.locator(".card").count() == len(sample["cards"]))
        page.wait_for_timeout(600)  # let the substitute reach the database
        srv_cards = api_state()["cards"]
        c10k = next((c for c in srv_cards if c["title"] == "Couch to 10k by October"), None)
        visa = next((c for c in srv_cards if "visa paperwork" in c["title"]), None)
        check("sample: effort/control and their provenance survive the save",
              c10k is not None and c10k.get("effort") == "high" and c10k.get("effortSrc") == "user"
              and visa is not None and visa.get("control") == "none" and visa.get("controlSrc") == "user")
        page.reload()
        page.wait_for_selector(".card")
        check("sample: board intact after reload", page.locator(".card").count() == len(sample["cards"]))

        # ---- Editor: effort & control --------------------------------------
        page.fill(".quick-add input", "EFFORT-CONTROL-probe")
        page.press(".quick-add input", "Enter")
        page.locator('[data-col="inbox"] .card', has_text="EFFORT-CONTROL-probe").first.click()
        page.wait_for_selector("#card-dialog[open]")
        check("editor: effort & control default to the middle of the scale",
              page.input_value("#card-effort") == "medium"
              and page.input_value("#card-control") == "influence")
        page.select_option("#card-effort", "low")
        page.select_option("#card-control", "act")
        page.click('#card-form button[type="submit"]')
        page.wait_for_timeout(600)
        probe = next((c for c in api_state()["cards"] if c["title"] == "EFFORT-CONTROL-probe"), None)
        check("editor: edited effort/control persist to the database as user-set",
              probe is not None and probe.get("effort") == "low" and probe.get("control") == "act"
              and probe.get("effortSrc") == "user" and probe.get("controlSrc") == "user")
        page.reload()
        page.wait_for_selector(".card")
        page.locator('[data-col="inbox"] .card', has_text="EFFORT-CONTROL-probe").first.click()
        page.wait_for_selector("#card-dialog[open]")
        check("editor: effort/control survive a reload round-trip",
              page.input_value("#card-effort") == "low"
              and page.input_value("#card-control") == "act")
        page.click("#cancel-dialog")

        # ---- Overview: PCA ⇄ t-SNE toggle -----------------------------------
        page.locator('.view-switch button[data-view="overview"]').click()
        page.wait_for_selector("#board.overview .plot-dot")
        n_dots = page.locator(".plot-dot").count()
        check("tsne: projection toggle renders with PCA pressed by default",
              page.locator(".plot-proj-toggle button").count() == 2
              and page.get_attribute('.plot-proj-toggle button[data-proj="pca"]', "aria-pressed") == "true"
              and "PC-1" in page.locator(".plot-axis-x").inner_text())
        pca_pos = page.eval_on_selector_all(
            ".plot-dot", "els => els.map(e => e.dataset.id + '@' + e.style.left + ',' + e.style.top)")
        page.click('.plot-proj-toggle button[data-proj="tsne"]')
        page.wait_for_selector('.plot-proj-toggle button[data-proj="tsne"][aria-pressed="true"]')
        check("tsne: axis labels switch to t-SNE-1/2",
              "t-SNE-1" in page.locator(".plot-axis-x").inner_text())
        page.wait_for_function(
            "document.querySelector('.plot-status').textContent.includes('t-SNE layout')",
            timeout=30000)
        check("tsne: one dot per question, unchanged by the projection",
              page.locator(".plot-dot").count() == n_dots)
        tsne_pos = page.eval_on_selector_all(
            ".plot-dot", "els => els.map(e => e.dataset.id + '@' + e.style.left + ',' + e.style.top)")
        check("tsne: layout actually moves the dots (differs from PCA)", tsne_pos != pca_pos)
        page.locator('.view-switch button[data-view="board"]').click()
        page.wait_for_selector("#board.board")
        page.locator('.view-switch button[data-view="overview"]').click()
        page.wait_for_selector("#board.overview .plot-dot")
        check("tsne: preference survives leaving and re-entering the view",
              page.get_attribute('.plot-proj-toggle button[data-proj="tsne"]', "aria-pressed") == "true")
        page.click('.plot-proj-toggle button[data-proj="pca"]')
        page.wait_for_selector('.plot-proj-toggle button[data-proj="pca"][aria-pressed="true"]')
        check("tsne: toggling back restores the PCA layout",
              "PC-1" in page.locator(".plot-axis-x").inner_text())

        # ---- Matrix: the four-lens picker -----------------------------------
        page.locator('.view-switch button[data-view="matrix"]').click()
        page.wait_for_selector("#board.matrix .matrix-grid")
        check("matrices: picker offers the four lenses",
              page.locator(".matrix-switch button").count() == 4
              and page.get_attribute('.matrix-switch button[data-matrix="eisenhower"]',
                                     "aria-pressed") == "true")
        check("matrices: Eisenhower 'Answer now' sits top-LEFT (urgent first)",
              page.eval_on_selector('.matrix-quad[data-imp="high"][data-urg="high"]',
                                    "el => getComputedStyle(el).gridColumnStart") == "2"
              and "answer now" in page.locator(
                  '.matrix-quad[data-imp="high"][data-urg="high"] .matrix-quad-verb')
                  .inner_text().lower())
        srv_cards = api_state()["cards"]
        n_importance = sum(1 for c in srv_cards if c.get("importance"))
        for lens in ("leverage", "serenity"):
            page.click(f'.matrix-switch button[data-matrix="{lens}"]')
            page.wait_for_selector(f'.matrix-switch button[data-matrix="{lens}"][aria-pressed="true"]')
            check(f"matrices: {lens} shows 6 cells and places every importance-set card",
                  page.locator(".matrix-quad").count() == 6
                  and page.locator(".matrix-quad-dots .plot-dot").count() == n_importance)
        page.click('.matrix-switch button[data-matrix="followthrough"]')
        page.wait_for_selector('.matrix-switch button[data-matrix="followthrough"][aria-pressed="true"]')
        n_ft = sum(1 for c in srv_cards
                   if c.get("importance") and c.get("columnId") != "answered")
        cols_with_dots = page.evaluate(
            """() => new Set([...document.querySelectorAll('.matrix-quad')]
                 .filter(q => q.querySelector('.plot-dot')).map(q => q.dataset.x)).size""")
        check("matrices: follow-through buckets open cards by age",
              page.locator(".matrix-quad-dots .plot-dot").count() == n_ft
              and cols_with_dots >= 2)
        page.click('.matrix-switch button[data-matrix="eisenhower"]')

        # ---- Areas view ------------------------------------------------------
        page.locator('.view-switch button[data-view="areas"]').click()
        page.wait_for_selector("#board.areas .area-tile")
        in_use = len({c["category"] for c in api_state()["cards"] if c.get("category")})
        check("areas: one tile per life area in use, wheel drawn",
              page.locator(".area-tile").count() == in_use
              and page.locator("svg.wheel").count() == 1)
        page.click('.area-tile[data-cat="money"]')
        page.wait_for_selector(".area-detail")
        check("areas: money detail shows the purchase cooling-off panel",
              page.locator(".cooloff-row").count() >= 3
              and "decide now" in page.eval_on_selector_all(
                  ".cooloff-days", "els => els.map(e => e.textContent).join('|')"))
        check("areas: money problems grouped on the serenity strip",
              page.locator(".serenity-strip .area-row").count() >= 1)
        page.click('.area-tile[data-cat="mind"]')
        page.wait_for_selector(".area-learning")
        check("areas: mind detail shows learning progress and a burn-up chart",
              page.locator(".learn-row").count() >= 2
              and page.locator(".learn-burnup polyline").count() == 2)
        page.click('.area-tile[data-cat="mind"]')  # clear the focus + category filter
        page.wait_for_timeout(100)
        check("areas: clicking the open tile again closes the detail",
              page.locator(".area-detail").count() == 0)
        page.screenshot(path=shot("areas.png"))

        # ---- Review view -----------------------------------------------------
        page.locator('.view-switch button[data-view="review"]').click()
        page.wait_for_selector("#board.review .review-tiles")
        check("review: the four stat tiles render",
              page.locator(".review-tile").count() == 4
              and all(page.locator(f'.review-tile[data-stat="{k}"]').count() == 1
                      for k in ("inbox", "answered-week", "new-week", "open")))
        check("review: resurfacing offers exactly 3 cards",
              page.locator(".resurface-card").count() == 3)
        target_id = page.get_attribute(".resurface-card", "data-id")
        page.click(f'.resurface-card[data-id="{target_id}"] .resurface-actions button:first-child')
        page.wait_for_selector(f'.resurface-card[data-id="{target_id}"][data-kept="true"]')
        page.wait_for_timeout(600)
        bumped = next((c for c in api_state()["cards"] if c["id"] == target_id), None)
        check("review: 'Still matters' bumps the card's updated stamp",
              bumped is not None
              and time.time() * 1000 - bumped["updatedAt"] < 60_000)
        page.click("#review-stamp")
        page.wait_for_selector(".review-stamped")
        page.reload()
        page.wait_for_selector("#board.review .review-tiles")
        check("review: the Reviewed stamp persists in localStorage",
              page.locator(".review-stamped").count() == 1)
        page.screenshot(path=shot("review.png"))
        page.locator('.view-switch button[data-view="board"]').click()
        page.wait_for_selector("#board.board")

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
    stop_server(brain)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
