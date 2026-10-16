"""Regenerate tests/fixtures/legacy.xls, a small legacy (BIFF) Excel workbook.

Not needed to run the tests: the .xls file is committed. Writing .xls requires the
unmaintained ``xlwt`` package, so it is kept out of the development requirements:

    pip install xlwt==1.3.0
    python tests/fixtures/make_legacy_xls.py
"""

from __future__ import annotations

import os

import xlwt


def main() -> None:
    wb = xlwt.Workbook()
    notes = wb.add_sheet("Notes")  # a title tab with fewer cells than the data sheet
    notes.write(0, 0, "Legacy workbook")
    ws = wb.add_sheet("Sales")
    ws.write(0, 0, "Quarterly report (legacy .xls)")  # banner row above the header
    for c, header in enumerate(["Region", "Units", "Revenue"]):
        ws.write(2, c, header)
    rows = [("North", 10, 1000.5), ("South", 5, 500.25), (None, 7, 700.0), ("North", 3, 300.0)]
    for r, row in enumerate(rows, start=3):
        for c, value in enumerate(row):
            if value is not None:
                ws.write(r, c, value)
    wb.save(os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy.xls"))


if __name__ == "__main__":
    main()
