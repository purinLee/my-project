import pandas as pd
import pytest

from catalog_intel.config import MAX_UPLOAD_BYTES
from catalog_intel.db import connect
from catalog_intel.validation import SAMPLE_CSV, UploadError, import_products, validate_csv_bytes


def csv(*rows, header="sku,title,price,brand,category,image_url,description"):
    return (header + "\n" + "\n".join(rows)).encode()


def test_sample_csv_is_valid():
    rep = validate_csv_bytes(SAMPLE_CSV.encode())
    assert len(rep.valid) == 2 and rep.rejected.empty
    assert any("no image_url" in w for w in rep.warnings)


def test_valid_rows_are_cleaned_and_typed():
    rep = validate_csv_bytes(csv('A-1,Nice Title,"$1,299.50",B,C,http://x.io/a.jpg,desc'))
    assert rep.valid.iloc[0]["price"] == 1299.50 and rep.valid.iloc[0]["currency"] == "USD"


def test_row_level_errors_reported_with_row_numbers():
    rep = validate_csv_bytes(csv("A-1,Good Title,10,,,,", ",No sku,10,,,,", "A-3,Hi,10,,,,", "A-4,Bad price,abc,,,,", "A-5,Neg price,-4,,,,", "A-1,Dup in file,10,,,,", "bad sku!,Title ok,5,,,,"))
    assert len(rep.valid) == 1
    errs = dict(zip(rep.rejected["row"], rep.rejected["errors"]))
    assert "sku is required" in errs[3]
    assert "title" in errs[4]
    assert "not a number" in errs[5]
    assert "price must be" in errs[6]
    assert "duplicate sku" in errs[7]
    assert "sku must be" in errs[8]


def test_missing_required_column_raises():
    with pytest.raises(UploadError, match="Missing required"):
        validate_csv_bytes(b"sku,title\nA,B\n")


@pytest.mark.parametrize("data", [b"", b"sku,title,price\n", b"\n\n"])
def test_empty_or_headerless_files_raise(data):
    with pytest.raises(UploadError):
        validate_csv_bytes(data)


def test_oversized_file_rejected():
    with pytest.raises(UploadError, match="too large"):
        validate_csv_bytes(b"a" * (MAX_UPLOAD_BYTES + 1))


def test_column_aliases_and_bom_and_latin1():
    rep = validate_csv_bytes("\ufeffSKU,Product Name,Price\nA-1,Café Table,10\n".encode("utf-8"))
    assert rep.valid.iloc[0]["title"] == "Café Table"
    rep = validate_csv_bytes("sku,title,price\nA-1,Caf\xe9 Table,10\n".encode("latin-1"))
    assert len(rep.valid) == 1


def test_invalid_currency_and_status_rejected():
    rep = validate_csv_bytes(b"sku,title,price,currency,status\nA-1,Good Title,10,XXX,active\nA-2,Good Title,10,USD,weird\n")
    assert len(rep.valid) == 0 and len(rep.rejected) == 2


def test_control_characters_stripped():
    rep = validate_csv_bytes(b"sku,title,price\nA-1,Good\x00 Title,10\n")
    assert rep.valid.iloc[0]["title"] == "Good Title"


def test_unknown_columns_warn():
    rep = validate_csv_bytes(b"sku,title,price,secret\nA-1,Good Title,10,x\n")
    assert any("unknown column" in w.lower() for w in rep.warnings)


def test_import_inserts_then_updates(wconn):
    rep = validate_csv_bytes(csv("NEW-1,Brand New Item,19.99,B,Blenders,http://x.io/a.jpg,A description that is long enough", "NEW-2,Second Item,5,,,,"))
    first = import_products(wconn, "M-001", rep.valid, "f.csv", rep.total_rows)
    assert first == {"inserted": 2, "updated": 0}
    rep2 = validate_csv_bytes(csv("NEW-1,Brand New Item,24.99,B,Blenders,http://x.io/a.jpg,A description that is long enough"))
    assert import_products(wconn, "M-001", rep2.valid, "g.csv", 1) == {"inserted": 0, "updated": 1}
    assert wconn.execute("SELECT price FROM products WHERE merchant_id='M-001' AND sku='NEW-1'").fetchone()[0] == 24.99
    assert wconn.execute("SELECT COUNT(*) FROM upload_log").fetchone()[0] == 2


def test_import_unknown_merchant_rejected(wconn):
    with pytest.raises(UploadError):
        import_products(wconn, "M-999", pd.DataFrame(columns=["sku"]), "f.csv")


def test_import_is_sql_injection_safe(wconn):
    rep = validate_csv_bytes(csv("X-1,\"Robert'); DROP TABLE products;--\",5,,,,"))
    import_products(wconn, "M-001", rep.valid, "f.csv", 1)
    assert wconn.execute("SELECT COUNT(*) FROM products").fetchone()[0] > 100


def test_malformed_csv_raises_upload_error():
    with pytest.raises(UploadError, match="parse"):
        validate_csv_bytes(b"sku,title,price\nA-1,Title,10,extra,fields,here\nB,\"unterminated,5\n")
