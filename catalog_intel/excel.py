"""XLSX export helpers (openpyxl write-only mode: low memory, formulas are never created)."""
from __future__ import annotations

import io
import re

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils.cell import get_column_letter

MAX_EXCEL_ROWS = 1_048_575  # one header row
MAX_CELL_CHARS = 32_000
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")
HEADER_FILL = PatternFill("solid", fgColor="312E81")


def safe_sheet_name(name: str, used: set[str] | None = None) -> str:
    base = _BAD_SHEET_CHARS.sub("-", str(name)).strip("' ")[:31] or "Sheet"
    out, n = base, 2
    while used is not None and out.lower() in {u.lower() for u in used}:
        suffix = f" ({n})"
        out, n = base[: 31 - len(suffix)] + suffix, n + 1
    if used is not None:
        used.add(out)
    return out


def _value(v):
    if v is None or v is pd.NaT:
        return None
    if isinstance(v, (float, np.floating)) and np.isnan(v):
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if isinstance(v, str):
        return _ILLEGAL.sub("", v)[:MAX_CELL_CHARS]
    return v


def to_xlsx_bytes(sheets: pd.DataFrame | dict[str, pd.DataFrame]) -> bytes:
    """Serialise one DataFrame (or several, keyed by sheet name) to .xlsx bytes.

    Text cells are always stored as strings, so a value such as ``=HYPERLINK(...)`` stays inert text in Excel
    instead of becoming a formula (spreadsheet formula injection).
    """
    if isinstance(sheets, pd.DataFrame):
        sheets = {"Data": sheets}
    wb = Workbook(write_only=True)
    used: set[str] = set()
    for name, df in sheets.items():
        ws = wb.create_sheet(safe_sheet_name(name, used))
        sample = df.head(200)
        for i, col in enumerate(df.columns, start=1):
            lengths = [len(str(col))] + [len(str(v)) for v in sample[col].tolist() if v is not None]
            ws.column_dimensions[get_column_letter(i)].width = min(max(max(lengths) + 2, 8), 60)
        ws.freeze_panes = "A2"
        head = []
        for col in df.columns:
            c = WriteOnlyCell(ws, value=str(col))
            c.font, c.fill, c.alignment = Font(bold=True, color="FFFFFF"), HEADER_FILL, Alignment(vertical="center")
            head.append(c)
        ws.append(head)
        for row in df.head(MAX_EXCEL_ROWS).itertuples(index=False, name=None):
            cells = []
            for v in row:
                v = _value(v)
                if isinstance(v, str) and v.startswith("="):
                    c = WriteOnlyCell(ws, value=v)
                    c.data_type = "s"  # force text; openpyxl would otherwise store "=..." as a formula
                    cells.append(c)
                else:
                    cells.append(v)
            ws.append(cells)
    if not sheets:
        wb.create_sheet("Data")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def sample_upload_xlsx(csv_text: str) -> bytes:
    """Build the downloadable XLSX template from the CSV template text."""
    df = pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False)
    return to_xlsx_bytes({"Catalog": df})
