"""
IRD Prize Coupon Checker
-------------------------
A small local web app that:
  1. Stores YOUR coupon codes permanently in a local SQLite database
     (add / remove any time; nothing is lost between runs).
  2. Fetches the current list of winning coupon codes from
     https://prize.ird.gov.np/ on demand (you click "Refresh & Check").
  3. Compares your codes against the winners and shows you any matches.

Run it with:
    python app.py
Then open http://127.0.0.1:5000 in your browser.

IMPORTANT NOTE ON SCRAPING
--------------------------
prize.ird.gov.np is a JavaScript-rendered site, so this app uses Playwright
(a headless browser) to load the page fully before reading it, rather than
a simple HTTP request.

I was not able to test this scraper against the live site while building it,
so the code that extracts coupon codes from the page (see `extract_codes`
below, and the PATTERN setting) is a best-effort guess based on the coupon
format Nepal's news outlets have reported (e.g. "007 315 254 493").

If, after running this, the "Refresh & Check" button finds 0 winners even
though the site clearly has some listed:
  1. Click "Show raw fetched text" (debug panel) after a refresh attempt.
  2. Look at what was actually captured from the page.
  3. Go to Settings and adjust the regex PATTERN (or CSS SELECTOR) to match
     the real format/location of the codes on the page.
This only takes a minute and you won't need to touch the Python code.
"""

import re
import sqlite3
import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, jsonify

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "coupons.db"
TARGET_URL = "https://prize.ird.gov.np/"

DEVANAGARI_DIGITS = "०१२३४५६७८९"
ARABIC_DIGITS = "0123456789"
_DEVA_TO_ARABIC = str.maketrans(DEVANAGARI_DIGITS, ARABIC_DIGITS)

# Coupon codes on the site are shown in Devanagari numerals (e.g. ०४८९१५६१८२११),
# so we match runs of either Devanagari or ordinary digits.
DEFAULT_PATTERN = r"[०-९]{9,14}|\d{9,14}"
DEFAULT_SELECTOR = ""  # empty = search the whole rendered page text

app = Flask(__name__)


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS coupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            normalized_code TEXT NOT NULL,
            note TEXT DEFAULT '',
            added_at TEXT NOT NULL,
            matched INTEGER DEFAULT 0,
            matched_at TEXT,
            expiry_date TEXT
        );

        CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            winner_count INTEGER,
            raw_text TEXT,
            success INTEGER,
            error TEXT,
            match_found INTEGER DEFAULT 0,
            matched_codes TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS winners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            normalized_code TEXT NOT NULL,
            fetch_id INTEGER,
            seen_at TEXT NOT NULL,
            claim_deadline_text TEXT,
            claim_deadline_date TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # seed default settings if missing
    for k, v in (("pattern", DEFAULT_PATTERN), ("selector", DEFAULT_SELECTOR)):
        row = conn.execute("SELECT 1 FROM settings WHERE key=?", (k,)).fetchone()
        if not row:
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v))

    # Migration: older versions of this app didn't have these columns.
    # Adding them here is safe to run every time the app starts.
    for ddl in (
        "ALTER TABLE fetch_log ADD COLUMN match_found INTEGER DEFAULT 0",
        "ALTER TABLE fetch_log ADD COLUMN matched_codes TEXT DEFAULT ''",
        "ALTER TABLE coupons ADD COLUMN expiry_date TEXT",
        "ALTER TABLE winners ADD COLUMN claim_deadline_text TEXT",
        "ALTER TABLE winners ADD COLUMN claim_deadline_date TEXT",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.commit()
    conn.close()


def get_setting(key, default=""):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def normalize(code: str) -> str:
    """Strip everything except digits/letters and lowercase, so formatting
    differences (spaces, dashes, case) don't cause false non-matches.
    Also converts Devanagari (Nepali) numerals to ordinary 0-9 digits, since
    the IRD site displays coupon codes in Devanagari numerals."""
    code = (code or "").translate(_DEVA_TO_ARABIC)
    return re.sub(r"[^A-Za-z0-9]", "", code).lower()


# --------------------------------------------------------------------------
# Scraper
# --------------------------------------------------------------------------
def fetch_rendered_page_text(url=TARGET_URL, timeout_ms=60000):
    """Load the page with a headless browser and return (full_text, full_html).
    Requires: pip install playwright && playwright install chromium

    Also saves a screenshot to debug_screenshot.png in this folder so you
    can visually see what the browser actually loaded — very useful if the
    extracted text ever comes back empty or wrong.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Some sites block requests that look like an automated/headless
        # browser. Setting a normal desktop user-agent and viewport makes
        # this look like an ordinary Chrome browser.
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        # "networkidle" can hang forever on sites with continuous background
        # polling, so we wait only for the initial HTML/DOM to load, then
        # give the page's own JavaScript extra time to render content.
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        # Switch the site to English if possible. This gives us plain 0-9
        # digits and predictable English button labels ("Winner List",
        # "Next"), which is far more reliable to script against than
        # matching Nepali text/Devanagari numerals.
        try:
            eng_btn = page.get_by_text("English", exact=False).first
            if eng_btn.count() > 0:
                eng_btn.click(timeout=5000)
                page.wait_for_timeout(2500)
        except Exception:
            pass

        # The site opens on the "bill registration" form by default. The
        # actual winners list lives behind the "Winner List" (or, if the
        # English switch didn't work, "विजेता सूची") link/tab.
        clicked = False
        for label in ["Winner List", "विजेता सूची", "Winners", "विजेता"]:
            try:
                locator = page.get_by_text(label, exact=False).first
                if locator.count() > 0:
                    locator.click(timeout=5000)
                    clicked = True
                    break
            except Exception:
                continue

        page.wait_for_timeout(6000 if clicked else 2000)

        # Each prize row (e.g. "16 Winners" / "१६ विजेता") is a collapsed
        # accordion that must be clicked to reveal the coupon codes inside.
        # IMPORTANT: clicking a row that's already open closes it, so we
        # capture the page's text BOTH before and after clicking every row
        # once, and combine everything. That way we never lose whatever was
        # open before we started clicking.
        toggle_pattern = re.compile(r"\d+\s*(Winners?|विजेता)\b", re.IGNORECASE)

        def click_all_toggles():
            try:
                toggles = page.get_by_text(toggle_pattern).all()
                for t in toggles:
                    try:
                        t.click(timeout=2000)
                        page.wait_for_timeout(400)
                    except Exception:
                        continue
                page.wait_for_timeout(1500)
            except Exception:
                pass

        all_text_parts = []
        for page_num in range(1, 21):  # safety cap of 20 pages
            # Capture whatever is already visible/open before we touch anything.
            all_text_parts.append(page.inner_text("body"))
            # Now click every row once to flip its state, and capture again
            # so newly-opened rows are also recorded.
            click_all_toggles()
            all_text_parts.append(page.inner_text("body"))

            try:
                page.screenshot(
                    path=str(BASE_DIR / "debug_screenshot.png"), full_page=True
                )
            except Exception:
                pass

            # Try to advance to the next page of results ("Next" / "अर्को").
            advanced = False
            for next_label in ["Next", "अर्को"]:
                try:
                    next_btn = page.get_by_text(next_label, exact=False).first
                    if next_btn.count() == 0:
                        continue
                    if next_btn.is_disabled():
                        continue
                    next_btn.click(timeout=3000)
                    page.wait_for_timeout(2500)
                    advanced = True
                    break
                except Exception:
                    continue
            if not advanced:
                break

        html = page.content()
        text = "\n".join(all_text_parts)
        browser.close()
        return text, html


def extract_codes(text: str, pattern: str, html: str = "", selector: str = ""):
    """Extract candidate coupon codes from page text using the configured
    regex. If a CSS selector is configured, this function expects the caller
    to have already narrowed `text` down to that selector's content."""
    if not pattern:
        pattern = DEFAULT_PATTERN
    matches = re.findall(pattern, text)
    # de-duplicate while preserving order
    seen = set()
    codes = []
    for m in matches:
        norm = normalize(m)
        if norm and norm not in seen:
            seen.add(norm)
            codes.append(m.strip())
    return codes


# Lines that introduce a claim-deadline value on the site (Nepali + best
# guess at the English translation). The deadline value itself is expected
# to appear on the same line or the line right after one of these markers.
CLAIM_DEADLINE_MARKERS = [
    "दाबी गर्न अन्तिम समय",
    "claim available until",
    "claim deadline",
    "last date to claim",
    "claim by",
]

# Common date formats we try when parsing a captured deadline string into a
# real, comparable date. This only succeeds for Gregorian-looking dates
# (which the site may show once switched to English) — Bikram Sambat dates
# (e.g. "वि.सं. २०८३ आश्विन १६") won't parse here and are left as unknown
# rather than guessed at, so we never delete something we're not sure about.
_DATE_FORMATS = [
    "%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y",
    "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y",
    "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
]


def parse_deadline_date(text: str):
    """Best-effort: pull a Gregorian date out of a deadline string and
    return it as an ISO 'YYYY-MM-DD' string, or None if we can't parse it."""
    if not text:
        return None
    candidate = text.translate(_DEVA_TO_ARABIC).strip()
    # try to isolate a date-looking substring first
    m = re.search(
        r"\d{1,4}[/\-]\d{1,2}[/\-]\d{1,4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4}",
        candidate,
    )
    if m:
        candidate = m.group(0)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# The site explicitly tags coupons whose claim window has closed with a
# message like "The claim period for this prize coupon has ended." This is
# far more reliable than us trying to parse/compare dates ourselves, so we
# use it as the primary signal for what NOT to import.
EXPIRED_MARKERS = [
    "claim period for this prize coupon has ended",
    "claim period has ended",
    "दाबी अवधि सकिएको",
]


def extract_codes_with_deadlines(text: str, pattern: str):
    """Like extract_codes, but also captures each code's claim-deadline text
    and whether the site has explicitly tagged it as expired. Returns a
    list of dicts: {code, deadline_text, deadline_date, expired}.

    On the site, each winner block is laid out as:
        <code>
        <category / status>
        ELIGIBLE TRANSACTION DATES
        <dates>
        CLAIM AVAILABLE UNTIL
        <deadline>
    i.e. the deadline marker comes AFTER the code it belongs to, not
    before. So for each code we look forward for the nearest deadline
    marker (rather than carrying one forward from earlier in the page)."""
    import bisect

    if not pattern:
        pattern = DEFAULT_PATTERN
    code_re = re.compile(pattern)
    marker_re = re.compile("|".join(re.escape(m) for m in CLAIM_DEADLINE_MARKERS), re.IGNORECASE)
    expired_re = re.compile("|".join(re.escape(m) for m in EXPIRED_MARKERS), re.IGNORECASE)

    lines = text.splitlines()

    # Pass 1: find every deadline marker's line number and value, in order.
    markers = []  # list of (line_idx, value)
    i = 0
    while i < len(lines):
        if marker_re.search(lines[i]):
            after_marker = marker_re.sub("", lines[i]).strip(" :\u2013-")
            value = after_marker
            if not value:
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].strip():
                        value = lines[j].strip()
                        break
            if value:
                markers.append((i, value))
        i += 1
    marker_line_nums = [m[0] for m in markers]

    # Pass 2: find every code, and match it to the nearest deadline marker
    # that appears AT OR AFTER its own line (within a reasonable distance,
    # so we don't accidentally borrow a far-away, unrelated marker).
    seen = set()
    results = []
    MAX_LOOKAHEAD_LINES = 15
    for i, line in enumerate(lines):
        for m in code_re.finditer(line):
            code = m.group(0).strip()
            norm = normalize(code)
            if not norm or norm in seen:
                continue
            seen.add(norm)

            deadline_text = None
            deadline_date = None
            pos = bisect.bisect_left(marker_line_nums, i)
            if pos < len(markers):
                mline, mval = markers[pos]
                if mline - i <= MAX_LOOKAHEAD_LINES:
                    deadline_text = mval
                    deadline_date = parse_deadline_date(mval)

            window = lines[max(0, i - 3): min(len(lines), i + 6)]
            is_expired = any(expired_re.search(w) for w in window)

            results.append(
                {
                    "code": code,
                    "deadline_text": deadline_text,
                    "deadline_date": deadline_date,
                    "expired": is_expired,
                }
            )
    return results


def run_scrape_and_compare():
    """Fetch the site, extract winner codes, log the attempt, compare
    against stored user coupons, and return a result dict."""
    pattern = get_setting("pattern", DEFAULT_PATTERN)
    selector = get_setting("selector", DEFAULT_SELECTOR)
    now = datetime.datetime.now().isoformat(timespec="seconds")

    conn = get_db()
    try:
        text, html = fetch_rendered_page_text()

        scoped_text = text
        if selector:
            try:
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(html, "html.parser")
                nodes = soup.select(selector)
                if nodes:
                    scoped_text = "\n".join(n.get_text(" ", strip=True) for n in nodes)
            except Exception:
                # fall back to full text if selector fails / bs4 missing
                pass

        entries_all = extract_codes_with_deadlines(scoped_text, pattern)
        # Don't import codes the site has already explicitly tagged as
        # past their claim period — no point tracking or comparing against
        # a prize that can't be claimed anymore.
        entries = [e for e in entries_all if not e["expired"]]
        codes = [e["code"] for e in entries]

        cur = conn.execute(
            "INSERT INTO fetch_log (fetched_at, winner_count, raw_text, success, error) "
            "VALUES (?, ?, ?, 1, NULL)",
            (now, len(codes), text[:20000]),
        )
        fetch_id = cur.lastrowid

        for e in entries:
            conn.execute(
                "INSERT INTO winners (code, normalized_code, fetch_id, seen_at, "
                "claim_deadline_text, claim_deadline_date) VALUES (?, ?, ?, ?, ?, ?)",
                (e["code"], normalize(e["code"]), fetch_id, now, e["deadline_text"], e["deadline_date"]),
            )

        # Safety net: also delete any winner entries (from this fetch or
        # earlier ones still stored) whose claim deadline we could parse
        # and has already passed, in case the explicit "ended" tag wasn't
        # detected for some entry — these can no longer be claimed anyway.
        today = datetime.date.today().isoformat()
        conn.execute(
            "DELETE FROM winners WHERE claim_deadline_date IS NOT NULL AND claim_deadline_date < ?",
            (today,),
        )

        # Only codes that are still present (i.e. not just deleted as
        # expired) count toward the comparison below.
        remaining = conn.execute(
            "SELECT code, normalized_code FROM winners WHERE fetch_id=?", (fetch_id,)
        ).fetchall()
        codes = [r["code"] for r in remaining]
        winner_norms = {r["normalized_code"] for r in remaining}

        # Compare EVERY one of your stored coupons against EVERY (still
        # claimable) winner code retrieved just now.
        user_coupons = conn.execute("SELECT * FROM coupons").fetchall()

        matched_codes = []
        newly_matched = []
        for uc in user_coupons:
            if uc["normalized_code"] in winner_norms:
                matched_codes.append(uc["code"])
                if not uc["matched"]:
                    conn.execute(
                        "UPDATE coupons SET matched=1, matched_at=? WHERE id=?",
                        (now, uc["id"]),
                    )
                    newly_matched.append(uc["code"])

        any_match = len(matched_codes) > 0
        conn.execute(
            "UPDATE fetch_log SET match_found=?, matched_codes=? WHERE id=?",
            (1 if any_match else 0, ", ".join(matched_codes), fetch_id),
        )

        conn.commit()
        return {
            "success": True,
            "winner_count": len(codes),
            "codes": codes,
            "newly_matched": newly_matched,
            "any_match": any_match,
            "matched_codes": matched_codes,
            "raw_text": text[:20000],
            "fetched_at": now,
        }
    except Exception as e:
        conn.execute(
            "INSERT INTO fetch_log (fetched_at, winner_count, raw_text, success, error) "
            "VALUES (?, 0, '', 0, ?)",
            (now, str(e)),
        )
        conn.commit()
        return {"success": False, "error": str(e), "fetched_at": now}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    conn = get_db()
    coupons = conn.execute(
        "SELECT * FROM coupons ORDER BY matched DESC, added_at DESC"
    ).fetchall()
    last_fetch = conn.execute(
        "SELECT * FROM fetch_log ORDER BY id DESC LIMIT 1"
    ).fetchone()

    latest_winners = []
    if last_fetch and last_fetch["success"]:
        latest_winners = conn.execute(
            "SELECT code, claim_deadline_text FROM winners WHERE fetch_id=? ORDER BY id",
            (last_fetch["id"],),
        ).fetchall()

    user_norms = {c["normalized_code"] for c in coupons}
    winners_view = [
        {
            "code": w["code"],
            "is_yours": normalize(w["code"]) in user_norms,
            "deadline": w["claim_deadline_text"],
        }
        for w in latest_winners
    ]

    today = datetime.date.today().isoformat()
    expired_count = sum(
        1 for c in coupons if c["expiry_date"] and c["expiry_date"] < today
    )

    conn.close()
    return render_template(
        "index.html",
        coupons=coupons,
        last_fetch=last_fetch,
        winners=winners_view,
        pattern=get_setting("pattern", DEFAULT_PATTERN),
        selector=get_setting("selector", DEFAULT_SELECTOR),
        today=today,
        expired_count=expired_count,
    )


@app.route("/coupons/add", methods=["POST"])
def add_coupon():
    raw = request.form.get("codes", "")
    note = request.form.get("note", "").strip()
    expiry_date = request.form.get("expiry_date", "").strip() or None
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn = get_db()

    existing_norms = {
        r["normalized_code"] for r in conn.execute("SELECT normalized_code FROM coupons").fetchall()
    }
    duplicates = []
    added_norms = set()  # catches duplicates entered together in the same paste
    for code in lines:
        norm = normalize(code)
        if norm in existing_norms or norm in added_norms:
            duplicates.append(code)
            continue
        conn.execute(
            "INSERT INTO coupons (code, normalized_code, note, added_at, expiry_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (code, norm, note, now, expiry_date),
        )
        added_norms.add(norm)
    conn.commit()
    conn.close()

    if duplicates:
        # Pass the duplicate codes back via the URL so the page can show a
        # popup, without needing to switch this form to an AJAX submission.
        return redirect(url_for("index", dup=",".join(duplicates)))
    return redirect(url_for("index"))


@app.route("/coupons/delete/<int:coupon_id>", methods=["POST"])
def delete_coupon(coupon_id):
    conn = get_db()
    conn.execute("DELETE FROM coupons WHERE id=?", (coupon_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.route("/coupons/delete_expired", methods=["POST"])
def delete_expired():
    """Delete every coupon whose claim-deadline date has already passed."""
    today = datetime.date.today().isoformat()
    conn = get_db()
    conn.execute(
        "DELETE FROM coupons WHERE expiry_date IS NOT NULL AND expiry_date != '' "
        "AND expiry_date < ?",
        (today,),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.route("/refresh", methods=["POST"])
def refresh():
    result = run_scrape_and_compare()
    return jsonify(result)


@app.route("/settings", methods=["POST"])
def update_settings():
    set_setting("pattern", request.form.get("pattern", DEFAULT_PATTERN))
    set_setting("selector", request.form.get("selector", ""))
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    print("Starting IRD Coupon Checker at http://127.0.0.1:5000")

    def _open_browser():
        import time
        import webbrowser

        time.sleep(1.5)  # give the server a moment to start
        webbrowser.open("http://127.0.0.1:5000")

    import threading

    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(debug=False, port=5000)
