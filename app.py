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
from dataclasses import dataclass, field

import requests
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULT_URL = "https://www.bracu.ac.bd/ug-wishlist-event-schedule-fall-2026"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 15

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

def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


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
            cols.append((ci, key, _find_round_label(texts)))
    return cols


def _group_session_columns(col_map):
    """
    Group day/date/start/end columns into rounds. Columns carrying an explicit
    'Round N' label are grouped by that label; unlabeled columns are grouped
    by repetition (second occurrence of a key starts the next round).
    Returns a list of (label, {key: column_index}).
    """
    groups = []  # each: {"label": str|None, "cols": {key: idx}}
    for idx, key, label in col_map:
        if key not in SESSION_KEYS:
            continue
        target = None
        if label:
            target = next((g for g in groups if g["label"] == label), None)
        else:
            target = next(
                (g for g in groups if g["label"] is None and key not in g["cols"]),
                None,
            )
        if target is None:
            target = {"label": label, "cols": {}}
            groups.append(target)
        target["cols"][key] = idx

    result = []
    for n, g in enumerate(groups, start=1):
        label = g["label"] or (f"Round {n}" if len(groups) > 1 else "Your Slot")
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

    full_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    time_ = r"\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?"
    block = (
        r"(?:(?:Round\s*\d+)\s+)?"
        r"(?:(?P<day{n}>Day\s*\d+)\s+)?"
        r"(?P<date{n}>[A-Za-z]{3}\s+\d{1,2}\s+[A-Za-z]+)\s+"
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
        return (
            '<div class="slot-session">'
            f"<h4>{esc(s.label)}</h4>"
            f"<p>📅 <b>Date:</b> {esc(s.date or '—')}</p>"
            f"<p>🗓️ <b>Day:</b> {esc(s.day or '—')}</p>"
            f"<p>⏰ <b>Start:</b> {esc(s.start or '—')}</p>"
            f"<p>⏰ <b>End:</b> {esc(s.end or '—')}</p>"
            "</div>"
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
            "Earned Credits", min_value=0.0, max_value=200.0, value=73.5, step=0.5, format="%.2f"
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

    submitted = st.form_submit_button("Find My Slot", use_container_width=True)

if submitted:
    if not url.strip():
        st.error("Please enter a valid URL.")
    elif not program.strip():
        st.error("Please enter your program.")
    else:
        with st.spinner("Fetching and reading the schedule..."):
            try:
                page_html = fetch_html(url.strip())
            except requests.exceptions.RequestException as e:
                st.error(f"Couldn't fetch that URL. Details: {e}")
                st.stop()

            slots = find_schedule_table(page_html)

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
            match = next((s for s in slots if s.matches(credits_, program)), None)

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
                    for s in slots:
                        times = " | ".join(
                            f"{x.label}: {x.day} {x.date} {x.start}–{x.end}" for x in s.sessions
                        )
                        st.write(f"{s.from_credit}–{s.to_credit} | {', '.join(s.programs)} | {times}")

st.divider()
st.caption(
    "Note: This tool scrapes the live page each time you search, so results "
    "reflect whatever is currently published on the BRACU site."
)
