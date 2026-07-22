"""End-to-end verification of Question Board per plan.md's verification section."""
import json
import os
import sys
from playwright.sync_api import sync_playwright, expect

URL = "http://localhost:8741"
ARTIFACTS = os.path.join(os.path.dirname(__file__), "artifacts")
os.makedirs(ARTIFACTS, exist_ok=True)
shot = lambda name: os.path.join(ARTIFACTS, name)
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    page.goto(URL)
    page.wait_for_selector(".card")

    # 1. Seed data renders
    check("seed: 4 cards on first run", page.locator(".card").count() == 4)

    # 2. Quick-add captures to top of Inbox
    page.fill(".quick-add input", "What is speculative decoding?")
    page.press(".quick-add input", "Enter")
    first = page.locator('[data-col="inbox"] .card .card-title').first
    check("quick-add: new question at top of Inbox",
          first.inner_text() == "What is speculative decoding?")

    # 3. Edit modal: change priority + tags + notes
    page.locator('[data-col="inbox"] .card').first.click()
    page.wait_for_selector("#card-dialog[open]")
    page.fill("#card-notes", "Draft tokens from a small model, verify with the big one.")
    page.locator('.priority-picker label:has(input[value="high"])').click()
    page.fill("#card-tags", "inference, decoding")
    page.click('#card-form button[type="submit"]')
    card = page.locator('[data-col="inbox"] .card').first
    check("modal: priority badge updated to high",
          card.locator(".badge.high").count() == 1)
    check("modal: tags rendered", card.locator(".card-tag").count() == 2)
    check("modal: notes indicator shown", card.locator(".notes-dot").count() == 1)

    # 4. Keyboard move across columns: ] moves Inbox -> To Research
    card.focus()
    page.keyboard.press("]")
    page.wait_for_timeout(100)
    moved = page.locator('[data-col="to-research"] .card', has_text="speculative decoding")
    check("keyboard: ] moved card to To Research", moved.count() == 1)
    check("keyboard: focus restored after move",
          page.evaluate("document.activeElement?.classList.contains('card')"))

    # 5. Alt+ArrowUp reorders within column (card is last of 3, move it up once)
    page.keyboard.press("Alt+ArrowUp")
    page.wait_for_timeout(100)
    titles = page.locator('[data-col="to-research"] .card-title').all_inner_texts()
    check("keyboard: Alt+Up reordered within column",
          len(titles) == 3 and "speculative decoding" in titles[1])

    # 6. Sort by priority: high should come first in To Research
    page.locator('[data-col="to-research"] .sort-btn').click()
    page.wait_for_timeout(100)
    first_badge = page.locator('[data-col="to-research"] .card .badge').first
    check("sort: high-priority card first after sort",
          first_badge.inner_text().lower() == "high")

    # 7. Search filter
    page.fill("#search", "kv cache")
    page.wait_for_timeout(100)
    check("filter: search narrows to 1 card", page.locator(".card").count() == 1)
    page.fill("#search", "")

    # 8. Priority filter
    page.select_option("#priority-filter", "high")
    page.wait_for_timeout(100)
    check("filter: priority=high shows only high badges",
          page.locator(".card").count() == page.locator(".card .badge.high").count()
          and page.locator(".card").count() == 2)
    page.select_option("#priority-filter", "")

    # 9. Tag chip filter
    page.locator('.tag-chip:has-text("rag")').first.click()
    page.wait_for_timeout(100)
    check("filter: tag chip 'rag' shows 2 cards", page.locator(".card").count() == 2)
    page.locator('.tag-chip:has-text("rag")').first.click()

    # 10. Drag & drop: drag first Inbox card to Answered
    src = page.locator('[data-col="inbox"] .card').first
    dragged_title = src.locator(".card-title").inner_text()
    src.drag_to(page.locator('.cards[data-col="answered"]'))
    page.wait_for_timeout(100)
    check("drag & drop: card landed in Answered",
          page.locator('[data-col="answered"] .card', has_text=dragged_title[:30]).count() == 1)

    # 11. Persistence across reload
    count_before = page.locator(".card").count()
    page.reload()
    page.wait_for_selector(".card")
    check("persistence: board intact after reload",
          page.locator(".card").count() == count_before
          and page.locator('[data-col="answered"] .card').count() == 1)

    # 12. Export produces valid JSON with all cards
    with page.expect_download() as dl:
        page.click("#export-btn")
    path = dl.value.path()
    data = json.loads(open(path).read())
    check("export: valid JSON, version 1, all cards",
          data.get("version") == 1 and len(data.get("cards", [])) == count_before)

    # 13. Theme modes: all four apply, persist across reload, Day is plain white
    for theme in ("white", "sepia", "dark", "light"):
        page.select_option("#theme-select", theme)
        page.wait_for_timeout(50)
        check(f"theme: '{theme}' mode applied",
              page.evaluate("document.documentElement.dataset.theme") == theme)
    page.select_option("#theme-select", "sepia")
    page.screenshot(path=shot("theme-dusk.png"))
    page.select_option("#theme-select", "white")
    page.screenshot(path=shot("theme-day.png"))
    page.reload()
    page.wait_for_selector(".card")
    check("theme: choice persisted across reload",
          page.evaluate("document.documentElement.dataset.theme") == "white"
          and page.evaluate("document.querySelector('#theme-select').value") == "white")
    check("theme: Day mode uses a plain white background",
          page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)")
    page.select_option("#theme-select", "light")

    # 14. Delete via modal (accept confirm)
    page.on("dialog", lambda d: d.accept())
    page.locator('[data-col="answered"] .card').first.click()
    page.wait_for_selector("#card-dialog[open]")
    page.click("#delete-card")
    page.wait_for_timeout(150)
    check("delete: card removed from Answered",
          page.locator('[data-col="answered"] .card').count() == 0)

    # 15. Mobile viewport renders horizontally scrollable board
    page.set_viewport_size({"width": 375, "height": 800})
    page.wait_for_timeout(100)
    overflow = page.evaluate(
        "getComputedStyle(document.getElementById('board')).display")
    check("responsive: board switches to flex scroll at 375px", overflow == "flex")
    page.screenshot(path=shot("board-mobile.png"))

    # 16. Backlog view: shows the Inbox as a ledger list
    page.set_viewport_size({"width": 1440, "height": 900})
    inbox_count = page.locator('[data-col="inbox"] .card').count()
    page.locator('.view-switch button[data-view="backlog"]').click()
    page.wait_for_selector("#board.backlog")
    check("backlog: rows match Inbox card count",
          page.locator(".backlog-row").count() == inbox_count)
    check("backlog: view button marked pressed",
          page.get_attribute('.view-switch button[data-view="backlog"]', "aria-pressed") == "true")

    # 17. Backlog row opens the edit dialog
    page.locator(".backlog-row").first.click()
    page.wait_for_selector("#card-dialog[open]")
    check("backlog: row opens edit dialog", page.locator("#card-dialog[open]").count() == 1)
    page.click("#cancel-dialog")
    page.screenshot(path=shot("board-backlog.png"))

    # 18. Switch back to Board view restores columns
    page.locator('.view-switch button[data-view="board"]').click()
    page.wait_for_selector("#board.board")
    check("backlog: switching back restores the board",
          page.locator('.column[data-col="inbox"]').count() == 1)

    # 19. Import dialog documents the file format
    page.click("#import-btn")
    page.wait_for_selector("#import-dialog[open]")
    schema_text = page.locator("#import-schema").inner_text()
    check("import: dialog shows the JSON schema",
          '"version": 1' in schema_text and '"cards"' in schema_text
          and "inbox | to-research | in-progress | answered" in schema_text)
    page.screenshot(path=shot("import-dialog.png"))
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=URL)
    page.click("#copy-schema")
    clipboard = page.evaluate("navigator.clipboard.readText()")
    check("import: 'Copy schema' puts the schema on the clipboard",
          '"version": 1' in clipboard)

    # 20. Importing a file generated from the schema replaces the board
    import_file = shot("generated-import.json")
    with open(import_file, "w") as f:
        json.dump({"version": 1, "cards": [
            {"title": "Imported: how do I eval tool use?", "columnId": "to-research",
             "priority": "high", "tags": ["evals"]},
            {"title": "Imported: KV cache sizing rule of thumb?"},
        ]}, f)
    page.set_input_files("#import-input", import_file)  # confirm accepted by handler above
    page.wait_for_timeout(250)
    check("import: dialog closed after choosing a file",
          page.locator("#import-dialog[open]").count() == 0)
    check("import: board replaced with the 2 imported questions",
          page.locator(".card").count() == 2)
    check("import: columnId honored, defaults fill the gaps",
          page.locator('[data-col="to-research"] .card', has_text="eval tool use").count() == 1
          and page.locator('[data-col="inbox"] .card', has_text="KV cache sizing").count() == 1
          and page.locator('[data-col="inbox"] .badge.medium').count() == 1)
    check("import: ledger numbers assigned automatically",
          page.locator(".card-num").first.inner_text().startswith("Q-0"))

    check("console: no JS errors during entire run", not errors)
    if errors:
        print("Errors:", errors)

    browser.close()

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
