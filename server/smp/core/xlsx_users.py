# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""The user list .xlsx format, shared by export, import and the sample file.

    | Name       | Username       | Class             |     (English)
    | Név        | Felhasználónév | Osztály           |     (Hungarian)
    | Kiss Péter | kiss.peter     | 9.A               |
    | Nagy Anna  | nagy.anna      | 10.B, Kosárlabda  |

"Class" holds group names separated by commas. Export writes the user's groups of
kind "class"; import puts the user in every listed group (creating missing ones as
classes).

Files are written with English or Hungarian headers (`lang`, chosen by the UI
language); import accepts either, case-insensitively. Import problems are reported
as coded English messages (see errors.py).
"""

import io
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from smp.core.errors import Problem

Lang = Literal["en", "hu"]

HEADERS: dict[Lang, tuple[str, str, str]] = {
    "en": ("Name", "Username", "Class"),
    "hu": ("Név", "Felhasználónév", "Osztály"),
}
# Login slips: the import columns first, so the file can be re-imported as is.
CREDENTIALS_HEADERS: dict[Lang, tuple[str, ...]] = {
    "en": (*HEADERS["en"], "One-time password", "Valid until"),
    "hu": (*HEADERS["hu"], "Egyszeri jelszó", "Érvényes eddig"),
}
SHEET_TITLES: dict[Lang, str] = {"en": "Users", "hu": "Felhasználók"}
# Download names: (users list, login slips, import sample); the list/slips get "-<date>.xlsx".
FILE_NAMES: dict[Lang, tuple[str, str, str]] = {
    "en": ("users", "login-details", "users-sample.xlsx"),
    "hu": ("felhasznalok", "belepesi-adatok", "felhasznalok-minta.xlsx"),
}
# Expiry column of the login slips, in each language's usual format.
DATETIME_FORMATS: dict[Lang, str] = {"en": "%Y-%m-%d %H:%M", "hu": "%Y.%m.%d. %H:%M"}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_ROWS = 5000
MAX_TEXT = 200  # name and class name length
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]+$")

SAMPLE_ROWS = [
    ("Kiss Péter", "kiss.peter", "9.A"),
    ("Nagy Anna", "nagy.anna", "9.A"),
    ("Szabó Bence", "szabo.bence", "10.B, Informatika szakkör"),
]


def natural_key(s: str) -> list:
    """Sort "9.A" before "10.B" (numbers compared as numbers)."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", s)]


def write_users(rows: list[tuple[str, ...]], lang: Lang = "en", credentials: bool = False) -> bytes:
    """The user list (or, with credentials=True, the login slips) with `lang` headers."""
    header = CREDENTIALS_HEADERS[lang] if credentials else HEADERS[lang]
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLES[lang]
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
    errors: list[tuple[int, Problem]] = field(default_factory=list)  # (row, problem)


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # e.g. a numeric username typed into Excel
    return str(v).strip()


def read_users(data: bytes) -> ParseResult:
    """Parse and validate an import file. Never raises for bad content; problems are
    returned as (row, problem) so the UI can show all of them at once."""
    result = ParseResult()
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 - corrupt zip, wrong format, ...: all mean "not a usable file"
        kind = type(e).__name__
        result.errors.append((0, Problem("import.unreadable", f"not a readable .xlsx file ({kind})", {"reason": kind})))
        return result
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        got = tuple(_text(v).casefold() for v in (header or ())[:3])
        if not any(got == tuple(h.casefold() for h in expected) for expected in HEADERS.values()):
            en, hu = " | ".join(HEADERS["en"]), " | ".join(HEADERS["hu"])
            found = " | ".join(_text(v) for v in (header or ())[:3])
            result.errors.append((1, Problem(
                "import.bad_header",
                f"the header row must be: {en} or {hu} (found: {found or 'empty'})",
                {"expected": en, "expected_hu": hu, "found": found},
            )))
            return result

        seen: dict[str, int] = {}
        for idx, values in enumerate(rows, start=2):
            values = tuple(values or ()) + (None, None, None)
            name, username, classes_raw = (_text(v) for v in values[:3])
            if not (name or username or classes_raw):
                continue  # blank line
            if len(result.rows) + len(result.errors) >= MAX_ROWS:
                result.errors.append(
                    (idx, Problem("import.too_many_rows", f"at most {MAX_ROWS} rows can be imported at once", {"max": MAX_ROWS}))
                )
                break
            if not username:
                result.errors.append((idx, Problem("import.missing_username", "the username is missing")))
                continue
            if len(username) > 128 or not USERNAME_RE.match(username):
                result.errors.append((idx, Problem(
                    "import.invalid_username",
                    f"invalid username: {username!r} (only letters, digits and . _ @ -, without accents)",
                    {"username": username},
                )))
                continue
            if username in seen:
                result.errors.append((idx, Problem(
                    "import.duplicate_username",
                    f"the username {username!r} already appears in row {seen[username]}",
                    {"username": username, "row": seen[username]},
                )))
                continue
            seen[username] = idx
            if len(name) > MAX_TEXT:
                result.errors.append(
                    (idx, Problem("import.name_too_long", f"the name can be at most {MAX_TEXT} characters", {"max": MAX_TEXT}))
                )
                continue
            classes = list(dict.fromkeys(c.strip() for c in classes_raw.split(",") if c.strip()))
            if any(len(c) > MAX_TEXT for c in classes):
                result.errors.append((idx, Problem(
                    "import.class_too_long", f"a class name can be at most {MAX_TEXT} characters", {"max": MAX_TEXT}
                )))
                continue
            result.rows.append(ParsedRow(row=idx, name=name or username, username=username, classes=classes))
    finally:
        wb.close()
    return result
