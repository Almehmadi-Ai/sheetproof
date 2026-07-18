# Example prompts

These prompts work on the bundled generic sample data
([`examples/sample_sales.csv`](../examples/sample_sales.csv) — orders with region, country, product,
category, sales rep, units, unit price, discount, revenue, status, and order date). Attach the file
and ask. The right‑hand column shows the operation the model maps each request to.

| Ask this | Operation |
| --- | --- |
| "Give me an overview of this file." | `overview` |
| "What's the total revenue?" | `aggregate` (sum) |
| "How many orders are there?" | `aggregate` (count) |
| "Average units per order, plus the median and max." | `aggregate` (multi‑stat) |
| "Total revenue by region, with each region's share of the total." | `group_aggregate` |
| "Top 5 products by units sold." | `top_n` |
| "Combined revenue of the top 3 regions and their share of the whole." | `top_n` + `summary` |
| "Pivot revenue by region (rows) and category (columns)." | `pivot` |
| "How many orders per status?" | `value_counts` |
| "Describe the numeric columns." | `describe` |
| "Monthly revenue trend for 2025." | `time_series` |
| "Month‑over‑month revenue change through 2025." | `compare` (period) |
| "Compare total revenue between North and West." | `compare` (segment) |
| "Flag revenue outliers and break them down by region." | `anomalies` |
| "Are there duplicate orders?" | `duplicates` |
| "Which columns have missing values, and how many?" | `missing` |
| "List the top 5 orders over 20 units, add a net‑of‑discount column, highest first." | `transform` |
| "Add a High/Low flag for orders above the average revenue and count each." | `transform` (derive + group) |
| "Running total of revenue by month." | `transform` (window: cumsum) |
| "Rank sales reps by total revenue." | `transform` (group + window: rank) |

## Tips for reliable answers

- **Name the column** when it's ambiguous ("revenue" vs "net revenue"): the tool asks for
  clarification rather than guessing, but naming it up front is faster.
- **For per‑row tables**, ask for a list/table of specific columns — the tool returns a ready‑to‑display
  table and the model reproduces it verbatim.
- **For date math** ("orders in the last 90 days", "days since order"), give the reference date
  ("as of 2025‑06‑30"); the tool computes the difference deterministically.
- **Trust the flags.** If the answer notes a figure could not be verified, re‑ask for that specific
  number so the tool computes it.
