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

# Write-triggered backups are exercised against a throwaway directory, and with
# rclone pointed at a path that does not exist. The suite must never add to the
# real backups/ history (it would evict genuine snapshots under the retention
# cap) and must never push a test board to Google Drive.
BACKUP_DIR = tempfile.mkdtemp(prefix="qboard-backups-")
NO_RCLONE = os.path.join(BACKUP_DIR, "no-such-rclone")


def snapshots():
    return [f for f in os.listdir(BACKUP_DIR)
            if f.startswith("board-") and f.endswith(".db")]


def wait_for_snapshots(n, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(snapshots()) >= n:
            break
        time.sleep(0.05)
    return snapshots()

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
             "AGENT_URL": f"http://127.0.0.1:{BRAIN_PORT}",
             "LODESTAR_BACKUP_ON_WRITE": "1", "LODESTAR_BACKUP_DIR": BACKUP_DIR,
             "LODESTAR_RCLONE_BIN": NO_RCLONE},
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
             "BRAIN_TRANSCRIBER": "fake",
             "BOARD_API_URL": f"http://127.0.0.1:{PORT}",
             # in-process Chroma: e2e must not depend on the Docker server,
             # and must never write into the user's real chat memory
             "BRAIN_CHROMA_URL": "memory",
             "BRAIN_CHAT_COLLECTION": "chat-e2e"},
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


# Synthetic microphone: getUserMedia returns a generated tone instead of asking
# for real hardware, so the voice-input flow runs unattended and headless.
MEDIA_ARGS = [
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
]


def launch_browser(p):
    # Prefer the system Chrome locally; fall back to bundled Chromium (CI).
    try:
        return p.chromium.launch(channel="chrome", headless=True, args=MEDIA_ARGS)
    except Exception:
        return p.chromium.launch(headless=True, args=MEDIA_ARGS)


# Back up the real board.db (local + Google Drive via rclone) before tests run.
# Never blocks the suite: the backup script always exits 0.
def backup_db():
    try:
        subprocess.run(["node", "scripts/backup-db.mjs"], cwd=ROOT, check=False,
                       timeout=180)
    except Exception as exc:  # pragma: no cover - defensive
        print("backup step skipped:", exc)


backup_db()
server = start_server()
brain = start_brain()
browser = None
try:
    with sync_playwright() as p:
        browser = launch_browser(p)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, accept_downloads=True
        )
        context.grant_permissions(
            ["clipboard-read", "clipboard-write", "microphone"], origin=URL)
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

        check("header: tagline reads 'Your compass for life!'",
              page.locator("header .tagline").inner_text() == "Your compass for life!")

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

        # Duplicate category name is rejected with an in-app alert.
        page.fill("#cat-add-name", "Reading")
        page.click("#cat-add-btn")
        page.wait_for_selector("#confirm-dialog[open]")
        check("categories: adding a duplicate name shows the 'already on the rail' alert",
              page.locator("#confirm-title").text_content().lower() == "already on the rail"
              and "already exists" in page.locator("#confirm-copy").text_content()
              and page.locator("#confirm-cancel").is_hidden())
        page.click("#confirm-ok")
        page.wait_for_timeout(100)
        check("categories: the duplicate was not added (still one 'reading' tab)",
              page.locator('.cat-tab[data-cat="reading"]').count() == 1)

        # Hue picker: pick a specific hue and confirm it lands on the created category.
        page.fill("#cat-add-name", "Fitness")
        page.check('#cat-hue-options input[value="120"]', force=True)
        page.click("#cat-add-btn")
        page.wait_for_timeout(650)  # debounced push
        fitness = next((c for c in api_state().get("categories", []) if c.get("id") == "fitness"), None)
        check("categories: the picked hue (120) is saved on the new category",
              fitness is not None and fitness.get("h") == 120)
        # Clean up Fitness so downstream count-based checks are unaffected.
        page.locator('#cats-list .cats-row:has-text("Fitness") .cats-remove').click()
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        page.wait_for_timeout(150)

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
        before_snapshots = len(snapshots())
        page.fill(".quick-add input", "What is speculative decoding?")
        page.press(".quick-add input", "Enter")
        first = page.locator('[data-col="inbox"] .card .card-title').first
        check("quick-add: new question at top of Inbox",
              first.inner_text() == "What is speculative decoding?")

        # Capturing a thought in the UI snapshots the database — the whole point
        # of the write-triggered backup.
        check("backup: adding a card through the UI produces a snapshot",
              len(wait_for_snapshots(before_snapshots + 1)) == before_snapshots + 1)

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
        # One sort menu per column header: a command-select that applies the
        # chosen order, then snaps back to its "Sort ⇅" placeholder.
        page.locator('[data-col="inbox"] .card', has_text="speculative decoding").first.press("]")
        page.wait_for_timeout(100)
        sort_values = page.locator('[data-col="in-progress"] .sort-select option').evaluate_all(
            "opts => opts.map(o => o.value)")
        check("sort: menu offers deadline, priority, type and age orders",
              {"deadline", "priority", "type", "newest", "oldest"} <= set(sort_values))
        page.select_option('[data-col="in-progress"] .sort-select', 'type')
        page.wait_for_timeout(100)
        first_badge = page.locator('[data-col="in-progress"] .card .badge').first
        check("sort: problem card sorts ahead of plans", "problem" in first_badge.inner_text().lower())
        check("sort: menu snaps back to its placeholder after sorting",
              page.input_value('[data-col="in-progress"] .sort-select') == "")

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

        # Backlog sort-by-type reorders the inbox rows by TYPE_RANK.
        # Row type stamps render as `.badge.type-<type>` (see renderBacklogRow/typeBadge
        # in app.js), not `.card-type` — the sort menu itself only renders when
        # more than one row is visible (renderBacklog in app.js).
        if page.locator(".backlog-sheet .sort-select").count() == 1:
            page.select_option(".backlog-sheet .sort-select", "type")
            page.wait_for_timeout(150)
            types_in_order = page.eval_on_selector_all(
                ".backlog-row .badge",
                "els => els.map(e => e.className.match(/type-(\\w+)/)[1])")
            rank = {"question": 0, "problem": 1, "task": 2, "idea": 3, "plan": 4}
            ranks = [rank.get(t, 99) for t in types_in_order]
            check("backlog: sort-by-type orders rows question→problem→task→idea→plan",
                  ranks == sorted(ranks))
        else:
            check("backlog: sort-by-type control present when >1 row", False)

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

        # ---- Automatic priority label ---------------------------------------
        # Priority is derived, never stored: 1 = urgent+important (red),
        # 2 = urgent+unimportant (orange), 3 = not-urgent+important (green),
        # 4 = neither (gray). Cards missing either judgement wear no label.
        # The matrix step above set "speculative decoding" to important + not
        # urgent, so it must already wear the P3 badge.
        spec_card = page.locator('.card', has_text="speculative decoding").first
        check("priority: important + not urgent card wears the P3 badge",
              spec_card.locator('.prio-badge[data-prio="3"]').count() == 1)

        # Exactly the cards with both judgements set wear a badge — no more.
        both_set = sum(1 for c in api_state()["cards"]
                       if c.get("importance") and c.get("urgency"))
        check("priority: badge appears exactly on cards with importance & urgency",
              page.locator(".card .prio-badge").count() == both_set)

        def set_judgements(title, importance, urgency, deadline=None):
            page.locator('.card', has_text=title).first.click()
            page.wait_for_selector("#card-dialog[open]")
            page.select_option("#card-importance", importance)
            page.select_option("#card-urgency", urgency)
            if deadline is not None:
                page.fill("#card-deadline", deadline)
            page.click('#card-form button[type="submit"]')
            page.wait_for_timeout(150)

        inbox_titles = page.locator('[data-col="inbox"] .card-title').all_inner_texts()
        probe_a, probe_b = inbox_titles[0], inbox_titles[1]

        # P1: flip the spec card to urgent + important.
        set_judgements("speculative decoding", "high", "high")
        check("priority: urgent + important card wears the P1 badge",
              page.locator('.card', has_text="speculative decoding")
                  .first.locator('.prio-badge[data-prio="1"]').count() == 1)

        # P2: urgent but unimportant; P4: neither urgent nor important.
        set_judgements(probe_a, "low", "high", deadline="2026-12-31")
        set_judgements(probe_b, "low", "low", deadline="2026-08-15")
        check("priority: urgent + unimportant card wears the P2 badge",
              page.locator('.card', has_text=probe_a)
                  .first.locator('.prio-badge[data-prio="2"]').count() == 1)
        check("priority: unimportant + not urgent card wears the P4 badge",
              page.locator('.card', has_text=probe_b)
                  .first.locator('.prio-badge[data-prio="4"]').count() == 1)

        # ---- Deadline field --------------------------------------------------
        page.locator('.card', has_text=probe_a).first.click()
        page.wait_for_selector("#card-dialog[open]")
        check("deadline: editor offers a date input",
              page.locator('#card-deadline[type="date"]').count() == 1)
        check("deadline: editor shows the saved date",
              page.input_value("#card-deadline") == "2026-12-31")
        page.click("#cancel-dialog")

        check("deadline: card shows its deadline chip",
              "2026-12-31" in page.locator('.card', has_text=probe_a)
                                 .first.locator(".card-deadline").inner_text())
        page.wait_for_timeout(450)
        srv = next((c for c in api_state()["cards"] if c["title"] == probe_a), None)
        check("deadline: persists to the database",
              srv is not None and srv.get("deadline") == "2026-12-31")

        # ---- Sort by deadline ------------------------------------------------
        # Earliest deadline first; undated cards keep to the back of the column.
        check("sort: inbox header offers the sort menu",
              page.locator('[data-col="inbox"] .sort-select').count() == 1)
        page.select_option('[data-col="inbox"] .sort-select', 'deadline')
        page.wait_for_timeout(100)
        sorted_titles = page.locator('[data-col="inbox"] .card-title').all_inner_texts()
        check("sort: by deadline puts the earlier-dated card first",
              sorted_titles[0] == probe_b and sorted_titles[1] == probe_a)
        check("sort: by deadline keeps undated cards at the back",
              len(sorted_titles) == len(inbox_titles)
              and all(t in sorted_titles for t in inbox_titles))

        # ---- Sort by priority ------------------------------------------------
        # P1 first, unlabelled cards last; ranks must come out non-decreasing.
        def prio_rank(c):
            if not c.get("importance") or not c.get("urgency"):
                return 5
            return {("high", "high"): 1, ("low", "high"): 2,
                    ("high", "low"): 3, ("low", "low"): 4}[(c["importance"], c["urgency"])]

        page.select_option('[data-col="inbox"] .sort-select', 'priority')
        page.wait_for_timeout(450)
        all_cards = api_state()["cards"]
        by_title = {c["title"]: c for c in all_cards}
        ranks = [prio_rank(by_title[t])
                 for t in page.locator('[data-col="inbox"] .card-title').all_inner_texts()]
        check("sort: by priority orders P1 → P4 with unlabelled last",
              ranks == sorted(ranks) and len(ranks) == len(inbox_titles))

        # ---- Priority filter --------------------------------------------------
        # Lives in the toolbar right beside the type filter; narrows every view.
        check("filter: priority select sits beside the type filter",
              page.evaluate("document.querySelector('#prio-filter')"
                            "?.previousElementSibling?.id === 'type-filter'"))
        p2_expected = sum(1 for c in all_cards if prio_rank(c) == 2)
        page.select_option("#prio-filter", "2")
        page.wait_for_timeout(100)
        check("filter: P2 narrows the board to urgent+unimportant cards",
              page.locator(".card").count() == p2_expected
              and page.locator(".card", has_text=probe_a).count() == 1)
        page.select_option("#prio-filter", "")
        page.wait_for_timeout(100)
        check("filter: clearing the priority filter restores the board",
              page.locator(".card").count() == len(all_cards))

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

        # ---- Import: ADD adopts categories the board doesn't have yet -------
        cat_import = shot("generated-cat-import.json")
        with open(cat_import, "w") as f:
            json.dump({"version": 1,
                       "categories": [{"id": "garden", "label": "Garden", "h": 120}],
                       "cards": [{"title": "Imported: plant tomatoes", "category": "garden"}]}, f)
        page.set_input_files("#import-input", cat_import)
        page.wait_for_selector("#import-mode-dialog[open]")
        page.click("#import-add")
        page.wait_for_timeout(200)
        check("import: add mode adopts a new category from the file onto the rail",
              page.locator('.cat-tab[data-cat="garden"]').count() == 1)

        # ---- Decisional-balance preview ---------------------------------------
        page.fill(".quick-add input", "Should we adopt the new framework?")
        page.press(".quick-add input", "Enter")
        page.wait_for_timeout(150)
        page.locator('[data-col="inbox"] .card', has_text="new framework").first.click()
        page.wait_for_selector("#card-dialog[open]")
        page.fill("#card-tags", "decision")
        page.fill("#card-notes", "+ faster builds\n+ better types\n- migration cost\n- team ramp-up")
        page.dispatch_event("#card-notes", "input")
        page.wait_for_timeout(100)
        check("balance: pro/con preview shows when tagged 'decision' with +/- notes",
              page.locator("#balance-preview").is_visible()
              and page.locator(".balance-pro ul li").count() == 2
              and page.locator(".balance-con ul li").count() == 2)
        # Close the dialog without saving via the dialog's own Cancel control.
        page.click("#cancel-dialog")
        page.wait_for_timeout(100)

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

        # ---- Assistant model settings ----------------------------------------
        # A "Models" panel with three pickers. Only the text-generation pick
        # changes behaviour today (it rides along on every chat request); the
        # omni and embedding picks are stored preferences for the brain's
        # coming media/RAG features. All three persist in localStorage.
        DEFAULT_TEXT = "moonshotai/kimi-k3"
        DEFAULT_OMNI = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
        DEFAULT_EMBED = "nvidia/llama-nemotron-embed-vl-1b-v2:free"
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        check("assistant: settings panel offers the three model pickers",
              page.locator(".chat-settings").count() == 1
              and page.locator("#model-text").count() == 1
              and page.locator("#model-omni").count() == 1
              and page.locator("#model-embed").count() == 1)
        check("assistant: pickers default to the chosen slugs",
              page.input_value("#model-text") == DEFAULT_TEXT
              and page.input_value("#model-omni") == DEFAULT_OMNI
              and page.input_value("#model-embed") == DEFAULT_EMBED)

        n_replies = page.locator(".chat-msg.assistant").count()
        page.fill("#chat-input", "model ride-along probe")
        with page.expect_request("**/api/agent/chat") as req_info:
            page.click("#chat-send")
        check("assistant: chat request carries the picked text model",
              f'"{DEFAULT_TEXT}"' in (req_info.value.post_data or ""))
        page.wait_for_function(
            f"document.querySelectorAll('.chat-msg.assistant').length >= {n_replies + 1}")

        page.select_option("#model-text", "openai/gpt-4o-mini")
        page.reload()
        page.wait_for_selector("#board")
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#model-text")
        check("assistant: model choice survives a reload",
              page.input_value("#model-text") == "openai/gpt-4o-mini")
        page.select_option("#model-text", DEFAULT_TEXT)

        # ---- Assistant error path: a 503 renders the friendly unavailable message.
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        n_before = len(errors)
        page.route("**/api/agent/chat", lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"error":"assistant unavailable"}'))
        page.fill("#chat-input", "This should fail")
        page.click("#chat-send")
        page.wait_for_selector(".chat-msg.assistant.error")
        check("assistant: a failed request shows the unavailable message",
              "assistant is unavailable" in page.locator(".chat-msg.assistant.error").last.inner_text())
        page.unroute("**/api/agent/chat")
        # The 503 we deliberately provoked surfaces as a browser console error;
        # it's expected here, not a real bug, so it shouldn't fail the console check.
        # Scrub only the entries provoked by this block (by count), so an unrelated
        # future error that happens to contain "503" isn't silently masked.
        provoked = [e for e in errors[n_before:] if "503" in e]
        check("assistant error surfaced a console error to scrub", len(provoked) >= 1)
        for e in provoked:
            errors.remove(e)
        page.locator('.view-switch button[data-view="board"]').click()

        # ---- Assistant voice input -------------------------------------------
        # Chrome feeds getUserMedia a synthetic tone (MEDIA_ARGS), the browser
        # encodes it to WAV, and the brain's fake transcriber answers offline —
        # so this is a real end-to-end dictation with no hardware and no network.
        FAKE_TRANSCRIPT = "FAKE TRANSCRIPT: hello from the microphone"
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        check("voice: mic button sits in the composer",
              page.locator(".chat-composer .chat-mic").count() == 1
              and page.is_enabled(".chat-mic"))

        page.click(".chat-mic")
        page.wait_for_selector(".chat-recording")
        check("voice: recording bar offers stop and cancel",
              page.locator(".chat-recording .chat-stop").count() == 1
              and page.locator(".chat-recording .chat-cancel").count() == 1)
        check("voice: mic reads as pressed while recording",
              page.get_attribute(".chat-mic", "aria-pressed") == "true")

        # Escape discards the take: no request fired, nothing typed.
        page.wait_for_timeout(600)
        page.keyboard.press("Escape")
        page.wait_for_selector(".chat-recording", state="detached")
        check("voice: Escape cancels the recording and types nothing",
              page.input_value("#chat-input") == "")

        n_user_msgs = page.locator(".chat-msg.user").count()
        page.click(".chat-mic")
        page.wait_for_selector(".chat-recording")
        page.wait_for_timeout(1200)
        with page.expect_request("**/api/agent/transcribe") as req_info:
            page.click(".chat-stop")
        req_body = json.loads(req_info.value.post_data or "{}")
        check("voice: request carries base64 wav audio and the picked omni model",
              req_body.get("format") == "wav"
              and len(req_body.get("audio") or "") > 1000
              and req_body.get("model") == DEFAULT_OMNI)
        page.wait_for_function(
            "document.querySelector('#chat-input').value.includes('FAKE TRANSCRIPT')")
        check("voice: transcript lands in the composer",
              FAKE_TRANSCRIPT in page.input_value("#chat-input"))
        check("voice: transcript is not auto-sent",
              page.locator(".chat-msg.user").count() == n_user_msgs)
        page.screenshot(path=shot("voice-transcript.png"))

        # Never lose a thought: dictation appends to what is already typed.
        page.fill("#chat-input", "note: ")
        page.click(".chat-mic")
        page.wait_for_selector(".chat-recording")
        page.wait_for_timeout(1200)
        page.click(".chat-stop")
        page.wait_for_function(
            "document.querySelector('#chat-input').value.includes('FAKE TRANSCRIPT')")
        check("voice: a new transcript appends instead of replacing",
              page.input_value("#chat-input").startswith("note: ")
              and FAKE_TRANSCRIPT in page.input_value("#chat-input"))

        # Error path: a failed transcription says so and keeps the typed text.
        page.fill("#chat-input", "keep me")
        n_before = len(errors)
        page.route("**/api/agent/transcribe", lambda route: route.fulfill(
            status=503, content_type="application/json",
            body='{"error":"assistant unavailable"}'))
        page.click(".chat-mic")
        page.wait_for_selector(".chat-recording")
        page.wait_for_timeout(800)
        page.click(".chat-stop")
        page.wait_for_selector(".chat-voice-error")
        check("voice: a failed transcription is reported, not silent",
              "transcribe" in page.inner_text(".chat-voice-error").lower())
        check("voice: a failed transcription leaves the typed text alone",
              page.input_value("#chat-input") == "keep me")
        page.unroute("**/api/agent/transcribe")
        provoked = [e for e in errors[n_before:] if "503" in e]
        check("voice error surfaced a console error to scrub", len(provoked) >= 1)
        for e in provoked:
            errors.remove(e)
        page.fill("#chat-input", "")

        # A thinking assistant takes the mic out of service. The delay has to
        # live in the browser, not in a route handler: blocking inside a sync-API
        # handler stalls Playwright's dispatcher too, so wait_for_timeout below
        # would not return until the reply had already landed.
        page.evaluate("""() => {
          window.__origFetch = window.fetch;
          window.fetch = (url, opts) => String(url).includes('/api/agent/chat')
            ? new Promise((r) => setTimeout(() => r(window.__origFetch(url, opts)), 1500))
            : window.__origFetch(url, opts);
        }""")
        page.fill("#chat-input", "make the assistant busy")
        page.click("#chat-send")
        page.wait_for_timeout(300)
        check("voice: mic is disabled while the assistant is thinking",
              page.locator(".chat-mic").is_disabled())
        page.evaluate("() => { window.fetch = window.__origFetch; }")
        # Reply lands ~1.2s later; the mic comes back with it.
        page.wait_for_function("!document.querySelector('.chat-mic').disabled")
        page.locator('.view-switch button[data-view="board"]').click()

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

        # The wheel's area names sit outside the ring, so the SVG viewport has to
        # make room for them: any label whose ink crosses the viewBox edge gets
        # sliced off ("COACHING 3" rendering as "NG 3"). Measured in user space,
        # where getBBox() and viewBox share units.
        clipped_labels = """() => {
          const svg = document.querySelector('svg.wheel');
          const vb = svg.viewBox.baseVal, EPS = 0.5;
          return [...svg.querySelectorAll('text')].filter((t) => {
            const b = t.getBBox();
            return b.x < vb.x - EPS || b.x + b.width > vb.x + vb.width + EPS
                || b.y < vb.y - EPS || b.y + b.height > vb.y + vb.height + EPS;
          }).map((t) => t.textContent);
        }"""
        clipped = page.evaluate(clipped_labels)
        check("areas: no wheel label is clipped by the SVG viewport",
              clipped == [])
        if clipped:
            print("   clipped:", clipped)
        page.screenshot(path=shot("areas.png"))

        # A long area name must not spill off the wheel either — the ring makes
        # room for the widest label, not the other way round.
        page.locator('.view-switch button[data-view="board"]').click()
        page.wait_for_selector("#board.board")
        page.click("#edit-cats-btn")
        page.wait_for_selector("#cats-dialog[open]")
        page.fill("#cat-add-name", "Photography")
        page.click("#cat-add-btn")
        page.wait_for_timeout(150)
        page.click("#close-cats")
        page.wait_for_timeout(100)
        page.locator('[data-col="inbox"] .card').first.click()
        page.wait_for_selector("#card-dialog[open]")
        page.locator('.category-picker label:has(input[value="photography"])').click()
        page.click('#card-form button[type="submit"]')
        page.wait_for_timeout(100)
        page.locator('.view-switch button[data-view="areas"]').click()
        page.wait_for_selector("#board.areas svg.wheel")
        long_clipped = page.evaluate(clipped_labels)
        check("areas: a long area name still fits inside the wheel viewport",
              page.locator("svg.wheel text", has_text="Photography").count() == 1
              and long_clipped == [])
        if long_clipped:
            print("   clipped:", long_clipped)
        page.screenshot(path=shot("areas-long-label.png"))

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

        # ---- Server-offline banner + recovery -------------------------------
        def block_state_put(route):
            if route.request.method == "PUT":
                route.abort()
            else:
                route.continue_()

        # Force the next debounced PUT /api/state to fail.
        n_before = len(errors)
        page.route("**/api/state", block_state_put)
        page.fill(".quick-add input", "Trigger an offline push")
        page.press(".quick-add input", "Enter")
        page.wait_for_timeout(400)  # debounce is 150ms + failure
        check("offline: PUT failure announces the local-save message",
              "saved locally" in page.locator("#live-region").inner_text())
        # Recovery: stop aborting, trigger another push, expect the reconnect message.
        page.unroute("**/api/state", block_state_put)
        page.fill(".quick-add input", "Trigger a reconnect push")
        page.press(".quick-add input", "Enter")
        page.wait_for_timeout(400)
        check("offline: a later successful push announces reconnection",
              "Reconnected" in page.locator("#live-region").inner_text())
        # The aborted PUT we deliberately provoked surfaces as a browser console
        # error; it's expected here, not a real bug, so it shouldn't fail the check.
        # Scrub only the entries provoked by this block (by count), so an unrelated
        # future error that happens to contain "ERR_FAILED" isn't silently masked.
        provoked = [e for e in errors[n_before:] if "ERR_FAILED" in e]
        check("offline route-abort surfaced a console error to scrub", len(provoked) >= 1)
        for e in provoked:
            errors.remove(e)

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
