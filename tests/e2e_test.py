"""End-to-end verification of Lodestar.

Launches the real SQLite-backed server, drives the app in headless Chrome, and
exercises every button and flow — including the in-app confirm dialogs and that
the board actually persists to (and deletes from) the database.

    uv run --with playwright python tests/e2e_test.py
"""
import json
import os
import re
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
URL = f"http://localhost:{PORT}"
DB_PATH = os.path.join(tempfile.mkdtemp(prefix="qboard-test-"), "board.db")
# The chat record beside it — without this the spawned server would write chat
# rows into the repo's real databases/ folder.
ASSISTANT_DB_PATH = os.path.join(os.path.dirname(DB_PATH), "assistant.db")

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

def wait_for_capture(page, box, n=1, timeout=20.0):
    """Wait until a route handler has captured `n` requests.

    Why this is not `wait_until(lambda: len(box) == n)`: with Playwright's SYNC
    api, route handlers are dispatched only while the client is inside a
    Playwright call, and `wait_until` polls with `time.sleep`, which never
    yields. So a bare list-length condition can spin to its deadline while the
    handler sits waiting its turn — and then run the instant the next
    `page.*` call happens, which is usually the assertion's own locator, making
    the failure look like a race.

    It worked by accident for years: the request used to be issued *during*
    `page.click`, so the handler ran inside that call. The moment a send became
    two round trips — the topic check, then the turn — the second one was issued
    after `click` had returned, and nothing was pumping the loop.

    Touching the page in the condition is what pumps it. `.chat-log` is used
    because it exists on every screen this helper is called from.
    """
    return wait_until(
        lambda: page.locator(".chat-log").count() >= 0 and len(box) >= n, timeout)


def open_meta(page):
    """Unfold the evidence strip under the newest reply.

    Sources, tool chips and the token split live behind a one-line indicator, so
    every check that reads them has to open it first. Guarded rather than
    clicked outright: a missing indicator must read as the red lines of the
    checks that needed it, not as a TimeoutError that abandons the rest."""
    meta = page.locator(".chat-meta").last
    if meta.count() == 1 and meta.get_attribute("open") is None:
        # .chat-meta-summary, not "summary": each tool chip inside the strip is
        # its own <details>, so the loose tag matches several.
        meta.locator(".chat-meta-summary").click()


def open_extras(page):
    """Open the Assistant's extras — the lab, the chat menu and the models.

    Shut, the conversation gets the whole sheet, so everything that drives one of
    those controls has to open this first."""
    btn = page.locator("#assistant-extras-btn")
    if btn.count() == 1 and btn.get_attribute("aria-expanded") != "true":
        btn.click()


def open_models(page):
    """Unfold the Models panel, opening the extras around it if need be.

    Which model answers is picked once and then left alone, so it is folded
    inside the extras and everything reading a picker has to open both."""
    open_extras(page)
    box = page.locator(".chat-settings")
    if box.count() == 1 and box.get_attribute("open") is None:
        box.locator(".chat-settings-name").click()


def open_chat_menu(page):
    """Open the Assistant's Chat menu, where export and import now live.

    Same idiom as the board's Menu, closing itself after any action inside it,
    so anything driving those two controls has to open it again first."""
    open_extras(page)
    btn = page.locator("#chat-menu-btn")
    if btn.count() == 1 and btn.get_attribute("aria-expanded") != "true":
        btn.click()


def open_chat_history(page):
    """Open the history panel — the list of past chats.

    Deliberately NOT inside the extras: history is a control you reach for while
    reading, not a setting you touch once, so it sits in the toolbar row beside
    New chat. It is still a panel rather than a rail, because a rail beside the
    transcript was measured costing it 300px and was removed."""
    btn = page.locator("#chat-history-btn")
    if btn.count() == 1 and btn.get_attribute("aria-expanded") != "true":
        btn.click()
    page.wait_for_selector(".chat-history")


def reopen_chat_history(page):
    """Open the history panel from scratch, however it was left.

    `open_chat_history` leaves an already-open panel alone, and an inherited one
    may be seconds into its idle countdown — the pointer leaves the dock every
    time a dialog takes it — so a wait that follows can watch the panel close
    under it. Closing first means the click that reopens it is also the one that
    asks the server for the list, and it puts focus back inside the dock, which
    is what keeps the panel up while a check reads it."""
    page.keyboard.press("Escape")
    wait_until(lambda: page.locator(".chat-history").count() == 0)
    open_chat_history(page)


def carried_run(carried, seeded, asked):
    """The positions of the seeded messages a request carried, in order.

    Returned so a check can assert the window is one *consecutive* run of the
    transcript. A pinned opening message — the bug — shows up here as a gap
    between position 0 and the rest, which an ordering assertion alone would
    happily accept."""
    contents = [m["content"] for m in seeded]
    return [contents.index(c) for c in carried if c != asked and c in contents]


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
    proc = subprocess.Popen(
        ["node", "server.js"],
        cwd=ROOT,
        env={**os.environ, "PORT": str(PORT), "BOARD_DB": DB_PATH,
             "ASSISTANT_DB": ASSISTANT_DB_PATH, "NODE_NO_WARNINGS": "1",
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
             # The real link-reputation backend refuses to build without a key,
             # which is deliberate — so the offline run names the fake one rather
             # than letting the brain fail at boot with nothing to search anyway.
             "BRAIN_URL_SAFETY": "fake",
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


def api_edits():
    with urllib.request.urlopen(URL + "/api/edits", timeout=3) as r:
        return json.loads(r.read())


def api_suggest_edit(card_id, fields):
    """Stand in for the Assistant proposing a change. The offline fake chat model
    has no script that calls update_card, and what this test is about is the
    review-and-save path, not the model's choice to suggest."""
    body = json.dumps({"cardId": card_id, "fields": fields}).encode()
    req = urllib.request.Request(
        URL + "/api/edits", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def api_put(cards):
    body = json.dumps({"version": 1, "cards": cards}).encode()
    req = urllib.request.Request(
        URL + "/api/state", data=body, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def api_chat_append(session_id, messages):
    """Seed a chat straight into the record.

    The transcript lives in assistant.db now, not in localStorage, so seeding a
    long conversation means writing it where the browser reads it from. Which is
    also the cheaper path it always was: the fake model would need minutes to
    produce sixty turns, and what these checks are about is the request body and
    the history list, not the model.
    """
    body = json.dumps({"sessionId": session_id, "messages": messages}).encode()
    req = urllib.request.Request(
        URL + "/api/chat/messages", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def api_chat_sessions():
    with urllib.request.urlopen(URL + "/api/chat/sessions", timeout=3) as r:
        return json.loads(r.read())["sessions"]


def api_chat_messages():
    with urllib.request.urlopen(URL + "/api/chat/messages", timeout=5) as r:
        return json.loads(r.read())["messages"]


def api_chat_trash():
    with urllib.request.urlopen(URL + "/api/chat/trash", timeout=5) as r:
        return json.loads(r.read())["messages"]


def api_chat_reindex():
    """Sync the chat index with the record, and report what moved.

    Seeding writes straight to assistant.db, so the brain has not indexed those
    rows — and a delete cannot be shown to reach Chroma if the chunk was never
    there. This is the call that puts them in, and its {indexed, pruned} is the
    only view a browser-level test gets of the index."""
    req = urllib.request.Request(URL + "/api/rag/chat/reindex", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
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
        # This is an end-to-end test. 'star' is the fifth theme; it must switch,
        # persist and fall back exactly like the other four.
        #
        # These two helpers exist because this suite is one script with a shared
        # check() collector and no per-test isolation: a missing option or a
        # missing element raises, and the raise takes every later check with it.
        # Measured — the first run of these checks aborted the suite at
        # select_option('star') and hid the ~180 assertions after it. So a gap in
        # the feature has to come back as False, never as an exception.
        def select_theme(theme):
            try:
                page.select_option("#theme-select", theme, timeout=1500)
                return True
            except Exception:
                return False

        def css(sel, prop):
            return page.evaluate(
                """([sel, prop]) => {
                     const el = document.querySelector(sel);
                     return el ? getComputedStyle(el)[prop] : null;
                   }""", [sel, prop])

        for theme in ("white", "sepia", "dark", "star", "light"):
            picked = select_theme(theme)
            page.wait_for_timeout(40)
            check(f"theme: '{theme}' mode applied",
                  picked and page.evaluate("document.documentElement.dataset.theme") == theme)
        # An unknown stored theme must not strand the board on a blank sky.
        page.evaluate("() => localStorage.setItem('lodestar:theme', 'supernova')")
        page.reload()
        page.wait_for_selector(".card")
        check("theme: an unknown stored theme falls back to Morning",
              page.evaluate("document.documentElement.dataset.theme") == "light")
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

        # ---- The brand mark -------------------------------------------------
        # This is an end-to-end test. The header mark is the nova, drawn as two
        # scene variants that swap on theme: the colour wash on the paper themes,
        # the wash plus a star field on the dark ones. Both live in the DOM and
        # CSS chooses; the test pins which one is showing, because a variant that
        # silently never renders is the failure this cannot catch by eye.
        def mark_variant():
            return page.evaluate("""() => {
              const vis = (sel) => {
                const el = document.querySelector('.brand-mark ' + sel);
                return !!el && getComputedStyle(el).display !== 'none';
              };
              return { colour: vis('.scene-colour'), stars: vis('.scene-stars') };
            }""")

        check("brand: the question mark is gone from the header",
              page.locator(".brand-mark").inner_text().strip() == "")
        check("brand: the mark is an SVG, not a glyph",
              page.locator(".brand-mark svg").count() > 0)
        for theme, want in (("light", "colour"), ("white", "colour"), ("sepia", "colour"),
                            ("dark", "stars")):
            select_theme(theme)
            page.wait_for_timeout(40)
            v = mark_variant()
            check(f"brand: '{theme}' shows the {want} scene and only that one",
                  v[want] and not v["stars" if want == "colour" else "colour"])
        # Under the star theme the sky already carries a nova, so the header mark
        # would be a second one. It goes; the words stay.
        select_theme("star")
        page.wait_for_timeout(40)
        check("brand: the star theme drops the header mark",
              css(".brand-mark", "display") == "none")
        check("brand: the star theme keeps the name and the tagline",
              page.locator(".brand h1").is_visible() and page.locator(".tagline").is_visible())
        select_theme("light")

        # ---- The Star theme's sky -------------------------------------------
        # This is an end-to-end test. The sky belongs to the star theme alone:
        # it must not leak onto the other four, its nova is anchored low-right,
        # and the clouds drift while the nova stays put.
        select_theme("star")
        page.wait_for_timeout(120)
        sky_display = css(".star-sky", "display")
        check("star: the sky renders under the star theme",
              page.locator(".star-sky").count() == 1
              and sky_display is not None and sky_display != "none")
        clouds_anim = css(".star-sky-clouds", "animationName")
        check("star: the clouds are animating",
              clouds_anim is not None and clouds_anim != "none")
        check("star: the nova is not animating - only the clouds move",
              css(".star-sky-nova", "animationName") == "none")
        # Anchored into the lower-right corner: its centre sits in that quadrant.
        nova_low_right = page.evaluate("""() => {
          const el = document.querySelector('.star-sky-nova');
          if (!el) return false;
          const r = el.getBoundingClientRect();
          return (r.left + r.width / 2) > innerWidth / 2
              && (r.top + r.height / 2) > innerHeight / 2;
        }""")
        check("star: the nova sits in the lower-right corner", nova_low_right)
        # "Somewhere no card is placed": the sky is background, so the nova must
        # land in empty board, not behind a card where it fights the text.
        nova_clear_of_cards = page.evaluate("""() => {
          const el = document.querySelector('.star-sky-nova');
          if (!el) return false;
          const n = el.getBoundingClientRect();
          return [...document.querySelectorAll('.card')].every((c) => {
            const r = c.getBoundingClientRect();
            return n.right < r.left || n.left > r.right
                || n.bottom < r.top || n.top > r.bottom;
          });
        }""")
        check("star: the nova does not sit behind a card", nova_clear_of_cards)
        # The sky is furniture, not a card: it must never take pointer events or
        # a tab stop away from the board underneath it.
        check("star: the sky ignores the pointer",
              css(".star-sky", "pointerEvents") == "none")
        select_theme("light")
        page.wait_for_timeout(40)
        check("star: the sky is hidden on the other themes",
              css(".star-sky", "display") == "none")

        # Reduced motion is a promise the rest of the app already keeps.
        page.emulate_media(reduced_motion="reduce")
        select_theme("star")
        page.wait_for_timeout(120)
        check("star: prefers-reduced-motion stops the clouds",
              css(".star-sky-clouds", "animationName") == "none")
        page.emulate_media(reduced_motion="no-preference")

        # ---- The Assistant over the sky -------------------------------------
        # This is an end-to-end test. On the Assistant view the sheet fills the
        # width, so an opaque sheet would hide the whole sky. Under the star
        # theme it goes translucent and the clouds drift behind it; on every
        # other theme it stays solid, because a see-through sheet over quad
        # paper is unreadable.
        def sheet_alpha():
            return page.evaluate("""() => {
              const el = document.querySelector('.assistant-sheet');
              if (!el) return null;
              const m = getComputedStyle(el).backgroundColor.match(/rgba?\\(([^)]+)\\)/);
              if (!m) return null;
              const parts = m[1].split(',').map((s) => parseFloat(s));
              return parts.length > 3 ? parts[3] : 1;
            }""")

        # Back to a paper theme first: the reduced-motion block above left the
        # board on 'star', so without this the next check asserts paper-theme
        # behaviour while the star theme is still applied. It passed before the
        # feature existed only because 'star' was unselectable.
        select_theme("light")
        page.click('[data-view="assistant"]')
        page.wait_for_selector(".assistant-sheet")
        check("assistant: the sheet is opaque on the paper themes", sheet_alpha() == 1)

        # The composer must never need a scroll to reach. An empty transcript
        # always fits, so the interesting case is a full one: .chat-log caps at
        # 62vh, which plus the header and the sheet's own chrome adds up past the
        # viewport, and the page - not the transcript - takes up the slack.
        page.evaluate("""() => {
          const log = document.querySelector('.chat-log');
          for (let i = 0; i < 40; i++) {
            const d = document.createElement('div');
            d.className = 'chat-msg ' + (i % 2 ? 'assistant' : 'user');
            d.textContent = 'filler turn ' + i + ' - '.repeat(20);
            log.append(d);
          }
        }""")
        page.wait_for_timeout(200)
        fit = page.evaluate("""() => {
          const c = document.querySelector('#chat-input').getBoundingClientRect();
          return {bottom: Math.round(c.bottom), vh: innerHeight,
                  over: Math.round(document.documentElement.scrollHeight - innerHeight)};
        }""")
        check(f"assistant: the composer is in view with a full transcript "
              f"(bottom {fit['bottom']} of {fit['vh']})",
              fit["bottom"] <= fit["vh"])
        check(f"assistant: a full transcript scrolls itself, not the page "
              f"(overflow {fit['over']}px)", fit["over"] <= 1)
        page.reload()
        page.wait_for_selector("#chat-input")
        select_theme("star")
        page.wait_for_timeout(120)
        a = sheet_alpha()
        check("assistant: the star theme makes the sheet translucent so the sky shows",
              a is not None and a < 1)
        anim = css(".star-sky-clouds", "animationName")
        check("assistant: the clouds keep drifting behind the sheet",
              anim is not None and anim != "none")
        check("assistant: the header mark is gone but the tagline remains",
              css(".brand-mark", "display") == "none" and page.locator(".tagline").is_visible())
        select_theme("light")
        page.click('[data-view="board"]')
        page.wait_for_selector(".card")

        # ---- The header fits one row ----------------------------------------
        # This is an end-to-end test. The view switch and the toolbar are already
        # children of one flex row; they wrapped onto two lines because the mark
        # and the paddings ate the width, which cost ~200px of vertical space
        # before a single card. Measured at 375px tall on a 1440-wide window.
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(150)
        rows = page.evaluate("""() => {
          const r = (s) => document.querySelector(s).getBoundingClientRect();
          const vs = r('.view-switch'), tb = r('.toolbar');
          return {same: Math.abs(vs.top - tb.top) < 12,
                  header: Math.round(r('.app-header').height)};
        }""")
        check("header: the view switch and the toolbar share one row", rows["same"])
        check(f"header: the top panel is under 240px tall (got {rows['header']})",
              rows["header"] <= 240)

        # ---- The board fits the window --------------------------------------
        # This is an end-to-end test. .column caps at calc(100vh - 200px), a
        # guess at the header's height, so the board fits only while the header
        # stays under 200px. Add a habit banner, an eleventh category and a
        # second line of tags - all ordinary states - and the page overflows and
        # the footer goes under the fold. The columns already scroll themselves;
        # what must not scroll is the page.
        def board_fits():
            return page.evaluate("""() => {
              const f = document.querySelector('.app-footer').getBoundingClientRect();
              return {footer: Math.round(f.bottom), vh: innerHeight,
                      over: Math.round(document.documentElement.scrollHeight - innerHeight)};
            }""")

        fits = board_fits()
        check(f"board: the footer is in view (bottom {fits['footer']} of {fits['vh']})",
              fits["footer"] <= fits["vh"])
        check(f"board: the page does not scroll (overflow {fits['over']}px)",
              fits["over"] <= 1)

        # The real regression is column content, not header height: a seeded
        # board is short enough that scrollHeight alone reports no overflow even
        # when a column is free to grow to 3800px. So load a column past the
        # window and then try to scroll - whether the page moves is the property
        # the user actually feels.
        page.evaluate("""() => {
          const probe = document.createElement('div');
          probe.id = 'tall-column-probe';
          probe.style.height = '2400px';
          document.querySelector('.column .cards').append(probe);
          const hdr = document.createElement('div');
          hdr.id = 'tall-header-probe';
          hdr.style.height = '120px';
          document.querySelector('.app-header').append(hdr);
        }""")
        page.wait_for_timeout(200)
        page.evaluate("window.scrollTo(0, 3000)")
        page.wait_for_timeout(150)
        tall = page.evaluate("""() => {
          const f = document.querySelector('.app-footer').getBoundingClientRect();
          return {footer: Math.round(f.bottom), vh: innerHeight, y: Math.round(scrollY),
                  col: Math.round(document.querySelector('.column').getBoundingClientRect().height)};
        }""")
        check(f"board: an overloaded column leaves the footer in view "
              f"(bottom {tall['footer']} of {tall['vh']})",
              tall["footer"] <= tall["vh"])
        check(f"board: the page refuses to scroll past the window "
              f"(scrollY {tall['y']})", tall["y"] == 0)
        # The discriminating measurement. Against the old rules this column grew
        # to 5146px on a 49-card board; a scrollHeight assertion alone passed on
        # the seeded board and would have shipped the bug.
        check(f"board: the column stays inside the window "
              f"({tall['col']}px of {tall['vh']})", tall["col"] <= tall["vh"])
        page.evaluate("""() => {
          document.querySelector('#tall-column-probe').remove();
          document.querySelector('#tall-header-probe').remove();
        }""")

        # ---- The star theme's paper ------------------------------------------
        # This is an end-to-end test. Day drops the quad grid because a ruled
        # sheet fights high-contrast reading; the sky is a photograph, and a
        # grid ruled over it reads as a bug rather than as paper.
        select_theme("star")
        page.wait_for_timeout(80)
        check("star: the sky has no quad grid, like Day",
              page.evaluate(
                  "getComputedStyle(document.body).backgroundImage") == "none")
        select_theme("light")
        check("theme: Morning keeps its quad grid",
              page.evaluate(
                  "getComputedStyle(document.body).backgroundImage") != "none")

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
        # wait_until, because the bubble now appears empty and fills in: the
        # reply is revealed at a readable rate rather than pasted in whole, so
        # ".chat-msg.assistant exists" no longer means "the text is all there".
        check("assistant: chat roundtrip through the Node proxy",
              wait_until(lambda: "FAKE: hello brain" in page.inner_text(".chat-log")))
        # What the turn spent. The offline backend reports usage precisely so
        # this path is exercised without a paid model in the loop. Read off the
        # folded indicator, which is where the total lives now: what a turn cost
        # is worth a glance, the in/out split is not.
        # wait_until, because the bubble appears while the turn is still
        # streaming and the usage lands with the `done` event after it.
        check("assistant: the turn reports the tokens it spent",
              wait_until(lambda: page.locator(".chat-meta-summary").count() >= 1
                         and "tokens" in page.locator(".chat-meta-summary").last.inner_text()))

        # ---- Streaming: the answer is revealed as it is written --------------
        # This is an end-to-end test.
        # The wire was never the problem. The brain has emitted `token` frames
        # since the stream route existed, the Node proxy pipes rather than
        # buffers, and the client accumulates. What a user actually meets is a
        # model that thinks for thirteen seconds and then delivers 51 tokens in
        # under one — and one animation frame paints all of them, so a correctly
        # streamed reply still lands as a single lump.
        #
        # So what is asserted is the *reveal*: the bubble passes through visible
        # intermediate states on its way to the finished text. That is the only
        # half a fake model can prove — it answers in one chunk, which is
        # precisely the worst case the reveal has to survive — and it is the half
        # the user sees.
        long_ask = "watch this arrive: " + " ".join(f"word{i}" for i in range(40))
        seen_before = page.locator(".chat-msg.assistant").count()
        # Sampled every frame from inside the page. Polling over the wire would
        # miss a reveal that completes between two round trips, and the question
        # here is exactly how many states were paintable.
        page.evaluate(
            """(before) => {
              window.__reveal = [];
              const tick = () => {
                const nodes = document.querySelectorAll('.chat-msg.assistant .chat-text');
                if (nodes.length > before) {
                  const text = nodes[nodes.length - 1].textContent;
                  const seen = window.__reveal;
                  if (!seen.length || seen[seen.length - 1] !== text) seen.push(text);
                }
                window.__revealRaf = requestAnimationFrame(tick);
              };
              tick();
            }""",
            seen_before)
        page.fill("#chat-input", long_ask)
        page.click("#chat-send")
        final = f"FAKE: {long_ask}"
        settled = wait_until(lambda: final in page.inner_text(".chat-log"))
        page.evaluate("() => cancelAnimationFrame(window.__revealRaf)")
        snapshots = page.evaluate("() => window.__reveal")
        # The states on the way there: non-empty, and not the finished text.
        partials = [t for t in snapshots if t and t != final]
        check("assistant: the reply is revealed progressively, not in one lump",
              settled
              # Three is the smallest number that cannot be an accident of one
              # empty bubble followed by one repaint.
              and len(partials) >= 3
              # Every state is the finished answer, truncated — never a reflow, a
              # placeholder, or text that is later taken back.
              and all(final.startswith(t) for t in partials)
              and all(len(a) < len(b) for a, b in zip(partials, partials[1:])))

        # ---- Agent card confirmation gate -----------------------------------
        # A card the agent invents is a PROPOSAL: nothing reaches the board until
        # the user approves it.
        board_before = len(api_state()["cards"])
        # Relative to what is already on screen, not an absolute count: this used
        # to wait for `>= 2`, which any turn added above it satisfies before this
        # one has even been sent, and the block then read the previous turn's
        # evidence. The same pattern is used further down for the same reason.
        replies_before = page.locator(".chat-msg.assistant").count()
        page.fill("#chat-input", "add: What is Leiden clustering?")
        page.click("#chat-send")
        page.wait_for_function(
            "n => document.querySelectorAll('.chat-msg.assistant').length > n",
            arg=replies_before)
        # This is an end-to-end test.
        # The evidence under a reply is folded away behind a one-line indicator:
        # most turns are read for the answer alone, and sources, tool chips and
        # a token receipt under every one of them is a wall of furniture. The
        # indicator still has to say what is behind it.
        # The strip is built from what the turn *did*, so it is only complete once
        # the turn has settled. Waited for rather than read immediately: this
        # check used to pass on the count race above, reading the previous turn's
        # already-finished strip instead of this one's.
        folded = page.locator(".chat-meta").last
        settled_meta = wait_until(
            lambda: folded.locator(".chat-meta-summary").count() == 1
            and "tool" in folded.locator(".chat-meta-summary").inner_text())
        check("assistant: the evidence under a reply is folded until asked for",
              settled_meta and not folded.locator(".chat-steps").is_visible())
        open_meta(page)
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
        open_meta(page)
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

        # This is an end-to-end test.
        # A reply is one column of prose with its evidence beneath it. The view
        # container is `#board.assistant` and a reply bubble is `.chat-msg
        # .assistant`, so an unscoped `.assistant` rule lands on both: the reply
        # became a flex row and broke into four narrow centred columns — answer,
        # tool chips, sources, tokens — each a few words wide. Measured rather
        # than asserted on the stylesheet, because any rule that reaches the
        # bubble breaks it the same way.
        box = reply.bounding_box()
        text_box = reply.locator(".chat-text").bounding_box()
        src_box = reply.locator(".chat-sources").bounding_box()
        check("assistant: a reply is one column, not columns beside its footnotes",
              text_box["width"] >= box["width"] * 0.85
              and src_box["y"] >= text_box["y"] + text_box["height"])

        page.unroute("**/api/agent/chat/stream")

        # ---- What one turn carries -------------------------------------------
        # The browser used to send the entire transcript on every turn: a long
        # chat meant a bigger, slower, dearer request each time, and past 80
        # messages the brain refused it outright — a conversation that rendered
        # perfectly and could not be talked into.
        #
        # Now it sends a window. The transcript is untouched on screen and in
        # assistant.db; what changes is how much of it rides along, so the cost
        # of turn fifty is the cost of turn five. Seeded through the record
        # rather than by holding fifty conversations: the fake model would take
        # minutes to produce them and the assertion is about the request body.
        long_chat = "e2e-long-chat"
        seeded = []
        for i in range(30):
            seeded.append({"role": "user", "content": f"seeded question {i}"})
            seeded.append({"role": "assistant", "content": f"seeded answer {i}"})
        api_chat_append(long_chat, seeded)
        page.reload()
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        open_chat_history(page)
        page.locator(".chat-history-item", has_text="seeded question 0").first.click()
        page.wait_for_selector("#chat-input")
        wait_until(lambda: page.locator(".chat-msg").count() >= 60)

        sent = []
        page.route("**/api/agent/chat/stream", lambda route: (
            sent.append(json.loads(route.request.post_data)),
            route.fulfill(status=200, content_type="text/event-stream",
                          body='event: done\ndata: {"reply": "ok", "mutated": false,'
                               ' "proposed": false, "steps": []}\n\n')))
        page.fill("#chat-input", "the newest question")
        page.click("#chat-send")
        # Longer than the default 5s, and for a reason worth writing down: a send
        # is now TWO round trips (the topic check, then the turn) against a
        # single-threaded Node server that is at the same time reading a
        # 61-message transcript out of SQLite. At 5s this raced and the handler
        # arrived just after the check gave up — a green suite that failed once in
        # a while for nothing to do with what it asserts.
        wait_for_capture(page, sent)
        body = sent[0] if sent else {"messages": []}
        carried = [m["content"] for m in body["messages"]]

        # This is an end-to-end test.
        check("context: a long chat is not resent whole on every turn",
              # 61 messages are on screen; far fewer travel.
              len(carried) < 25
              # The turn being asked is always the last thing the model reads.
              and carried[-1] == "the newest question")
        # This is an end-to-end test.
        # The window used to prepend the transcript's first user message on every
        # turn, outside the char budget, as "framing". With one endless
        # transcript that message was the subject of the FIRST conversation this
        # board ever had, pinned to the top of every request forever — so a new
        # question got an answer about last month. The session boundary is the
        # framing now, and the carried window is a plain contiguous tail.
        run = carried_run(carried, seeded, "the newest question")
        # No gap between the oldest carried message and the newest. A pinned
        # opener is exactly what a gap would look like here.
        contiguous = bool(run) and run == list(range(run[0], run[0] + len(run)))
        check("context: the window is a contiguous tail with nothing pinned",
              contiguous
              and "seeded question 29" in carried
              and "seeded question 0" not in carried
              and "seeded question 5" not in carried)
        # This is an end-to-end test.
        # Trimming that nobody can see is the quiet loss this project refuses. The
        # transcript keeps every message; the marker is what makes the difference
        # between "kept but not sent" and "gone" visible in the one place the
        # reader is looking.
        check("context: the reader is told where the sent window begins",
              page.locator(".chat-trimmed").count() == 1
              and page.locator(".chat-msg").count() >= 61)
        page.unroute("**/api/agent/chat/stream")

        # ---- Sessions: a new chat, and the ones before it ---------------------
        # The whole point of the block above: a conversation has a boundary, so
        # "hi" cannot be read as the next line of last month's thread.
        #
        # This is an end-to-end test.
        page.click("#chat-new")
        page.wait_for_selector("#chat-input")
        check("sessions: New chat empties the transcript",
              wait_until(lambda: page.locator(".chat-msg").count() == 0)
              and page.locator(".chat-trimmed").count() == 0)

        sent2 = []
        page.route("**/api/agent/chat/stream", lambda route: (
            sent2.append(json.loads(route.request.post_data)),
            route.fulfill(status=200, content_type="text/event-stream",
                          body='event: done\ndata: {"reply": "a clean answer",'
                               ' "mutated": false, "proposed": false, "steps": []}\n\n')))
        page.fill("#chat-input", "a brand new subject entirely")
        page.click("#chat-send")
        wait_for_capture(page, sent2)
        fresh = sent2[0] if sent2 else {"messages": []}
        # This is an end-to-end test.
        # The bug the user reported, as one assertion: the first turn of a new
        # chat carries exactly that turn. Nothing from before can reach it, so
        # there is nothing for the model to answer instead.
        check("sessions: the first turn of a new chat carries only itself",
              [m["content"] for m in fresh["messages"]] == ["a brand new subject entirely"]
              and fresh.get("session_id"))
        # This is an end-to-end test.
        # And it is a different chat from the one we left, not a cleared view of
        # the same one — otherwise New chat would be destroying history.
        check("sessions: a new chat is a new session, and the old one survives",
              fresh.get("session_id") != long_chat
              and any(s["id"] == long_chat for s in api_chat_sessions()))
        page.unroute("**/api/agent/chat/stream")

        # This is an end-to-end test.
        # One REAL turn, through the brain rather than a mocked stream, because
        # this is the check that the recording path carries the session all the
        # way: browser → /agent/chat/stream → remember() → /api/chat/messages →
        # the sessions table → back into the history panel. Every mocked block
        # above deliberately stops the brain ever hearing about the chat, so a
        # mocked chat has no row to list — which is exactly how this check first
        # failed, and why it is the one turn here that is not mocked.
        recorded_title = "a chat that is really recorded"
        page.click("#chat-new")
        page.wait_for_selector("#chat-input")
        page.fill("#chat-input", recorded_title)
        page.click("#chat-send")
        page.wait_for_selector(".chat-msg.assistant", timeout=20000)
        open_chat_history(page)
        titles = wait_until(
            lambda: page.locator(".chat-history-item").count() >= 3
            and recorded_title in page.locator(".chat-history-item").first.inner_text(),
            timeout=20)
        check("sessions: the history panel lists the chats, newest first",
              titles)

        # This is an end-to-end test.
        # Reopening is the other half of never losing a thought: a chat you left
        # is still there, with its messages, and you can keep talking in it.
        page.locator(".chat-history-item", has_text="seeded question 0").first.click()
        page.wait_for_selector("#chat-input")
        check("sessions: a historic chat reopens with its messages",
              wait_until(lambda: page.locator(".chat-msg").count() >= 60)
              and "seeded answer 29" in page.inner_text(".chat-log"))

        sent3 = []
        page.route("**/api/agent/chat/stream", lambda route: (
            sent3.append(json.loads(route.request.post_data)),
            route.fulfill(status=200, content_type="text/event-stream",
                          body='event: done\ndata: {"reply": "still talking",'
                               ' "mutated": false, "proposed": false, "steps": []}\n\n')))
        page.fill("#chat-input", "picking this back up")
        page.click("#chat-send")
        wait_for_capture(page, sent3)
        # This is an end-to-end test.
        check("sessions: a reopened chat is live, not read-only",
              sent3 and sent3[0].get("session_id") == long_chat
              and sent3[0]["messages"][-1]["content"] == "picking this back up")
        page.unroute("**/api/agent/chat/stream")

        # This is an end-to-end test.
        # The panel has to paint its own surface. It shipped with a --line border
        # and nothing else, and on the two dark skies that border is ~1.2:1
        # against the translucent sheet: the container had no visible edge, so a
        # list of chats read as loose text lying over the conversation. Every
        # other panel in this app that sits on a surface — .menu-panel, .plot-tip
        # — carries --card, --card-line and a lift; this asserts the panel's own
        # paint rather than a screenshot, on the themes where it actually failed.
        surfaces, on_top = {}, {}
        for sky in ("star", "dark"):
            select_theme(sky)
            page.wait_for_timeout(60)
            open_chat_history(page)
            surfaces[sky] = (css(".chat-history", "backgroundColor"),
                             css(".chat-history", "boxShadow"))
            # And it has to be the thing you are looking at. Asked of the browser
            # rather than of z-index: the panel's own 60 is meaningless if an
            # ancestor traps it, which is exactly what the star sky did — the
            # header and #board both take z-index 1 there to clear the sky, and
            # #board is later in the DOM, so the panel painted UNDER the sheet.
            on_top[sky] = page.evaluate("""() => {
                const p = document.querySelector('.chat-history');
                if (!p) return false;
                const b = p.getBoundingClientRect();
                const hit = document.elementFromPoint(b.x + 20, b.y + 20);
                return !!hit && p.contains(hit);
            }""")
        select_theme("light")
        check("sessions: the history panel paints its own surface on the dark skies",
              all(bg not in ("rgba(0, 0, 0, 0)", "transparent") and shadow != "none"
                  for bg, shadow in surfaces.values()))
        # This is an end-to-end test.
        check("sessions: the history panel is on top of the sheet, on every sky",
              all(on_top.values()))

        # This is an end-to-end test.
        # The hint has to be ours. Both switcher buttons used the native `title`
        # attribute, and its show delay belongs to the browser — around a second,
        # unreachable from CSS or JS — so the hint arrived after the pointer had
        # already moved on. Asserted through the pseudo-element the replacement
        # paints, plus the absence of the attribute that raced it; the accessible
        # name has to survive, because a decorative hint is not a label.
        page.hover("#chat-new")

        def hint():
            return page.evaluate("""() => {
                const s = getComputedStyle(document.getElementById('chat-new'),
                                           '::after');
                return [s.content, s.opacity];
            }""")

        shown = wait_until(lambda: float(hint()[1]) > 0.9, timeout=0.4)
        check("sessions: the New chat hint appears at once, not on the browser's delay",
              shown and "New chat" in hint()[0]
              and not page.get_attribute("#chat-new", "title")
              and not page.get_attribute("#chat-history-btn", "title")
              and page.get_attribute("#chat-new", "aria-label") == "New chat")

        # ---- The dock, and the assistant's tools in the header -----------------
        # History and the settings gear live in the app header beside the theme
        # picker. They were inside the sheet — the gear in the search row, the
        # chats behind a ▾ on a button hanging in the page margin — and the
        # person who asked for the trash could not find where deleted messages
        # had gone. What stays in the margin is where you are and New chat: a
        # label, because the list it used to open is now in the header.
        def box(sel):
            return page.locator(sel).bounding_box()

        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(120)

        # This is an end-to-end test.
        dock, sheet_box = box(".chat-dock"), box(".assistant-sheet")
        here = box(".chat-current")
        check("dock: the margin keeps the chat's name and nothing else",
              dock and sheet_box and here
              and dock["x"] + dock["width"] <= sheet_box["x"] + 1
              and abs(dock["y"] - sheet_box["y"]) <= 8
              # Every control has left it. A margin holding one label is a label;
              # a margin holding buttons is a second toolbar nobody looks at,
              # which is how the chats panel went unfound in the first place.
              and page.locator(".chat-dock button").count() == 0)

        # This is an end-to-end test.
        # All four tools are header furniture now, in one row, left to right in
        # the order you reach for them: search the record, list it, start a new
        # chat, configure. View-specific, like the board's own search and
        # filters — a gear for a screen you are not on is furniture that does
        # nothing.
        recall = box(".assistant-tools .chat-recall")
        hist, plus = box("#chat-history-btn"), box("#chat-new")
        gear, theme = box("#assistant-extras-btn"), box("#theme-select")
        row = [recall, hist, plus, gear, theme]
        in_header = (all(row) and sheet_box
                     and all(b["y"] + b["height"] <= sheet_box["y"] for b in row)
                     and all(abs(b["y"] - theme["y"]) <= 12 for b in row))
        ordered = all(row[i]["x"] < row[i + 1]["x"] for i in range(len(row) - 1))
        page.click("[data-view='board']")
        page.wait_for_selector(".column")
        away = not page.locator(".assistant-tools").is_visible()
        page.click("[data-view='assistant']")
        page.wait_for_selector("#chat-input")
        check("tools: search, History, New chat and the gear sit in the header, in order",
              in_header and ordered and away
              and page.locator(".assistant-tools").is_visible())

        # This is an end-to-end test.
        # The gear drops a panel like the two beside it. It used to open inside
        # the sheet, which put a settings drawer in the reading column and left
        # the button that opened it somewhere else entirely.
        page.click("#assistant-extras-btn")
        page.wait_for_selector("#assistant-extras:not([hidden])")
        drawer = box("#assistant-extras")
        gear = box("#assistant-extras-btn")
        dropped = (drawer and gear and sheet_box
                   and drawer["y"] >= gear["y"] + gear["height"] - 1
                   and drawer["y"] < sheet_box["y"] + 40)
        page.mouse.click(700, 700)
        shut = wait_until(lambda: page.locator("#assistant-extras[hidden]").count() == 1)
        check("tools: the settings drop from the gear and shut on a click outside",
              dropped and shut)

        # This is an end-to-end test.
        # Three ways out, because a panel dropped over the page is easy to walk
        # away from and one left open covers what it opened over.
        open_chat_history(page)
        hist = box("#chat-history-btn")
        # Polled, not sampled once: the panel is painted on the click and again
        # when the chats and the trash come back from the server, and a box read
        # in the instant between the two replacements is None.
        opened = wait_until(lambda: bool(box(".chat-history"))
                            and box(".chat-history")["y"] >= hist["y"])
        page.keyboard.press("Escape")
        by_escape = wait_until(lambda: page.locator(".chat-history").count() == 0)
        open_chat_history(page)
        page.mouse.click(700, 700)           # the transcript, not the tools
        by_click = wait_until(lambda: page.locator(".chat-history").count() == 0)
        open_chat_history(page)
        page.mouse.move(700, 820)            # pointer walks away and stays away
        by_idle = wait_until(lambda: page.locator(".chat-history").count() == 0,
                             timeout=9)
        check("tools: the History panel drops from its button and closes when unused",
              opened and by_escape and by_click and by_idle)

        # This is an end-to-end test.
        # Below the width where a margin exists, the dock tucks back inside the
        # sheet: controls hanging off the left of the window would be worse than
        # controls in a crowded row.
        page.set_viewport_size({"width": 1200, "height": 800})
        page.wait_for_timeout(160)
        tucked, sheet_narrow = box(".chat-dock"), box(".assistant-sheet")
        check("dock: with no margin to sit in, the dock tucks inside the sheet",
              tucked and sheet_narrow and tucked["x"] >= sheet_narrow["x"] - 1)
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(120)

        # ---- The topic-change nudge -------------------------------------------
        # Signal one is pure pattern matching, so it fires with BRAIN_EMBEDDER
        # =fake and needs no test-only override: typing a bare greeting into a
        # chat that already has messages is the case the user reported.
        #
        # This is an end-to-end test.
        nudged = []
        page.route("**/api/agent/chat/stream", lambda route: (
            nudged.append(json.loads(route.request.post_data)),
            route.fulfill(status=200, content_type="text/event-stream",
                          body='event: done\ndata: {"reply": "unexpected",'
                               ' "mutated": false, "proposed": false, "steps": []}\n\n')))
        page.fill("#chat-input", "hi")
        page.click("#chat-send")
        drifted = wait_until(lambda: page.locator(".chat-drift").count() == 1)
        check("nudge: a bare greeting in a long chat offers a new chat instead",
              drifted
              # Not sent. The turn is held back, so nothing is spent and there
              # is no answer to move afterwards.
              and not nudged
              # And the words are still in the composer, not swallowed by the
              # offer — a nudge that eats your message is worse than the bug.
              and page.input_value("#chat-input") == "hi")

        # This is an end-to-end test.
        # Dismissing sends the turn as asked. The user's judgement wins: this is
        # a suggestion, and a suggestion you cannot refuse is a decision.
        page.locator(".chat-drift button", has_text="Keep this one").click()
        wait_for_capture(page, nudged, 1)
        check("nudge: Keep this one sends the turn into the chat you were in",
              nudged and nudged[0].get("session_id") == long_chat
              and page.locator(".chat-drift").count() == 0)

        # This is an end-to-end test.
        # And once refused it stops asking for the rest of the chat — a broad
        # conversation must not be interrupted on every greeting.
        page.fill("#chat-input", "hello again")
        page.click("#chat-send")
        wait_for_capture(page, nudged, 2)
        check("nudge: a dismissed nudge does not ask again in the same chat",
              len(nudged) == 2 and page.locator(".chat-drift").count() == 0)

        # This is an end-to-end test.
        # Accepting is the whole feature: the message you typed opens its own
        # chat and is answered there, with nothing behind it.
        page.click("#chat-new")
        page.wait_for_selector("#chat-input")
        page.fill("#chat-input", "the weekend plan")
        page.click("#chat-send")
        wait_for_capture(page, nudged, 3)
        page.fill("#chat-input", "hi")
        page.click("#chat-send")
        wait_until(lambda: page.locator(".chat-drift").count() == 1)
        page.locator(".chat-drift button", has_text="Start a new chat").click()
        wait_for_capture(page, nudged, 4)
        started = nudged[3] if len(nudged) > 3 else {"messages": []}
        check("nudge: Start a new chat sends the message into a fresh session",
              [m["content"] for m in started["messages"]] == ["hi"]
              and started.get("session_id") != nudged[2].get("session_id"))
        page.unroute("**/api/agent/chat/stream")

        # ---- Renaming and deleting a chat -------------------------------------
        # Its own chat, seeded through the record, so neither control is exercised
        # against a conversation a later block still needs.
        api_chat_append("e2e-doomed", [
            {"role": "user", "content": "a chat that exists to be deleted"},
            {"role": "assistant", "content": "understood"},
        ])
        open_chat_history(page)
        # The panel asks the server on opening, so the new chat is there.
        wait_until(lambda: page.locator(
            ".chat-history-item", has_text="a chat that exists to be deleted").count() == 1)
        doomed = page.locator(".chat-history-item",
                              has_text="a chat that exists to be deleted").first

        # This is an end-to-end test.
        # A derived title is a starting point, not a name. Renaming goes through
        # the in-app prompt dialog — never a native one, which the run-wide
        # no-native-dialogs check also covers.
        # Hover the row first, the way the control is actually reached: it is
        # collapsed to no width until its row is under the pointer or holds focus,
        # so that a 320px panel spends its width on chat titles rather than on two
        # buttons nobody can see. A click without the hover is a click on
        # something invisible, which is not an interaction this app offers.
        doomed.hover()
        doomed.locator(".chat-history-rename").click()
        page.wait_for_selector("#prompt-dialog[open]")
        page.fill("#prompt-input", "Doomed chat, renamed")
        page.click("#prompt-ok")
        renamed = wait_until(lambda: page.locator(
            ".chat-history-item", has_text="Doomed chat, renamed").count() == 1)
        check("sessions: a chat can be renamed off its derived title",
              renamed
              and any(s["title"] == "Doomed chat, renamed" for s in api_chat_sessions()))

        # This is an end-to-end test.
        # Deleting has to reach the recall index, not just the list: ChatStore.sync
        # only ever adds, so without the reindex a deleted chat would go on
        # answering recall_chat — the worst version of this feature. The request is
        # the observable half of that; ChatStore.prune's own test owns the rest.
        renamed_row = page.locator(".chat-history-item",
                                   has_text="Doomed chat, renamed").first
        renamed_row.hover()
        renamed_row.locator(".chat-history-delete").click()
        page.wait_for_selector("#confirm-dialog[open]")
        with page.expect_request("**/api/rag/chat/reindex"):
            page.click("#confirm-ok")
        gone = wait_until(lambda: page.locator(
            ".chat-history-item", has_text="Doomed chat, renamed").count() == 0)
        check("sessions: deleting a chat drops it from the list and the index",
              gone
              and all(s["title"] != "Doomed chat, renamed" for s in api_chat_sessions())
              # Soft, not destroyed: the messages leave every live read, and the
              # rows are still in assistant.db — which is why no route can show
              # them, and why this asserts absence rather than presence.
              and all("a chat that exists to be deleted" != m["content"]
                      for m in api_chat_messages()))

        # ---- Deleting one message, and the trash behind it --------------------
        # Its own seeded chat again: this block deletes a turn out of the middle
        # of a transcript, and doing that to a conversation a later check reads
        # would make the two blocks depend on each other's order.
        blurted = "my card number is 4111 1111 1111 1111"
        api_chat_append("e2e-one-message", [
            {"role": "user", "content": "what should I pack for berlin"},
            {"role": "user", "content": blurted},
            {"role": "assistant", "content": "please do not paste that here"},
        ])
        # Indexed first, or the delete below could not be shown to reach Chroma:
        # a chunk that was never there cannot be pruned, and the check would
        # pass on an index that had simply never heard of the message.
        indexed_seed = api_chat_reindex()["indexed"] >= 3
        # Opened fresh, not inherited: the panel left over from the block above
        # was already counting down its idle timer — the pointer left the dock
        # when the confirm dialog took it — and it would close mid-wait here.
        # Escape first, then the click that opens it also refreshes the list.
        reopen_chat_history(page)
        wait_until(lambda: page.locator(
            ".chat-history-item", has_text="what should I pack for berlin").count() == 1)
        page.locator(".chat-history-item",
                     has_text="what should I pack for berlin").first.click()
        page.wait_for_selector("#chat-input")
        wait_until(lambda: page.locator(".chat-msg").count() == 3)

        # This is an end-to-end test.
        # A whole chat could always be deleted; one sentence inside it could not,
        # so a pasted card number stayed in the record and in recall for good.
        # The control is quiet until its turn is under the pointer — a delete
        # button over every sentence would make the transcript read as a list of
        # things to remove — so hovering is how it is really reached.
        doomed_msg = page.locator(".chat-msg", has_text=blurted).first
        doomed_msg.hover()
        with page.expect_response("**/api/rag/chat/reindex") as reindexed:
            doomed_msg.locator(".chat-msg-delete").click()
        # The response, not merely the request: what has to be true is that the
        # chunk left Chroma, and `pruned` is that fact rather than a proxy for it.
        pruned = reindexed.value.json().get("pruned", 0) >= 1
        left = wait_until(lambda: page.locator(".chat-msg").count() == 2)
        check("messages: deleting one turn drops it from the transcript and the index",
              left and indexed_seed and pruned
              and page.locator(".chat-msg", has_text=blurted).count() == 0
              # Gone from every live read, so recall_chat stops answering from
              # it — the reindex above is what carries that into Chroma.
              and all(m["content"] != blurted for m in api_chat_messages())
              # And its neighbours are untouched: a delete reaches one turn.
              and page.locator(".chat-msg").count() == 2
              and [m["content"] for m in api_chat_trash()] == [blurted])

        # This is an end-to-end test.
        # The trash is what makes the first delete safe to press: hidden, listed
        # with the chat it came out of, and one click from being back.
        reopen_chat_history(page)
        listed = wait_until(lambda: page.locator(".chat-trash-item").count() == 1)
        trashed_row = page.locator(".chat-trash-item").first
        says_where = "what should I pack for berlin" in trashed_row.inner_text()
        with page.expect_response("**/api/rag/chat/reindex") as resynced:
            trashed_row.locator(".chat-trash-restore").click()
        # The index has to follow the record in both directions: sync only ever
        # adds, which is exactly what a restore needs, and without this the turn
        # would come back visible and permanently unrecallable.
        reindexed_back = resynced.value.json().get("indexed", 0) >= 1
        back = wait_until(lambda: page.locator(".chat-msg").count() == 3)
        check("messages: the trash lists a deleted turn and restores it in place",
              listed and says_where and back and reindexed_back
              # Order is by createdAt, so it returns to the middle of the chat
              # it was taken out of rather than to the end.
              and [m.strip() for m in page.locator(".chat-text").all_inner_texts()]
                  == ["what should I pack for berlin", blurted,
                      "please do not paste that here"]
              and api_chat_trash() == [])

        # This is an end-to-end test.
        # The second step, and the only one that destroys anything: the row
        # leaves assistant.db for good. Confirmed through the in-app dialog, and
        # reindexed, because a restore may have put the chunks back since.
        page.locator(".chat-msg", has_text=blurted).first.hover()
        page.locator(".chat-msg", has_text=blurted).first.locator(".chat-msg-delete").click()
        wait_until(lambda: page.locator(".chat-msg").count() == 2)
        reopen_chat_history(page)
        wait_until(lambda: page.locator(".chat-trash-item").count() == 1)
        page.locator(".chat-trash-item").first.locator(".chat-trash-purge").click()
        page.wait_for_selector("#confirm-dialog[open]")
        with page.expect_request("**/api/rag/chat/reindex"):
            page.click("#confirm-ok")
        # The dialog took the pointer out of the dock, so hold the panel open the
        # way a reader would — otherwise an emptied list and a closed panel look
        # identical, and this check would pass for the wrong reason.
        page.locator(".chat-dock").hover()
        emptied = wait_until(lambda: page.locator(".chat-history").count() == 1
                             and page.locator(".chat-trash-item").count() == 0)
        check("messages: delete permanently erases the row from the record",
              emptied and api_chat_trash() == []
              # Nothing left to restore anywhere: this is the one chat route
              # that really erases, and the chat holding it survives it.
              and all(m["content"] != blurted for m in api_chat_messages())
              and any(s["id"] == "e2e-one-message" for s in api_chat_sessions()))

        # Leave a chat that holds a REAL, recorded, priced turn open. Every chat
        # this block made with a mocked stream has no receipt — the brain never
        # saw the turn — and the session-cost check further down reads the open
        # transcript. Ending on an empty new chat starved it, which is a fair
        # description of what a mocked send is: not a turn.
        open_chat_history(page)
        page.locator(".chat-history-item", has_text=recorded_title).first.click()
        page.wait_for_selector("#chat-input")
        wait_until(lambda: page.locator(".chat-msg").count() >= 2)

        # This is an end-to-end test.
        # The sheet was pinned at 720px while the replies it holds are card
        # dumps with uuids in them, so every second line wrapped. On a desktop
        # window it must take the room that is there.
        check("assistant: the sheet uses the width of a desktop window",
              page.locator(".assistant-sheet").bounding_box()["width"] >= 900)

        # This is an end-to-end test.
        # A side rail for the settings cost the transcript 300px of width and
        # stood mostly empty, because a model is chosen once and then left alone.
        # Everything that is not the conversation now hides behind one sign above
        # it, and while that is shut the conversation has the whole sheet. Width
        # is measured rather than assumed: a rail that is merely invisible would
        # still be holding the column open.
        sheet_box = page.locator(".assistant-sheet").bounding_box()
        log_box = page.locator(".chat-log").bounding_box()
        check("assistant: with the extras shut the conversation has the full width",
              page.locator(".assistant-extras").count() == 1
              and not page.locator("#chat-menu-btn").is_visible()
              and not page.locator(".chat-settings").is_visible()
              and log_box["width"] >= sheet_box["width"] - 40)
        # This is an end-to-end test.
        # The sign shares the search fold's row, at its right end — one line of
        # furniture above the transcript rather than two. Measured, because a
        # button that has wrapped onto its own line still reads as "present".
        gear_box = page.locator("#assistant-extras-btn").bounding_box()
        recall_box = page.locator(".chat-recall").bounding_box()
        check("assistant: the sign sits at the right end of the search row",
              gear_box["x"] >= recall_box["x"] + recall_box["width"]
              and gear_box["y"] < recall_box["y"] + recall_box["height"])

        open_extras(page)
        check("assistant: the sign opens onto the chat menu and the models",
              page.locator("#chat-menu-btn").is_visible()
              and page.locator(".chat-settings").is_visible()
              and page.locator(".assistant-extras #model-provider").count() == 1)

        # This is an end-to-end test.
        # Two buttons for one job became one menu, built out of the board's own
        # menu parts — the same control in two places must not be two designs.
        closed = (page.locator("#chat-menu-btn").count() == 1
                  and not page.locator("#chat-export-btn").is_visible())
        open_chat_menu(page)
        check("assistant: export and import are one menu in the board's idiom",
              closed
              and page.locator("#chat-menu-panel.menu-panel .menu-item").count() == 2
              and page.locator("#chat-export-btn").is_visible()
              and page.locator("#chat-import-btn").is_visible())
        page.keyboard.press("Escape")

        # This is an end-to-end test.
        # The board's search, filters, category tabs, tag bar and Menu filter and
        # act on cards, and the footer explains dragging cards and the keys that
        # move them. None of it reaches the Assistant, so around a conversation
        # they are furniture that does nothing. The theme picker stays: it is the
        # app's, not the board's.
        hidden_here = (not page.locator("#search").is_visible()
                       and not page.locator("#type-filter").is_visible()
                       and not page.locator("#prio-filter").is_visible()
                       and not page.locator("#menu-btn").is_visible()
                       and not page.locator("#cat-rail").is_visible()
                       and not page.locator("#tag-bar").is_visible()
                       and not page.locator(".app-footer").is_visible()
                       and page.locator("#theme-select").is_visible())
        page.locator('.view-switch button[data-view="board"]').click()
        page.wait_for_selector(".board .column")
        back_on_board = (page.locator("#search").is_visible()
                         and page.locator("#type-filter").is_visible()
                         and page.locator("#menu-btn").is_visible()
                         and page.locator("#cat-rail").is_visible()
                         and page.locator(".app-footer").is_visible())
        check("assistant: the board's own controls leave with the board",
              hidden_here and back_on_board)
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")

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

        # Recall now answers from the board's cards as well as the chat
        # record, so every hit must say which it is.
        labeled = False
        if has_panel and recalled:
            metas = page.locator(".recall-hit-meta").all_inner_texts()
            labeled = bool(metas) and all(
                m.startswith(("chat", "card")) for m in metas)
        check("assistant: every recall hit is labeled with its source", labeled)

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

        # This is an end-to-end test.
        # Something waiting for approval has to stay findable. It sits above the
        # transcript, which is correct until the transcript is long enough to push
        # the composer past the fold: the reader is then at the bottom typing, and
        # what needs their decision is somewhere off the top of the screen. So the
        # panel is pinned — scroll wherever you like, it is still on screen.
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        waiting = page.locator(".assistant-waiting")
        box = waiting.bounding_box() if waiting.count() else None
        height = page.evaluate("() => window.innerHeight")
        check("gate: what is waiting for approval stays on screen when scrolled away",
              box is not None
              # Wholly inside the viewport, not merely overlapping its edge.
              and box["y"] >= 0 and box["y"] + box["height"] <= height
              # And it is not inside the transcript, which scrolls on its own:
              # nesting it there would hide it behind the same problem again.
              and page.locator(".chat-log .assistant-waiting").count() == 0)

        # This is an end-to-end test.
        # What the session has cost so far, in money rather than tokens. Offline
        # the backend is a local fake, so the honest figure is 0.000$ — which is
        # the formatting this pins. That it is *true* of a local model rather than
        # a missing number is brain/tests/test_pricing.py's job.
        cost = page.locator(".assistant-cost")
        check("assistant: the session's cost is shown to three decimals",
              cost.count() == 1
              and re.search(r"= ?0\.000\$", cost.inner_text()) is not None
              # Beside the conversation, not inside it: a running total that
              # scrolls out of view with the transcript is not a running total.
              and page.locator(".chat-log .assistant-cost").count() == 0)

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

        # ---- Suggested edits: the agent asks, the user saves -----------------
        # The guardrail that replaced the one ungated write. A suggestion changes
        # nothing on its own; the user opens it, may adjust it, and their own save
        # is what applies it. So the assertions are in that order: unchanged,
        # then reviewable, then applied only after a save.
        target = api_state()["cards"][0]
        api_suggest_edit(target["id"], {"title": "A title the agent suggested",
                                       "columnId": "in-progress"})
        # Away and back: setView refreshes the waiting lists on entry, and the
        # previous block left us already on the Assistant, where a click is a
        # no-op. A suggestion made by a real chat turn arrives on its own flag.
        page.locator('.view-switch button[data-view="board"]').click()
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector(".suggestion")
        check("edits: the suggestion renders with review and dismiss",
              page.locator(".suggestion").count() == 1
              and page.locator(".suggestion-review").count() == 1
              and page.locator(".suggestion-dismiss").count() == 1)
        check("edits: the row says what would change, without opening it",
              "in progress" in page.inner_text(".suggestion-change").lower())
        check("edits: the card is untouched while the suggestion waits",
              next(c for c in api_state()["cards"]
                   if c["id"] == target["id"])["title"] == target["title"])
        check("edits: a waiting suggestion counts on the Assistant tab",
              page.locator('.view-switch button[data-view="assistant"] .view-badge')
                  .inner_text().strip() == "1")

        page.click(".suggestion-review")
        page.wait_for_selector("#card-dialog[open]")
        check("edits: reviewing opens the card with the suggestion filled in",
              page.input_value("#card-title") == "A title the agent suggested")
        # The user's own wording wins — that is the whole point of reviewing.
        page.fill("#card-title", "A title I chose myself")
        page.click("#card-form button[type=submit]")
        page.wait_for_function("document.querySelectorAll('.suggestion').length === 0")
        # The board push is debounced, so wait for the thing being asserted
        # rather than for a proxy — the way this suite's earlier flakes happened.
        def saved_card():
            return next(c for c in api_state()["cards"] if c["id"] == target["id"])
        applied = wait_until(lambda: saved_card()["title"] == "A title I chose myself")
        check("edits: saving applies what the USER left in the form", applied)
        check("edits: a suggested column move rides along with the save",
              wait_until(lambda: saved_card()["columnId"] == "in-progress"))
        check("edits: the answered suggestion leaves the list",
              len(api_edits()["edits"]) == 0)

        # Dismissing answers it the other way and touches nothing.
        api_suggest_edit(target["id"], {"notes": "notes the agent wanted"})
        page.locator('.view-switch button[data-view="board"]').click()
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector(".suggestion")
        page.click(".suggestion-dismiss")
        page.wait_for_function("document.querySelectorAll('.suggestion').length === 0")
        check("edits: dismissing clears it and leaves the card alone",
              len(api_edits()["edits"]) == 0
              and next(c for c in api_state()["cards"]
                       if c["id"] == target["id"])["notes"] != "notes the agent wanted")

        # ---- Assistant model settings ----------------------------------------
        # A "Models" panel with two pickers (text, omni) plus a fixed embedder
        # display. Only the text-generation pick changes behaviour today (it
        # rides along on every chat request); the omni pick is a stored
        # preference for the brain's coming media features. Both persist in
        # localStorage. The embedding row is deliberately NOT a picker: the
        # brain embeds with heydariAI/persian-embeddings (the
        # sentence-transformers default), the old dropdown was a stored
        # preference nothing read, and a control that does nothing teaches the
        # user not to trust the ones that do. The row now states the real,
        # fixed model instead.
        # The text picker is local-first: it offers the models pulled on this
        # machine (verified against `ollama list`), and OpenRouter is one
        # explicit selector away.
        DEFAULT_TEXT = "4skl/gemma4-e2b-mtp"
        ALT_TEXT = "gemma4:e2b"
        THIRD_TEXT = "deepseek-r1:8b"

        # Every option now says which route it takes, so the label is no longer
        # the slug. Assert on the values — the label carries its own check below.
        def option_values(selector):
            return page.locator(f"{selector} option").evaluate_all(
                "os => os.map(o => o.value)")
        # Every omni option must genuinely receive audio at a sane price — the
        # default is the cheapest usable audio model in the catalogue
        # (2026-08-02). Free dictation is Parakeet's job, locally, inside the
        # brain.
        DEFAULT_OMNI = "google/gemini-2.5-flash-lite"
        ALT_OMNI = ["openai/gpt-audio-mini"]
        # The one embedder the brain actually runs — fixed, local, not a pick.
        FIXED_EMBED = "heydariAI/persian-embeddings"
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")

        # This is an end-to-end test.
        # A model is chosen once and then left alone, so the panel is folded like
        # the evidence strip under a reply rather than filling the sheet with
        # controls nobody is using. Staying open is the load-bearing half:
        # choosing a provider re-renders these controls, and a panel that
        # refolded itself after every pick would be unusable.
        folded_first = not page.locator("#model-text").is_visible()
        open_models(page)
        unfolded = page.locator("#model-text").is_visible()
        page.locator('.view-switch button[data-view="board"]').click()
        page.wait_for_selector(".board .column")
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        # The settings are a dropdown in the header now, so leaving the view
        # dismisses the drawer the way a click anywhere else does. What has to
        # survive that is the FOLD inside it — reopening the gear must not also
        # refold the pickers.
        open_extras(page)
        check("assistant: the models panel is folded until asked for, then stays open",
              folded_first and unfolded and page.locator("#model-text").is_visible())

        check("assistant: settings panel offers the two model pickers",
              page.locator(".chat-settings").count() == 1
              and page.locator("#model-text").count() == 1
              and page.locator("#model-omni").count() == 1)
        check("assistant: the embedding dropdown is gone",
              page.locator("#model-embed").count() == 0)
        # The row is informative, not interactive: it names the real model and
        # says it runs locally, in the panel where the old picker stood.
        check("assistant: the fixed embedder display names the real model",
              page.locator(".chat-settings #model-embed-fixed").count() == 1
              and FIXED_EMBED in page.locator("#model-embed-fixed").inner_text())
        check("assistant: the fixed embedder says it runs locally",
              "local" in page.locator("#model-embed-fixed").inner_text().lower())
        # The note read "Persian-tuned", which undersells the model and reads as a
        # warning to anyone whose board is in English. It handles both.
        embed_note = page.locator("#model-embed-fixed").inner_text().lower()
        check("assistant: the embedder is described as multilingual, not Farsi-only",
              "multilingual" in embed_note and "english" in embed_note
              and "farsi" in embed_note and "persian-tuned" not in embed_note)
        check("assistant: the stale embedding-pick hint sentence is gone",
              "embedding pick is saved"
              not in page.locator(".chat-settings").inner_text())
        check("assistant: pickers default to the chosen slugs",
              page.input_value("#model-text") == DEFAULT_TEXT
              and page.input_value("#model-omni") == DEFAULT_OMNI)
        omni_options = option_values("#model-omni")
        check("assistant: the audio picker offers only models that take audio",
              omni_options == [DEFAULT_OMNI, *ALT_OMNI])
        text_options = option_values("#model-text")
        check("assistant: the text picker offers the local default and alternatives",
              text_options == [DEFAULT_TEXT, ALT_TEXT, THIRD_TEXT])
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
        # Sending is a click in the conversation, which dismisses the settings
        # dropdown like any other click outside it. Reopened rather than kept
        # open: a panel that survived a click into the transcript would be a
        # panel that never goes away.
        open_extras(page)
        page.select_option("#model-provider", "ollama")
        check("assistant: switching back restores the local list and its default",
              option_values("#model-text") == [DEFAULT_TEXT, ALT_TEXT, THIRD_TEXT]
              and page.input_value("#model-text") == DEFAULT_TEXT)
        # openrouter/auto is gone from every picker, not just the text one: it is
        # deprecated, and the resolved model was never read back out of the
        # response, so no picker should be able to hand the brain a router.
        check("assistant: no picker offers the deprecated openrouter/auto router",
              all("openrouter/auto" not in opts
                  for opts in (text_options, omni_options)))
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

        open_extras(page)   # sending dismissed the dropdown, as a click outside it does
        page.select_option("#model-text", ALT_TEXT)
        page.reload()
        page.wait_for_selector("#board")
        page.locator('.view-switch button[data-view="assistant"]').click()
        # attached, not visible: the Models panel is folded after a reload, so
        # waiting for the select to be *visible* would wait for a click that has
        # not happened yet.
        page.wait_for_selector("#model-text", state="attached")
        open_models(page)
        check("assistant: model choice survives a reload",
              page.input_value("#model-text") == ALT_TEXT)
        page.select_option("#model-text", DEFAULT_TEXT)

        # ---- A stale `embed` key from the removed embedding picker must stay
        # inert: no resurrected dropdown, and the other saved picks load as-is.
        MODELS_KEY = "lodestar:models"
        page.evaluate(
            "([key, text]) => localStorage.setItem(key, JSON.stringify("
            "{ text, embed: 'openai/text-embedding-3-small' }))",
            [MODELS_KEY, ALT_TEXT])
        page.reload()
        page.wait_for_selector("#board")
        page.locator('.view-switch button[data-view="assistant"]').click()
        # attached, not visible: the Models panel is folded after a reload, so
        # waiting for the select to be *visible* would wait for a click that has
        # not happened yet.
        page.wait_for_selector("#model-text", state="attached")
        open_models(page)
        check("assistant: a stale saved embed pick resurrects no dropdown",
              page.locator("#model-embed").count() == 0
              and page.locator("#model-embed-fixed").count() == 1)
        check("assistant: the saved picks beside a stale key load as-is",
              page.input_value("#model-text") == ALT_TEXT
              and page.input_value("#model-omni") == DEFAULT_OMNI)

        # The escape hatch: a slug unknown to the preset list is still
        # selected and still offered.
        HAND_PICKED = "anthropic/claude-sonnet-5"
        page.evaluate(
            "([key, text]) => localStorage.setItem(key, JSON.stringify({ text }))",
            [MODELS_KEY, HAND_PICKED])
        page.reload()
        page.wait_for_selector("#board")
        page.locator('.view-switch button[data-view="assistant"]').click()
        # attached, not visible: the Models panel is folded after a reload, so
        # waiting for the select to be *visible* would wait for a click that has
        # not happened yet.
        page.wait_for_selector("#model-text", state="attached")
        open_models(page)
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

        # ---- Chat transcript survives a reload (Session 6) -------------------
        # This is an end-to-end test.
        # The transcript is the one thing in the Assistant that was still lost on
        # every refresh, which contradicts the project's never-lose-a-thought
        # pillar as plainly as losing a card would. It follows the MODELS_KEY
        # pattern: same 'lodestar:' prefix, same write-behind-try/catch so a
        # private-mode quota error costs the session's history and never the
        # session itself.
        #
        # Three ways this can break, so three asserts in one test rather than
        # three tests. (1) The transcript does not come back at all. (2) `busy`
        # is restored as true — the reload lands mid-stream, the composer is
        # disabled forever and the view looks hung with no way out. (3) An
        # errored or partial turn comes back *and is replayed to the model as
        # history*: those turns are deliberately filtered from what is sent
        # (Session 3), and a restore that persists the text while dropping the
        # `error`/`partial` flag would silently undo that filter — the model
        # would be asked to continue from something it never finished saying.
        CHAT_KEY = "lodestar:chat"
        page.evaluate("key => localStorage.removeItem(key)", CHAT_KEY)
        page.reload()
        page.wait_for_selector("#board")
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")

        page.fill("#chat-input", "remember this across a reload")
        page.click("#chat-send")
        wait_until(lambda: page.locator(".chat-msg.assistant").count() >= 1)
        # An errored turn in the same transcript, so the restore is exercised
        # against a history that must come back visible but unsendable.
        n_errs = page.locator(".chat-msg.assistant.error").count()
        page.route("**/api/agent/chat/stream", lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"error":"nope"}'))
        n_before_err = len(errors)
        page.fill("#chat-input", "this turn fails")
        page.click("#chat-send")
        wait_until(lambda: page.locator(".chat-msg.assistant.error").count() > n_errs)
        page.unroute("**/api/agent/chat/stream")
        for e in [e for e in errors[n_before_err:] if "503" in e]:
            errors.remove(e)

        before = page.locator(".chat-msg").count()
        page.reload()
        page.wait_for_selector("#board")
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        check("chat: the transcript survives a reload",
              wait_until(lambda: page.locator(".chat-msg").count() == before)
              and "remember this across a reload"
              in page.locator(".chat-log").inner_text())
        # A reload that lands mid-stream must not restore a disabled composer.
        check("chat: a restored transcript never comes back busy",
              not page.locator("#chat-input").is_disabled()
              and not page.locator("#chat-send").is_disabled())
        # The errored turn is shown again...
        check("chat: a failed turn is restored as failed, not as an answer",
              page.locator(".chat-msg.assistant.error").count() >= 1)
        # ...but is still withheld from the model on the next turn.
        with page.expect_request("**/api/agent/chat/stream") as req_info:
            page.fill("#chat-input", "what did we say")
            page.click("#chat-send")
        sent = req_info.value.post_data or ""
        check("chat: a restored failed turn is still not replayed to the model",
              "remember this across a reload" in sent
              and "nope" not in sent)
        wait_until(lambda: not page.locator("#chat-input").is_disabled(), timeout=8.0)

        # ---- Chat export, JSON and Markdown (Session 6) ----------------------
        # This is an end-to-end test.
        # Reuses the board's export dialog rather than adding a second one, so
        # the copy/download fallback for browsers that block downloads is not
        # reimplemented (and cannot drift from the original). Both formats are
        # asserted in one test because they are one feature with a switch, not
        # two features: the failure worth catching is a format that renders the
        # wrong transcript or an empty one, and that is the same bug twice.
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        check("chat: the Assistant offers an export control",
              page.locator("#chat-export-btn").count() == 1)
        if page.locator("#chat-export-btn").count() == 1:
            open_chat_menu(page)
            page.click("#chat-export-btn")
            check("chat export: the shared export dialog opens",
                  wait_until(lambda: page.locator("#export-dialog").is_visible()))
            page.select_option("#chat-export-format", "json")
            payload = page.input_value("#export-json")
            try:
                parsed = json.loads(payload)
                roles = [m.get("role") for m in parsed.get("messages", [])]
                texts = " ".join(str(m.get("content", ""))
                                 for m in parsed.get("messages", []))
            except (ValueError, AttributeError):
                parsed, roles, texts = None, [], ""
            check("chat export: JSON parses and carries the transcript",
                  parsed is not None and "user" in roles and "assistant" in roles
                  and "remember this across a reload" in texts)
            page.select_option("#chat-export-format", "markdown")
            md = page.input_value("#export-json")
            check("chat export: Markdown names the speakers and keeps the text",
                  "remember this across a reload" in md
                  and md.lower().count("#") >= 2)
            # The copy button names what it will actually put on the clipboard.
            # Found by looking at the dialog rather than by a failing check: it
            # read "Copy JSON" while Markdown was selected, which is only
            # discovered after pasting.
            check("chat export: the copy button follows the chosen format",
                  page.locator("#copy-export").inner_text().strip() == "Copy Markdown")
            page.click("#cancel-export")
            wait_until(lambda: not page.locator("#export-dialog").is_visible())
            # The board's own export must still produce a board, not a chat.
            page.locator('.view-switch button[data-view="board"]').click()
            page.click("#menu-btn")
            page.click("#export-btn")
            wait_until(lambda: page.locator("#export-dialog").is_visible())
            board_json = page.input_value("#export-json")
            check("chat export: the board export is unchanged by it",
                  '"cards"' in board_json)
            page.click("#cancel-export")
            wait_until(lambda: not page.locator("#export-dialog").is_visible())

        # ---- Chat import (Session 7, stage 4) --------------------------------
        # This is an end-to-end test.
        # The missing half of export: a JSON export chosen from disk lands in the
        # durable record (assistant.db) and the Chroma index is asked to catch up
        # (POST /api/rag/chat/reindex). The file imported here is this browser's
        # own transcript — the agenda's first test case — which includes the
        # earlier failed turn, so the skip rule runs against real data: an
        # errored or partial turn is withheld from the record exactly as it is
        # withheld from the model.
        page.locator('.view-switch button[data-view="assistant"]').click()
        page.wait_for_selector("#chat-input")
        check("chat import: the Assistant offers an import control",
              page.locator("#chat-import-btn").count() == 1
              and page.locator("#chat-import-file").count() == 1)
        if page.locator("#chat-import-btn").count() == 1:
            # This browser's own transcript, read back out of the export dialog.
            open_chat_menu(page)
            page.click("#chat-export-btn")
            wait_until(lambda: page.locator("#export-dialog").is_visible())
            page.select_option("#chat-export-format", "json")
            import_payload = page.input_value("#export-json")
            page.click("#cancel-export")
            wait_until(lambda: not page.locator("#export-dialog").is_visible())
            exported_msgs = json.loads(import_payload)["messages"]
            clean_msgs = [m for m in exported_msgs
                          if not m.get("error") and not m.get("partial")
                          and str(m.get("content", "")).strip()]
            failed_texts = {m["content"] for m in exported_msgs
                            if m.get("error") and m.get("content")}
            check("chat import: the transcript carries a failed turn to exercise the skip",
                  len(failed_texts) > 0)

            def record_messages():
                with urllib.request.urlopen(f"{URL}/api/chat/messages") as r:
                    return json.load(r)["messages"]

            before_import = len(record_messages())
            page.set_input_files("#chat-import-file", [{
                "name": "lodestar-chat.json", "mimeType": "application/json",
                "buffer": import_payload.encode(),
            }])
            check("chat import: a confirm dialog states how many turns will import",
                  wait_until(lambda: page.locator("#confirm-dialog").is_visible())
                  and str(len(clean_msgs)) in page.locator("#confirm-copy").inner_text())
            with page.expect_request("**/api/rag/chat/reindex"):
                page.click("#confirm-ok")
            check("chat import: the clean turns land in the record",
                  wait_until(lambda: len(record_messages()) == before_import + len(clean_msgs)))
            imported_rows = record_messages()[before_import:]
            check("chat import: imported rows keep their role and content",
                  [r["role"] for r in imported_rows] == [m["role"] for m in clean_msgs]
                  and all(r["content"] == m["content"]
                          for r, m in zip(imported_rows, clean_msgs)))
            check("chat import: a failed turn's text is never recorded",
                  not any(r["content"] in failed_texts for r in imported_rows))

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
