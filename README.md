# BYOD — Bring Your Own Data

A containerized self-service data platform. Upload any CSV or XLSX file and immediately browse, insert, update, and delete rows through a REST API or a built-in web UI — no code changes required.

---

## Quick Start

```bash
git clone https://github.com/HonYaoZhi/Bring-Your-Own-Data-Service.git
docker compose up --build
```

| Service  | URL                          |
|----------|------------------------------|
| Web UI   | http://localhost:3000        |
| REST API | http://localhost:8000        |
| API Docs | http://localhost:8000/docs   |

Data is persisted in a Docker volume (`byod-data`) and survives container restarts.

---

## Architecture

```
┌─────────────────────────────────────────┐
│  docker compose                         │
│                                         │
│  ┌──────────┐      ┌──────────────────┐ │
│  │ frontend │─────▶│    backend       │ │
│  │  nginx   │      │  FastAPI + Python│ │
│  │ :3000    │      │  :8000           │ │
│  └──────────┘      └────────┬─────────┘ │
│                             │           │
│                    ┌────────▼─────────┐ │
│                    │  DuckDB (file)   │ │
│                    │  /data/byod.duckdb│ │
│                    └──────────────────┘ │
└─────────────────────────────────────────┘
```

**Stack choices:**

| Layer    | Choice     | Rationale |
|----------|------------|-----------|
| API      | FastAPI    | Async, automatic OpenAPI docs, Pydantic validation |
| Database | DuckDB     | Schema-on-write, embeddable, zero config, excellent CSV/XLSX ingest, full SQL |
| Frontend | Vanilla JS | No build step, single HTML file, simple to serve |
| Server   | Nginx      | Serves SPA + proxies API, lightweight |

---

## API Reference

All responses are JSON. Base URL: `http://localhost:8000`

### Upload Files

```http
POST /datasets/upload
Content-Type: multipart/form-data

files=@sales.csv&files=@customers.csv
```

```bash
# Single file
curl -X POST http://localhost:8000/datasets/upload \
  -F "files=@sales.csv"

# Multiple files at once
curl -X POST http://localhost:8000/datasets/upload \
  -F "files=@sales.csv" \
  -F "files=@customers.csv" \
  -F "files=@inventory.xlsx"
```

**Response:**
```json
{
  "uploads": [
    {
      "file": "sales.csv",
      "status": "ok",
      "dataset": "sales",
      "rows": 1500,
      "columns": ["_row_id", "order_id", "amount", "date"]
    }
  ]
}
```

---

### List Datasets

```http
GET /datasets
```

```bash
curl http://localhost:8000/datasets
```

**Response:**
```json
{
  "datasets": [
    {
      "name": "sales",
      "original_filename": "sales.csv",
      "row_count": 1500,
      "created_at": "2026-06-14 12:55:53.833997"
    },
    {
      "name": "customers",
      "original_filename": "customers.csv",
      "row_count": 342,
      "created_at": "2026-06-14 12:55:53.833997"
    }
  ]
}
```

---

### Get Schema

```http
GET /datasets/{dataset}/schema
```

```bash
curl http://localhost:8000/datasets/customers/schema
```

**Response:**
```json
{
    "dataset": "customers",
    "columns": [
        {
            "name": "_row_id",
            "type": "BIGINT",
            "nullable": true
        },
        {
            "name": "customer_id",
            "type": "VARCHAR",
            "nullable": true
        },
        {
            "name": "name",
            "type": "VARCHAR",
            "nullable": true
        },
        {
            "name": "email",
            "type": "VARCHAR",
            "nullable": true
        },
        {
            "name": "country",
            "type": "VARCHAR",
            "nullable": true
        }
    ]
}
```

---

### Browse Rows

```http
GET /datasets/{dataset}/rows
```

Query parameters:

| Param       | Type    | Default | Description |
|-------------|---------|---------|-------------|
| `page`      | int     | 1       | Page number |
| `page_size` | int     | 50      | Rows per page (max 1000) |
| `filter_col`| string  | —       | Column to filter on |
| `filter_val`| string  | —       | Substring match (case-insensitive) |
| `sort_col`  | string  | —       | Column to sort by |
| `sort_dir`  | string  | asc     | `asc` or `desc` |

```bash
# Page 2, 25 rows per page
curl "http://localhost:8000/datasets/customers/rows?page=1&page_size=50"

# Filter + sort
curl "http://localhost:8000/datasets/customers/rows?page=1&page_size=50&filter_col=customer_id&filter_val=C001&sort_col=name&sort_dir=asc"
```

**Response:**
```json
{
    "dataset": "customers",
    "total": 1,
    "page": 1,
    "page_size": 50,
    "pages": 1,
    "rows": [
        {
            "_row_id": 1,
            "customer_id": "C001",
            "name": "Alice Tan",
            "email": "alice@example.com",
            "country": "Singapore"
        }
    ]
}
```

---

### Insert Row

```http
POST /datasets/{dataset}/rows
Content-Type: application/json
```

```bash
curl -X POST http://localhost:8000/datasets/customers/rows \
  -H "Content-Type: application/json" \
  -d '{"data":{"customer_id":"C005","name":"Danial","email":"danial@gmail.com","country":"Malaysia"}}'
```

**Response:**
```json
{
    "status": "inserted",
    "_row_id": 7,
    "row": {
        "_row_id": 7,
        "customer_id": "C005",
        "name": "Danial",
        "email": "danial@gmail.com",
        "country": "Malaysia"
    }
}
```

---

### Update Row

```http
PUT /datasets/{dataset}/rows/{row_id}
Content-Type: application/json
```

```bash
curl -X PUT http://localhost:8000/datasets/customers/rows/7 \
  -H "Content-Type: application/json" \
  -d '{"data":{"customer_id":"C005","name":"Danial","email":"danial70@gmail.com","country":"Malaysia"}}'
```

**Response:**
```json
{
    "status": "updated",
    "_row_id": 7
}
```

---

### Delete Row

```http
DELETE /datasets/{dataset}/rows/{row_id}
```

```bash
curl -X DELETE http://localhost:8000/datasets/customers/rows/6
```

**Response:**
```json
{
    "status": "deleted",
    "_row_id": 6
}
```

---

### Drop Dataset

```http
DELETE /datasets/{dataset}
```

```bash
curl -X DELETE http://localhost:8000/datasets/customers
```

**Response:**
```json
{
    "status": "deleted",
    "dataset": "customers"
}
```

---

### Execute SQL Query (Bonus)

Read-only `SELECT` queries across any dataset.

```http
POST /query
Content-Type: application/json
```

```bash
# Cross-dataset join
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT\n    s.order_id,\n    s.date,\n    c.name AS customer_name,\n    c.country,\n    i.product_name,\n    i.category,\n    s.amount\nFROM sales_record s\nJOIN customers c\n    ON s.customer_id = c.customer_id\nJOIN inventory i\n    ON s.product_id = i.product_id\nORDER BY s.date DESC;"}'
```

**Response:**
```json
{
    "rows": [
        {
            "order_id": "ORD-008",
            "date": "2024-01-22T00:00:00",
            "customer_name": "Carol Smith",
            "country": "USA",
            "product_name": "USB-C Hub",
            "category": "Electronics",
            "amount": 89.5
        },
        {
            "order_id": "ORD-007",
            "date": "2024-01-20T00:00:00",
            "customer_name": "Eva Rossi",
            "country": "Italy",
            "product_name": "Wireless Headphones",
            "category": "Electronics",
            "amount": 199.99
        }
    ],
    "count": 2,
    "columns": [
        "order_id",
        "date",
        "customer_name",
        "country",
        "product_name",
        "category",
        "amount"
    ]
}
```

---

## Web UI

Open http://localhost:3000 in your browser.

- **Sidebar**: drag-and-drop or click to upload files; click any dataset to load it
- **Schema bar**: shows all column names and their inferred types
- **Table**: sortable by clicking column headers; paginated
- **Filter**: pick a column and type a substring to filter rows
- **Add Row**: form auto-generated from schema
- **Edit/Delete**: hover over any row to reveal action buttons
- **SQL panel**: collapsed by default; expand at bottom to run cross-dataset queries

---

## Design Decisions & Tradeoffs

### DuckDB as storage

DuckDB is a high-performance, open-source, in-process analytical SQL database designed to run directly within application. DuckDB reads CSV and XLSX natively, infers types automatically, and speaks full SQL. It persists to a single `.duckdb` file, eliminating the need for a separate database container. 

The downside is write concurrency. DuckDB holds a file-level write lock, so concurrent uploads will queue or fail. 
 
## Chunked file streaming
 
Uploaded files are read in **64 KB chunks** and written to a temp file on disk. The database work only starts after the full file is saved.
 
```python
chunk = await file.read(65536)  # 64 KB at a time
```
 
The simple alternative is `await file.read()`, which loads the entire file into memory at once. A 300 MB upload would use 300 MB of RAM. With chunked reading, the process holds at most 64 KB at any moment, regardless of file size.
 
The size limit is also enforced during streaming, so an oversized file is rejected and cleaned up before it ever reaches DuckDB.
 
> **Measured:** A 300 MB CSV takes ~10.5 seconds to ingest end-to-end. Most of that time is DuckDB's full-file type inference (explained below), not the streaming step.

## Full-file type inference (`sample_size = -1`)
 
DuckDB support Auto Detection that automatically analyzes `.csv` and `.xlsx` file to figure out how to parse and read it without requiring to manually specify the schema or formatting rules. More details can be found in the official DuckDB documentations on:

- https://duckdb.org/docs/lts/data/csv/auto_detection
- https://duckdb.org/docs/current/guides/file_formats/excel_import

Because `.csv/.xlsx` files are not self-describing and come in countless messy variations, DuckDB evaluate a sample chunk of the file (2,048 rows by default) and predict its structure.
 
**Known limitation:** Even full-scan inference can get date and timestamp columns wrong. Ambiguous formats like `"2024-01"` or mixed date formats in the same column may end up stored as `VARCHAR`. Two fixes were considered:
 
- **Pandas pre-inference:** Use pandas to detect types first, then pass to DuckDB. Rejected because pandas loads the entire file into memory, which defeats the chunked upload approach.
- **Schema review after upload:** Instead of relying entirely on automatic type inference, the system can first import all columns as text and return the detected schema to the frontend. Users may then review and adjust column types before the final table is created using `TRY_CAST`. This approach provides better control over ambiguous columns, such as dates and timestamps, but introduces an additional upload-and-review step compared to the current zero-configuration workflow.

![Schema review after upload](https://https://github.com/HonYaoZhi/Bring-Your-Own-Data-Service/raw/main/images/schema-review-after-upload.png)

## Temp table → permanent table
 
Files are not loaded directly into the final table. The steps are:
 
1. DuckDB reads the file into a **temporary table** with a random UUID name.
2. The temp table's schema is inspected to build sanitized column names.
3. The permanent table is created with `CREATE TABLE AS SELECT ... FROM temp_table`, applying column renames and adding `_row_id` in one step.
4. The temp table is dropped immediately.
This keeps the logic clean and safe. If step 3 fails for any reason, the transaction rolls back and no partial table is left behind.
 
## Transactional uploads
 
All database writes during an upload — creating the table, creating the sequence, inserting the metadata row — happen inside a single `BEGIN / COMMIT` block:

```python
conn.execute("BEGIN TRANSACTION")
# create table, create sequence, insert meta ...
conn.execute("COMMIT")
# on any failure:
conn.execute("ROLLBACK")
```

This means the database is never left in a broken state. For example, a table with no sequence would make every future insert fail. With the transaction, either all three objects are created together, or none of them are.

### `_row_id` synthetic key

CSV files have no primary key. The service adds a synthetic `_row_id` column to give each row a stable address for update and delete.
 
The naive approach is `MAX(_row_id) + 1` on every insert, but that requires a read before every write and has a race condition under concurrent inserts. Instead, the service creates a **DuckDB sequence per dataset** at upload time:
 
```sql
CREATE SEQUENCE "_seq_{dataset}" START {max_id + 1}
```
 
Every insert then calls `nextval()` to get the next ID atomically. Sequence values are never reused.

### Table-per-file isolation

Each uploaded file becomes its own DuckDB table named after the file's base name (sanitized). Tables are completely independent — no cross-table foreign key enforcement, no schema coupling. This matches the spec's "independent datasets" requirement.

## Two-layer SQL injection defense
 
Table and column names cannot use SQL parameters — only values can. This makes dynamic SQL with identifiers a real injection risk.
 
The service defends at two layers:
 
**Layer 1 — Sanitization at upload.** All column names from the file go through `sanitize_name()`, which produces lowercase snake_case. The permanent table is created using only these cleaned names.
 
**Layer 2 — Validation on every request.** Every dataset or column name from an HTTP path or query string is checked by `validate_identifier()`, which enforces a strict regex (`^[a-z_][a-z0-9_]*$`). Anything that did not go through layer 1 will fail here.
 
Together, these ensure that identifiers inserted into dynamic SQL can never contain characters that break out of the quoting context.
 
---
 
## Read-only SQL endpoint
 
The `/query` endpoint accepts arbitrary SQL but restricts it to `SELECT` only. Two checks are applied:
 
1. A **blocklist** of forbidden keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `COPY`, `ATTACH`, `DETACH`, `CALL`, `PRAGMA`) rejects anything that contains them.
2. A **`SELECT\b` regex** requires the statement to actually start with `SELECT`, catching tricks like `WITH DELETE AS (...)` that a keyword scan alone might miss.
This is a lightweight safeguard. A stricter implementation would open a read-only DuckDB connection for query execution, making writes structurally impossible rather than just rejected by a check.
 
## Column name sanitization
 
CSV headers can contain spaces, special characters, or leading digits. All headers are normalized to lowercase snake_case at ingest time. If two columns produce the same name after normalization, a numeric suffix is added (`col`, `col_1`, `col_2`, …).
 
## NaN and NaT normalization
 
DuckDB returns `float('nan')` for NULL values in numeric columns and `pandas.NaT` for NULL timestamps. Neither is valid JSON. Every row returned by the browse endpoint checks each value with `pd.isna()` and converts it to `None` (JSON `null`) before serializing. Without this, numeric NULL columns would crash the response or produce invalid JSON.
 
## Single-file frontend
 
The UI is one static HTML file with inline CSS and vanilla JavaScript. No npm, no bundler, no build step — nginx just serves the file. This keeps the container small and means `docker compose up` works without any frontend tooling on the host.
 
The downside is that the file grows large as features are added and there is no module separation. For a team project this would be split into a proper frontend build, but for a local-run service the simplicity is the right call.

---

## Assumptions & Limitations
- **Blocking uploads.** The upload request waits until ingestion is fully complete. For very large files, a background task queue with a polling endpoint would be better — but that adds significant complexity for a local-first tool.
- **Original file not kept.** The temp file is deleted after the DuckDB table is created. Only the parsed table persists.
- **Date/timestamp inference.** Ambiguous columns (e.g. `"01"`) may be inferred as strings rather than integers. Non-ISO or mixed date formats may be stored as `VARCHAR`. A future improvement would let callers declare column types at upload time.
- **Name collision on re-upload.** Uploading a file whose name matches an existing dataset returns a `400`. Delete the existing dataset first, or rename the file. 
- **Max upload size.** Controlled by the `MAX_UPLOAD_BYTES` environment variable (default 1 GB). The nginx `client_max_body_size` must be kept in sync.

---

## File Structure

```
byod/
├── backend/
│   ├── main.py            # FastAPI application
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── index.html         # Single-page UI
│   ├── nginx.conf
│   └── Dockerfile
├── docker-compose.yml
└── README.md
```
