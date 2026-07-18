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
| `agg` | string | many | `sum`\|`mean`\|`count`\|`min`\|`max`\|`median`\|`std`\|`nunique`. |
| `aggs` | list | aggregate/group | Compute several statistics at once. |
| `n` | int | top_n/value_counts | How many rows/groups to return. |
| `filters` | string | any | A pandas query applied before computing. |
| `date_column` | string | time_series/compare | The temporal column. |
| `freq` | string | time_series/compare | `D`\|`W`\|`M`\|`Q`\|`Y`. |
| `method`, `threshold` | string, float | anomalies | `iqr`\|`zscore`\|`pct_above_mean` and its threshold. |
| `subset` | list | duplicates/compare | Duplicate key, or the segments to compare. |
| `pipeline`, `params` | list, dict | transform | The composable pipeline and named values. |
| `sheet` | string | all | Sheet name (auto‑detected if omitted). |

## Operations

| Operation | Purpose |
| --- | --- |
| `overview` | One‑shot summary: shape, roles, nulls, duplicates, per‑measure stats, top dimension values, date range. |
| `aggregate` | One statistic of `value_column` over the whole file; add `group_by` to split, or `aggs` for several statistics. |
| `group_aggregate` | `value_column` per `group_by`, with count and % of total, sorted descending. |
| `top_n` | The top `n` groups by `agg` of `value_column`, with a `summary` (grand total, returned subtotal, returned share). |
| `pivot` | `value_column` aggregated by `group_by` (rows) × `columns[0]` (cols). |
| `value_counts` | Frequency and % of each value in a column. |
| `describe` | Descriptive statistics for `columns` (or all measures). |
| `time_series` | `value_column` resampled by `date_column` at `freq`. |
| `anomalies` | Outliers by `method`; returns the count, bounds, flagged rows (capped), and a per‑dimension `breakdown`. |
| `duplicates` | Duplicate full rows, or duplicates by `subset`. |
| `missing` | Null counts per column. |
| `compare` | Period‑over‑period change (with `date_column` + `freq`) or segment comparison across `group_by`. |
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

**Expressions** reference columns as `` `Column Name` `` or `col('Column Name')`. External values are
passed via `params={...}` and referenced with `@name` (e.g. `@today`). Allowed functions:
`where, coalesce, datediff, year, month, day, date, abs, round, num, text, lower, upper, len,
contains, startswith, endswith, isnull, notnull, least, greatest`. No arbitrary code is possible.

**Example** — top 5 large orders with a net‑of‑discount column:

```json
[
  {"op": "derive", "name": "Net", "expr": "`Revenue` * (1 - `Discount`)"},
  {"op": "filter", "expr": "`Units` > 20"},
  {"op": "select", "columns": ["Order ID", "Region", "Units", "Net"]},
  {"op": "sort", "by": "Net", "dir": "desc"},
  {"op": "limit", "n": 5}
]
```

The result includes `table_markdown`, a ready‑to‑display table of the exact executed rows.

## Reading results

Every response is JSON with `status`, a `result`, and `provenance`. Where relevant it also includes a
`summary` (grand total, returned subtotal, share of total) and `truncated` flags. The model is
instructed to report these figures verbatim rather than recomputing anything.
