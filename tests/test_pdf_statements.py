import os
import unittest

from helpers import TempAppCase
from evbudget import import_engine, pdf_statements

# Shaped like a Synchrony statement; the cycle crosses New Year on purpose.
SYNCHRONY_TEXT = """\
JANE DOE
Account Number ending in  1234
Sam's Club World Elite Mastercard
Account Summary
31 Day Billing Cycle from 12/15/2026 to 01/14/2027
Transaction Detail
Date Reference # Description           Amount
Payments -$100.00
12/20 F146800JV00CHGDDA AUTOMATIC PAYMENT - THANK YOU -$100.00
Purchases and Other Debits $143.42
12/16 5270487J6DQ4L2H1Z KWIK TRIP #281 PRIOR LAKE  MN $40.78
12/30 8521333J601S6RBYA SAM'S CLUB 006311 SHAKOPEE  MN $100.00
, PEPPERONI, CHEESE, SALT
BOSTON BUTT, COTTAGE BREAD
(Continued on next page)
PAGE 3 of 3 Visit us at SamsClubCredit.com/login
Transaction Detail (Continued)
Date Reference # Description           Amount
01/02 0230537JA00ARR0QT HY-VEE F&F SHAKOPEE 55 SHAKOPEE  MN $2.64
Total Fees Charged This Period $0.00
Total Interest Charged This Period $0.00
01/14 INTEREST CHARGE ON PURCHASES $0.00
Interest Charge Calculation
Synchrony Bank may continue to obtain information
"""


def make_pdf(path: str, lines) -> None:
    """Write a one-page PDF containing the given lines of text (no libraries needed)."""
    def esc(t):
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = "BT /F1 9 Tf 12 TL 40 760 Td\n" + "\n".join(f"({esc(l)}) '" for l in lines) + "\nET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    with open(path, "wb") as f:
        f.write(out.encode("latin-1"))


class SynchronyParserTests(unittest.TestCase):
    def test_rows_and_signs(self):
        r = pdf_statements.parse_statement_text(SYNCHRONY_TEXT)
        self.assertEqual(r["bank"], "Synchrony")
        self.assertEqual(r["account_last4"], "1234")
        self.assertEqual([x["amount"] for x in r["rows"]], [100.00, -40.78, -100.00, -2.64])
        self.assertEqual(r["rows"][0]["description"], "AUTOMATIC PAYMENT - THANK YOU")
        self.assertEqual(r["rows"][1]["description"], "KWIK TRIP #281 PRIOR LAKE MN")
        self.assertEqual(r["rows"][1]["ref"], "5270487J6DQ4L2H1Z")

    def test_year_follows_the_billing_cycle(self):
        dates = [x["date"] for x in pdf_statements.parse_statement_text(SYNCHRONY_TEXT)["rows"]]
        self.assertEqual(dates, ["2026-12-20", "2026-12-16", "2026-12-30", "2027-01-02"])

    def test_itemised_lines_are_a_note_not_a_transaction(self):
        rows = pdf_statements.parse_statement_text(SYNCHRONY_TEXT)["rows"]
        self.assertEqual(len(rows), 4)
        self.assertIn("PEPPERONI", rows[2]["detail"])
        self.assertIn("BOSTON BUTT", rows[2]["detail"])
        self.assertEqual(rows[3]["detail"], "")  # page header after "(Continued)" must not attach

    def test_totals_reconcile(self):
        r = pdf_statements.parse_statement_text(SYNCHRONY_TEXT)
        self.assertTrue(r["reconciled"], r["warnings"])

    def test_missing_row_is_flagged(self):
        broken = SYNCHRONY_TEXT.replace("12/16 5270487J6DQ4L2H1Z KWIK TRIP #281 PRIOR LAKE  MN $40.78\n", "")
        r = pdf_statements.parse_statement_text(broken)
        self.assertFalse(r["reconciled"])
        self.assertIn("Purchases and other debits", r["warnings"][0])

    def test_interest_charge_becomes_a_row(self):
        text = SYNCHRONY_TEXT.replace("$0.00\nInterest Charge Calculation", "$12.50\nInterest Charge Calculation") \
            .replace("01/14 INTEREST CHARGE ON PURCHASES $0.00", "01/14 INTEREST CHARGE ON PURCHASES $12.50")
        r = pdf_statements.parse_statement_text(text)
        fee = r["rows"][-1]
        self.assertEqual((fee["description"], fee["amount"], fee["ref"]), ("INTEREST CHARGE ON PURCHASES", -12.50, ""))
        self.assertTrue(r["reconciled"], r["warnings"])  # interest isn't part of the purchases subtotal

    def test_unknown_bank_is_refused_with_supported_list(self):
        with self.assertRaises(pdf_statements.StatementError) as cm:
            pdf_statements.parse_statement_text("Some Other Bank\nTransaction History\n")
        self.assertIn("Synchrony", str(cm.exception))


class PdfImportFlowTests(TempAppCase):
    def put_statement(self, name="Synchrony_test.pdf"):
        path = os.path.join(self.input_dir, name)
        make_pdf(path, SYNCHRONY_TEXT.splitlines())
        return path

    def test_extract_from_real_pdf(self):
        r = pdf_statements.parse_statement(self.put_statement())
        self.assertEqual(len(r["rows"]), 4)
        self.assertTrue(r["reconciled"], r["warnings"])

    def test_scan_and_inspect_list_pdfs(self):
        self.put_statement()
        self.assertEqual([f["filename"] for f in import_engine.list_input_files(self.input_dir)], ["Synchrony_test.pdf"])
        info = import_engine.inspect_file_header(import_engine.resolve_input_file("Synchrony_test.pdf", self.input_dir))
        self.assertTrue(info["valid"] and info["is_mapped"])
        self.assertEqual((info["kind"], info["bank"], info["row_count"]), ("pdf", "Synchrony", 4))

    def test_unreadable_pdf_is_reported_not_raised(self):
        path = os.path.join(self.input_dir, "bad.pdf")
        with open(path, "wb") as f:
            f.write(b"not a pdf")
        info = import_engine.inspect_file_header(path)
        self.assertFalse(info["valid"])
        self.assertTrue(info["error"])

    def test_upload_keeps_pdf_extension(self):
        target = import_engine.save_upload("My Statement.pdf", b"%PDF-1.4", self.input_dir)
        self.assertTrue(target.endswith("My Statement.pdf"))

    def test_preview_commit_and_reimport_dedupes(self):
        self.put_statement()

        def configs():
            path = import_engine.resolve_input_file("Synchrony_test.pdf", self.input_dir)
            return [import_engine.inspect_file_header(path)]

        c = self.conn()
        preview = import_engine.build_preview(c, configs())
        self.assertEqual(preview["summary"]["new"], 4)
        by_name = {r["raw_name"]: r for r in preview["records"]}
        self.assertEqual(by_name["KWIK TRIP #281 PRIOR LAKE MN"]["amount"], -40.78)
        self.assertEqual(by_name["KWIK TRIP #281 PRIOR LAKE MN"]["txn_id"], "TXN-5270487J6DQ4L2H1Z")
        self.assertEqual(by_name["AUTOMATIC PAYMENT - THANK YOU"]["txn_kind"], "cc_payment")
        result = import_engine.commit_import(c, preview["records"], ["Synchrony_test.pdf"])
        self.assertEqual(result["inserted"], 4)

        again = import_engine.build_preview(c, configs())
        self.assertEqual((again["summary"]["new"], again["summary"]["existing"]), (0, 4))
        c.close()

    def test_api_config_needs_no_mapping_for_pdf(self):
        self.put_statement()
        resolved = self.app._resolve_file_configs([{"filename": "Synchrony_test.pdf", "account_type": "Credit / Debit"}])
        self.assertTrue(resolved[0]["is_mapped"])


if __name__ == "__main__":
    unittest.main()
