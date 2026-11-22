"""The user list .xlsx format, shared by export, import and the sample file.

    | Név        | Felhasználónév | Osztály           |
    | Kiss Péter | kiss.peter     | 9.A               |
    | Nagy Anna  | nagy.anna      | 10.B, Kosárlabda  |

"Osztály" holds group names separated by commas. Export writes the user's groups of
kind "class"; import puts the user in every listed group (creating missing ones as
classes).
"""

import io
import re
from dataclasses import dataclass, field
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

HEADER = ("Név", "Felhasználónév", "Osztály")
# Login slips: the import columns first, so the file can be re-imported as is.
CREDENTIALS_HEADER = (*HEADER, "Egyszeri jelszó", "Érvényes eddig")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_ROWS = 5000
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]+$")

SAMPLE_ROWS = [
    ("Kiss Péter", "kiss.peter", "9.A"),
    ("Nagy Anna", "nagy.anna", "9.A"),
    ("Szabó Bence", "szabo.bence", "10.B, Informatika szakkör"),
]


def natural_key(s: str) -> list:
    """Sort "9.A" before "10.B" (numbers compared as numbers)."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", s)]


def write_users(rows: list[tuple[str, ...]], header: tuple[str, ...] = HEADER, sheet_title: str = "Felhasználók") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append(list(row))
    # Spreadsheet formula injection: a display name like "=HYPERLINK(...)" must stay
    # text. openpyxl turns strings starting with "=" into formulas, so force text type.
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    ws.freeze_panes = "A2"
    for col, width in zip("ABCDE", (28, 22, 30, 20, 20), strict=False):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@dataclass
class ParsedRow:
    row: int  # 1-based spreadsheet row number, for error messages
    name: str
    username: str
    classes: list[str]


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)  # (row, message)


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # e.g. a numeric username typed into Excel
    return str(v).strip()


def read_users(data: bytes) -> ParseResult:
    """Parse and validate an import file. Never raises for bad content; problems are
    returned as (row, message) so the UI can show all of them at once."""
    result = ParseResult()
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 - corrupt zip, wrong format, ...: all mean "not a usable file"
        result.errors.append((0, f"nem olvasható .xlsx fájl ({type(e).__name__})"))
        return result
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        got = tuple(_text(v) for v in (header or ())[:3])
        if tuple(h.casefold() for h in got) != tuple(h.casefold() for h in HEADER):
            result.errors.append((1, f"a fejléc legyen: {' | '.join(HEADER)} (ez van: {' | '.join(got) or 'üres'})"))
            return result

        seen: dict[str, int] = {}
        for idx, values in enumerate(rows, start=2):
            values = tuple(values or ()) + (None, None, None)
            name, username, classes_raw = (_text(v) for v in values[:3])
            if not (name or username or classes_raw):
                continue  # blank line
            if len(result.rows) + len(result.errors) >= MAX_ROWS:
                result.errors.append((idx, f"legfeljebb {MAX_ROWS} sor importálható egyszerre"))
                break
            if not username:
                result.errors.append((idx, "hiányzik a felhasználónév"))
                continue
            if len(username) > 128 or not USERNAME_RE.match(username):
                result.errors.append(
                    (idx, f"érvénytelen felhasználónév: {username!r} (csak betű, szám, . _ @ - ; ékezet nélkül)")
                )
                continue
            if username in seen:
                result.errors.append((idx, f"a(z) {username!r} felhasználónév már szerepel a {seen[username]}. sorban"))
                continue
            seen[username] = idx
            if len(name) > 200:
                result.errors.append((idx, "a név legfeljebb 200 karakter lehet"))
                continue
            classes = list(dict.fromkeys(c.strip() for c in classes_raw.split(",") if c.strip()))
            if any(len(c) > 200 for c in classes):
                result.errors.append((idx, "az osztály neve legfeljebb 200 karakter lehet"))
                continue
            result.rows.append(ParsedRow(row=idx, name=name or username, username=username, classes=classes))
    finally:
        wb.close()
    return result
