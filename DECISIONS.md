# DECISIONS.md

### 1. Indexes
We indexed `employees` on `emp_code` (unique) and `(department, joined_on)` for headcount lookups. On `attendance_logs`, we created `(emp_code, date)` (unique) to enforce one log daily, `(date, -1, emp_code, 1)` for chronological sorting, and `(emp_code, punch_in, -1)` for open punch-outs. We rejected indexing `late_minutes` directly because date filters narrow queries first, avoiding write overhead.

### 2. Punch-in race
Both requests compute the attendance date and call `insert_one()`. MongoDB's unique index on `(emp_code, date)` serializes the write: one insert succeeds with 201, while the second raises `DuplicateKeyError`, caught by our handler to return 409 Conflict.

### 3. Ties
We compute ranks using MongoDB's `$rank` window function (standard competition: 1, 2, 2, 4). Filtering `{ rank: { $lte: limit } }` occurs after ranking, so all employees tied at the cutoff are included, sorted by `emp_code ASC`.

### 4. Headcount
The aggregation starts from `employees`, filtering `joined_on <= period_end`, and joins logs via `$lookup`. Employees with no logs still produce documents, correctly contributing to headcount while adding zero present days.

### 5. 100x scale
At 100x scale (~10M logs), I would shard `attendance_logs` on `{ emp_code: 1, date: 1 }` to distribute writes and partition history. I would also maintain pre-aggregated daily summaries to avoid scanning raw logs for monthly reports.
