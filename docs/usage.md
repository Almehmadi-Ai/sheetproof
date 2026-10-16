# Usage & operation reference

You interact with SheetProof in natural language — attach a spreadsheet and ask. Behind
the scenes the model calls two functions.

## The two functions

### `inspect_spreadsheet`
Returns the file's real schema: sheet names, row/column counts, and for every column an inferred
**role** (`measure`, `dimension`, `temporal`, `identifier`, `text`, `boolean`), dtype, null and
distinct counts, and sample values. The model always calls this first so it uses the file's actual
column names.

### `analyze_spreadsheet`
Runs one deterministic operation and returns the executed result plus **provenance** (columns used,
rows processed, and the operation performed). Key parameters:

| Parameter | Type | Used by | Meaning |
| --- | --- | --- | --- |
| `operation` | string | all | Which analysis to run (see table below). |
| `value_column` | string | most | The numeric/measure column to compute on. |
| `group_by` | string | group/pivot/compare | The dimension to group by. |
| `columns` | list | describe/pivot | Explicit columns (pivot's column dimension is `columns[0]`). |
| `agg` | string | many | `sum`\|`mean`\|`count`\|`min`\|`max`\|`median`\|`std`\|`nunique` (`top_n` ranks by it). Anything else is an error. |
| `aggs` | list | aggregate/group | Compute several statistics at once. |
| `n` | int | top_n/value_counts | How many rows/groups to return. |
| `filters` | string | any | A row condition applied before computing, written in the [expression language](#the-expression-language) (not a pandas query). |
| `date_column` | string | time_series/compare | The temporal column. |
| `freq` | string | time_series/compare | `D`\|`W`\|`M`\|`Q`\|`Y`. |
| `method`, `threshold` | string, float | anomalies | `iqr`\|`zscore`\|`pct_above_mean` and its threshold. |
| `subset` | list | duplicates/compare | Duplicate key, or the segments to compare. |
| `pipeline`, `params` | list, dict | transform | The composable pipeline and named values. |
| `sheet` | string | all | Sheet name (auto-detected if omitted). |

## Operations

| Operation | Purpose |
| --- | --- |
| `overview` | One-shot summary: shape, roles, nulls, duplicates, per-measure stats, top dimension values, date range. |
| `aggregate` | One statistic of `value_column` over the whole file; add `group_by` to split, or `aggs` for several statistics. |
| `group_aggregate` | `value_column` per `group_by`, with count and % of total, sorted descending. Rows whose group value is blank form a `(blank)` group, so the groups always add up to the column total. |
| `top_n` | The top `n` groups by `agg` of `value_column`, with a `summary` (grand total, returned subtotal, returned share). |
| `pivot` | `value_column` aggregated by `group_by` (rows) × `columns[0]` (cols). |
| `value_counts` | Frequency and % of each value in a column. |
| `describe` | Descriptive statistics for `columns` (or all measures). |
| `time_series` | `value_column` resampled by `date_column` at `freq`. |
| `anomalies` | Outliers by `method`; returns the count, bounds, flagged rows (capped), and a per-dimension `breakdown`. |
| `duplicates` | Duplicate full rows, or duplicates by `subset`. |
| `missing` | Null counts per column. |
| `compare` | Period-over-period change (with `date_column` + `freq`) or segment comparison across `group_by`. |
| `transform` | A safe composable pipeline — see below. |

## The `transform` pipeline

Pass `pipeline=[steps]`. Each step is an object with an `op`:

| Step | Fields |
| --- | --- |
| `select` | `columns` |
| `filter` | `expr` |
| `derive` | `name`, `expr` |
| `group` | `by`, `agg` = `{out_name: "fn" or "fn:Column"}` |
| `sort` | `by`, `dir` (`asc`/`desc`) |
| `limit` | `n` |
| `distinct` | `columns` (optional) |
| `window` | `fn` (`share`\|`rank`\|`cumsum`\|`lag`\|`lead`\|`pct_change`), `value`, `by`, `order_by`, `name` |
| `pivot` | `index`, `columns`, `values`, `agg` |
| `unpivot` | `id_vars`, `value_vars` |

`filter` and `derive` steps take expressions in the [expression language](#the-expression-language)
below. No arbitrary code is possible.

**Example** — top 5 large orders with a net-of-discount column:

```json
[
  {"op": "derive", "name": "Net", "expr": "`Revenue` * (1 - `Discount`)"},
  {"op": "filter", "expr": "`Units` > 20"},
  {"op": "select", "columns": ["Order ID", "Region", "Units", "Net"]},
  {"op": "sort", "by": "Net", "dir": "desc"},
  {"op": "limit", "n": 5}
]
```

The result includes `table_markdown`, a ready-to-display table of the exact executed rows.

## The expression language

The `filters` parameter and the transform `filter` / `derive` steps share one small, safe
expression language. An expression is parsed with Python's `ast` module and checked against an
allow-list **before** anything is evaluated; it is then evaluated with direct pandas operations. It
is never passed to `DataFrame.query`, `DataFrame.eval`, `pandas.eval`, `eval` or `exec`.

| Element | Syntax |
| --- | --- |
| Column | `Units`, or `` `Order Date` `` (backticks for names with spaces, punctuation or non-Latin letters, e.g. `` `المنطقة` ``), or `col('Order Date')` |
| Literal | numbers (`20`, `-3.5`), text (`'North'` or `"North"`), `True`, `False`, `None`, lists (`['North', 'South']`) |
| Parameter | `@name` — a value passed in `params` (text, number, boolean, null, or a list of those) |
| Comparison | `==` `!=` `<` `<=` `>` `>=`, including chains such as `2 < Units <= 10` |
| Membership | `in [...]` and `not in [...]` (the list may be a parameter: `Region in @regions`) |
| Logic | `and` `or` `not`, or `&` `\|` `~` — always logical; parenthesize comparisons: `(Units > 5) & (Region == 'North')` |
| Arithmetic | `+` `-` `*` `/` `//` `%`, and `**` with a literal exponent between −100 and 100 (evaluated as floating point) |
| Functions | `where, coalesce, datediff, year, month, day, date, abs, round, num, text, lower, upper, len, contains, startswith, endswith, isnull, notnull, least, greatest` |
| Column methods | `.isna()` `.notna()` `.isnull()` `.notnull()` `.between(low, high)` (optional `inclusive='both'\|'neither'\|'left'\|'right'`) `.isin([...])` `.str.contains('x')` (optional `case=False`) `.str.startswith('x')` / `.str.endswith('x')` (text or a list of texts) `.str.lower()` `.str.upper()` `.str.strip()` `.str.len()` |

**How values compare**

- A **number** literal compares numerically. The column is read with the same parsing as measures, so
  `Revenue > 500` works on text such as `$1,200.00`; cells that are not numbers never match.
- A **text** literal compared with a date column compares as a date: `` `Order Date` >= '2025-01-01' ``.
- Any other **text** literal is an exact, case-sensitive text comparison. For case-insensitive matching
  use `Region.str.lower() == 'north'` or `Region.str.contains('north', case=False)`.
- A **blank** cell never satisfies `==`, `<`, `<=`, `>`, `>=` against a value, and always satisfies `!=`
  (as in pandas). Test for blanks with `.isna()` / `.notna()`; `== None` is the same as `.isna()`.
- `.str.contains` (and the `contains` function) match **literal text**, not regular expressions.
- In arithmetic, text columns are read as numbers; `+` between two texts joins them (at most 10,000
  characters per cell).

**Examples:** `` Region == 'North' and Units > 20 `` · `` `Order Date`.between('2025-01-01', '2025-03-31') `` ·
`` Status in ['Completed', 'Pending'] `` · `` Product.str.contains('desk', case=False) `` · `` Units >= @min_units ``

**Not supported** (rejected with an error that explains why): attribute access other than the column
methods above, indexing, imports, lambdas, comprehensions, f-strings, `is` / `is not`, regular
expressions, `.dt` accessors (use the `year`/`month`/`day`/`date` functions), and any other function
or method. Expressions are limited to 2,000 characters, 400 syntax nodes and a nesting depth of 64.

## Reading results

Every response is JSON with `status`, a `result`, and `provenance`. Where relevant it also includes a
`summary` (grand total, returned subtotal, share of total) and a `truncated` flag (the result was cut to
`max_return_rows`; totals still cover every row). The model is instructed to report these figures
verbatim rather than recomputing anything.

- `provenance.number_format` reports, for each measure stored as text where the decimal separator
  mattered or some cells were not numbers, the separator used, what decided it (`column values`,
  `other values in the file`, the valve, or the default), how many distinct values were ambiguous, and
  how many non-blank cells were not numbers.
- `status: "needs_clarification"` means the tool would have had to guess (an unknown or ambiguous column,
  a non-numeric measure): it lists `candidates` and `columns_available` instead.
- `status: "error"` explains what was rejected (an invalid filter, an unknown aggregation or
  frequency, a pivot that would be too large) and, for expressions, includes a `hint`.
- A file that cannot be used — not found or not readable by the user, over a size limit, or not a
  valid spreadsheet — returns `{"error": "..."}` with the reason.
