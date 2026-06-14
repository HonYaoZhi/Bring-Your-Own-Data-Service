import uuid
import re
import os
import tempfile
from typing import Optional, List, Any, Dict
import pandas as pd

from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
import duckdb
from pydantic import BaseModel

app = FastAPI(title="BYOD - Bring Your Own Data", version="1.0.0")


# ── CORS ─────────────────────────────────────────────────────────────────────
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8080")
ALLOWED_ORIGINS: List[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


# ── Config ───────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "/data/byod.duckdb")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Nginx also enforces 100 MB; keep in sync.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(1000 * 1024 * 1024)))  # 100 MB


# ── Connection helper ────────────────────────────────────────────────────────
# Keep a single shared connection per process; guard every caller with try/finally so the connection is never leaked on error paths.
_shared_conn: Optional[duckdb.DuckDBPyConnection] = None

def get_conn() -> duckdb.DuckDBPyConnection:
    global _shared_conn
    if _shared_conn is None:
        _shared_conn = duckdb.connect(DB_PATH)
    return _shared_conn


# ── Utils ────────────────────────────────────────────────────────────────────
_SAFE_NAME_RE = re.compile(r'^[a-z_][a-z0-9_]*$')

def sanitize_name(name: str) -> str:
    """Clean up table/column names for DuckDB."""
    if not name:
        raise ValueError("Name cannot be empty")
    name = re.sub(r"[^\w]", "_", name.strip())
    if name and name[0].isdigit():
        name = "_" + name
    result = name.lower()
    if not result:
        raise ValueError("Name is empty after sanitization")
    return result


def validate_identifier(name: str) -> str:
    """
    Vvalidate that a dataset or column name received from an HTTP
    request matches the safe pattern we enforce at upload time.  Rejects anything
    that could break out of a double-quoted SQL identifier.
    """
    if not _SAFE_NAME_RE.match(name):
        raise HTTPException(400, f"Invalid identifier: '{name}'")
    return name


def get_all_datasets(conn) -> List[str]:
    """List all user datasets (tables) in the database."""
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    return [t[0] for t in tables if not t[0].startswith("_byod_meta")]


def ensure_meta_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _byod_meta (
            dataset_name VARCHAR PRIMARY KEY,
            original_filename VARCHAR,
            row_count INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def get_valid_columns(conn, dataset: str) -> List[str]:
    """Return the list of column names for a dataset (already stored as safe identifiers)."""
    return [c[1] for c in conn.execute(f'PRAGMA table_info("{dataset}")').fetchall()]


# ── Upload ───────────────────────────────────────────────────────────────────
ALLOWED_EXTENSIONS = {"csv", "xlsx"}

@app.post("/datasets/upload", summary="Upload one or more CSV/XLSX files")
async def upload_files(request: Request, files: List[UploadFile] = File(...)):
    results = []
    conn = get_conn()
    ensure_meta_table(conn)

    for file in files:
        transaction_started = False
        original_name = file.filename or "upload"
        ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
        base_name = original_name.rsplit(".", 1)[0]
        temp_filepath = None

        try:
            if ext not in ALLOWED_EXTENSIONS:
                raise ValueError(f"Unsupported file type '.{ext}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")

            dataset_name = sanitize_name(base_name)

            existing = conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
                [dataset_name]
            ).fetchone()[0]
            if existing > 0:
                raise ValueError(f"Dataset '{dataset_name}' already exists")

            temp_file_obj = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
            temp_filepath = temp_file_obj.name
            total_bytes = 0
            try:
                while True:
                    chunk = await file.read(65536)  # 64 KB chunks
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MAX_UPLOAD_BYTES:
                        raise ValueError(
                            f"File exceeds maximum allowed size of {MAX_UPLOAD_BYTES // (1024*1024)} MB"
                        )
                    temp_file_obj.write(chunk)
            finally:
                temp_file_obj.close()

            conn.execute("BEGIN TRANSACTION")
            transaction_started = True

            temp_table = f"_temp_upload_{uuid.uuid4().hex}"

            if ext == "xlsx":
                conn.execute(
                    f'CREATE OR REPLACE TEMPORARY TABLE "{temp_table}" AS '
                    f"SELECT * FROM read_xlsx('{temp_filepath}')"
                )
            else:
                conn.execute(
                    f'CREATE OR REPLACE TEMPORARY TABLE "{temp_table}" AS '
                    f"SELECT * FROM read_csv('{temp_filepath}', sample_size = -1)"
                )

            original_cols_info = conn.execute(f'PRAGMA table_info("{temp_table}")').fetchall()
            sanitized_column_map = {}
            seen_sanitized_names: Dict[str, int] = {}
            select_exprs = []

            for col_info in original_cols_info:
                original_col_name = col_info[1]
                sanitized_col = sanitize_name(original_col_name)  # raises on empty

                if sanitized_col in seen_sanitized_names:
                    seen_sanitized_names[sanitized_col] += 1
                    candidate = f"{sanitized_col}_{seen_sanitized_names[sanitized_col]}"
                    # Ensure the generated name doesn't collide with another original column
                    while candidate in seen_sanitized_names:
                        seen_sanitized_names[sanitized_col] += 1
                        candidate = f"{sanitized_col}_{seen_sanitized_names[sanitized_col]}"
                    sanitized_col_final = candidate
                    seen_sanitized_names[sanitized_col_final] = 0
                else:
                    seen_sanitized_names[sanitized_col] = 0
                    sanitized_col_final = sanitized_col

                sanitized_column_map[original_col_name] = sanitized_col_final
                select_exprs.append(f'"{original_col_name}" AS "{sanitized_col_final}"')

            create_sql = (
                f'CREATE TABLE "{dataset_name}" AS '
                f'SELECT ROW_NUMBER() OVER () AS _row_id, '
                f'{", ".join(select_exprs)} '
                f'FROM "{temp_table}"'
            )
            conn.execute(create_sql)
            conn.execute(f'DROP TABLE IF EXISTS "{temp_table}"')  # clean up temp table immediately

            max_id = conn.execute(
                f'SELECT COALESCE(MAX(_row_id), 0) FROM "{dataset_name}"'
            ).fetchone()[0]

            conn.execute(
                f'CREATE SEQUENCE "_seq_{dataset_name}" START {int(max_id) + 1}'
            )

            row_count = conn.execute(f'SELECT COUNT(*) FROM "{dataset_name}"').fetchone()[0]

            conn.execute(
                "INSERT OR REPLACE INTO _byod_meta (dataset_name, original_filename, row_count) VALUES (?, ?, ?)",
                [dataset_name, original_name, row_count]
            )
            conn.execute("COMMIT")

            results.append({
                "file": original_name,
                "status": "ok",
                "dataset": dataset_name,
                "rows": row_count,
                "columns": ["_row_id"] + list(sanitized_column_map.values()),
            })

        except Exception as e:
            if transaction_started:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
            results.append({"file": original_name, "status": "error", "detail": str(e)})
        finally:
            if temp_filepath and os.path.exists(temp_filepath):
                os.remove(temp_filepath)
            await file.close()

    return {"uploads": results}


# ── Dataset List ──────────────────────────────────────────────────────────────
@app.get("/datasets", summary="List all datasets")
def list_datasets():
    conn = get_conn()
    try:
        ensure_meta_table(conn)
        datasets = get_all_datasets(conn)
        meta = {}
        rows = conn.execute(
            "SELECT dataset_name, original_filename, row_count, created_at FROM _byod_meta"
        ).fetchall()
        for r in rows:
            meta[r[0]] = {"original_filename": r[1], "row_count": r[2], "created_at": str(r[3])}
    except Exception as e:
        raise HTTPException(500, f"Failed to load datasets: {str(e)}")

    result = []
    for ds in datasets:
        entry = {"name": ds}
        if ds in meta:
            entry.update(meta[ds])
        result.append(entry)
    return {"datasets": result}


# ── Schema ────────────────────────────────────────────────────────────────────
@app.get("/datasets/{dataset}/schema", summary="Get dataset schema")
def get_schema(dataset: str):
    validate_identifier(dataset)
    conn = get_conn()
    try:
        datasets = get_all_datasets(conn)
        if dataset not in datasets:
            raise HTTPException(404, f"Dataset '{dataset}' not found")
        cols = conn.execute(f'PRAGMA table_info("{dataset}")').fetchall()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

    return {
        "dataset": dataset,
        "columns": [{"name": c[1], "type": c[2], "nullable": not c[3]} for c in cols],
    }


# ── Browse (Read) ─────────────────────────────────────────────────────────────
@app.get("/datasets/{dataset}/rows", summary="Browse rows with optional pagination and filtering")
def browse_rows(
    dataset: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000),
    filter_col: Optional[str] = Query(None),
    filter_val: Optional[str] = Query(None),
    sort_col: Optional[str] = Query(None),
    sort_dir: Optional[str] = Query("asc", pattern="^(asc|desc)$"),
):
    validate_identifier(dataset)
    conn = get_conn()
    try:
        datasets = get_all_datasets(conn)
        if dataset not in datasets:
            raise HTTPException(404, f"Dataset '{dataset}' not found")

        valid_cols = get_valid_columns(conn, dataset)

        where_clause = ""
        params: list = []
        if filter_col and filter_val is not None:
            if filter_col not in valid_cols:
                raise HTTPException(400, f"Column '{filter_col}' not found")
            where_clause = f'WHERE CAST("{filter_col}" AS VARCHAR) ILIKE ?'
            params.append(f"%{filter_val}%")

        order_clause = ""
        if sort_col:
            if sort_col not in valid_cols:
                raise HTTPException(400, f"Column '{sort_col}' not found")
            order_clause = f'ORDER BY "{sort_col}" {sort_dir.upper()}'

        total = conn.execute(
            f'SELECT COUNT(*) FROM "{dataset}" {where_clause}', params
        ).fetchone()[0]
        offset = (page - 1) * page_size

        result = conn.execute(
            f'SELECT * FROM "{dataset}" {where_clause} {order_clause} LIMIT {page_size} OFFSET {offset}',
            params
        )

        columns = [col[0] for col in result.description]
        records = []

        for row in result.fetchall():
            record = {}

            for col, val in zip(columns, row):
                # Convert NaN / NA to None
                if pd.isna(val):
                    record[col] = None
                else:
                    record[col] = val
            
            records.append(record)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

    return {
        "dataset": dataset,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if total > 0 else 1,
        "rows": records,
    }


# ── Insert ────────────────────────────────────────────────────────────────────
class RowData(BaseModel):
    data: Dict[str, Any]


@app.post("/datasets/{dataset}/rows", summary="Insert a new row")
def insert_row(dataset: str, body: RowData):
    validate_identifier(dataset)
    conn = get_conn()
    try:
        datasets = get_all_datasets(conn)
        if dataset not in datasets:
            raise HTTPException(404, f"Dataset '{dataset}' not found")

        valid_cols = get_valid_columns(conn, dataset)

        new_id = conn.execute(f'SELECT nextval(\'_seq_{dataset}\')').fetchone()[0]

        row: Dict[str, Any] = {"_row_id": new_id}
        for col in valid_cols:
            if col == "_row_id":
                continue
            row[col] = body.data.get(col, None)

        col_str = ", ".join([f'"{c}"' for c in valid_cols])
        placeholders = ", ".join(["?" for _ in valid_cols])
        values = [row.get(c) for c in valid_cols]

        conn.execute(f'INSERT INTO "{dataset}" ({col_str}) VALUES ({placeholders})', values)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

    return {"status": "inserted", "_row_id": new_id, "row": row}


# ── Update ────────────────────────────────────────────────────────────────────
@app.put("/datasets/{dataset}/rows/{row_id}", summary="Update a row by _row_id")
def update_row(dataset: str, row_id: int, body: RowData):
    validate_identifier(dataset)
    conn = get_conn()
    try:
        datasets = get_all_datasets(conn)
        if dataset not in datasets:
            raise HTTPException(404, f"Dataset '{dataset}' not found")

        existing = conn.execute(
            f'SELECT * FROM "{dataset}" WHERE _row_id = ?', [row_id]
        ).fetchone()
        if not existing:
            raise HTTPException(404, f"Row {row_id} not found")

        valid_cols = get_valid_columns(conn, dataset)
        valid_col_set = set(valid_cols) - {"_row_id"}

        updates = {}
        for k, v in body.data.items():
            if k == "_row_id":
                continue
            if k not in valid_col_set:
                raise HTTPException(400, f"Unknown column: '{k}'")
            updates[k] = v

        if not updates:
            raise HTTPException(400, "No valid fields to update")

        set_clause = ", ".join([f'"{k}" = ?' for k in updates])
        values = list(updates.values()) + [row_id]
        conn.execute(f'UPDATE "{dataset}" SET {set_clause} WHERE _row_id = ?', values)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

    return {"status": "updated", "_row_id": row_id}


# ── Delete Row ────────────────────────────────────────────────────────────────
@app.delete("/datasets/{dataset}/rows/{row_id}", summary="Delete a row by _row_id")
def delete_row(dataset: str, row_id: int):
    validate_identifier(dataset)
    conn = get_conn()
    try:
        datasets = get_all_datasets(conn)
        if dataset not in datasets:
            raise HTTPException(404, f"Dataset '{dataset}' not found")

        existing = conn.execute(
            f'SELECT _row_id FROM "{dataset}" WHERE _row_id = ?', [row_id]
        ).fetchone()
        if not existing:
            raise HTTPException(404, f"Row {row_id} not found")

        conn.execute(f'DELETE FROM "{dataset}" WHERE _row_id = ?', [row_id])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

    return {"status": "deleted", "_row_id": row_id}


# ── Delete Dataset ────────────────────────────────────────────────────────────
@app.delete("/datasets/{dataset}", summary="Drop an entire dataset")
def delete_dataset(dataset: str):
    validate_identifier(dataset)
    conn = get_conn()
    try:
        datasets = get_all_datasets(conn)
        if dataset not in datasets:
            raise HTTPException(404, f"Dataset '{dataset}' not found")

        # Wrap both statements in a transaction so they are atomic
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{dataset}"')
            conn.execute("DELETE FROM _byod_meta WHERE dataset_name = ?", [dataset])
            # Drop the associated sequence if present
            conn.execute(f'DROP SEQUENCE IF EXISTS "_seq_{dataset}"')
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

    return {"status": "deleted", "dataset": dataset}


# ── SQL Query ─────────────────────────────────────────────────────────────────
class QueryBody(BaseModel):
    sql: str

FORBIDDEN = [
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "COPY",
    "ATTACH",
    "DETACH",
    "CALL",
    "PRAGMA"
]


@app.post("/query", summary="Execute a read-only SQL query across datasets")
def run_query(request: Request, body: QueryBody):
    sql = body.sql.strip()
    
    if any(keyword in sql.upper() for keyword in FORBIDDEN):
        raise HTTPException(400, "Forbidden SQL keywords detected")

    if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
        raise HTTPException(400, "Only SELECT statements are allowed")

    conn = get_conn()
    try:
        result = conn.execute(sql).fetchdf()
    except Exception as e:
        raise HTTPException(400, f"Query error: {str(e)}")

    return {
        "rows": result.to_dict(orient="records"),
        "count": len(result),
        "columns": list(result.columns),
    }


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"service": "BYOD API", "docs": "/docs", "version": "1.0.0"}
