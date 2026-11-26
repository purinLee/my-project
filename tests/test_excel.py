import io
import zipfile

import pandas as pd
import pytest
from openpyxl import Workbook, load_workbook

from catalog_intel import validation
from catalog_intel.excel import safe_sheet_name, sample_upload_xlsx, to_xlsx_bytes
from catalog_intel.quality import run_audit
from catalog_intel.reports import executive_report_xlsx, to_excel_bytes
from catalog_intel.validation import SAMPLE_CSV, UploadError, import_products, read_xlsx_bytes, validate_upload_bytes
from tests.conftest import AS_OF


def make_xlsx(rows, sheets=None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Catalog"
    for r in rows:
        ws.append(r)
    for name in sheets or []:
        wb.create_sheet(name).append(["x"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


HEADER = ["sku", "title", "price", "brand", "category", "image_url", "description"]
OK_ROW = ["A-1", "Good title", 5, "", "", "", ""]


def test_roundtrip_export_then_upload():
    df = pd.DataFrame({"sku": ["A-1", "A-2"], "title": ["First product", "Second product"], "price": [10.5, 20.0], "brand": ["B", None]})
    out = read_xlsx_bytes(to_xlsx_bytes(df))
    assert list(out.columns) == ["sku", "title", "price", "brand"]
    assert out["sku"].tolist() == ["A-1", "A-2"]
    assert out["price"].tolist() == ["10.5", "20"]
    assert out["brand"].tolist() == ["B", ""]


def test_xlsx_template_validates():
    rep = validate_upload_bytes(sample_upload_xlsx(SAMPLE_CSV), "template.xlsx")
    assert len(rep.valid) == 2 and rep.rejected.empty


def test_numeric_and_native_cells_are_normalised():
    data = make_xlsx([HEADER, [12345, "Numeric sku product", 129.99, "B", "Cat", None, "d"], ["A-2", "Integer price", 5, None, None, None, None]])
    rep = validate_upload_bytes(data, "n.xlsx")
    assert rep.valid["sku"].tolist() == ["12345", "A-2"]
    assert rep.valid["price"].tolist() == [129.99, 5.0]


def test_formulas_are_never_evaluated_on_upload():
    data = make_xlsx([HEADER, ["=1+1", "Formula sku row", 5, "", "", "", ""], ["A-1", '=HYPERLINK("http://evil")', 5, "", "", "", ""]])
    rep = validate_upload_bytes(data, "f.xlsx")
    assert rep.valid.empty and len(rep.rejected) == 2  # formulas have no cached value -> empty -> rejected


def test_export_text_that_looks_like_formula_stays_text():
    df = pd.DataFrame({"t": ["=1+1", "+cmd", "@x", "ok"], "n": [-5, 1, 2, 3]})
    ws = load_workbook(io.BytesIO(to_excel_bytes(df, "S"))).active
    assert [ws.cell(row=i, column=1).data_type for i in range(2, 6)] == ["s", "s", "s", "s"]
    assert ws.cell(row=2, column=1).value == "=1+1"
    assert ws.cell(row=2, column=2).value == -5


def test_export_structure_and_illegal_characters():
    df = pd.DataFrame({"a": ["bad\x00char", None], "d": pd.to_datetime(["2026-01-01", None])})
    ws = load_workbook(io.BytesIO(to_xlsx_bytes({"Sheet [1]: x/y": df}))).active
    assert ws.title == "Sheet -1-- x-y"
    assert ws.freeze_panes == "A2"
    assert ws["A2"].value == "badchar" and ws["A3"].value is None
    assert ws["A1"].font.bold


def test_empty_dataframe_exports_header_only():
    ws = load_workbook(io.BytesIO(to_xlsx_bytes(pd.DataFrame(columns=["a", "b"])))).active
    assert [c.value for c in ws[1]] == ["a", "b"] and ws.max_row == 1


def test_sheet_name_rules():
    used: set[str] = set()
    assert safe_sheet_name("x" * 50) == "x" * 31
    assert safe_sheet_name("A", used) == "A"
    assert safe_sheet_name("a", used) == "a (2)"


@pytest.mark.parametrize("data,msg", [(b"", "empty"), (b"not a zip at all", "Not a valid"), (make_xlsx([]), "empty"), (make_xlsx([HEADER]), "no data rows")])
def test_bad_workbooks_raise_upload_error(data, msg):
    with pytest.raises(UploadError, match=msg):
        validate_upload_bytes(data, "x.xlsx")


def test_legacy_xls_and_unknown_extension_rejected():
    with pytest.raises(UploadError, match="Unsupported"):
        validate_upload_bytes(b"whatever", "data.xls")
    with pytest.raises(UploadError, match="Unsupported"):
        validate_upload_bytes(b"whatever", "data.json")


def test_missing_required_column_in_xlsx():
    with pytest.raises(UploadError, match="Missing required"):
        validate_upload_bytes(make_xlsx([["sku", "title"], ["A", "B"]]), "x.xlsx")


def test_row_limit(monkeypatch):
    monkeypatch.setattr(validation, "MAX_UPLOAD_ROWS", 3)
    rows = [HEADER] + [[f"A-{i}", "Some title", 1, "", "", "", ""] for i in range(5)]
    with pytest.raises(UploadError, match="Too many rows"):
        read_xlsx_bytes(make_xlsx(rows))


def test_file_size_limit(monkeypatch):
    monkeypatch.setattr(validation, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(UploadError, match="too large"):
        read_xlsx_bytes(make_xlsx([HEADER, OK_ROW]))


def test_decompression_bomb_limit(monkeypatch):
    monkeypatch.setattr(validation, "MAX_XLSX_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(UploadError, match="100 MB"):
        read_xlsx_bytes(make_xlsx([HEADER, OK_ROW]))


def test_multiple_sheets_warn_and_blank_rows_skipped():
    data = make_xlsx([HEADER, OK_ROW, [None] * 7, ["A-2", "Another title", 6, "", "", "", ""]], sheets=["Notes"])
    rep = validate_upload_bytes(data, "m.xlsx")
    assert len(rep.valid) == 2
    assert any("other sheet" in w for w in rep.warnings)


def test_renamed_zip_without_extension_detected_by_magic_bytes():
    assert len(validate_upload_bytes(make_xlsx([HEADER, OK_ROW]), "upload").valid) == 1


def test_csv_still_works_through_dispatcher():
    assert len(validate_upload_bytes(SAMPLE_CSV.encode(), "t.csv").valid) == 2


def test_xlsx_import_into_database(wconn):
    row = ["XL-1", "Excel imported product", 49.5, "Brand", "Blenders", "https://x.io/a.jpg", "A long enough description"]
    rep = validate_upload_bytes(make_xlsx([HEADER, row]), "u.xlsx")
    assert import_products(wconn, "M-001", rep.valid, "u.xlsx", rep.total_rows) == {"inserted": 1, "updated": 0}
    assert wconn.execute("SELECT price FROM products WHERE sku='XL-1'").fetchone()[0] == 49.5


def test_executive_report_xlsx_sheets_and_values(conn):
    data = executive_report_xlsx(conn, run_audit(conn), AS_OF)
    assert zipfile.is_zipfile(io.BytesIO(data))
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["KPIs", "SLA by priority", "Processing time", "Rework by type", "Quality findings"]
    kpis = {r[0].value: r[1].value for r in wb["KPIs"].iter_rows(min_row=2)}
    assert kpis["Total requests"] == 700
    assert kpis["Reference time (UTC)"] == "2026-11-26 12:00:00"
    assert wb["Quality findings"].max_row > 100
