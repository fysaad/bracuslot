"""
BRACU Slot Finder
------------------
Paste any BRAC University Wishlist / Self Registration / Advising schedule
link, enter your earned credits and program, and get your exact slot
(Date, Day, Start, End) without scrolling through the whole table.

Run locally:
    pip install -r requirements.txt
    streamlit run app.py
"""

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

# Keywords used to identify each logical column from raw header text.
# Order matters for the ambiguous ones (check "date" before "day").
COLUMN_KEYWORDS = {
    "from": ["from"],
    "to": ["to"],
    "program": ["program", "programme", "dept", "department"],
    "date": ["date"],
    "day": ["day"],
    "start": ["start"],
    "end": ["end"],
}

PROGRAM_ALIASES = {
    # Handles common variants students might type vs. how the site lists them
    "CSE": ["CSE", "CS"],
    "CS": ["CSE", "CS"],
}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class SlotRow:
    from_credit: float
    to_credit: float
    programs: list = field(default_factory=list)
    day: str = ""
    date: str = ""
    start: str = ""
    end: str = ""

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
    """Return the logical column key a header cell text most likely refers to."""
    lowered = text.lower()
    # Check 'date' before 'day' since 'date' does not contain 'day' but both
    # can appear together in the same header cluster.
    for key in ["date", "day", "from", "to", "start", "end", "program"]:
        for kw in COLUMN_KEYWORDS[key]:
            if kw in lowered:
                return key
    return None


def _build_column_map(header_rows):
    """
    header_rows: list of list[str] -- one or more header rows (positionally
    aligned) that sit above the data rows. We union keyword matches across
    all of them so split headers like:
        Row A: | Credits      | Program | Wishlist            |
        Row B: | From | To    |         | Day | Date | Start | End |
    still resolve correctly per column index.
    """
    max_cols = max((len(r) for r in header_rows), default=0)
    col_map = {}
    for col_idx in range(max_cols):
        for row in header_rows:
            if col_idx >= len(row):
                continue
            key = _classify_header(row[col_idx])
            if key and key not in col_map.values():
                col_map[col_idx] = key
                break
    return col_map  # {column_index: logical_key}


def _row_is_data_row(cells) -> bool:
    """A data row's first non-empty cell should parse as a credit number."""
    for cell in cells:
        if cell.strip():
            return _to_float(cell) is not None
    return False


def _table_to_grid(table):
    """
    Convert an HTML table into a 2D grid of cell text, correctly accounting
    for colspan/rowspan so that header and data cells line up by column
    index even when header cells merge across multiple columns or rows
    (very common in schedule tables, e.g. one "Program" header spanning
    two header rows, or "Wishlist" spanning four sub-columns).
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
    Scan every <table> on the page using a rowspan/colspan-aware grid, split
    rows into header rows (everything before the first row that looks like
    data) and data rows, and return the first table where a usable column
    map (from + to + program) can be built.
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
        required = {"from", "to", "program"}
        if not required.issubset(col_map.values()):
            continue

        slots = _rows_to_slots(data_rows, col_map)
        if slots:
            return slots

    # No usable <table> found (or none matched) — fall back to scanning the
    # whole page's visible text for schedule-shaped lines. This doesn't rely
    # on any particular tag structure, so it survives layout changes.
    return _parse_freeform_schedule(soup)


def _rows_to_slots(data_rows, col_map):
    inv_map = {v: k for k, v in col_map.items()}  # logical_key -> column_index
    slots = []
    for row in data_rows:
        try:
            from_idx = inv_map.get("from")
            to_idx = inv_map.get("to")
            prog_idx = inv_map.get("program")
            if from_idx is None or to_idx is None or prog_idx is None:
                continue
            if max(from_idx, to_idx, prog_idx) >= len(row):
                continue

            from_val = _to_float(row[from_idx])
            to_val = _to_float(row[to_idx])
            if from_val is None or to_val is None:
                continue

            programs = [p.strip().upper() for p in row[prog_idx].split(",") if p.strip()]

            def _get(key):
                idx = inv_map.get(key)
                return row[idx] if idx is not None and idx < len(row) else ""

            slots.append(
                SlotRow(
                    from_credit=from_val,
                    to_credit=to_val,
                    programs=programs,
                    day=_get("day"),
                    date=_get("date"),
                    start=_get("start"),
                    end=_get("end"),
                )
            )
        except (ValueError, IndexError):
            continue
    return slots


def _parse_freeform_schedule(soup):
    """
    Last-resort parser: strip the page down to its plain visible text and
    scan the whole thing for schedule-shaped substrings, regardless of
    whether they sit inside a <table>, a <ul>, or plain paragraphs. This
    survives most markup/layout changes since it doesn't depend on any tag.

    Matches patterns like:
    'From To Program1, Program2, ... Day N Weekday DD Month Start End'
    e.g. "115 206 ARC, CSE, ... Day 1 Sun 12 July 9:00 AM 10:30 AM"
    """
    # Drop elements that are almost never part of the actual schedule data
    # (nav/menus/scripts) so their text doesn't pollute the scan.
    for tag in soup.find_all(["nav", "script", "style", "header", "footer"]):
        tag.decompose()

    full_text = soup.get_text(" ", strip=True)
    full_text = re.sub(r"\s+", " ", full_text)

    pattern = re.compile(
        r"(?P<from>\d+(?:\.\d+)?)\s+(?P<to>\d+(?:\.\d+)?)\s+"
        r"(?P<programs>(?:[A-Z]{2,6}\s*,\s*)+[A-Z]{2,6}|[A-Z]{2,6})\s+"
        r"(?:(?P<day>Day\s*\d+)\s+)?"
        r"(?P<date>[A-Za-z]{3}\s+\d{1,2}\s+[A-Za-z]+)\s+"
        r"(?P<start>\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)\s*[-–—to]*\s*"
        r"(?P<end>\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)"
    )

    slots = []
    for match in pattern.finditer(full_text):
        g = match.groupdict()
        programs = [p.strip().upper() for p in g["programs"].split(",") if p.strip()]
        slots.append(
            SlotRow(
                from_credit=float(g["from"]),
                to_credit=float(g["to"]),
                programs=programs,
                day=(g.get("day") or "").strip(),
                date=g["date"].strip(),
                start=g["start"].upper().replace(".", ""),
                end=g["end"].upper().replace(".", ""),
            )
        )
    return slots or None


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------

st.set_page_config(page_title="BRACU Slot Finder", page_icon="🎓", layout="centered")

st.title("🎓 BRACU Slot Finder")
st.caption(
    "Paste a Wishlist / Self Registration / Advising schedule link, enter your "
    "earned credits and program, and get your exact time slot instantly."
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

    submitted = st.form_submit_button("Find My Slot", use_container_width=True)

if submitted:
    if not url.strip():
        st.error("Please enter a valid URL.")
    elif not program.strip():
        st.error("Please enter your program.")
    else:
        with st.spinner("Fetching and reading the schedule..."):
            try:
                html = fetch_html(url.strip())
            except requests.exceptions.RequestException as e:
                st.error(f"Couldn't fetch that URL. Details: {e}")
                st.stop()

            slots = find_schedule_table(html)

        if not slots:
            st.warning(
                "⚠️ Couldn't locate a recognizable schedule table on this page. "
                "The page's formatting may have changed, or this isn't a "
                "schedule page. Try opening the link in a browser to confirm "
                "it shows a From/To/Program/Date table."
            )
            with st.expander("See raw fetched content (debug)"):
                st.code(html[:5000], language="html")
        else:
            match = next((s for s in slots if s.matches(credits_, program)), None)

            if match:
                st.success("✅ Slot found!")
                st.markdown(
                    f"""
                    <style>
                    .slot-card {{
                        --card-bg: #f1f8f2;
                        --card-border: #2e7d32;
                        --card-heading: #1b5e20;
                        --card-text: #1b1b1b;
                        --card-hr: #a5d6a7;
                    }}
                    @media (prefers-color-scheme: dark) {{
                        .slot-card {{
                            --card-bg: #16241a;
                            --card-border: #4caf50;
                            --card-heading: #81c784;
                            --card-text: #f1f1f1;
                            --card-hr: #3e5c44;
                        }}
                    }}
                    .slot-card {{
                        border: 1px solid var(--card-border);
                        border-radius: 12px;
                        padding: 24px;
                        background-color: var(--card-bg);
                        color: var(--card-text);
                        margin-top: 10px;
                    }}
                    .slot-card h3 {{ margin-top: 0; color: var(--card-heading); }}
                    .slot-card p {{ font-size: 20px; margin: 6px 0; color: var(--card-text); }}
                    .slot-card p.meta {{ font-size: 16px; margin: 4px 0; }}
                    .slot-card hr {{ border-color: var(--card-hr); }}
                    </style>
                    <div class="slot-card">
                        <h3>Your Designated Slot</h3>
                        <p class="meta"><b>Program:</b> {program.upper()}</p>
                        <p class="meta"><b>Earned Credits:</b> {credits_}</p>
                        <hr>
                        <p>📅 <b>Date:</b> {match.date or '—'}</p>
                        <p>🗓️ <b>Day:</b> {match.day or '—'}</p>
                        <p>⏰ <b>Start:</b> {match.start or '—'}</p>
                        <p>⏰ <b>End:</b> {match.end or '—'}</p>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            else:
                st.error(
                    "❌ No matching slot found for that credit/program combination. "
                    "Double-check your entered credits and program code, or the "
                    "page may not include your program in its current schedule."
                )
                with st.expander("See all parsed rows (debug)"):
                    for s in slots:
                        st.write(
                            f"{s.from_credit}–{s.to_credit} | {', '.join(s.programs)} | "
                            f"{s.day} | {s.date} | {s.start}–{s.end}"
                        )

st.divider()
st.caption(
    "Note: This tool scrapes the live page each time you search, so results "
    "reflect whatever is currently published on the BRACU site."
    "Made BY BLUE"
)
