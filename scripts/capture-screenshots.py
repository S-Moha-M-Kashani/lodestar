"""Capture the README screenshots from a throwaway demo board.

    uv run --with playwright python scripts/capture-screenshots.py docs/img

Starts its own server on :3017 with a temporary database, loads
sample-overview.json plus a few deadlines, plans and habits, and saves one PNG
per view. The real board is never touched.
"""
import json, os, subprocess, sys, tempfile, time, urllib.request
from datetime import date, datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1]
PORT = 3017
URL = f"http://127.0.0.1:{PORT}"
PW = "demo-gallery-password"
tmp = tempfile.mkdtemp()
today = date.today()

def iso(d): return d.isoformat()
def stamp(days_ago): return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()

sample = json.load(open(os.path.join(ROOT, "sample-overview.json")))
cards = []
for i, c in enumerate(sample["cards"]):
    c = dict(c, id=f"demo-{i:03d}", num=i + 1)
    if c["type"] == "plan": c["type"] = "task"
    cards.append(c)
by = {c["title"]: c for c in cards}
def edit(title, **kw): by[title].update(kw)

# A little life on top of the sample: deadlines, plans, dreams, parts, habits.
edit("Renew the passport before it lapses", deadline=iso(today - timedelta(days=2)), importance="high", urgency="high")
edit("Find a dentist that does evening appointments", deadline=iso(today + timedelta(days=3)))
edit("Prepare the performance review write-ups", deadline=iso(today + timedelta(days=5)), importance="high")
edit("Write the anniversary letter", deadline=iso(today + timedelta(days=9)))
edit("Japan in cherry-blossom season — 2027?", type="dream", plan="2027-04")
edit("Interrail with the nieces next summer", type="dream", plan="2027")
edit("Record a lo-fi cover of 'Heroes'", type="dream")
edit("Shortlist towns for the autumn hiking trip", plan=iso(today))
edit("Draft the conference talk proposal", plan=f"{today.year}-{today.month:02d}")
edit("Fix the squeaky bedroom door", plan=iso(today + timedelta(days=1)))
parent = by["Sort out grandpa's photo archive"]
n = len(cards)
for j, t in enumerate(["Scan the 1970s albums", "Label faces with grandma", "Share the folder with the cousins"]):
    cards.append({"id": f"demo-{n + j:03d}", "num": n + j + 1, "title": t, "type": "task", "category": "family",
                  "columnId": "inbox" if j else "in-progress", "parentId": parent["id"], "notes": "", "tags": [],
                  "createdAt": stamp(10), "updatedAt": stamp(1)})
n = len(cards)
def history(days, per_day):
    return {iso(today - timedelta(days=d)): per_day for d in range(1, days) if d % 5 != 3}
cards.append({"id": f"demo-{n:03d}", "num": n + 1, "title": "Meditate", "type": "habit", "category": "health",
              "columnId": "inbox", "habitFreq": "daily", "habitCount": 2,
              "habitTimes": [{"time": "07:30"}, {"time": "21:00"}], "habitHistory": {**history(21, 2), iso(today): 1},
              "notes": "", "tags": [], "createdAt": stamp(30), "updatedAt": stamp(0)})
cards.append({"id": f"demo-{n+1:03d}", "num": n + 2, "title": "Practise guitar", "type": "habit", "category": "music",
              "columnId": "inbox", "habitFreq": "daily", "habitCount": 1, "habitTimes": [{"time": "18:00"}],
              "habitHistory": history(14, 1), "notes": "", "tags": [], "createdAt": stamp(20), "updatedAt": stamp(1)})
cards.append({"id": f"demo-{n+2:03d}", "num": n + 3, "title": "Call mum", "type": "habit", "category": "family",
              "columnId": "inbox", "habitFreq": "weekly", "habitCount": 1, "habitTimes": [{"weekday": 0, "time": "10:00"}],
              "habitHistory": {}, "notes": "", "tags": [], "createdAt": stamp(40), "updatedAt": stamp(3)})
for c in cards:
    if c["columnId"] == "answered": c["doneAt"] = c.get("updatedAt")

env = {**os.environ, "PORT": str(PORT), "BOARD_DB": f"{tmp}/board.db", "ASSISTANT_DB": f"{tmp}/assistant.db",
       "AGENT_URL": "http://127.0.0.1:9", "LODESTAR_AUTH_PASSWORD": PW, "LODESTAR_BACKUP_ON_WRITE": "0",
       "LODESTAR_BACKUP_DIR": f"{tmp}/backups", "LODESTAR_RCLONE_BIN": "/nonexistent", "NODE_NO_WARNINGS": "1"}
for k in [k for k in env if k.startswith("LODESTAR_AUTH_PASSWORD_HASH")]: env.pop(k)
srv = subprocess.Popen(["node", "server.js"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    try: urllib.request.urlopen(URL + "/api/health", timeout=1); break
    except Exception: time.sleep(0.1)

def shot(page, name, **kw):
    page.wait_for_timeout(700)
    page.screenshot(path=f"{OUT}/{name}.png", scale="css", animations="disabled", **kw)
    print("saved", name)

def view(page, name):
    if name in ("overview", "matrix", "areas"):
        page.click("#lenses-btn"); page.click(f"#lenses-panel button[data-view={name}]")
    else:
        page.click(f".view-switch button[data-view={name}]")
    page.wait_for_timeout(900)

def open_board(browser, **ctx_args):
    ctx = browser.new_context(reduced_motion="reduce", **ctx_args)
    page = ctx.new_page()
    page.add_init_script("window.QBOARD_DISABLE_SEMANTIC = true")
    assert page.request.post(URL + "/api/login", data={"password": PW}).ok
    return ctx, page

try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx, page = open_board(b, viewport={"width": 1440, "height": 900})
        r = page.request.put(URL + "/api/state", data={"version": 1, "cards": cards})
        assert r.ok, (r.status, r.text())
        page.goto(URL + "/"); page.wait_for_selector("#board .card", state="attached")
        shot(page, "hero")
        page.click("#filter-btn"); page.wait_for_selector("#filter-panel:not([hidden])")
        shot(page, "filters"); page.keyboard.press("Escape")
        page.locator(".card", has_text="Sort out grandpa").first.click()
        page.wait_for_selector("dialog[open]")
        shot(page, "card"); page.keyboard.press("Escape")
        for v in ("matrix", "areas", "review", "overview"):
            view(page, v); shot(page, v)
        ctx.close()
        ctx, page = open_board(b, viewport={"width": 390, "height": 844}, device_scale_factor=2,
                               is_mobile=True, has_touch=True)
        page.goto(URL + "/"); page.wait_for_selector("#board .card", state="attached")
        page.wait_for_timeout(700)
        page.screenshot(path=f"{OUT}/mobile.png", animations="disabled",
                        clip={"x": 0, "y": 0, "width": 390, "height": 750})
        print("saved mobile")
        b.close()
finally:
    srv.terminate()
