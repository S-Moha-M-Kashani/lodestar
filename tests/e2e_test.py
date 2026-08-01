"""End-to-end verification of Lodestar.

Launches the real SQLite-backed server, drives the app in headless Chrome, and
exercises every button and flow — including the in-app confirm dialogs and that
the board actually persists to (and deletes from) the database.

    uv run --with playwright python tests/e2e_test.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("TEST_PORT", "8799"))
BRAIN_PORT = int(os.environ.get("TEST_BRAIN_PORT", "8798"))
# The RAG lab is the one upstream this suite deliberately never starts, because
# the absent-lab panel is part of what it checks. So the proxy is pinned at a
# port nothing listens on: left unset, server.js falls back to :9002 and the
# suite would reach a real lab a developer happens to have running, turning
# three checks red on their machine and green in CI.
RAGLAB_PORT = int(os.environ.get("TEST_RAGLAB_PORT", "8797"))
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


def wait_until(cond, timeout=5.0):
    """Poll until cond() holds, then report whether it does.

    For the case where the thing to wait for is what is being asserted, and no
    selector expresses it: waiting for a *proxy* condition is how both of this
    suite's real flakes happened. Returns rather than raises, because a check
    that times out must be one red line among the results, not an exception
    that abandons every check after it."""
    deadline = time.time() + timeout
    while time.time() < deadline and not cond():
        time.sleep(0.05)
    return cond()

ARTIFACTS = os.path.join(os.path.dirname(__file__), "artifacts")
os.makedirs(ARTIFACTS, exist_ok=True)
shot = lambda name: os.path.join(ARTIFACTS, name)
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


# A port this suite needs must be free before anything is started on it. The
# readiness probes below cannot tell our server from a stranger's: a board left
# behind by an earlier crashed run answers /api/state instantly, so the probe
# green-lights it and the whole suite drives someone else's database. Measured
# once as 22 phantom failures beginning at "seed: 6 cards on first run", with
# nothing in the output pointing at the port. :8797 is included because the
# suite deliberately never starts a lab there — a real one left running would
# turn the "lab is not running" checks red for the person working on the lab.
def require_free(port, what):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
    except OSError:
        return
    raise RuntimeError(
        f":{port} is already serving something, so the {what} cannot start "
        f"there and this run would silently measure whatever is. Stop it first "
        f"(lsof -nP -iTCP:{port} -sTCP:LISTEN).")


def start_server():
    require_free(PORT, "test board")
    require_free(RAGLAB_PORT, "absent RAG lab the proxy checks for")
    proc = subprocess.Popen(
        ["node", "server.js"],
        cwd=ROOT,
        env={**os.environ, "PORT": str(PORT), "BOARD_DB": DB_PATH, "NODE_NO_WARNINGS": "1",
             "AGENT_URL": f"http://127.0.0.1:{BRAIN_PORT}",
             "RAGLAB_URL": f"http://127.0.0.1:{RAGLAB_PORT}",
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
    require_free(BRAIN_PORT, "test brain")
    proc = subprocess.Popen(
        ["uv", "run", "--project", "brain", "uvicorn",
         "lodestar_brain.server:app", "--port", str(BRAIN_PORT)],
        cwd=ROOT,
        env={**os.environ, "BRAIN_LLM": "fake", "BRAIN_EMBEDDER": "fake",
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


def api_proposals():
    with urllib.request.urlopen(URL + "/api/proposals", timeout=3) as r:
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
        check("import: cards added on top of the existing board",
              page.locator(".card").count() == count_before_import + 2)
        check("import: added card kept its column, defaults fill the gaps",
              page.locator('[data-col="in-progress"] .card', has_text="offsite").count() == 1
              and page.locator('[data-col="inbox"] .card', has_text="onboarding doc").count() == 1)
        nums = page.locator("#board .card-num").all_inner_texts()
        check("import: ledger numbers stay unique after adding",
              len(nums) == len(set(nums)) and all(n.startswith("C-0") for n in nums))

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

        # ---- localStorage keys: 'question-board:*' → 'lodestar:*' ----
        # This is an end-to-end test.
        # The prefix was renamed with the board's word, and several of the ten
        # keys hold data that exists nowhere else — the undo timeline, Review
        # state, model picks. So the migration copies rather than moves, and a
        # value changed since migrating must survive the next boot.
        mig = browser.new_context(viewport={"width": 1200, "height": 800})
        mig_page = mig.new_page()
        mig_page.goto(URL)
        mig_page.wait_for_selector(".card")
        mig_page.evaluate("""() => {
          localStorage.clear();
          localStorage.setItem('question-board:theme', 'night');
          localStorage.setItem('question-board:reviewed', '12345');
          localStorage.setItem('question-board:habit-mute', '');
        }""")
        mig_page.reload()
        mig_page.wait_for_selector(".card")
        get = lambda k: mig_page.evaluate("k => localStorage.getItem(k)", k)
        check("migration: legacy keys are copied under the lodestar: prefix",
              get("lodestar:theme") == "night" and get("lodestar:reviewed") == "12345")
        # '' is a real stored value, so the copy must test for null, not falsiness.
        check("migration: an empty legacy value is copied, not skipped",
              get("lodestar:habit-mute") == "")
        check("migration: the legacy keys survive, so an older build still finds them",
              get("question-board:theme") == "night")
        mig_page.evaluate("() => localStorage.setItem('lodestar:theme', 'morning')")
        mig_page.reload()
        mig_page.wait_for_selector(".card")
        check("migration: a value changed after migrating is not clobbered on the next boot",
              get("lodestar:theme") == "morning")
        mig.close()

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
        # Waiting for the section is not waiting for this row. refreshTrash()
        # unhides it only after awaiting the fetch, so an earlier delete (line
        # ~484) has already left it unhidden and the wait returns at once —
        # against the previous render, before this card's row is appended.
        check("trash: deleted question is listed in the Trash panel",
              wait_until(lambda: trash_rows.count() == 1))
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

        # ---- Agent card confirmation gate -----------------------------------
        # A card the agent invents is a PROPOSAL: nothing reaches the board until
        # the user approves it.
        board_before = len(api_state()["cards"])
        page.fill("#chat-input", "add: What is Leiden clustering?")
        page.click("#chat-send")
        page.wait_for_function(
            "document.querySelectorAll('.chat-msg.assistant').length >= 2")
        check("assistant: tool chip shown for create_card",
              "create_card" in page.inner_text(".chat-log"))
        # The chip used to be the whole story: a tool's name, with what it was
        # asked and what it answered thrown away. Both are on the wire already.
        step = page.locator(".chat-step").last
        # Lowercased throughout: inner_text returns *rendered* text, and the
        # field labels are uppercased by CSS. Asserting on the styling would
        # make a design change look like a broken feature.
        check("assistant: a tool step stays collapsed until it is asked to open",
              "arguments" not in step.inner_text().lower())
        # Guarded rather than clicked outright: a missing summary must be one red
        # line, not a TimeoutError that abandons every check after this one.
        expandable = step.locator("summary").count() == 1
        if expandable:
            step.locator("summary").click()
        opened = step.inner_text().lower() if expandable else ""
        check("assistant: expanding a step shows its arguments and its result",
              "arguments" in opened and "result" in opened
              and "leiden clustering" in opened and "pending" in opened)

        # ---- Sources: three tools, three result shapes, one list -------------
        # Stubbed rather than provoked through the fake model: web_search needs
        # the network and each tool answers in its own shape, so a reader that
        # misreads one is the way this breaks. The steps are repeated inside
        # `done` because `done` is the record of the turn — the client replaces
        # what it streamed with it, and a stub that omitted them would pass
        # while the real contract was broken.
        seed = api_state()["cards"][0]
        source_steps = [
            {"tool": "web_search", "arguments": {"query": "rrf"},
             "result": [{"title": "RRF explained", "url": "https://example.com/rrf",
                         "snippet": "reciprocal rank fusion, briefly"}]},
            {"tool": "find_related", "arguments": {"text": "rrf"},
             "result": [{"card": {"id": seed["id"], "title": seed["title"],
                                  "columnId": seed["columnId"], "tags": []},
                         "rank": 1}]},
            {"tool": "recall_chat", "arguments": {"text": "rrf"},
             "result": [{"text": "we discussed fusion last week", "score": 0.8,
                         "metadata": {"role": "user"}}]},
        ]
        done = {"reply": "It is covered at https://example.com/rrf in detail.",
                "mutated": False, "proposed": False, "steps": source_steps}
        sse = "".join(f"event: step\ndata: {json.dumps(s)}\n\n" for s in source_steps)
        sse += f"event: done\ndata: {json.dumps(done)}\n\n"
        page.route("**/api/agent/chat/stream", lambda route: route.fulfill(
            status=200, content_type="text/event-stream", body=sse))
        n_before = page.locator(".chat-msg.assistant").count()
        page.fill("#chat-input", "where is rrf explained?")
        page.click("#chat-send")
        page.wait_for_function(
            f"document.querySelectorAll('.chat-msg.assistant').length > {n_before}")
        # wait_until, not wait_for_selector: a missing sources list is the very
        # thing under test, and it must read as red lines rather than a
        # TimeoutError that abandons every check after this one.
        cited = wait_until(lambda: page.locator(".chat-source").count() == 3)
        reply = page.locator(".chat-msg.assistant").last

        check("assistant: a url in the reply is a real link, not inert text",
              reply.locator("a.chat-link").count() == 1
              and reply.locator("a.chat-link").get_attribute("href")
                  == "https://example.com/rrf"
              and reply.locator("a.chat-link").get_attribute("rel") == "noopener noreferrer")
        listed = reply.locator(".chat-source").all_inner_texts()
        check("assistant: all three retrieval tools feed one sources list",
              cited
              and any("RRF explained" in t for t in listed)
              and any(seed["title"] in t for t in listed)
              and any("discussed fusion last week" in t for t in listed))
        check("assistant: a web source links out, a recalled snippet does not",
              reply.locator(".chat-source a[href='https://example.com/rrf']").count() == 1
              and reply.locator(".chat-source a").count() == 1)

        opens_card = reply.locator(".chat-source-card").count() == 1
        if opens_card:
            reply.locator(".chat-source-card").click()
        check("assistant: a retrieved card source opens that card",
              opens_card and wait_until(lambda: page.locator("#card-dialog[open]").count() == 1)
              and page.input_value("#card-title") == seed["title"])
        if opens_card:
            page.keyboard.press("Escape")
        page.unroute("**/api/agent/chat/stream")

        # ---- Recall: /rag/recall has existed with no UI at all ---------------
        # Until now the only route to a past conversation was to ask the agent
        # and hope it chose the tool. Earlier turns in this run were recorded by
        # the brain's in-memory Chroma, so there is real history to find.
        # Every interaction below is behind `has_panel`: driving a control that
        # is not there raises and abandons the rest of the suite, where what is
        # wanted is one red line per broken expectation.
        has_panel = page.locator(".chat-recall").count() == 1
        check("assistant: the recall panel is closed until it is asked for",
              has_panel and not page.locator("#recall-input").is_visible())

        recalled = found_text = said_off = off_text = False
        if has_panel:
            page.locator(".chat-recall summary").click()
            page.fill("#recall-input", "Leiden clustering")
            page.click("#recall-search")
            recalled = wait_until(lambda: page.locator(".recall-hit").count() > 0,
                                  timeout=10.0)
            found_text = "leiden" in page.locator(".chat-recall").inner_text().lower()
        check("assistant: searching past conversations finds an earlier exchange",
              recalled and found_text)

        # An empty list means two opposite things, and the brain now says which.
        # Reporting a switched-off memory as "no matches" sends the user hunting
        # for a conversation that was never recordable.
        if has_panel:
            page.route("**/api/rag/recall", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body='{"matches": [], "memory": false}'))
            page.fill("#recall-input", "anything at all")
            page.click("#recall-search")
            said_off = wait_until(
                lambda: "memory is off"
                in page.locator(".chat-recall").inner_text().lower())
            off_text = page.locator(".chat-recall").inner_text().lower()
            page.unroute("**/api/rag/recall")
        check("assistant: memory being off is not reported as 'no matches'",
              said_off and "no matches" not in off_text
              and page.locator(".recall-hit").count() == 0)
        check("gate: the proposed card is NOT on the board yet",
              not any(c["title"] == "What is Leiden clustering?"
                      for c in api_state()["cards"])
              and len(api_state()["cards"]) == board_before)
        check("gate: it is waiting in the proposals list",
              any(c["title"] == "What is Leiden clustering?"
                  for c in api_proposals()["cards"]))

        page.wait_for_selector(".proposal")
        check("gate: the proposal renders in the Assistant view with both actions",
              page.locator(".proposal").count() == 1
              and "Leiden clustering" in page.inner_text(".proposal-title")
              and page.locator(".proposal-approve").count() == 1
              and page.locator(".proposal-reject").count() == 1)
        check("gate: the Assistant tab carries a count badge",
              page.locator('.view-switch button[data-view="assistant"] .view-badge')
                  .inner_text().strip() == "1")

        page.click(".proposal-approve")
        page.wait_for_function("document.querySelectorAll('.proposal').length === 0")
        check("gate: approving puts the card on the board",
              any(c["title"] == "What is Leiden clustering?" for c in api_state()["cards"]))
        check("gate: the badge clears once nothing is pending",
              page.locator('.view-switch button[data-view="assistant"] .view-badge').count() == 0)
        page.locator('.view-switch button[data-view="board"]').click()
        approved = page.locator(".card", has_text="What is Leiden clustering?").first
        check("gate: approved card visible on the board", approved.count() >= 1)
        # ensureNums must run on adopt, or the confirmed card shows C-000.
        check("gate: the approved card gets a real ledger number, not C-000",
              approved.locator(".card-num").inner_text().strip() not in ("C-000", ""))

        # Rejecting sends the proposal to the Trash rather than erasing it.
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        page.fill("#chat-input", "add: A thought I do not want")
        page.click("#chat-send")
        page.wait_for_selector(".proposal")
        page.click(".proposal-reject")
        page.wait_for_function("document.querySelectorAll('.proposal').length === 0")
        check("gate: rejecting clears it from the proposals list",
              not any(c["title"] == "A thought I do not want"
                      for c in api_proposals()["cards"]))
        check("gate: a rejected proposal is not on the board",
              not any(c["title"] == "A thought I do not want"
                      for c in api_state()["cards"]))
        check("gate: a rejected proposal is recoverable from the Trash",
              any(c["title"] == "A thought I do not want" for c in api_trash()["cards"]))

        # ---- Assistant model settings ----------------------------------------
        # A "Models" panel with three pickers. Only the text-generation pick
        # changes behaviour today (it rides along on every chat request); the
        # omni and embedding picks are stored preferences for the brain's
        # coming media/RAG features. All three persist in localStorage.
        # The text picker is local-first: it offers the models pulled on this
        # machine, and OpenRouter is one explicit selector away. All three local
        # slugs were verified against `ollama list` rather than assumed.
        # moonshotai/kimi-k3 and openai/gpt-4o-mini are retired, and so is
        # openrouter/auto: it is deprecated, it routes to a different model per
        # request, and the brain never reads the resolved slug back out of the
        # response — so a slow or badly tool-calling turn was unattributable.
        DEFAULT_TEXT = "4skl/gemma4-e2b-mtp"
        ALT_TEXT = "gemma4:e2b"
        THIRD_TEXT = "deepseek-r1:8b"
        RETIRED_TEXT = ["moonshotai/kimi-k3", "openai/gpt-4o-mini", "openrouter/auto"]

        # Every option now says which route it takes, so the label is no longer
        # the slug. Assert on the values — the label carries its own check below.
        def option_values(selector):
            return page.locator(f"{selector} option").evaluate_all(
                "os => os.map(o => o.value)")
        # Every omni option must be a model that actually receives audio.
        # nemotron:free advertises audio input but its provider discards the
        # input_audio part, so every dictation came back an invented apology; it
        # was kept selectable only for being free. It is gone now: OpenRouter has
        # exactly one free audio-input model and that is it, so "free" was never
        # a working choice — free dictation is Parakeet's job, locally and offline
        # (BRAIN_TRANSCRIBER defaults to parakeet). Voxtral replaces it: a
        # purpose-built speech model at the same price as the default.
        # openai/whisper-large-v3-turbo was briefly the default and is not one:
        # measured on 2026-07-31, OpenRouter's published catalogue is 337 models
        # and holds no whisper, embedding or rerank entry, so it can transcribe
        # nothing. The picker is the remote route by definition — local dictation
        # is Parakeet's job inside the brain, which ignores this pick entirely, so
        # the local checkpoint is not an option here either.
        DEFAULT_OMNI = "google/gemini-2.5-flash-lite"
        ALT_OMNI = ["openai/gpt-audio-mini", "mistralai/voxtral-small-24b-2507"]
        BROKEN_OMNI = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
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
        omni_options = option_values("#model-omni")
        check("assistant: the audio-dropping free model is gone from the picker",
              BROKEN_OMNI not in omni_options)
        check("assistant: the audio picker offers only models that take audio",
              omni_options == [DEFAULT_OMNI, *ALT_OMNI])
        text_options = option_values("#model-text")
        check("assistant: the text picker offers the local default and alternatives",
              text_options == [DEFAULT_TEXT, ALT_TEXT, THIRD_TEXT])
        check("assistant: the retired text models are gone from the picker",
              not any(slug in text_options for slug in RETIRED_TEXT))
        # ---- Local-first: the text provider is a choice, and every option says
        # where it runs. Free-and-private against billed-and-remote is the one
        # difference a picker must never leave implicit, so the label carries the
        # route rather than the explainer alone.
        check("assistant: the panel offers a text provider selector",
              page.locator("#model-provider").count() == 1)
        check("assistant: the provider defaults to the local daemon",
              page.input_value("#model-provider") == "ollama")
        check("assistant: the provider selector offers exactly local and remote",
              option_values("#model-provider") == ["ollama", "openrouter"])
        text_labels = page.locator("#model-text option").all_inner_texts()
        check("assistant: every local text option says it runs locally",
              all("local" in label for label in text_labels))
        omni_labels = page.locator("#model-omni option").all_inner_texts()
        check("assistant: every audio option names the remote route it bills",
              all("OpenRouter API" in label for label in omni_labels))
        page.select_option("#model-provider", "openrouter")
        check("assistant: choosing OpenRouter switches the text list to remote models",
              option_values("#model-text") == ["openai/gpt-5-nano"])
        check("assistant: the remote text option says which API serves it",
              all("OpenRouter API" in label
                  for label in page.locator("#model-text option").all_inner_texts()))
        with page.expect_request("**/api/agent/chat/stream") as prov_req:
            page.fill("#chat-input", "provider ride-along probe")
            page.click("#chat-send")
        check("assistant: the chosen provider rides along on the chat request",
              '"provider":"openrouter"' in (prov_req.value.post_data or "").replace(" ", ""))
        page.wait_for_selector(".chat-msg.assistant")
        page.select_option("#model-provider", "ollama")
        check("assistant: switching back restores the local list and its default",
              option_values("#model-text") == [DEFAULT_TEXT, ALT_TEXT, THIRD_TEXT]
              and page.input_value("#model-text") == DEFAULT_TEXT)
        # openrouter/auto is gone from every picker, not just the text one: it is
        # deprecated, and the resolved model was never read back out of the
        # response, so no picker should be able to hand the brain a router.
        embed_options = option_values("#model-embed")
        check("assistant: no picker offers the deprecated openrouter/auto router",
              all("openrouter/auto" not in opts
                  for opts in (text_options, omni_options, embed_options)))
        # The brain now says which models it can serve, because the text pick
        # rides on every chat turn: pointed at a local backend, a picker offering
        # `openai/gpt-5-nano` would fail every turn with no way out from the UI.
        # Here the brain is the fake provider, so the honest answer is "nothing
        # verified" — and the presets above must therefore stand untouched, which
        # is exactly what the three checks before this one just asserted.
        served = page.evaluate(
            "() => fetch('/api/agent/models').then(r => r.json())")
        check("assistant: the brain reports which models it can serve",
              served.get("provider") == "fake")
        check("assistant: an unprobed backend claims nothing rather than nothing-serves",
              served.get("verified") is False and served.get("models") == [])
        check("assistant: no local-backend hint when the models are unverified",
              "served locally" not in page.locator(".chat-settings").inner_text())

        n_replies = page.locator(".chat-msg.assistant").count()
        page.fill("#chat-input", "model ride-along probe")
        with page.expect_request("**/api/agent/chat/stream") as req_info:
            page.click("#chat-send")
        check("assistant: chat request carries the picked text model",
              f'"{DEFAULT_TEXT}"' in (req_info.value.post_data or ""))
        page.wait_for_function(
            f"document.querySelectorAll('.chat-msg.assistant').length >= {n_replies + 1}")

        page.select_option("#model-text", ALT_TEXT)
        page.reload()
        page.wait_for_selector("#board")
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#model-text")
        check("assistant: model choice survives a reload",
              page.input_value("#model-text") == ALT_TEXT)
        page.select_option("#model-text", DEFAULT_TEXT)

        # ---- Retired picks do not survive, even though off-list picks do.
        # Removing a model from the options list was not enough: a saved slug that
        # left the list stays *selected* (it is re-added as an extra option), and no
        # migration shipped, so the browser that had nemotron kept dictating through
        # the model whose provider drops the audio — the removal never reached the
        # only person affected by it. Same for the retired text models: kimi-k3 cost
        # $3/$15 per M tokens against the default's $0.05/$0.40 — ~50x per turn for
        # the same work — and it is not in the OpenRouter key's allowlist, so it is
        # retired for good. It was slow with it: 11s against nano's 2.6s behind a
        # non-streaming "Thinking…", which reads as a hang.
        #
        # So a slug retired *for cause* is reset to the default on load, while a
        # slug the user picked deliberately is still honoured — that escape hatch is
        # why off-list options exist and it has to stay. The reset is also written
        # back to storage: left in place, the dead slug would return the moment a
        # future version trimmed the retirement list.
        MODELS_KEY = "lodestar:models"
        for retired_text in RETIRED_TEXT:
            page.evaluate(
                "([key, text, omni]) => localStorage.setItem(key, JSON.stringify("
                "{ text, omni, embed: 'openai/text-embedding-3-small' }))",
                [MODELS_KEY, retired_text, BROKEN_OMNI])
            page.reload()
            page.wait_for_selector("#board")
            page.locator('.view-switch button[data-view="assistant"]').click()
            page.wait_for_selector("#model-text")
            check(f"assistant: retired text pick {retired_text} resets to the default",
                  page.input_value("#model-text") == DEFAULT_TEXT)
            check(f"assistant: retired text pick {retired_text} is not offered",
                  retired_text not in option_values("#model-text"))
            check(f"assistant: retired {retired_text} is cleared from storage",
                  retired_text not in page.evaluate(
                      "key => localStorage.getItem(key) || ''", MODELS_KEY))
            # The broken omni pick rides along on the same load: one retirement
            # sweep covers every picker, not just the text one.
            check("assistant: the audio-dropping saved omni pick resets to the default",
                  page.input_value("#model-omni") == DEFAULT_OMNI)
            check("assistant: the audio-dropping omni pick is not offered",
                  BROKEN_OMNI not in option_values("#model-omni"))
            # A live pick in the same payload must be left exactly as it was.
            check("assistant: a still-valid saved pick survives the sweep",
                  page.input_value("#model-embed") == "openai/text-embedding-3-small")

        # The escape hatch: a slug that was never retired, merely unknown to the
        # preset list, is still selected and still offered.
        HAND_PICKED = "anthropic/claude-sonnet-5"
        page.evaluate(
            "([key, text]) => localStorage.setItem(key, JSON.stringify({ text }))",
            [MODELS_KEY, HAND_PICKED])
        page.reload()
        page.wait_for_selector("#board")
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#model-text")
        check("assistant: a deliberately hand-set model is still honoured",
              page.input_value("#model-text") == HAND_PICKED
              and HAND_PICKED in option_values("#model-text"))

        # Back to a clean slate so the later assistant checks see the defaults.
        page.evaluate("key => localStorage.removeItem(key)", MODELS_KEY)
        page.reload()
        page.wait_for_selector("#board")

        # ---- Assistant error path: a 503 renders the friendly unavailable message.
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        n_before = len(errors)
        page.route("**/api/agent/chat/stream", lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"error":"assistant unavailable"}'))
        page.fill("#chat-input", "This should fail")
        page.click("#chat-send")
        page.wait_for_selector(".chat-msg.assistant.error")
        check("assistant: a failed request shows the unavailable message",
              "assistant is unavailable" in page.locator(".chat-msg.assistant.error").last.inner_text())
        page.unroute("**/api/agent/chat/stream")
        # The 503 we deliberately provoked surfaces as a browser console error;
        # it's expected here, not a real bug, so it shouldn't fail the console check.
        # Scrub only the entries provoked by this block (by count), so an unrelated
        # future error that happens to contain "503" isn't silently masked.
        provoked = [e for e in errors[n_before:] if "503" in e]
        check("assistant error surfaced a console error to scrub", len(provoked) >= 1)
        for e in provoked:
            errors.remove(e)

        # ---- A refusal is not a dead brain. Told "check that the brain service
        # is running", the user would go restart a service that works perfectly
        # and never learn what to do instead — wait, or start a new chat.
        # A closure, not a lambda with a default argument: Playwright passes the
        # Request as a second argument whenever the handler accepts one, so the
        # default would be overwritten with a Request and fulfill() would raise
        # from inside the route — abandoning the rest of the run.
        def refuse_with(status):
            return lambda route: route.fulfill(
                status=status, content_type="application/json",
                body='{"error":"refused"}')

        for status, want, name in [
                (429, "too many", "a rate-limited turn says to wait"),
                (413, "too long", "an over-long conversation says to start a new chat")]:
            n_before = len(errors)
            n_errs = page.locator(".chat-msg.assistant.error").count()
            page.route("**/api/agent/chat/stream", refuse_with(status))
            page.fill("#chat-input", f"provoke {status}")
            page.click("#chat-send")
            wait_until(lambda: page.locator(".chat-msg.assistant.error").count() > n_errs)
            refused = page.locator(".chat-msg.assistant.error").last.inner_text()
            check(f"assistant: {name}, not that the brain is down",
                  want in refused.lower() and "brain service" not in refused)
            page.unroute("**/api/agent/chat/stream")
            for e in [e for e in errors[n_before:] if str(status) in e]:
                errors.remove(e)
        page.locator('.view-switch button[data-view="board"]').click()

        # ---- RAG test lab ----------------------------------------------------
        # The lab is a page inside the platform, reached from the Assistant, and it
        # talks to a service (brain/tests/raglab) that this suite never starts and
        # pins to a dead port (RAGLAB_PORT), so "not running" is a fact here rather
        # than an assumption about the developer's machine. Both states are covered:
        # the honest "not running" panel, and — with the lab's own API shape mocked —
        # the working page. The mock is what keeps these checks about the
        # integration rather than about retrieval quality.
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        check("raglab: the Assistant offers a button into the lab",
              page.locator("#raglab-open").count() == 1)
        check("raglab: the lab is not a life view, so the switcher has no button for it",
              page.locator('.view-switch button[data-view="raglab"]').count() == 0
              and page.locator(".view-switch button").count() == 7)

        n_before = len(errors)
        page.click("#raglab-open")
        page.wait_for_selector(".raglab-sheet")
        check("raglab: the button opens the lab page",
              page.locator(".raglab-sheet h2").inner_text().strip().lower() == "rag test lab")
        page.wait_for_selector(".rag-absent")
        # .rag-absent is rendered in two phases: "Reaching the lab…" while the
        # probe is in flight, and the how-to-start card once it has failed. The
        # class matches both, so waiting for it caught the loading card and the
        # command genuinely was not on screen yet.
        check("raglab: a lab that is not running says how to start it",
              wait_until(lambda: "npm run raglab"
                         in page.locator(".rag-absent").inner_text()))
        check("raglab: no view-switch button is pressed while on the lab page",
              page.locator('.view-switch button[aria-pressed="true"]').count() == 0)

        page.click("#raglab-back")
        page.wait_for_selector("#chat-input")
        check("raglab: back returns to the Assistant",
              page.get_attribute('.view-switch button[data-view="assistant"]',
                                 "aria-pressed") == "true")

        # Probing an absent lab is a 503 by design, and the browser logs it. Scrub
        # only what this block provoked (by count and status), so an unrelated
        # future error is still caught by the console check.
        provoked = [e for e in errors[n_before:] if "503" in e]
        check("raglab: the absent-lab probe surfaced a console error to scrub",
              len(provoked) >= 1)
        for e in provoked:
            errors.remove(e)

        # Now with the lab's API mocked, to exercise the working page.
        LAB_OPTIONS = {
            "chunkers": ["fixed", "semantic-drift"],
            "embedders": ["ascii-hash", "char-hash", "fastembed",
                          "sentence-transformers", "openai"],
            "retrievers": ["dense", "bm25", "hybrid-rrf"],
            "rerankers": ["none", "lexical", "cross-encoder", "llm"],
            "graders": ["none", "lexical", "llm"],
            "answerers": ["none", "extractive", "llm"],
            "question_types": ["single-hop", "temporal", "habit"],
            "defaults": {
                "index": {"chunker": "semantic-drift", "chunk_chars": 500,
                          "overlap": 100, "contextual": True, "embedder": "char-hash",
                          "embed_model": ""},
                "retrieval": {"retriever": "hybrid-rrf", "k": 8, "candidates": 40,
                              "time_filter": True,
                              "multi_query": True, "hyde": False, "mmr_lambda": 1.0,
                              "reranker": "lexical", "rerank_depth": 20,
                              "reranker_model": "", "grader": "none",
                              "grade_threshold": 0.0, "grader_model": "",
                              "expansion_model": ""},
                "generation": {"answerer": "extractive", "key_facts_judge": False,
                               "model": "", "judge_model": "", "ragas_model": ""},
                "label": "",
            },
            # Which languages an embedder can actually represent is the first
            # thing about it: on this corpus the English-only options measure
            # nothing at all, and the panel has to say so before a run, not after.
            "embedder_hints": [
                {"kind": "ascii-hash", "label": "ascii-hash (brain default)",
                 "languages": "Latin script only", "farsi": False,
                 "available": True,
                 "note": "tokenises [a-z0-9]+, so Farsi embeds to the zero vector"},
                {"kind": "char-hash", "label": "char-hash",
                 "languages": "any script, lexical only", "farsi": True,
                 "available": True,
                 "note": "character n-grams recover Persian stems"},
                {"kind": "fastembed", "label": "fastembed (real model)",
                 "languages": "English + Farsi, depends on the model below",
                 "farsi": True, "available": True,
                 "note": "a real multilingual transformer"},
                {"kind": "sentence-transformers",
                 "label": "sentence-transformers (any HuggingFace model)",
                 "languages": "English + Farsi, depends on the model below",
                 "farsi": True, "available": True,
                 "note": "reaches Qwen3 and the Persian-tuned models fastembed "
                         "does not serve"},
                {"kind": "openai", "label": "openai (API)",
                 "languages": "English + Farsi, depends on the model below",
                 "farsi": True, "available": False,
                 "note": "no download, but it needs OPENAI_API_KEY and spends money"},
            ],
            "embed_models": [
                {"id": "", "label": "lab default (paraphrase-multilingual-MiniLM-L12-v2)",
                 "languages": "English + Farsi (50+ languages)", "farsi": True,
                 "source": "default", "available": True, "dim": 384,
                 "backend": "fastembed",
                 "note": "follows RAGLAB_FASTEMBED_MODEL",
                 "query_prefix": "", "passage_prefix": ""},
                {"id": "intfloat/multilingual-e5-large", "label": "multilingual-e5-large",
                 "languages": "English + Farsi (100+ languages)", "farsi": True,
                 "source": "open", "available": True, "dim": 1024,
                 "backend": "fastembed",
                 "note": "strongest widely available multilingual retriever",
                 "query_prefix": "query: ", "passage_prefix": "passage: "},
                {"id": "BAAI/bge-small-en-v1.5", "label": "bge-small-en-v1.5",
                 "languages": "English only", "farsi": False, "source": "open",
                 "available": True, "dim": 384, "backend": "fastembed",
                 "note": "what the brain hardwires today — here as the baseline",
                 "query_prefix": "", "passage_prefix": ""},
                {"id": "BAAI/bge-m3", "label": "bge-m3", "languages":
                 "English + Farsi (100+ languages)", "farsi": True, "source": "open",
                 "available": False, "dim": 1024, "backend": "fastembed",
                 "note": "strong on Persian in public evals, not served here yet",
                 "query_prefix": "", "passage_prefix": ""},
                # Neither of these is a fastembed model: one is a HuggingFace
                # checkpoint, the other an API call. The backend is part of the
                # option because it decides what picking it actually costs.
                {"id": "Qwen/Qwen3-Embedding-8B", "label": "Qwen3-Embedding-8B",
                 "languages": "English + Farsi (100+ languages)", "farsi": True,
                 "source": "open", "available": False, "dim": 4096,
                 "backend": "sentence-transformers", "tag": "recommended",
                 "note": "the recommended pick for Farsi retrieval; ~16 GB",
                 "query_prefix": "Instruct: retrieve the diary passage\nQuery: ",
                 "passage_prefix": ""},
                {"id": "heydariAI/persian-embeddings",
                 "label": "persian-embeddings",
                 "languages": "Farsi + English (Persian-tuned)", "farsi": True,
                 "source": "open", "available": True, "dim": 1024,
                 "backend": "sentence-transformers", "tag": "lab default",
                 "note": "trained on Persian text specifically",
                 "query_prefix": "", "passage_prefix": ""},
                {"id": "openai/text-embedding-3-small",
                 "label": "text-embedding-3-small",
                 "languages": "English + Farsi (100+ languages)", "farsi": True,
                 "source": "closed", "available": False, "dim": 1536,
                 "backend": "openai",
                 "note": "cheapest strong multilingual option; needs OPENAI_API_KEY",
                 "query_prefix": "", "passage_prefix": ""},
                {"id": "openai/text-embedding-3-large",
                 "label": "text-embedding-3-large",
                 "languages": "English + Farsi (100+ languages)", "farsi": True,
                 "source": "closed", "available": False, "dim": 3072,
                 "backend": "openai",
                 "note": "the strongest API option; needs OPENAI_API_KEY",
                 "query_prefix": "", "passage_prefix": ""},
            ],
            # Every stage that can call a model offers the whole catalogue, with
            # the licence spelled out and unverified models kept as NA rather
            # than hidden.
            "models": [
                {"id": "", "label": "lab default (openai/gpt-5-nano)",
                 "source": "default", "available": True, "note": "RAGLAB_MODEL"},
                {"id": "openai/gpt-5-nano", "label": "GPT-5 nano", "source": "closed",
                 "available": True, "note": "every grade so far was measured on this"},
                {"id": "meta-llama/llama-3.3-70b-instruct", "label": "Llama 3.3 70B",
                 "source": "open", "available": True, "note": "open weights"},
                {"id": "qwen/qwen-2.5-72b-instruct", "label": "Qwen2.5 72B",
                 "source": "open", "available": False,
                 "note": "strong on Farsi, never run here"},
            ],
            # The three steps of the pipeline, in order. The panel groups and
            # colours every control by these, so they are served rather than
            # invented in the browser.
            "steps": [
                {"key": "index", "short": "Index",
                 "label": "Index — what gets stored",
                 "note": "Runs once per corpus and decides what can ever be found."},
                {"key": "retrieval", "short": "Retrieval",
                 "label": "Retrieval & ranking",
                 "note": "Runs per question and decides what the answerer sees."},
                {"key": "generation", "short": "Generation",
                 "label": "Generation & scoring",
                 "note": "Turns contexts into a Farsi answer, and grades it."},
            ],
            "model_roles": [
                {"key": "expand", "label": "Query rewriting (HyDE)",
                 "step": "retrieval",
                 "field": "retrieval.expansion_model", "only_when": "HyDE is on",
                 "help": "Invents a plausible diary answer and searches with that."},
                {"key": "rerank", "label": "Reranker", "step": "retrieval",
                 "field": "retrieval.reranker_model", "only_when": "Reranker = llm",
                 "help": "Scores each candidate chunk against the question."},
                {"key": "grade", "label": "Relevance gate", "step": "retrieval",
                 "field": "retrieval.grader_model", "only_when": "Gate = llm",
                 "help": "Decides whether a chunk is relevant at all, which is what "
                         "lets the lab abstain instead of inventing an answer."},
                {"key": "answer", "label": "Answer", "field": "generation.model",
                 "step": "generation", "only_when": "Answerer = llm",
                 "help": "Writes the Farsi answer from the retrieved context."},
                {"key": "judge", "label": "Key-facts judge", "step": "generation",
                 "field": "generation.judge_model", "only_when": "the judge is on",
                 "help": "Checks the answer against the ground truth's key facts."},
                {"key": "ragas", "label": "RAGAS judge", "step": "generation",
                 "field": "generation.ragas_model", "only_when": "RAGAS = judged",
                 "help": "The model RAGAS uses for faithfulness and correctness."},
            ],
            # Every number the panel can print, defined once by the lab: label,
            # the step it grades (so it wears that ink), the exact arithmetic, and
            # what computed it. Judged metrics arrive in the same shape as
            # deterministic ones so the dashboard renders one concept.
            "metrics": [
                {"key": "headline", "label": "Composite", "short": "weighted retrieval score",
                 "step": "", "formula": "0.4·recall + 0.3·quote_recall + 0.2·ndcg + 0.1·abstained_correctly",
                 "library": "metrics._headline (pure Python)",
                 "help": "One comparable number for the leaderboard."},
                {"key": "recall", "label": "Recall@k (evidence sessions)",
                 "short": "evidence sessions found", "step": "retrieval",
                 "formula": "|gold ∩ top-k| / |gold|",
                 "library": "metrics.recall_at_k (pure Python, no model)",
                 "help": "Share of the ground truth's evidence sessions inside the top k."},
                {"key": "quote_recall", "label": "Quote recall", "short": "answering sentence survived",
                 "step": "retrieval", "formula": "matched quotes / total quotes, a quote counts at >= 0.9 similarity",
                 "library": "metrics.quote_recall (difflib.SequenceMatcher)",
                 "help": "Did the sentence that answers the question survive chunking?"},
                {"key": "ndcg", "label": "nDCG@k", "short": "evidence ranked first",
                 "step": "retrieval", "formula": "DCG/IDCG with binary gains, discount 1/log2(i+2)",
                 "library": "metrics.ndcg_at_k (pure Python, math.log2)",
                 "help": "Rewards putting evidence first, not merely including it."},
                {"key": "mrr", "label": "MRR", "short": "rank of first evidence",
                 "step": "retrieval", "formula": "1 / rank of the first evidence session",
                 "library": "metrics.mrr (pure Python, no model)",
                 "help": "How deep the reader has to go before the first hit."},
                {"key": "precision", "label": "Precision@k", "short": "signal to noise",
                 "step": "retrieval", "formula": "|gold ∩ top-k| / k",
                 "library": "metrics.precision_at_k (pure Python, no model)",
                 "help": "How much of the context is actually evidence."},
                {"key": "abstained_correctly", "label": "Abstention", "short": "unanswerable refused",
                 "step": "generation", "formula": "refusals / unanswerable questions",
                 "library": "metrics.score_question (pure Python, no model)",
                 "help": "Honest silence, measured."},
                {"key": "false_abstention", "label": "False refusals",
                 "short": "answerable wrongly refused", "step": "generation",
                 "formula": "refusals / answerable questions",
                 "library": "metrics.score_question (pure Python, no model)",
                 "help": "The failure mode a badly tuned gate produces."},
                {"key": "latency_ms", "label": "Latency", "short": "ms per question",
                 "step": "", "formula": "sum of the per-stage timings, in ms",
                 "library": "time.perf_counter around each stage",
                 "help": "Whole-pipeline wall clock for one question."},
                {"key": "non_llm_context_recall", "label": "Context recall (offline)",
                 "short": "RAGAS, string distance", "step": "retrieval",
                 "formula": "reference quotes matched in the retrieved contexts / reference quotes",
                 "library": "ragas 0.4.x NonLLMContextRecall (rapidfuzz string distance, no model)",
                 "help": "RAGAS's context recall without a judge."},
                {"key": "faithfulness", "label": "Faithfulness", "short": "answer supported by context",
                 "step": "generation", "formula": "supported claims / total claims in the answer",
                 "library": "ragas 0.4.x Faithfulness, scored by the RAGAS judge model",
                 "help": "RAGAS breaks the answer into claims and asks whether the "
                         "retrieved context supports each one."},
                # The one number the architecture is chosen by, so of everything
                # on the screen it is the one that must not be a bare figure.
                {"key": "ragas_decision", "label": "RAGAS decision score",
                 "short": "the number the architecture was chosen by", "step": "",
                 "formula": "mean(faithfulness, answer_relevancy, "
                            "llm_context_precision_with_reference, context_recall)",
                 "library": "ragas_eval.decision_score over ragas 0.4.x metrics, "
                            "scored by the RAGAS judge model",
                 "help": "Four judged RAGAS metrics averaged unweighted: "
                         "faithfulness, answer relevancy, context precision and "
                         "context recall. Everything else is reported and none "
                         "of it votes."},
            ],
            "help": {
                "index.chunker": "How a day of chat is cut into retrievable pieces.",
                "index.chunk_chars": "Target size of one piece, in characters.",
                "index.overlap": "Characters repeated between neighbouring pieces.",
                "index.contextual": "Prepend a situating header to every chunk.",
                "index.embedder": "Turns text into the vector Chroma searches. "
                                  "Farsi is unreadable to most of them.",
                "index.embed_model": "Which real embedding model fastembed loads. "
                                     "Only the multilingual ones can represent Farsi "
                                     "at all, so this is the knob that decides "
                                     "whether a run measures anything.",
                "retrieval.retriever": "Vectors, keywords, or both fused.",
                "retrieval.k": "How many contexts the answerer finally sees.",
                "retrieval.candidates": "How deep each retriever looks first.",
                "retrieval.reranker": "Re-scores the candidates before the cut.",
                "retrieval.rerank_depth": "How many candidates the reranker sees.",
                "retrieval.mmr_lambda": "Below 1 trades relevance for variety.",
                "retrieval.grader": "The gate that makes abstention possible.",
                "retrieval.grade_threshold": "Score a chunk must clear to survive.",
                "retrieval.time_filter": "Read Farsi time words as a date range.",
                "retrieval.multi_query": "Search several rewrites of the question.",
                "retrieval.hyde": "Search with an invented answer, not the question.",
                "generation.answerer": "Who writes the answer, if anyone.",
                "generation.key_facts_judge": "Score answers against the key facts.",
                "run.ragas_mode": "Offline metrics, judged metrics, or none.",
                "run.limit": "How many ground-truth questions to score.",
                "model.expand": "Model that invents the HyDE query.",
                "model.rerank": "Model that re-scores candidates.",
                "model.grade": "Model behind the relevance gate.",
                "model.answer": "Model that writes the Farsi answer.",
                "model.judge": "Model that checks the key facts.",
                "model.ragas": "Model RAGAS judges with.",
            },
            "corpus": {"sessions": 167, "messages": 1002, "from": "2025-08-02",
                       "to": "2026-07-27", "threads": 18, "habits": 5,
                       "questions": 112, "query_date": "2026-07-28"},
            "capabilities": {"fastembed": True, "cross_encoder": False, "llm": True,
                             "sentence_transformers": True,
                             "openai_embeddings": False,
                             "llm_model": "openai/gpt-5-nano",
                             "ragas": {"installed": True, "llm_ready": False,
                                       "version": "0.4.3", "notes": []},
                             # The lab keeps no database: its index is process
                             # memory, and the one durable artifact is the JSON
                             # run. The panel says so instead of badging a
                             # service that is not involved.
                             "storage": {"index": "memory",
                                         "runs": "brain/tests/raglab/.runs"}},
            "indexes": [],
        }
        # Exactly how the service composes metric help (metrics.MEASURE_HELP):
        # the paragraph, then the arithmetic, then what computed it. Built here
        # rather than hand-written so the mock cannot drift from that rule.
        LAB_OPTIONS["help"].update({
            f"metric.{m['key']}": f"{m['help']} Formula: {m['formula']}. "
                                  f"Computed by {m['library']}."
            for m in LAB_OPTIONS["metrics"]})

        LAB_RESULT = {
            "run_id": "20260729-000000-abcdef", "label": "e2e mock run",
            "seconds": 3.2, "started_at": "2026-07-29 00:00:00", "notes": [],
            "index": {"collection": "raglab-abc", "chunks": 688,
                      "avg_chars": 640.0, "p95_chars": 1200, "embed_dim": 384,
                      "build_seconds": 12.0, "reused": True},
            "summary": {"n_questions": 100,
                        "overall": {"headline": 0.5129, "recall": 0.5881,
                                    "quote_recall": 0.5552, "ndcg": 0.5555,
                                    "mrr": 0.7005, "precision": 0.3407,
                                    "abstained_correctly": 0.0,
                                    "false_abstention": 0.0217, "latency_ms": 26.5},
                        "by_type": {"single-hop": {"n": 20, "recall": 0.775,
                                                   "quote_recall": 0.6, "ndcg": 0.7,
                                                   "hit": 0.9,
                                                   "abstained_correctly": None,
                                                   "false_abstention": 0.0}},
                        "by_difficulty": {}},
            "ragas": {"mode": "llm", "n_samples": 92, "skipped": 8,
                      "metrics": {"non_llm_context_recall": 0.2249,
                                  "faithfulness": 0.7431,
                                  "answer_relevancy": 0.6812,
                                  "llm_context_precision_with_reference": 0.5904,
                                  "context_recall": 0.6653},
                      "decision": 0.68,
                      "decision_spread": {"n": 24, "mean": 0.68,
                                          "stderr": 0.0421},
                      "decision_metrics": ["faithfulness", "answer_relevancy",
                                           "llm_context_precision_with_reference",
                                           "context_recall"],
                      "notes": ["offline RAGAS context metrics are "
                                "whole-string similarity"]},
            "rows": [{"id": "q-sh-001", "type": "single-hop", "difficulty": "easy",
                      "answerable": True, "n_contexts": 8,
                      "latency_ms": 26.0, "abstained": False}],
        }

        job_polls = []

        def lab_route(route):
            url = route.request.url
            payload = None
            if "/api/raglab/options" in url:
                payload = LAB_OPTIONS
            elif "/api/raglab/runs" in url:
                # Deliberately out of order, and one row that could not be
                # scored on all four: the page has to rank on the decision
                # score and push the unranked run last without dropping it.
                def row(run_id, label, decision, headline, stderr=None):
                    return {"run_id": run_id, "label": label,
                            "ragas_decision_stderr": stderr,
                            "started_at": "2026-07-30 12:00:00", "seconds": 40,
                            "n_questions": 20,
                            "config": {"index": {"chunker": "semantic-drift",
                                                 "embedder": "sentence-transformers",
                                                 "embed_model": "heydariAI/persian-embeddings"},
                                       "retrieval": {"retriever": "hybrid-rrf",
                                                     "reranker": "lexical"},
                                       "generation": {"answerer": "llm"}},
                            "summary": {"overall": {"headline": headline},
                                        "n_questions": 20},
                            "ragas": {"faithfulness": 0.9,
                                      "answer_relevancy": 0.7,
                                      "llm_context_precision_with_reference": 0.6,
                                      "context_recall": 0.8}
                            if decision is not None else {},
                            "ragas_decision": decision}
                # "middle" carries an error large enough to overlap the winner,
                # and "old" carries none at all — both have to render honestly.
                payload = {"runs": [row("run-mid", "middle", 0.55, 0.9, 0.12),
                                    row("run-none", "unranked", None, 0.99),
                                    row("run-old", "old", 0.6, 0.5),
                                    row("run-best", "winner", 0.75, 0.4, 0.03)]}
            elif "/api/raglab/questions" in url:
                payload = {"questions": [
                    {"id": "q-sh-001", "type": "single-hop", "difficulty": "easy",
                     "question_fa": "الان وضعیت کارم چیه؟",
                     "question_en": "What is my job situation?",
                     "answerable": True, "evidence_sessions": ["2026-05-12-a"]}]}
            elif "/api/raglab/query" in url:
                payload = {
                    "question": "باشگاه هفته‌ای چند بار بود؟", "abstained": False,
                    "answer": "هفته‌ای سه بار.", "time_scope": None,
                    "sessions": [], "diagnostics": {"candidates_in_scope": 12,
                                                    "dense_hits": 8,
                                                    "bm25_hits": 8,
                                                    "graded_out": 0,
                                                    "queries": []},
                    "timings": {"retrieve_ms": 4.0},
                    # Exactly the fields pipeline.Context.as_dict() sends — no
                    # more, so a panel reading a field the lab dropped shows up
                    # here as rendered "undefined" rather than in production.
                    "contexts": [{"chunk_id": "2026-05-16-a#3",
                                  "session_id": "2026-05-16-a",
                                  "date": "2026-05-16",
                                  "score": 0.81, "stages": {"retrieval": 1.0},
                                  "expanded_from": "",
                                  "text": "این هفته سه بار باشگاه رفتم."}]}
            elif "/api/raglab/jobs/" in url:
                # The first poll is still running and carries a detail. A judged
                # run on a local model spends hours inside one stage, so the
                # detail is the only thing that moves — a page that shows only the
                # percentage looks hung for the whole judged phase.
                if not job_polls:
                    job_polls.append(1)
                    payload = {"id": "job-1", "kind": "run", "state": "running",
                               "stage": "ragas", "progress": 0.94,
                               "detail": "judge call 137 of ~420",
                               "result": None, "error": None}
                else:
                    payload = {"id": "job-1", "kind": "run", "state": "done",
                               "stage": "done", "progress": 1.0,
                               "detail": "done", "result": LAB_RESULT,
                               "error": None}
            elif "/api/raglab/run" in url:
                payload = {"job_id": "job-1"}
            if payload is None:
                return route.continue_()
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps(payload))

        page.route("**/api/raglab/**", lab_route)
        page.click("#raglab-open")
        page.wait_for_selector(".rag-grid")
        check("raglab: every stage of the pipeline gets a panel",
              page.locator(".rag-panel").count() >= 3)
        # Three lists from three different steps, because one could be a
        # coincidence: every dropdown's options arrive in the options payload.
        offered = " ".join(page.locator(".rag-panel select").all_inner_texts())
        check("raglab: the strategy lists come from the lab, not from the browser",
              "semantic-drift" in offered and "hybrid-rrf" in offered
              and "cross-encoder" in offered)
        corpus_line = page.locator(".rag-corpus").inner_text()
        check("raglab: the corpus under test is named on the page",
              "167 sessions" in corpus_line and "112 ground-truth questions" in corpus_line)
        check("raglab: missing capabilities are shown as missing",
              page.locator(".rag-cap.off").count() >= 1
              and page.locator(".rag-cap.on").count() >= 1)
        # An experiment is not a record. The page has to say where a run's index
        # lives — and that it is thrown away — rather than name a database, which
        # would imply the next run can find what this one built.
        caps_text = page.locator(".rag-caps").inner_text()
        check("raglab: the page says the index is in memory and where runs land",
              "index in memory" in caps_text
              and "brain/tests/raglab/.runs" in caps_text)
        check("raglab: the page names no vector database",
              "chroma" not in caps_text.lower())

        # Model choice per task. Six stages can call a model and they want
        # different things from one, so none of them is hard-coded; the licence is
        # on the label because on a Farsi corpus the open-weight candidates are
        # the interesting ones.
        check("raglab: every task that calls a model has its own dropdown",
              page.locator(".rag-models select.rag-model").count() == 6)
        models_text = page.locator(".rag-models").inner_text()
        check("raglab: each model role is named after the task it drives",
              "Reranker" in models_text and "Relevance gate" in models_text
              and "RAGAS judge" in models_text)
        options_text = page.locator('.rag-models select.rag-model[data-role="answer"]').inner_text()
        check("raglab: model options say whether the weights are open or closed",
              "(open source)" in options_text and "(closed source)" in options_text)
        check("raglab: a model worth trying but unverified is offered as NA, not hidden",
              "NA" in options_text and "Qwen2.5 72B" in options_text)
        check("raglab: the lab default is the first choice in every role",
              options_text.strip().startswith("lab default"))

        # The embedder decides everything downstream, and most well-known ones
        # cannot represent Farsi at all — so the dropdown says which languages
        # each one covers instead of leaving it to be discovered by a run.
        embedder_text = page.locator("select.rag-embedder").inner_text()
        check("raglab: every embedder says which languages it can represent",
              "Latin script only" in embedder_text
              and "English + Farsi" in embedder_text)
        embed_models_text = page.locator("select.rag-embed-model").inner_text()
        check("raglab: a Farsi-capable embedding model can be picked by name",
              "multilingual-e5-large" in embed_models_text
              and "English + Farsi" in embed_models_text)
        check("raglab: an English-only embedding model is labelled English-only",
              "English only" in embed_models_text
              and "bge-small-en" in embed_models_text)
        check("raglab: an embedding model that is not served is offered as NA",
              "bge-m3" in embed_models_text and "NA" in embed_models_text)
        check("raglab: the panel says when the embedding model is consulted",
              "Embedder = fastembed" in page.locator(".rag-models").inner_text())

        page.locator('.rag-panel .rag-why[data-topic="index.embed_model"]').click()
        page.wait_for_selector(".rag-help")
        check("raglab: the embedding-model explainer says why it decides Farsi recall",
              "farsi" in page.locator(".rag-help").first.inner_text().lower())
        page.locator('.rag-panel .rag-why[data-topic="index.embed_model"]').click()

        # The strongest Persian models are not fastembed models: two are
        # HuggingFace checkpoints and two are an API call. They are offered with
        # the backend that serves them, because that is what picking one costs.
        check("raglab: the Qwen3 model is offered and marked as the recommendation",
              "Qwen3-Embedding-8B" in embed_models_text
              and "recommended" in embed_models_text.lower())
        check("raglab: a Persian-tuned model is offered by name",
              "persian-embeddings" in embed_models_text
              and "Persian-tuned" in embed_models_text)
        check("raglab: both OpenAI embedding models are offered as closed source",
              "text-embedding-3-small" in embed_models_text
              and "text-embedding-3-large" in embed_models_text
              and "(closed source)" in embed_models_text)
        check("raglab: each option says which backend would serve it",
              "sentence-transformers" in embed_models_text
              and "openai" in embed_models_text.lower())
        check("raglab: the two new backends are pickable as embedders",
              "sentence-transformers" in embedder_text and "openai" in embedder_text)
        check("raglab: a backend that cannot run yet is offered as NA, not hidden",
              page.locator('select.rag-embedder option[value="openai"]')
              .inner_text().find("NA") >= 0)
        check("raglab: the model knob says which backends consult it",
              "sentence-transformers" in page.locator(
                  ".rag-models label:has(select.rag-embed-model) .rag-when"
              ).inner_text())

        page.select_option("select.rag-embedder", "sentence-transformers")
        page.select_option("select.rag-embed-model", "heydariAI/persian-embeddings")
        page.reload()
        page.wait_for_selector(".rag-grid")
        check("raglab: the chosen embedding model survives a reload",
              page.input_value("select.rag-embed-model")
              == "heydariAI/persian-embeddings"
              and page.input_value("select.rag-embedder") == "sentence-transformers")
        page.screenshot(path=shot("raglab-embedders.png"))
        page.select_option("select.rag-embedder", "char-hash")
        page.select_option("select.rag-embed-model", "intfloat/multilingual-e5-large")

        # Twenty-eight knobs on one sheet are navigable only if the sheet says
        # which of the three steps each one belongs to, and colour is the fastest
        # way to say it: index orange, retrieval green, generation blue.
        check("raglab: each pipeline step gets its own panel, marked with the step",
              page.locator('fieldset.rag-panel[data-step="index"]').count() == 1
              and page.locator('fieldset.rag-panel[data-step="retrieval"]').count() == 1
              and page.locator('fieldset.rag-panel[data-step="generation"]').count() == 1)
        hues = page.evaluate(
            "() => { const s = getComputedStyle(document.documentElement);"
            " return ['index', 'retrieval', 'generation'].map((k) =>"
            " parseFloat(s.getPropertyValue('--step-' + k + '-h'))); }")
        check("raglab: index reads orange, retrieval green, generation blue",
              20 <= hues[0] <= 70 and 120 <= hues[1] <= 175
              and 215 <= hues[2] <= 280)
        inks = page.evaluate(
            "() => ['index', 'retrieval', 'generation'].map((k) =>"
            " getComputedStyle(document.querySelector('fieldset.rag-panel"
            "[data-step=\"' + k + '\"] legend')).color)")
        check("raglab: the three steps are three visibly different inks",
              len(set(inks)) == 3 and all(inks))

        # The embedder is a language model too, so every model choice belongs in
        # the one column — each still wearing the ink of the step it drives, so
        # moving it right does not lose which stage it affects.
        check("raglab: every language model lives in the models panel",
              page.locator(".rag-models select.rag-embedder").count() == 1
              and page.locator(".rag-models select.rag-embed-model").count() == 1
              and page.locator(".rag-models select.rag-model").count() == 6)
        check("raglab: the index knobs no longer hold a model picker",
              page.locator('fieldset.rag-panel[data-step="index"] select').count() >= 1
              and page.locator(
                  'fieldset.rag-panel[data-step="index"] select.rag-embedder,'
                  'fieldset.rag-panel[data-step="index"] select.rag-embed-model,'
                  'fieldset.rag-panel[data-step="index"] select.rag-model'
              ).count() == 0)
        check("raglab: the embedder is coloured as the index-step model it is",
              page.locator('.rag-models [data-step="index"] select.rag-embedder')
              .count() == 1
              and page.locator('.rag-models [data-step="index"] '
                               "select.rag-embed-model").count() == 1)
        # No chat model writes to the index step any more — the summariser was the
        # only one, and it went with the rollup layers. The embedder is the index
        # step's model now, and the check above is what colours it.
        check("raglab: each per-task model wears the ink of the step it serves",
              page.locator('.rag-models [data-step="retrieval"] '
                           'select.rag-model[data-role="grade"]').count() == 1
              and page.locator('.rag-models [data-step="generation"] '
                               'select.rag-model[data-role="answer"]').count() == 1)
        sides = page.evaluate(
            "() => { const left = (s) =>"
            " document.querySelector(s).getBoundingClientRect().left;"
            " return {models: left('.rag-models'), steps:"
            " ['index', 'retrieval', 'generation'].map((k) =>"
            " left('fieldset.rag-panel[data-step=\"' + k + '\"]'))}; }")
        check("raglab: the models column sits to the right of the step panels",
              all(sides["models"] > edge for edge in sides["steps"]))
        # Rendered text, so it comes back as the stylesheet's uppercase.
        step_tags = [t.strip().lower() for t
                     in page.locator(".rag-models .rag-step-tag").all_inner_texts()]
        check("raglab: the models panel names the step each group belongs to",
              step_tags == ["index", "retrieval", "generation"])
        model_inks = page.evaluate(
            "() => ['index', 'retrieval', 'generation'].map((k) =>"
            " getComputedStyle(document.querySelector('.rag-models "
            "[data-step=\"' + k + '\"] .rag-step-tag')).color)")
        check("raglab: a model's ink matches the step panel it belongs to",
              model_inks == inks)

        # Theme-derived tokens, not three hard-coded hexes: the lab is used on all
        # four papers, and an ink that only works on one of them is a bug.
        before = inks[0]
        theme_was = page.input_value("#theme-select")
        page.select_option("#theme-select", "dark")
        page.wait_for_timeout(700)
        after = page.evaluate(
            "() => getComputedStyle(document.querySelector('fieldset.rag-panel"
            "[data-step=\"index\"] legend')).color")
        check("raglab: step inks follow the theme instead of being hard-coded",
              after != before)
        page.screenshot(path=shot("raglab-steps.png"))
        page.select_option("#theme-select", theme_was)
        page.wait_for_timeout(700)

        # An unexplained knob is a knob nobody can make a real decision about.
        check("raglab: every knob carries a clickable explainer",
              page.locator(".rag-panel .rag-why").count()
              >= page.locator(".rag-panel select, .rag-panel input[type=number]").count())
        check("raglab: explainers stay out of the way until asked",
              page.locator(".rag-help").count() == 0)
        page.locator('.rag-models .rag-why[data-topic="model.grade"]').click()
        page.wait_for_selector(".rag-help")
        check("raglab: the explainer says what the factor actually does",
              "relevance gate" in page.locator(".rag-help").first.inner_text().lower())
        page.locator('.rag-models .rag-why[data-topic="model.grade"]').click()
        check("raglab: clicking the explainer again puts it away",
              page.locator(".rag-help").count() == 0)

        # The explainer sits inside a <label>, so a click on it would otherwise
        # activate the labelled control — asking what a checkbox does must not
        # tick it.
        was = page.is_checked('.rag-panel input[type="checkbox"]')
        page.locator('.rag-panel .field.rag-inline .rag-why').first.click()
        check("raglab: asking what a checkbox means does not toggle it",
              page.is_checked('.rag-panel input[type="checkbox"]') == was
              and page.locator(".rag-help").count() == 1)
        page.locator('.rag-panel .field.rag-inline .rag-why').first.click()

        page.select_option('select.rag-model[data-role="grade"]',
                           "meta-llama/llama-3.3-70b-instruct")
        page.reload()
        page.wait_for_selector(".rag-grid")
        check("raglab: a per-task model choice survives a reload",
              page.input_value('select.rag-model[data-role="grade"]')
              == "meta-llama/llama-3.3-70b-instruct")
        page.screenshot(path=shot("raglab-models.png"))

        # The lab is a page like any other, so a reload lands back on it rather
        # than dumping the developer on the Board mid-experiment.
        page.reload()
        page.wait_for_selector(".rag-grid")
        check("raglab: the lab page is remembered across a reload",
              page.locator(".raglab-sheet").count() == 1)

        page.click("#raglab-run")
        # Caught on the first poll, before the second one completes the job.
        page.wait_for_selector(".rag-progress")
        check("raglab: a running job says which call it is on, not just a percent",
              "judge call 137 of ~420" in page.locator(".rag-meta").last.inner_text())
        page.wait_for_selector(".rag-figures")
        figures = page.locator(".rag-figures").inner_text()
        check("raglab: a run renders its grades",
              "0.588" in figures and "0.555" in figures)
        check("raglab: grades are broken down by question type",
              "single-hop" in page.locator(".rag-table").first.inner_text())
        check("raglab: the RAGAS caveat travels with the numbers",
              "whole-string similarity" in page.locator(".rag-results").inner_text())

        # A score nobody can check is worse than no score: every number says whose
        # definition it is, the arithmetic behind it, and what computed it — through
        # the same explainer the knobs use, so the page has one idea of "explain".
        # Rendered text, so the stylesheet's uppercase comes back with it.
        check("raglab: the metric labels come from the lab, not the browser",
              "recall@k (evidence sessions)" in figures.lower())
        check("raglab: every grade carries an explainer",
              page.locator(".rag-figure .rag-why").count()
              >= page.locator(".rag-figure").count())
        page.locator('.rag-figure .rag-why[data-topic="metric.recall"]').click()
        page.wait_for_selector(".rag-help")
        recall_help = page.locator(".rag-help").first.inner_text()
        check("raglab: a metric explainer gives the exact formula and the library",
              "|gold ∩ top-k| / |gold|" in recall_help
              and "metrics.recall_at_k" in recall_help)
        page.locator('.rag-figure .rag-why[data-topic="metric.recall"]').click()

        # A judged metric is somebody else's definition, computed by somebody
        # else's code, with a model's variance in it. All three get said.
        page.locator('.rag-why[data-topic="metric.faithfulness"]').click()
        page.wait_for_selector(".rag-help")
        judged_help = page.locator(".rag-help").first.inner_text()
        check("raglab: a RAGAS metric names the RAGAS class behind it",
              "Faithfulness" in judged_help and "ragas" in judged_help.lower())
        check("raglab: a judged metric says a model produced the number",
              "RAGAS judge" in judged_help)
        page.locator('.rag-why[data-topic="metric.faithfulness"]').click()

        # Same three inks as the panels: a retrieval number is green wherever it
        # appears, so a colour means one thing across the whole page.
        check("raglab: each grade wears the ink of the step it measures",
              page.locator('.rag-figure[data-step="retrieval"]').count() >= 3
              and page.locator('.rag-figure[data-step="generation"]').count() >= 1)
        figure_ink = page.evaluate(
            "() => getComputedStyle(document.querySelector('.rag-figure"
            "[data-step=\"retrieval\"] .rag-figure-label')).color")
        panel_ink = page.evaluate(
            "() => getComputedStyle(document.querySelector('fieldset.rag-panel"
            "[data-step=\"retrieval\"] legend')).color")
        check("raglab: a grade's ink matches its step panel's ink",
              figure_ink == panel_ink)
        page.screenshot(path=shot("raglab-metrics.png"))

        # ---- the score that picks the architecture ---------------------------
        # Four judged RAGAS metrics, averaged, and the only number that chooses
        # between configurations. Everything else on the page is reported and
        # does not vote — so the page has to say which one decided, or a reader
        # ranks on whichever figure is biggest.
        decision_card = page.locator(".rag-figure-decision")
        check("raglab: the deciding score gets its own figure",
              decision_card.count() == 1
              and "0.680" in decision_card.inner_text())
        check("raglab: the deciding figure names the four metrics behind it",
              all(name in decision_card.inner_text() for name in
                  ("faithfulness", "answer_relevancy",
                   "llm_context_precision_with_reference", "context_recall")))
        page.locator('.rag-figure-decision .rag-why[data-topic="metric.ragas_decision"]').click()
        page.wait_for_selector(".rag-help")
        decision_help = page.locator(".rag-help").first.inner_text()
        check("raglab: the deciding score explains why those four and not others",
              "unweighted" in decision_help.lower()
              and "none of it votes" in decision_help.lower())
        check("raglab: the deciding score admits it carries a model's variance",
              "RAGAS judge" in decision_help)
        # A ranking whose gaps are smaller than its error bars has not ranked
        # anything, so the figure states the error beside the mean rather than
        # leaving a reader to assume three decimal places of precision.
        check("raglab: the deciding figure shows the error on its own mean",
              "0.042" in decision_card.inner_text()
              and "24" in decision_card.inner_text())
        page.locator('.rag-figure-decision .rag-why[data-topic="metric.ragas_decision"]').click()

        # The leaderboard ranks on it, says so, and keeps the runs it could not
        # rank rather than hiding them.
        board = page.locator(".rag-board")
        check("raglab: the leaderboard says which column chose the architecture",
              "Ranked by the RAGAS decision score"
              in page.locator(".rag-basis").inner_text())
        labels = [c.strip() for c in
                  board.locator("tbody tr td:first-child").all_inner_texts()]
        check("raglab: the leaderboard is ordered by the deciding score",
              labels == ["winner", "old", "middle", "unranked"])
        check("raglab: an unrankable run is kept and shown as unranked, not dropped",
              board.locator("tbody tr").count() == 4
              and board.locator("tbody tr").last.inner_text().count("—") >= 5)
        # The error on the row, next to the number it qualifies — and absent on
        # the row that never measured one, rather than shown as ± 0.
        # Not named `errors`: that is the console-error collector this suite
        # checks at the very end, and shadowing it silently fails that check.
        row_errors = [tr.locator(".rag-stderr").inner_text().strip()
                      if tr.locator(".rag-stderr").count() else ""
                      for tr in board.locator("tbody tr").all()]
        check("raglab: each ranked row carries the error on its deciding score",
              row_errors == ["± 0.030", "", "± 0.120", ""])
        # Rendered text, so the stylesheet's uppercase comes back with it.
        header = board.locator("thead").inner_text().lower()
        check("raglab: the deciding score and its four parts are all on the row",
              "decision" in header and "faith" in header and "ans rel" in header
              and "ctx prec" in header and "ctx recall" in header)
        check("raglab: the deterministic scores stay on the row without voting",
              "composite" in header and "quote" in header)
        page.screenshot(path=shot("raglab-leaderboard.png"))

        # ---- a retrieved context ----------------------------------------------
        # There is one kind of row in the index now, so the meta line's whole job
        # is saying which chunk this is and when it was written. It reads straight
        # off the payload, so a field the lab has stopped sending renders as the
        # word "undefined" — visible, unfailing, and easy to ship.
        page.fill("#raglab-question", "باشگاه هفته‌ای چند بار بود؟")
        page.click("#raglab-ask")
        page.wait_for_selector(".rag-context")
        context_meta = page.locator(".rag-context .rag-meta").first.inner_text()
        check("raglab: a retrieved context says which chunk it is and when",
              "2026-05-16-a#3" in context_meta and "2026-05-16" in context_meta
              and "undefined" not in context_meta)
        page.screenshot(path=shot("raglab-context.png"))

        # A chosen strategy is what a developer changes twenty times in a sitting;
        # losing it on every reload would make the page useless.
        page.select_option(".rag-panel select", "fixed")
        page.reload()
        page.wait_for_selector(".rag-grid")
        check("raglab: the chosen configuration survives a reload",
              page.locator(".rag-panel select").first.input_value() == "fixed")

        page.unroute("**/api/raglab/**")
        page.evaluate("() => localStorage.removeItem('lodestar-raglab-config')")
        page.click("#raglab-back")
        page.wait_for_selector("#chat-input")
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

        # A model that silently drops the audio is the bug that broke dictation:
        # the brain answers 502 with a detail naming the model. The composer must
        # show that reason — blaming a brain that is running fine sent the user
        # debugging the wrong service — and must never paste the model's invented
        # apology in as if it were speech.
        page.fill("#chat-input", "still mine")
        n_before = len(errors)
        page.route("**/api/agent/transcribe", lambda route: route.fulfill(
            status=502, content_type="application/json",
            body=json.dumps({"detail": "the model 'nvidia/nemotron-3-nano-omni-30b"
                                       "-a3b-reasoning:free' did not receive the "
                                       "audio; pick a different omni model"})))
        page.click(".chat-mic")
        page.wait_for_selector(".chat-recording")
        page.wait_for_timeout(800)
        page.click(".chat-stop")
        page.wait_for_selector(".chat-voice-error")
        voice_error = page.inner_text(".chat-voice-error")
        check("voice: an audio-dropping model is named, not blamed on the brain",
              "nemotron" in voice_error.lower()
              and "brain service" not in voice_error.lower())
        check("voice: a dropped-audio failure leaves the typed text alone",
              page.input_value("#chat-input") == "still mine")
        page.unroute("**/api/agent/transcribe")
        provoked = [e for e in errors[n_before:] if "502" in e]
        check("voice 502 surfaced a console error to scrub", len(provoked) >= 1)
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

        # ---- Habits ---------------------------------------------------------
        # A habit is a card you repeat. The punch strip is the count, the
        # progress and the control in one object, so almost every check here
        # goes through it rather than through a separate widget.
        # text_content, not inner_text: the column heading is styled uppercase,
        # and the label is "Done" whatever the stylesheet does to it.
        check("board: the third column is labelled Done",
              page.locator('[data-col="answered"] .column-title').text_content() == "Done")

        page.fill(".quick-add input", "Meditate")
        page.press(".quick-add input", "Enter")
        habit_card = page.locator('[data-col="inbox"] .card', has_text="Meditate").first
        habit_card.click()
        page.wait_for_selector("#card-dialog[open]")
        check("habit: the cadence fields are hidden until the card is a habit",
              page.locator("#card-habit[hidden]").count() == 1)
        page.locator('.type-picker label:has(input[value="habit"])').click()
        check("habit: stamping Habit reveals the cadence fields",
              page.locator("#card-habit:not([hidden])").count() == 1)
        page.select_option("#card-habit-freq", "daily")
        page.fill("#card-habit-count", "2")
        page.locator('.category-picker label:has(input[value="health"])').click()
        page.click('#card-form button[type="submit"]')

        habit_card = page.locator('[data-col="inbox"] .card', has_text="Meditate").first
        check("habit: the card wears the habit stamp",
              habit_card.locator(".badge.type-habit").count() == 1)
        check("habit: the card states the cadence in words",
              "2× per day" in habit_card.locator(".habit-cadence").inner_text())
        check("habit: the punch strip has one box per repetition",
              habit_card.locator(".habit-punch .punch-box").count() == 2)
        check("habit: no box is stamped yet",
              habit_card.locator(".habit-punch .punch-box.done").count() == 0)

        rail_row = page.locator('.habit-rail .habit-rail-row', has_text="Meditate").first
        check("habit: the rail lists the habit as due", rail_row.count() == 1)
        check("habit: the rail counts none of two done",
              "0/2" in rail_row.inner_text())

        # Punching from the rail and from the card must be the same act.
        rail_row.locator(".punch-box").first.click()
        page.wait_for_timeout(120)
        rail_row = page.locator('.habit-rail .habit-rail-row', has_text="Meditate").first
        check("habit: punching from the rail records one repetition",
              "1/2" in rail_row.inner_text())
        habit_card = page.locator('[data-col="inbox"] .card', has_text="Meditate").first
        check("habit: the card's strip shows the same single stamp",
              habit_card.locator(".habit-punch .punch-box.done").count() == 1)

        habit_card.locator(".habit-punch .punch-box:not(.done)").first.click()
        page.wait_for_timeout(120)
        rail_row = page.locator('.habit-rail .habit-rail-row', has_text="Meditate").first
        check("habit: punching the card's last box completes the day",
              "2/2" in rail_row.inner_text())
        check("habit: a completed habit is marked done in the rail, not removed",
              rail_row.evaluate("el => el.classList.contains('done')"))

        # Undo — a mis-tap must be takeable back, or the history stops being true.
        habit_card = page.locator('[data-col="inbox"] .card', has_text="Meditate").first
        habit_card.locator(".habit-punch .punch-box.done").last.click()
        page.wait_for_timeout(120)
        rail_row = page.locator('.habit-rail .habit-rail-row', has_text="Meditate").first
        check("habit: clicking the newest stamp takes the repetition back",
              "1/2" in rail_row.inner_text())

        check("habit: the completions reach the database",
              any(len(c.get("habitHistory", {})) == 1 and c.get("habitFreq") == "daily"
                  for c in api_state()["cards"] if c["title"] == "Meditate"))

        # History lives on the card, behind a button.
        check("habit: the history tape is closed until asked for",
              habit_card.locator(".habit-tape").count() == 0)
        habit_card.locator(".habit-history-toggle").click()
        page.wait_for_timeout(120)
        habit_card = page.locator('[data-col="inbox"] .card', has_text="Meditate").first
        check("habit: the history button opens the tape on the card",
              habit_card.locator(".habit-tape").count() == 1)
        check("habit: today's cell carries the number punched into it",
              habit_card.locator(".habit-tape .tape-cell.today").inner_text() == "1")
        page.screenshot(path=shot("habits.png"))

        # The reminder is the thing that makes a habit a habit.
        page.reload()
        page.wait_for_selector("#board.board")
        check("habit: a due habit announces itself when the board opens",
              page.locator(".habit-banner").count() == 1
              and "Meditate" in page.locator(".habit-banner").inner_text())
        check("habit: the sound can be muted from the board",
              page.locator("#habit-mute").count() == 1)
        page.locator(".habit-banner .habit-banner-hide").click()
        page.wait_for_timeout(100)
        check("habit: the banner can be dismissed",
              page.locator(".habit-banner").count() == 0)

        # Finish the day, then reload: nothing is due, so nothing nags.
        rail_row = page.locator('.habit-rail .habit-rail-row', has_text="Meditate").first
        rail_row.locator(".punch-box:not(.done)").first.click()
        page.wait_for_timeout(300)
        page.reload()
        page.wait_for_selector("#board.board")
        check("habit: a finished habit does not announce itself",
              page.locator(".habit-banner").count() == 0)
        check("habit: the rail still shows it, marked done",
              page.locator('.habit-rail .habit-rail-row.done', has_text="Meditate").count() == 1)

        # Done means retired: it leaves the rail and stops reminding, but keeps
        # its history.
        habit_card = page.locator('[data-col="inbox"] .card', has_text="Meditate").first
        habit_card.focus()
        page.keyboard.press("]")
        page.wait_for_timeout(100)
        page.locator('[data-col="in-progress"] .card', has_text="Meditate").first.focus()
        page.keyboard.press("]")
        page.wait_for_timeout(150)
        check("habit: a habit moved to Done is retired from the rail",
              page.locator('.habit-rail .habit-rail-row', has_text="Meditate").count() == 0)
        retired = page.locator('[data-col="answered"] .card', has_text="Meditate").first
        check("habit: a retired habit keeps its history",
              any(sum(len(v) for v in c.get("habitHistory", {}).values()) == 2
                  for c in api_state()["cards"] if c["title"] == "Meditate"))
        check("habit: a retired habit is still stamped a habit",
              retired.locator(".badge.type-habit").count() == 1)

        # Backlog sorting knows the new type, and the toolbar can filter to it.
        page.select_option("#type-filter", "habit")
        page.wait_for_timeout(150)
        check("habit: the type filter narrows the board to habits",
              page.locator(".card").count() == 1)
        page.select_option("#type-filter", "")
        page.wait_for_timeout(150)

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
