"""Generate the generic sample spreadsheets used in the README and tests.

Deterministic (fixed seed) so the output is reproducible. Writes:
  examples/sample_sales.csv
  examples/sample_sales.xlsx

Usage:
    python examples/generate_samples.py
"""

from __future__ import annotations

import os
import random

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))

REGIONS = ["North", "South", "East", "West", "Central"]
COUNTRIES = {"North": "Canada", "South": "Brazil", "East": "Japan", "West": "Germany", "Central": "France"}
PRODUCTS = [
    ("Laptop", "Hardware"),
    ("Monitor", "Hardware"),
    ("Keyboard", "Accessories"),
    ("Mouse", "Accessories"),
    ("Desk Chair", "Furniture"),
    ("Standing Desk", "Furniture"),
    ("Webcam", "Accessories"),
    ("Docking Station", "Hardware"),
]
PRICES = {
    "Laptop": 1200,
    "Monitor": 300,
    "Keyboard": 45,
    "Mouse": 25,
    "Desk Chair": 180,
    "Standing Desk": 520,
    "Webcam": 70,
    "Docking Station": 210,
}
REPS = ["A. Silva", "B. Okafor", "C. Nakamura", "D. Muller", "E. Rossi", "F. Haddad"]
STATUSES = ["Completed", "Completed", "Completed", "Pending", "Refunded"]


def build() -> pd.DataFrame:
    random.seed(42)
    rows, oid = [], 1000
    for year in (2024, 2025):
        for month in range(1, 13):
            for _ in range(random.randint(7, 12)):
                region = random.choice(REGIONS)
                product, category = random.choice(PRODUCTS)
                units = random.randint(1, 40)
                price = PRICES[product]
                discount = random.choice([0, 0, 0, 0.05, 0.10, 0.15])
                revenue = round(units * price * (1 - discount), 2)
                day = random.randint(1, 28)
                rows.append(
                    {
                        "Order ID": f"ORD-{oid}",
                        "Order Date": f"{year}-{month:02d}-{day:02d}",
                        "Region": region,
                        "Country": COUNTRIES[region],
                        "Product": product,
                        "Category": category,
                        "Sales Rep": random.choice(REPS),
                        "Units": units,
                        "Unit Price": price,
                        "Discount": discount,
                        "Revenue": revenue,
                        "Status": random.choice(STATUSES),
                    }
                )
                oid += 1
    df = pd.DataFrame(rows)
    # Imperfections for the data-quality, anomaly and duplicate examples.
    df.loc[5, "Region"] = None  # a missing dimension value
    df.loc[9, "Sales Rep"] = None  # a missing text value
    df = pd.concat([df, df.iloc[[3]]], ignore_index=True)  # one duplicate order
    df.loc[len(df) - 1, "Order ID"] = df.loc[3, "Order ID"]
    df.loc[20, "Revenue"] = 250000.0  # a clear revenue outlier
    return df


def main() -> None:
    df = build()
    csv_path = os.path.join(HERE, "sample_sales.csv")
    xlsx_path = os.path.join(HERE, "sample_sales.xlsx")
    df.to_csv(csv_path, index=False)
    df.to_excel(xlsx_path, index=False, sheet_name="Sales")
    print(f"Wrote {len(df)} rows to:\n  {csv_path}\n  {xlsx_path}")


if __name__ == "__main__":
    main()
