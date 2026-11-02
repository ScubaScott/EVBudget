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


class SynchronyStatementTests(unittest.TestCase):
    """Synchrony has no parser of its own: its statements are read by the generic one."""

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
        # the ", PEPPERONI, CHEESE" and "BOSTON BUTT" lines under the Sam's Club purchase are not rows of their own

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

    def test_text_with_no_transactions_is_refused_and_says_what_is_expected(self):
        with self.assertRaises(pdf_statements.StatementError) as cm:
            pdf_statements.parse_statement_text("Some Other Bank\nTransaction History\n")
        self.assertIn("date and an amount", str(cm.exception))

    def test_a_synchrony_statement_credit_without_a_reference_number_is_kept(self):
        text = SYNCHRONY_TEXT.replace("Purchases and Other Debits $143.42",
                                      "Other Credits -$25.00\nPurchases and Other Debits $143.42\n12/18 STATEMENT CREDIT -$25.00")
        r = pdf_statements.parse_statement_text(text)
        credit = [x for x in r["rows"] if x["description"] == "STATEMENT CREDIT"]
        self.assertEqual([(c["amount"], c["ref"]) for c in credit], [(25.0, "")])
        self.assertTrue(r["reconciled"], r["warnings"])


# Shaped like a Chase card statement (the text comes out of the PDF in a scrambled order, so the summary
# is first, activity is split across pages, and "Order Number" lines sit under Amazon purchases).
CHASE_TEXT = """\
ACCOUNT SUMMARY
Payment Due Date: 11/26/26
New Balance: $152.40
Account Number:  XXXX XXXX XXXX 3855
Previous Balance $200.00
Payment, Credits -$250.00
Purchases +$202.40
Cash Advances $0.00
Fees Charged $0.00
Interest Charged $0.00
Opening/Closing Date 10/02/26 - 11/01/26
www.chase.com/amazon
ACCOUNT ACTIVITY
Date of
Transaction Merchant  Name or Transaction Description $ Amount
10/09     & STATEMENT CREDIT -25.00
10/26     AUTOMATIC PAYMENT - THANK YOU -225.00
10/16     & TRAVELERS PER INS 800-842-5075 CT .10
10/05     & Amazon.com*597XZ8ZV0 Amzn.com/bill WA 20.08
Order Number       111-5721004-6944260
10/04     & HY-VEE SHAKOPEE 1562 SHAKOPEE MN 175.39
Page 4 of 4 Statement Date: 11/01/26
09/30     TACO BELL 7005058 952-942-5766 MN 6.83
Year-to-date totals do not reflect any fee or interest refunds
"""


class GenericParserTests(unittest.TestCase):
    """A bank with no code of its own: read by what every statement has, and checked against its own totals."""

    def setUp(self):
        self.text = CHASE_TEXT

    def test_a_bank_with_no_special_code_is_read_and_reconciles(self):
        r = pdf_statements.parse_statement_text(self.text)
        self.assertEqual((r["bank"], r["account_last4"]), ("Chase", "3855"))
        self.assertEqual((r["period_start"], r["period_end"]), ("2026-10-02", "2026-11-01"))
        self.assertTrue(r["reconciled"], r["warnings"])

    def test_signs_follow_the_statement_totals(self):
        rows = {x["description"]: x["amount"] for x in pdf_statements.parse_statement_text(self.text)["rows"]}
        self.assertEqual(rows["STATEMENT CREDIT"], 25.0)                      # money in
        self.assertEqual(rows["AUTOMATIC PAYMENT - THANK YOU"], 225.0)
        self.assertEqual(rows["HY-VEE SHAKOPEE 1562 SHAKOPEE MN"], -175.39)
        self.assertEqual(rows["TRAVELERS PER INS 800-842-5075 CT"], -0.10)    # ".10" with no leading digit, and the & marker

    def test_thousands_separators(self):
        text = self.text.replace("175.39", "1,175.39").replace("$202.40", "$1,202.40").replace("New Balance: $152.40", "New Balance: $1,152.40")
        r = pdf_statements.parse_statement_text(text)
        self.assertTrue(r["reconciled"], r["warnings"])

    def test_order_number_is_a_note_not_a_transaction(self):
        rows = pdf_statements.parse_statement_text(self.text)["rows"]
        self.assertEqual(len(rows), 6)
        amazon = [r for r in rows if r["description"].startswith("Amazon.com")][0]
        self.assertIn("111-5721004-6944260", amazon["detail"])

    def test_dates_get_their_year_from_the_statement_period(self):
        dates = {x["description"]: x["date"] for x in pdf_statements.parse_statement_text(self.text)["rows"]}
        self.assertEqual(dates["STATEMENT CREDIT"], "2026-10-09")

    def test_a_missed_row_is_flagged_not_silent(self):
        r = pdf_statements.parse_statement_text(self.text.replace("09/30     TACO BELL 7005058 952-942-5766 MN 6.83\n", ""))
        self.assertFalse(r["reconciled"])
        self.assertTrue(any("Purchases" in w for w in r["warnings"]), r["warnings"])

    def test_opposite_sign_convention_is_detected(self):
        # A bank that prints payments as positive and purchases as negative
        flipped = []
        for line in self.text.splitlines():
            m = pdf_statements._GEN_ROW.match(pdf_statements.squash(line))
            if m:
                amount = m.group(4)
                line = line.replace(amount, amount[1:] if amount.startswith("-") else "-" + amount)
            flipped.append(line)
        text = "\n".join(flipped).replace("Payment, Credits -$250.00", "Payment, Credits $250.00").replace("Purchases +$202.40", "Purchases -$202.40")
        r = pdf_statements.parse_statement_text(text)
        self.assertTrue(r["reconciled"], r["warnings"])
        rows = {x["description"]: x["amount"] for x in r["rows"]}
        self.assertEqual((rows["STATEMENT CREDIT"], rows["HY-VEE SHAKOPEE 1562 SHAKOPEE MN"]), (25.0, -175.39))

    def test_balances_can_vouch_for_the_rows_when_labels_are_unfamiliar(self):
        text = self.text.replace("Payment, Credits -$250.00", "Money you sent us -$250.00").replace("Purchases +$202.40", "New stuff +$202.40")
        r = pdf_statements.parse_statement_text(text)
        self.assertTrue(r["reconciled"], r["warnings"])

    def test_no_totals_at_all_imports_with_a_warning(self):
        keep = [l for l in self.text.splitlines() if not l.startswith(("Payment, Credits", "Purchases", "New Balance", "Previous Balance"))]
        r = pdf_statements.parse_statement_text("\n".join(keep))
        self.assertFalse(r["reconciled"])
        self.assertEqual(len(r["rows"]), 6)
        self.assertEqual({x["description"]: x["amount"] for x in r["rows"]}["AUTOMATIC PAYMENT - THANK YOU"], 225.0)  # judged by the word PAYMENT

    def test_dates_but_no_statement_period_is_refused_with_a_reason(self):
        keep = [l for l in self.text.splitlines() if "Opening/Closing" not in l and "Statement Date" not in l]
        with self.assertRaises(pdf_statements.StatementError) as cm:
            pdf_statements.parse_statement_text("\n".join(keep))
        self.assertIn("statement's dates", str(cm.exception))

    def test_a_year_on_each_line_and_a_posting_date_are_understood(self):
        text = ("Statement Period: 01/05/2027 - 02/04/2027\nAccount ending in 4321\nPurchases $9.99\n"
                "01/20/2027 01/21/2027 NETFLIX.COM 9.99\n")
        r = pdf_statements.parse_statement_text(text)
        self.assertEqual((r["rows"][0]["date"], r["rows"][0]["amount"], r["account_last4"]), ("2027-01-20", -9.99, "4321"))

    def test_zero_dollar_lines_are_not_transactions(self):
        r = pdf_statements.parse_statement_text(self.text + "11/01 INTEREST CHARGE ON PURCHASES $0.00\n")
        self.assertEqual(len(r["rows"]), 6)

    def test_a_reference_number_before_the_description_becomes_the_ref(self):
        text = self.text + "10/20 F146800JV00CHGDDA CORNER STORE  MN 2.50\n"
        text = text.replace("Purchases +$202.40", "Purchases +$204.90").replace("New Balance: $152.40", "New Balance: $154.90")
        row = [x for x in pdf_statements.parse_statement_text(text)["rows"] if x["description"] == "CORNER STORE MN"][0]
        self.assertEqual(row["ref"], "F146800JV00CHGDDA")


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
