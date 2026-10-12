"""Generate realistic, deliberately messy sample files into sample_data/.

    .venv/bin/python scripts/make_sample_data.py

Every scenario the tool must handle appears at least once (see SCENARIOS below).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook

OUT = Path(__file__).resolve().parent.parent / "sample_data"

# payout_id, emp_id, name, account, ifsc, amount, date, claims, status
PAYOUTS = [
    ("PO-2026-0101", "E1001", "Rahul Kumar", "XXXXXX4521", "HDFC0001234", 4250.00, "03-02-2026", "CL-9001, CL-9002", "PAID"),        # ID match
    ("PO-2026-0102", "E1002", "Priya Shah", "XXXXXX7788", "ICIC0004567", 1200.00, "03-02-2026", "CL-9003", "PAID"),                  # account + name
    ("PO-2026-0103", "E1003", "Amit Verma", "XXXXXX3301", "SBIN0007788", 900.00, datetime(2026, 2, 4), "CL-9004", "PAID"),          # amount differs
    ("PO-2026-0104", "E1004", "Neha Iyer", "XXXXXX1290", "UTIB0001122", 2000.00, "04-02-2026", "CL-9005", "FAILED"),                 # failed, no debit
    ("PO-2026-0105", "E1005", "Karan Mehta", "XXXXXX5562", "HDFC0009876", 3100.00, "05-02-2026", "CL-9006;CL-9007", "PAID"),        # paid then returned
    ("PO-2026-0106", "E1006", "Sneha Rao", "XXXXXX8890", "KKBK0002233", 750.00, "05-02-2026", "CL-9008", "PAID"),                   # paid twice
    ("PO-2026-0107", "E1007", "Vikram Singh", "XXXXXX4410", "SBIN0001111", 1500.00, "06-02-2026", "CL-9009", "PAID"),               # amount + date only
    ("PO-2026-0108", "E1008", "Anjali Desai", "XXXXXX6672", "ICIC0003344", 2200.00, "06-02-2026", "CL-9010", "PAID"),               # missing in bank
    ("PO-2026-0109", "E1009", "Rohan Gupta", "XXXXXX1123", "HDFC0005566", 1800.00, "07-02-2026", "CL-9011", "Processing"),          # pending
    ("PO-2026-0110", "E1010", "Meera Nair", "XXXXXX9081", "UTIB0007788", 5000.00, "07-02-2026", "CL-9012", "PAID"),                 # batch (2 IDs, 1 debit)
    ("PO-2026-0111", "E1011", "Arjun Das", "XXXXXX3345", "SBIN0009900", 2500.00, "07-02-2026", "CL-9013", "PAID"),                  # batch
    ("PO-2026-0112", "E1012", "Divya Pillai", "XXXXXX7001", "KKBK0001010", 600.00, "09-02-2026", "CL-9014", "PAID"),                # ambiguous
    ("PO-2026-0113", "E1013", "Suresh Babu", "XXXXXX7002", "KKBK0001010", 600.00, "09-02-2026", "CL-9015", "PAID"),                 # ambiguous
    ("PO-2026-0114", "E1014", "Farhan Ali", "XXXXXX2210", "HDFC0004321", 1350.00, "10-02-2026", "CL-9016", "Success"),              # ID w/o dashes in bank
    ("PO-2026-0115", "E1015", "Lakshmi Iyer", "XXXXXX5098", "ICIC0001212", "1,999.50", "11-02-2026", "CL-9017", "PAID"),            # text amount
    ("PO-2026-0116", "E1016", "Nikhil Joshi", "XXXXXX6604", "SBIN0004545", 4200.00, "12-02-2026", "CL-9018", "PAID"),               # in transit
    ("PO-2026-0117", "E1017", "Pooja Kulkarni", "XXXXXX8812", "UTIB0006767", "12O0", "12-02-2026", "CL-9019", "PAID"),              # unreadable amount
    ("PO-2026-0114", "E1014", "Farhan Ali", "XXXXXX2210", "HDFC0004321", 1350.00, "10-02-2026", "CL-9016", "PAID"),                 # duplicate ID row
]

# date, narration, ref, debit, credit   (None = blank). Strings are written as text cells on purpose.
BANK = [
    ("03-02-2026", "NEFT/CHRONON/PO-2026-0101/RAHUL KUMAR", "N0301", "4,250.00", None),
    ("03/02/2026", "IMPS/P2A/PRIYA S/XXXXXX7788/REIMB", "I0302", "₹1,200", None),
    ("04-02-2026", "NEFT/AMIT VERMA/CHRONON REIMB", "N0401", 950, None),
    ("04-02-2026", "CONSOLIDATED CHARGES FOR A/C", "", "59.00", None),
    ("2026-02-05", "NEFT/CHRONON/PO-2026-0105/KARAN MEHTA", "N0501", "3,100.00 Dr", None),
    ("05-02-2026", "IMPS/CHRONON PO-2026-0106 SNEHA RAO", "I0501", 750, None),
    "REPEAT_HEADER",
    ("06-02-2026", "IMPS/CHRONON PO-2026-0106 SNEHA RAO", "I0601", 750, None),
    ("06-02-2026", "NEFT SALARY FEB ADVANCE - OPS TEAM", "N0602", "45,000.00", None),
    ("06-02-2026", "IMPS TRF 98231", "I0603", 1500, None),
    ("07-02-2026", "NEFT/CHRONON BULK/PO-2026-0110,PO-2026-0111", "N0701", "7,500.00", None),
    ("CONT", "/FEB WEEK1 REIMBURSEMENTS"),
    (datetime(2026, 2, 8), "NEFT RETURN PO-2026-0105 INVALID A/C", "N0501R", None, "3,100.00"),
    "BLANK",
    ("09-02-2026", "IMPS/P2A/REIMB", "I0901", 600, None),
    ("09-02-2026", "IMPS/P2A/REIMB", "I0902", 600, None),
    ("10-02-2026", "NEFT-CHRONON-PO20260114-FARHAN ALI", "N1001", 1350, None),
    ("11-02-2026", "NEFT/CHRONON/PO-2026-0115/LAKSHMI IYER", "N1101", 1999.5, None),
    ("11-02-2026", "IMPS/RAMESH K/XX0099", "I1102", "3,333.00", None),
    ("12-02-2026", "NEFT TOTAL SOLUTIONS PVT LTD INV 4471", "N1201", "18,500.00", None),
    ("13-02-2026", "INT.PD:01-01-2026 TO 31-01-2026", "", None, "412.35"),
]
OPENING = Decimal("250000.00")
HEADER = ["Txn Date", "Value Date", "Narration", "Cheque/Ref No", "Debit", "Credit", "Balance"]


def _dec(v) -> Decimal:
    if v is None:
        return Decimal("0")
    return Decimal(str(v).replace("₹", "").replace(",", "").replace("Dr", "").strip())


def make_payout_register(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Payouts"
    ws.append(["Payout ID", "Employee ID", "Employee Name", "Bank Account (masked)", "IFSC", "Amount",
               "Payout Date", "Claim IDs", "Status"])
    for row in PAYOUTS:
        ws.append(list(row))
    wb.save(path)


def make_bank_statement(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Statement"
    ws.append(["ABC BANK LTD - ACCOUNT STATEMENT"])
    ws.append(["Account No: 50100XXXXXX123", None, "Customer: CHRONON TECHNOLOGIES PVT LTD"])
    ws.append(["Statement Period: 01-02-2026 to 13-02-2026"])
    ws.append([])
    ws.append(HEADER)
    ws.append([None, None, "Opening Balance", None, None, None, f"{OPENING:,.2f}"])
    balance = OPENING
    for item in BANK:
        if item == "REPEAT_HEADER":
            ws.append(HEADER)
            continue
        if item == "BLANK":
            ws.append([])
            continue
        if item[0] == "CONT":
            ws.append([None, None, item[1]])
            continue
        d, narration, ref, debit, credit = item
        balance = balance - _dec(debit) + _dec(credit)
        ws.append([d, d, narration, ref, debit, credit, f"{balance:,.2f}"])
    ws.append([None, None, "Closing Balance", None, None, None, f"{balance:,.2f}"])
    ws.append([])
    ws.append(["** This is a computer generated statement and does not require a signature **"])
    wb.save(path)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    make_payout_register(OUT / "payout_register.xlsx")
    make_bank_statement(OUT / "bank_statement.xlsx")
    print(f"Wrote {OUT / 'payout_register.xlsx'} and {OUT / 'bank_statement.xlsx'}")
