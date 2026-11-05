# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Shared formatting reads right in each locale, and accessibility helpers give honest descriptions."""

import datetime as dt

from lyst import i18n

M = "−"


def test_numbers_currency_percent_per_locale():
    assert i18n.number(1234567.891, 2, "en-US") == "1,234,567.89"
    assert i18n.number(1234567.891, 2, "de-DE") == "1.234.567,89"
    assert i18n.number(1234567.891, 2, "fr-FR") == "1 234 567,89"
    assert i18n.number(12345678, 0, "hi-IN") == "1,23,45,678"
    assert i18n.number(2.5, 0) == "3" and i18n.number(-0.004, 2) == "0.00"  # half up; no negative zero
    assert i18n.currency(-1234.5, "EUR", tag="de-DE") == M + "1.234,50 €"
    assert i18n.currency(1234.5, "USD", tag="en-US") == "$1,234.50"
    assert i18n.currency(1234, "JPY", tag="ja-JP") == "¥1,234"
    assert i18n.percent(0.125, 1, "en-US") == "12.5%" and i18n.percent(0.125, 1, "fr-FR") == "12,5 %"
    assert i18n.compact(2_340_000, tag="en-US") == "2.3M" and i18n.compact(2_340_000, tag="de-DE") == "2,3 Mio."
    assert i18n.locale("de-AT").tag == "de-DE" and i18n.locale("xx").tag == "en-US"


def test_dates():
    d = dt.date(2026, 3, 9)
    assert i18n.date(d, "long", "en-US") == "March 9, 2026"
    assert i18n.date(d, "long", "de-DE") == "9. März 2026"
    assert i18n.date(d, "short", "en-GB") == "09/03/2026"
    assert i18n.date("2026-03-09", "long", "ja-JP") == "2026年3月9日"


def test_accessibility_helpers():
    assert i18n.contrast("#000000", "#ffffff") == 21.0
    assert i18n.passes_aa("#0B6B64", "#ffffff")  # the brand teal on white is readable text
    assert not i18n.passes_aa("#9aa7a1", "#ffffff") and i18n.passes_aa("#9aa7a1", "#121816")
    s = i18n.describe_series("Revenue", [dt.date(2026, m, 1) for m in (1, 2, 3, 4)], [100, 140, 90, 120])
    assert s.startswith("Revenue rose from 100 in January 1, 2026 to 120 in April 1, 2026 (20%)")
    assert "peaking at 140" in s and "lowest at 90" in s and "4 points" in s
    t = i18n.describe_table("Top regions", ["region", "revenue"], [["EMEA", 10], ["APAC", 8], ["NA", 7], ["LATAM", 2]])
    assert t.startswith("Top regions: a table of 4 rows and 2 columns") and t.endswith("and 1 more.")
