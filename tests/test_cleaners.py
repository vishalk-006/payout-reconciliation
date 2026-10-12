from datetime import date, datetime
from decimal import Decimal

import pytest

from app.cleaners import CleanError, DateFormatError, infer_date_format, looks_like_date, parse_amount, parse_date


@pytest.mark.parametrize("raw, value, marker", [
    ("1,200.00", "1200.00", None),
    ("1,00,000.50", "100000.50", None),  # Indian digit grouping
    ("₹1,200", "1200.00", None),
    ("INR 4,250.00", "4250.00", None),
    ("Rs. 99", "99.00", None),
    ("(500.00)", "-500.00", None),
    ("-1200", "-1200.00", None),
    ("1200-", "-1200.00", None),
    ("950 Dr", "950.00", "DR"),
    ("3,100.00 CR.", "3100.00", "CR"),
    (" 75 ", "75.00", None),
    (1999.5, "1999.50", None),
    (1199.9999999999998, "1200.00", None),  # Excel float noise
    (4250, "4250.00", None),
])
def test_amount_accepts_known_shapes(raw, value, marker):
    a = parse_amount(raw)
    assert a.value == Decimal(value) and a.marker == marker


@pytest.mark.parametrize("raw", [None, "", "  ", "-", "--", "NIL"])
def test_amount_empty_is_none(raw):
    assert parse_amount(raw) is None


@pytest.mark.parametrize("raw", [
    "12O0",        # letter O, must not become 120
    "1.200,50",    # European format, must not become 120050
    "1,2,3",
    "(500",        # unbalanced
    "-(500)",      # conflicting signs
    "12.345",      # sub-paisa
    "abc",
    True,
    datetime(2026, 2, 3),
])
def test_amount_rejects_anything_else(raw):
    with pytest.raises(CleanError):
        parse_amount(raw)


def test_date_inference_uses_profile_when_ambiguous():
    values = ["03-02-2026", "04-02-2026", "05-02-2026"]  # every day <= 12: dd/mm and mm/dd both fit
    assert infer_date_format(values, ["%d-%m-%Y"]) == "%d-%m-%Y"


def test_date_inference_refuses_to_guess_without_profile():
    # Both readings are valid and both keep the rows in order -> must not guess.
    with pytest.raises(DateFormatError):
        infer_date_format(["03-02-2026", "03-02-2026", "04-02-2026"])


def test_date_inference_uses_row_order_to_break_tie():
    # dd-mm reading: 01-Feb, 02-Feb, 01-Mar is in order; mm-dd reading: 2-Jan, 2-Feb, 3-Jan is not.
    assert infer_date_format(["01-02-2026", "02-02-2026", "01-03-2026"]) == "%d-%m-%Y"
    # ...and the mirror image picks month-first.
    assert infer_date_format(["02-01-2026", "02-02-2026", "03-01-2026"]) == "%m-%d-%Y"


def test_date_inference_any_value_over_12_decides():
    assert infer_date_format(["03-02-2026", "25-02-2026"]) == "%d-%m-%Y"
    assert infer_date_format(["02-25-2026", "02-03-2026"]) == "%m-%d-%Y"


def test_parse_date_variants():
    assert parse_date("03/02/2026", "%d-%m-%Y") == (date(2026, 2, 3), None)
    assert parse_date("03.02.2026 10:15:00", "%d-%m-%Y") == (date(2026, 2, 3), None)
    assert parse_date(datetime(2026, 2, 3, 9, 0), "%d-%m-%Y") == (date(2026, 2, 3), None)
    assert parse_date(46056, None) == (date(2026, 2, 3), None)  # Excel serial
    d, warn = parse_date("2026-02-05", "%d-%m-%Y")  # unambiguous ISO accepted with a warning
    assert d == date(2026, 2, 5) and warn


@pytest.mark.parametrize("raw", ["31/02/2026", "hello", "03-02-1890"])
def test_parse_date_rejects(raw):
    with pytest.raises(CleanError):
        parse_date(raw, "%d-%m-%Y")


def test_looks_like_date_separates_footers():
    assert looks_like_date("03-02-2026") and looks_like_date("31/02/2026") and looks_like_date(datetime(2026, 1, 1))
    assert not looks_like_date("Page 1 of 3")
    assert not looks_like_date("** This is a computer generated statement **")
