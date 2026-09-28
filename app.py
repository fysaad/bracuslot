"""
BRACU Slot Finder
------------------
Paste any BRAC University Wishlist / Self Registration / Advising schedule
link, enter your earned credits and program, and get your exact slot(s)
(Date, Day, Start, End) for every round the page publishes.

Run locally:
    pip install -r requirements.txt
    streamlit run app.py
"""

import html as html_lib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULT_URL = "https://www.bracu.ac.bd/self-registration-schedule-fall-2026"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
]
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}
REQUEST_TIMEOUT = 15
RELAY_PREFIX = "https://r.jina.ai/"  # last-resort public reader, used only if direct fetch is blocked

# Header keyword patterns (word-boundary based). Order matters: "date" is
# checked before "day".
KEY_PATTERNS = [
    ("date", r"\bdate\b"),
    ("day", r"\bday\b"),
    ("from", r"\bfrom\b"),
    ("to", r"\bto\b"),
    ("start", r"\bstart\b|\bbegin"),
    ("end", r"\bend\b|\bfinish"),
    ("program", r"\bprogram(?:me)?s?\b|\bdept\b|\bdepartment\b"),
]
SINGLE_KEYS = {"from", "to", "program"}
SESSION_KEYS = ("day", "date", "start", "end")

ROUND_RE = re.compile(
    r"(?:round|phase)\s*[-:]?\s*(\d+)|(\d+)\s*(?:st|nd|rd|th)\s*round", re.I
)

PROGRAM_ALIASES = {
    "CSE": ["CSE", "CS"],
    "CS": ["CSE", "CS"],
}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Session:
    label: str
    day: str = ""
    date: str = ""
    start: str = ""
    end: str = ""


@dataclass
class SlotRow:
    from_credit: float
    to_credit: float
    programs: list = field(default_factory=list)
    sessions: list = field(default_factory=list)  # one Session per round

    def matches(self, credits_: float, program: str) -> bool:
        program = program.strip().upper()
        candidates = PROGRAM_ALIASES.get(program, [program])
        credit_hit = self.from_credit <= credits_ <= self.to_credit
        program_hit = any(p in self.programs for p in candidates)
        return credit_hit and program_hit


# --------------------------------------------------------------------------
# Scraping / parsing helpers
# --------------------------------------------------------------------------

class FetchError(Exception):
    pass


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def load_snapshot(url: str):
    """
    Saved copy of a schedule page (snapshots/<last-url-segment>.txt), used only
    when the live site blocks this server. Returns (body_text, meta) or None.
    """
    slug = re.sub(r"[^a-z0-9-]", "", urlparse(url.strip()).path.rstrip("/").split("/")[-1].lower())
    if not slug:
        return None
    f = SNAPSHOT_DIR / f"{slug}.txt"
    if not f.is_file():
        return None
    meta, body = {}, []
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            if ":" in line:
                k, v = line[1:].split(":", 1)
                meta[k.strip().lower()] = v.strip()
        else:
            body.append(line)
    return "\n".join(body), meta


def apply_labels(slots, labels_meta: str):
    labels = [x.strip() for x in labels_meta.split("|") if x.strip()]
    for row in slots:
        if len(labels) == len(row.sessions) > 1:
            for sess, label in zip(row.sessions, labels):
                sess.label = label


def _looks_like_schedule(text: str) -> bool:
    low = text.lower()
    return "program" in low and re.search(r"\d{1,2}:\d{2}", text) is not None


def fetch_html(url: str) -> str:
    """
    Fetch a page like a real browser would. BRACU's server sometimes answers
    403 to bare/cloud-hosted clients, so: full browser headers, a session,
    a few retries with different user agents, then a last-resort public
    reader relay (only used if every direct attempt is blocked).
    """
    last_err = "unknown error"
    session = requests.Session()
    for attempt, ua in enumerate(USER_AGENTS):
        headers = {**BROWSER_HEADERS, "User-Agent": ua}
        try:
            resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.text
            last_err = f"HTTP {resp.status_code}"
            if resp.status_code not in (403, 429, 503):
                break
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(0.8 * (attempt + 1))

    try:
        resp = requests.get(
            RELAY_PREFIX + url,
            headers={"X-Return-Format": "html", "Accept": "text/html"},
            timeout=30,
        )
        if resp.status_code == 200 and _looks_like_schedule(resp.text):
            return resp.text
    except requests.RequestException:
        pass

    raise FetchError(f"The site refused the request ({last_err}).")


def _cell_text(cell) -> str:
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()


def _to_float(text: str):
    """Extract the first numeric value (supports decimals) from a string."""
    match = re.search(r"-?\d+(\.\d+)?", text.replace(",", ""))
    return float(match.group()) if match else None


def _classify_header(text: str):
    lowered = text.lower()
    for key, pattern in KEY_PATTERNS:
        if re.search(pattern, lowered):
            return key
    return None


def _find_round_label(texts):
    for t in texts:
        m = ROUND_RE.search(t)
        if m:
            return f"Round {m.group(1) or m.group(2)}"
    return None


NON_GROUP_TEXT = {"credits", "credit", "program", "programs"}


def _group_label(texts):
    """Use the merged group header above a column (e.g. 'Self-Registration
    (Repeat)') as the round label."""
    for t in texts:
        t = t.strip()
        if t and _classify_header(t) is None and t.lower() not in NON_GROUP_TEXT:
            return t
    return None


def _build_column_map(header_rows):
    """
    Returns a list of (column_index, key, round_label_or_None).
    Header rows are already grid-aligned (rowspan/colspan resolved), so each
    column's texts are read straight down the header rows. The lowest header
    row is the most specific, so it is checked first.
    """
    max_cols = max((len(r) for r in header_rows), default=0)
    cols = []
    seen_single = set()
    for ci in range(max_cols):
        texts = [r[ci] for r in header_rows if ci < len(r) and r[ci].strip()]
        key = None
        for t in reversed(texts):
            key = _classify_header(t)
            if key:
                break
        if key in SINGLE_KEYS:
            if key in seen_single:
                key = None
            else:
                seen_single.add(key)
        if key:
            cols.append((ci, key, _group_label(texts) or _find_round_label(texts)))
    return cols


def _group_session_columns(col_map):
    """
    Group day/date/start/end columns into rounds. Columns sharing a group
    header (e.g. 'Self-Registration (Round 1)') form one round; a repeated key
    under the same/no label starts the next round.
    Returns a list of (label, {key: column_index}).
    """
    groups = []  # each: {"label": str|None, "cols": {key: idx}}
    for idx, key, label in col_map:
        if key not in SESSION_KEYS:
            continue
        target = next(
            (g for g in groups if g["label"] == label and key not in g["cols"]),
            None,
        )
        if target is None:
            target = {"label": label, "cols": {}}
            groups.append(target)
        target["cols"][key] = idx

    labels = [g["label"] for g in groups]
    use_own_labels = (
        len(groups) > 1 and all(labels) and len(set(labels)) == len(labels)
    )
    result = []
    for n, g in enumerate(groups, start=1):
        if len(groups) == 1:
            label = "Your Slot"
        elif use_own_labels:
            label = g["label"]
        else:
            label = f"Round {n}"
        result.append((label, g["cols"]))
    return result


def _row_is_data_row(cells) -> bool:
    """A data row's first non-empty cell should parse as a credit number."""
    for cell in cells:
        if cell.strip():
            return _to_float(cell) is not None
    return False


def _table_to_grid(table):
    """
    Convert an HTML table into a 2D grid of cell text, correctly accounting
    for colspan/rowspan so header and data cells line up by column index.
    """
    trs = table.find_all("tr")
    grid = []
    pending = {}  # col_idx -> [text, remaining_rows]
    max_cols = 0

    for tr in trs:
        cells = tr.find_all(["th", "td"])
        row = []
        col = 0
        cell_idx = 0
        while cell_idx < len(cells) or col in pending:
            if col in pending:
                text, remaining = pending[col]
                row.append(text)
                if remaining <= 1:
                    del pending[col]
                else:
                    pending[col][1] -= 1
                col += 1
                continue

            cell = cells[cell_idx]
            text = _cell_text(cell)
            try:
                colspan = int(cell.get("colspan", 1) or 1)
            except ValueError:
                colspan = 1
            try:
                rowspan = int(cell.get("rowspan", 1) or 1)
            except ValueError:
                rowspan = 1

            for c in range(colspan):
                row.append(text)
                if rowspan > 1:
                    pending[col + c] = [text, rowspan - 1]
            col += colspan
            cell_idx += 1

        grid.append(row)
        max_cols = max(max_cols, len(row))

    for row in grid:
        while len(row) < max_cols:
            row.append("")

    return grid


def find_schedule_table(html: str):
    """
    Scan every <table> using a rowspan/colspan-aware grid, split header rows
    from data rows, map columns by header keywords, and return the parsed
    SlotRow list from the first table that has from/to/program + schedule
    columns. Falls back to a whole-page text scan.
    """
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")

    for table in tables:
        grid = _table_to_grid(table)
        parsed_rows = [row for row in grid if any(cell.strip() for cell in row)]
        if not parsed_rows:
            continue

        split_idx = None
        for i, row in enumerate(parsed_rows):
            if _row_is_data_row(row):
                split_idx = i
                break
        if split_idx is None or split_idx == 0:
            continue

        header_rows = parsed_rows[:split_idx]
        data_rows = parsed_rows[split_idx:]

        col_map = _build_column_map(header_rows)
        keys = {k for _, k, _ in col_map}
        if not {"from", "to", "program"}.issubset(keys):
            continue
        if not ({"date", "start"} & keys):
            continue

        slots = _rows_to_slots(data_rows, col_map)
        if slots:
            return slots

    return _parse_freeform_schedule(soup)


def _rows_to_slots(data_rows, col_map):
    single = {}
    for idx, key, _ in col_map:
        if key in SINGLE_KEYS:
            single[key] = idx
    groups = _group_session_columns(col_map)

    slots = []
    for row in data_rows:
        try:
            if max(single.values()) >= len(row):
                continue
            from_val = _to_float(row[single["from"]])
            to_val = _to_float(row[single["to"]])
            if from_val is None or to_val is None:
                continue

            programs = [
                p.strip().upper()
                for p in row[single["program"]].split(",")
                if p.strip()
            ]

            sessions = []
            for label, cols in groups:
                def _get(key, cols=cols):
                    i = cols.get(key)
                    return row[i] if i is not None and i < len(row) else ""

                s = Session(
                    label=label,
                    day=_get("day"),
                    date=_get("date"),
                    start=_get("start"),
                    end=_get("end"),
                )
                if s.date or s.start or s.end:
                    sessions.append(s)

            slots.append(
                SlotRow(
                    from_credit=from_val,
                    to_credit=to_val,
                    programs=programs,
                    sessions=sessions,
                )
            )
        except (ValueError, IndexError):
            continue
    return slots


def _parse_freeform_schedule(soup):
    """
    Last-resort parser: scan the page's visible text for schedule-shaped
    substrings, independent of tag structure. Supports one or two
    (Day, Date, Start, End) blocks per row, optionally preceded by
    'Round N' labels.
    """
    for tag in soup.find_all(["nav", "script", "style", "header", "footer"]):
        tag.decompose()

    full_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True).replace("*", ""))

    time_ = r"\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?"
    block = (
        r"(?:(?:Round\s*\d+)\s+)?"
        r"(?:(?P<day{n}>Day\s*\d+)\s+)?"
        r"(?P<date{n}>[A-Za-z]{3}\s+\d{1,2}(?:\s+|-)[A-Za-z]+)\s+"
        r"(?P<start{n}>" + time_ + r")\s*[-–—to]*\s*"
        r"(?P<end{n}>" + time_ + r")"
    )
    pattern = re.compile(
        r"(?P<from>\d+(?:\.\d+)?)\s+(?P<to>\d+(?:\.\d+)?)\s+"
        r"(?P<programs>(?:[A-Z]{2,6}\s*,\s*)+[A-Z]{2,6}|[A-Z]{2,6})\s+"
        + block.replace("{n}", "1")
        + r"(?:\s+" + block.replace("{n}", "2") + r")?"
    )

    slots = []
    for m in pattern.finditer(full_text):
        g = m.groupdict()
        programs = [p.strip().upper() for p in g["programs"].split(",") if p.strip()]

        blocks = []
        for n in ("1", "2"):
            if g.get(f"date{n}"):
                blocks.append(
                    Session(
                        label="",
                        day=(g.get(f"day{n}") or "").strip(),
                        date=g[f"date{n}"].strip(),
                        start=g[f"start{n}"].upper().replace(".", ""),
                        end=g[f"end{n}"].upper().replace(".", ""),
                    )
                )
        for i, s in enumerate(blocks, start=1):
            s.label = f"Round {i}" if len(blocks) > 1 else "Your Slot"

        slots.append(
            SlotRow(
                from_credit=float(g["from"]),
                to_credit=float(g["to"]),
                programs=programs,
                sessions=blocks,
            )
        )
    return slots or None


# --------------------------------------------------------------------------
# Result card rendering
# --------------------------------------------------------------------------

CARD_CSS = """<style>
.slot-card{--card-bg:#f1f8f2;--card-border:#2e7d32;--card-heading:#1b5e20;--card-text:#1b1b1b;--card-hr:#a5d6a7;}
@media (prefers-color-scheme: dark){.slot-card{--card-bg:#16241a;--card-border:#4caf50;--card-heading:#81c784;--card-text:#f1f1f1;--card-hr:#3e5c44;}}
.slot-card{border:1px solid var(--card-border);border-radius:12px;padding:24px;background-color:var(--card-bg);color:var(--card-text);margin-top:10px;}
.slot-card h3{margin-top:0;color:var(--card-heading);}
.slot-card p{font-size:20px;margin:6px 0;color:var(--card-text);}
.slot-card p.meta{font-size:16px;margin:4px 0;}
.slot-card hr{border-color:var(--card-hr);}
.slot-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-top:8px;}
.slot-session{border:1px solid var(--card-hr);border-radius:10px;padding:14px 18px;}
.slot-session h4{margin:0 0 6px 0;color:var(--card-heading);font-size:18px;}
</style>"""


def render_card(match: SlotRow, program: str, credits_: float) -> str:
    esc = html_lib.escape

    def session_html(s: Session) -> str:
        day_line = f"<p>🗓️ <b>Day:</b> {esc(s.day)}</p>" if s.day else ""
        return (
            '<div class="slot-session">'
            f"<h4>{esc(s.label)}</h4>"
            f"<p>📅 <b>Date:</b> {esc(s.date or '—')}</p>"
            + day_line
            + f"<p>⏰ <b>Start:</b> {esc(s.start or '—')}</p>"
            + f"<p>⏰ <b>End:</b> {esc(s.end or '—')}</p>"
            + "</div>"
        )

    sessions = "".join(session_html(s) for s in match.sessions)
    return (
        CARD_CSS
        + '<div class="slot-card">'
        + "<h3>Your Designated Slot</h3>"
        + f'<p class="meta"><b>Program:</b> {esc(program.upper())}</p>'
        + f'<p class="meta"><b>Earned Credits:</b> {credits_}</p>'
        + "<hr>"
        + f'<div class="slot-grid">{sessions}</div>'
        + "</div>"
    )


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------

st.set_page_config(page_title="BRACU Slot Finder", page_icon="🎓", layout="centered")

st.title("🎓 BRACU Slot Finder")
st.caption(
    "Paste a Wishlist / Self Registration / Advising schedule link, enter your "
    "earned credits and program, and get your exact time slot(s) instantly."
)

with st.form("slot_form"):
    url = st.text_input("Scheduling page URL", value=DEFAULT_URL)

    col1, col2 = st.columns(2)
    with col1:
        credits_ = st.number_input(
            "Earned Credits", min_value=0.0, max_value=207.0, value=0.00, step=0.5, format="%.2f"
        )
    with col2:
        program = st.selectbox(
            "Program",
            [
                "CSE", "CS", "EEE", "ECE", "BBA", "APE", "ARC", "BIO", "MIC",
                "PHY", "MAT", "ANT", "ECO", "ENG", "LLB", "AELS", "BDM", "Other",
            ],
            index=0,
        )
        if program == "Other":
            program = st.text_input("Enter your program code", value="")

    with st.expander("Can't load the link? Paste the schedule instead"):
        pasted = st.text_area(
            "Open the BRACU page, copy the schedule table, and paste it here",
            height=150,
        )

    submitted = st.form_submit_button("Find My Slot", use_container_width=True)

if submitted:
    pasted_text = pasted.strip()
    if not program.strip():
        st.error("Please enter your program.")
    elif not pasted_text and not url.strip():
        st.error("Please enter a valid URL (or paste the schedule text).")
    else:
        page_html = None
        snap_meta = None
        if pasted_text:
            page_html = pasted_text
        else:
            with st.spinner("Fetching and reading the schedule..."):
                try:
                    page_html = fetch_html(url.strip())
                except FetchError as e:
                    snap = load_snapshot(url.strip())
                    if snap:
                        page_html, snap_meta = snap
                        st.warning(
                            "BRACU is blocking live access from this server, so this result "
                            f"comes from a saved copy (saved {snap_meta.get('saved', 'earlier')}). "
                            "If the university changed the schedule since then, check the official page."
                        )
                    else:
                        st.error(f"Couldn't fetch that URL. {e}")
                        st.info(
                            "BRACU may be blocking this server. Open the page in your "
                            "browser, copy the schedule table, paste it into "
                            "\"Can't load the link?\" above, and press Find My Slot again."
                        )

        if page_html is not None:
            slots = find_schedule_table(page_html)
            if slots and snap_meta and snap_meta.get("labels"):
                apply_labels(slots, snap_meta["labels"])

            if not slots:
                st.warning(
                    "⚠️ Couldn't locate a recognizable schedule table on this page. "
                    "The page's formatting may have changed, or this isn't a "
                    "schedule page. Try opening the link in a browser to confirm "
                    "it shows a From/To/Program/Date table."
                )
                with st.expander("See raw fetched content (debug)"):
                    st.code(page_html[:5000], language="html")
            else:
                match = next((x for x in slots if x.matches(credits_, program)), None)

                if match:
                    st.success("✅ Slot found!")
                    st.markdown(render_card(match, program, credits_), unsafe_allow_html=True)
                else:
                    st.error(
                        "❌ No matching slot found for that credit/program combination. "
                        "Double-check your entered credits and program code, or the "
                        "page may not include your program in its current schedule."
                    )
                    with st.expander("See all parsed rows (debug)"):
                        for x in slots:
                            times = " | ".join(
                                f"{t.label}: {t.day} {t.date} {t.start}–{t.end}" for t in x.sessions
                            )
                            st.write(f"{x.from_credit}–{x.to_credit} | {', '.join(x.programs)} | {times}")

st.divider()
st.caption(
    "Note: This tool scrapes the live page each time you search, so results "
    "reflect whatever is currently published on the BRACU site.  \n"
    "Made BY BLUE"
)
