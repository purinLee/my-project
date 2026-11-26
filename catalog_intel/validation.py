"""CSV / XLSX upload validation and import."""
from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass, field

import pandas as pd

from .config import CURRENCIES, MAX_UPLOAD_BYTES, MAX_UPLOAD_ROWS, fmt, utcnow

REQUIRED_COLUMNS = ["sku", "title", "price"]
OPTIONAL_COLUMNS = ["brand", "category", "currency", "image_url", "description", "status"]
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,63}$")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
VALID_STATUS = {"active", "draft", "archived"}
MAX_PRICE = 10_000_000

SAMPLE_CSV = (
    "sku,title,brand,category,price,currency,image_url,description,status\n"
    "DEMO-0001,Auralis Pro 500 Wireless Headphones - Black,Auralis,Wireless Headphones,129.99,USD,"
    "https://images.example-demo.com/demo/DEMO-0001.jpg,Over-ear wireless headphones with active noise cancelling.,active\n"
    "DEMO-0002,Sonex Lite 210 Earbuds - White,Sonex,Wireless Headphones,59.00,USD,,Compact earbuds with charging case.,draft\n"
)


class UploadError(ValueError):
    """The file as a whole cannot be processed (size, encoding, missing columns...)."""


@dataclass
class ValidationReport:
    valid: pd.DataFrame
    rejected: pd.DataFrame  # columns: row, sku, errors
    warnings: list[str] = field(default_factory=list)
    total_rows: int = 0

    @property
    def ok(self) -> bool:
        return len(self.valid) > 0


def _clean(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return CONTROL_CHARS.sub("", str(value)).strip()


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [re.sub(r"[\s\-]+", "_", str(c).strip().lower().lstrip("\ufeff")) for c in df.columns]
    aliases = {"product_name": "title", "name": "title", "product_title": "title", "image": "image_url", "price_usd": "price"}
    return df.rename(columns={k: v for k, v in aliases.items() if k in df.columns and v not in df.columns})


def read_csv_bytes(data: bytes) -> pd.DataFrame:
    if not data:
        raise UploadError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"File is too large ({len(data) / 1e6:.1f} MB). Maximum is {MAX_UPLOAD_BYTES / 1e6:.0f} MB.")
    data = data.replace(b"\x00", b"")  # NUL bytes make the C parser silently truncate fields
    for enc in ("utf-8-sig", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, skip_blank_lines=True)
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise UploadError(f"Could not parse the CSV: {exc}") from exc
    if df.empty:
        raise UploadError("The CSV has a header but no data rows.")
    if len(df) > MAX_UPLOAD_ROWS:
        raise UploadError(f"Too many rows ({len(df):,}). Maximum is {MAX_UPLOAD_ROWS:,} per upload.")
    return df


MAX_XLSX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # guards against decompression bombs
UPLOAD_EXTENSIONS = ("csv", "xlsx")


def _xlsx_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))  # 12345.0 -> "12345" so numeric SKUs stay clean
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return str(v)


def read_xlsx_bytes(data: bytes) -> pd.DataFrame:
    """Read the first worksheet of an .xlsx file into a string DataFrame.

    Formulas are never evaluated (`data_only=True` returns cached values only), the workbook is opened
    read-only, and size / uncompressed-size / row limits are enforced before and during parsing.
    Extra sheets are ignored and reported via ``df.attrs['notes']``.
    """
    from openpyxl import load_workbook

    if not data:
        raise UploadError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"File is too large ({len(data) / 1e6:.1f} MB). Maximum is {MAX_UPLOAD_BYTES / 1e6:.0f} MB.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if sum(i.file_size for i in zf.infolist()) > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise UploadError("The workbook expands to more than 100 MB and was rejected.")
    except zipfile.BadZipFile as exc:
        raise UploadError("Not a valid .xlsx file (legacy .xls and renamed files are not supported).") from exc
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types for corrupt files
        raise UploadError(f"Could not read the Excel file: {exc}") from exc
    try:
        ws = wb.worksheets[0]
        notes = [f"Only the first sheet ('{ws.title}') was read; {len(wb.worksheets) - 1} other sheet(s) ignored."] if len(wb.worksheets) > 1 else []
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None or not any(c not in (None, "") for c in header):
            raise UploadError("The first sheet is empty (the first row must contain column headers).")
        keep = [i for i, c in enumerate(header) if c not in (None, "")]
        body: list[list[str]] = []
        for row in rows:
            cells = [_xlsx_cell(row[i]) if i < len(row) else "" for i in keep]
            if not any(cells):
                continue
            body.append(cells)
            if len(body) > MAX_UPLOAD_ROWS:
                raise UploadError(f"Too many rows (more than {MAX_UPLOAD_ROWS:,}). Maximum is {MAX_UPLOAD_ROWS:,} per upload.")
    finally:
        wb.close()
    if not body:
        raise UploadError("The sheet has a header but no data rows.")
    df = pd.DataFrame(body, columns=[str(header[i]) for i in keep], dtype=str)
    df.attrs["notes"] = notes
    return df


def read_upload_bytes(data: bytes, filename: str = "") -> pd.DataFrame:
    """Dispatch on extension (falling back to the ZIP magic bytes) to the CSV or XLSX reader."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "xlsx" or (ext not in UPLOAD_EXTENSIONS and data[:2] == b"PK"):
        return read_xlsx_bytes(data)
    if ext == "csv" or ext == "":
        return read_csv_bytes(data)
    raise UploadError(f"Unsupported file type '.{ext}'. Upload a .csv or .xlsx file.")


def validate_upload_bytes(data: bytes, filename: str = "") -> "ValidationReport":
    raw = read_upload_bytes(data, filename)
    report = validate_dataframe(raw)
    report.warnings = list(raw.attrs.get("notes", [])) + report.warnings
    return report


def validate_dataframe(raw: pd.DataFrame) -> ValidationReport:
    df = normalize_columns(raw)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise UploadError(f"Missing required column(s): {', '.join(missing)}. Required: {', '.join(REQUIRED_COLUMNS)}.")
    warnings = []
    unknown = [c for c in df.columns if c not in ALL_COLUMNS]
    if unknown:
        warnings.append(f"Ignored unknown column(s): {', '.join(unknown)}")
    for col in OPTIONAL_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    good, bad, seen = [], [], set()
    for pos, rec in enumerate(df[ALL_COLUMNS].to_dict("records"), start=2):  # row 1 is the header
        errors = []
        rec = {k: _clean(v) for k, v in rec.items()}
        sku = rec["sku"]
        if not sku:
            errors.append("sku is required")
        elif not SKU_RE.match(sku):
            errors.append("sku must be 1-64 chars: letters, digits, . _ - /")
        elif sku in seen:
            errors.append(f"duplicate sku '{sku}' within file")
        if not 3 <= len(rec["title"]) <= 200:
            errors.append("title must be 3-200 characters")
        price = None
        try:
            price = float(re.sub(r"[,$€£₩¥\s]", "", rec["price"]))
            if not (0 < price < MAX_PRICE) or price != price:
                errors.append(f"price must be > 0 and < {MAX_PRICE:,}")
        except ValueError:
            errors.append("price is not a number")
        cur = (rec["currency"] or "USD").upper()
        if cur not in CURRENCIES:
            errors.append(f"currency must be one of {', '.join(CURRENCIES)}")
        status = (rec["status"] or "active").lower()
        if status not in VALID_STATUS:
            errors.append("status must be active, draft or archived")
        for col, limit in (("brand", 80), ("category", 80), ("description", 2000), ("image_url", 500)):
            if len(rec[col]) > limit:
                errors.append(f"{col} exceeds {limit} characters")
        if errors:
            bad.append({"row": pos, "sku": sku, "errors": "; ".join(errors)})
            continue
        seen.add(sku)
        good.append({**rec, "price": round(price, 2), "currency": cur, "status": status})

    valid = pd.DataFrame(good, columns=ALL_COLUMNS)
    rejected = pd.DataFrame(bad, columns=["row", "sku", "errors"])
    if len(valid):
        no_img = int((valid["image_url"] == "").sum())
        if no_img:
            warnings.append(f"{no_img} valid row(s) have no image_url (will be flagged by the quality audit).")
    return ValidationReport(valid, rejected, warnings, total_rows=len(df))


def validate_csv_bytes(data: bytes) -> ValidationReport:
    return validate_dataframe(read_csv_bytes(data))


def import_products(conn, merchant_id: str, valid: pd.DataFrame, filename: str, rows_total: int | None = None) -> dict:
    """Upsert validated rows for a merchant. Returns counts. Parameterised SQL only."""
    if conn.execute("SELECT 1 FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone() is None:
        raise UploadError(f"Unknown merchant '{merchant_id}'.")
    now = fmt(utcnow())
    inserted = updated = 0
    for r in valid.to_dict("records"):
        existing = conn.execute("SELECT 1 FROM products WHERE merchant_id=? AND sku=?", (merchant_id, r["sku"])).fetchone()
        conn.execute(
            """INSERT INTO products (merchant_id, sku, title, brand, category, price, currency, image_url, description, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(merchant_id, sku) DO UPDATE SET title=excluded.title, brand=excluded.brand, category=excluded.category,
                 price=excluded.price, currency=excluded.currency, image_url=excluded.image_url, description=excluded.description,
                 status=excluded.status, updated_at=excluded.updated_at""",
            (merchant_id, r["sku"], r["title"], r["brand"] or None, r["category"] or None, r["price"], r["currency"],
             r["image_url"] or None, r["description"] or None, r["status"], now, now),
        )
        updated += bool(existing)
        inserted += not existing
    total = rows_total if rows_total is not None else len(valid)
    conn.execute(
        "INSERT INTO upload_log (merchant_id, filename, rows_total, rows_valid, rows_rejected, rows_inserted, rows_updated, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (merchant_id, filename[:200], total, len(valid), total - len(valid), inserted, updated, now),
    )
    return {"inserted": inserted, "updated": updated}
