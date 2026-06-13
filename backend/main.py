from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
import duckdb
import pandas as pd
import io
import re
import os
import json
from typing import Optional, List, Any, Dict
from pydantic import BaseModel


# Every method below is registered onto this FastAPI application object.
app = FastAPI(title="BYOD - Bring Your Own Data", version="1.0.0")


# Allow CORS from calling an API on a different port or domain. None production only.
# (e.g., frontend running on localhost:3000 calling backend on localhost:8000).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Fetch environment variable for database path. Default value = /data/byod.duckdb
DB_PATH = os.environ.get("DB_PATH", "/data/byod.duckdb")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def get_conn():
    return duckdb.connect(DB_PATH)


# ─── Utils ───────────────────────────────────────────────────────────────────

def sanitize_name(name: str) -> str:
    """Clean up table/column names for DuckDB."""
    name = re.sub(r"[^\w]", "_", name.strip())
    if name and name[0].isdigit():
        name = "_" + name
    return name.lower() or "unnamed"


def get_all_datasets(conn) -> List[str]:
    """List all user datasets (tables) in the database, excluding internal metadata tables."""
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


# ─── Upload ──────────────────────────────────────────────────────────────────

@app.post("/datasets/upload", summary="Upload one or more CSV/XLSX files")
async def upload_files(files: List[UploadFile] = File(...)):
    results = []
    conn = get_conn()
    ensure_meta_table(conn)

    for file in files:
        original_name = file.filename or "upload"
        ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else "csv"
        base_name = original_name.rsplit(".", 1)[0]
        dataset_name = sanitize_name(base_name)

        contents = await file.read()

        try:
            if ext in ("xlsx", "xls"):
                df = pd.read_excel(io.BytesIO(contents))
            else:
                df = pd.read_csv(io.BytesIO(contents))
        except Exception as e:
            results.append({"file": original_name, "status": "error", "detail": str(e)})
            continue

        # Sanitize column names
        df.columns = [sanitize_name(c) for c in df.columns]

        # Deduplicate column names
        seen = {}
        new_cols = []
        for col in df.columns:
            if col in seen:
                seen[col] += 1
                new_cols.append(f"{col}_{seen[col]}")
            else:
                seen[col] = 0
                new_cols.append(col)
        df.columns = new_cols

        # Add internal row id
        df.insert(0, "_row_id", range(1, len(df) + 1))

        try:
            conn.execute(f'DROP TABLE IF EXISTS "{dataset_name}"')
            conn.execute(f'CREATE TABLE "{dataset_name}" AS SELECT * FROM df')
            conn.execute("""
                INSERT OR REPLACE INTO _byod_meta (dataset_name, original_filename, row_count)
                VALUES (?, ?, ?)
            """, [dataset_name, original_name, len(df)])
            results.append({
                "file": original_name,
                "status": "ok",
                "dataset": dataset_name,
                "rows": len(df),
                "columns": list(df.columns),
            })
        except Exception as e:
            results.append({"file": original_name, "status": "error", "detail": str(e)})

    conn.close()
    return {"uploads": results}


# ─── Dataset List ─────────────────────────────────────────────────────────────

@app.get("/datasets", summary="List all datasets")
def list_datasets():
    conn = get_conn()
    ensure_meta_table(conn)
    datasets = get_all_datasets(conn)
    meta = {}
    try:
        rows = conn.execute("SELECT dataset_name, original_filename, row_count, created_at FROM _byod_meta").fetchall()
        for r in rows:
            meta[r[0]] = {"original_filename": r[1], "row_count": r[2], "created_at": str(r[3])}
    except Exception:
        pass
    conn.close()
    result = []
    for ds in datasets:
        entry = {"name": ds}
        if ds in meta:
            entry.update(meta[ds])
        result.append(entry)
    return {"datasets": result}


# ─── Schema ───────────────────────────────────────────────────────────────────

@app.get("/datasets/{dataset}/schema", summary="Get dataset schema")
def get_schema(dataset: str):
    conn = get_conn()
    datasets = get_all_datasets(conn)
    if dataset not in datasets:
        conn.close()
        raise HTTPException(404, f"Dataset '{dataset}' not found")
    cols = conn.execute(f'PRAGMA table_info("{dataset}")').fetchall()
    conn.close()
    columns = []
    for c in cols:
        columns.append({"name": c[1], "type": c[2], "nullable": not c[3]})
    return {"dataset": dataset, "columns": columns}


# ─── Browse (Read) ────────────────────────────────────────────────────────────

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
    conn = get_conn()
    datasets = get_all_datasets(conn)
    if dataset not in datasets:
        conn.close()
        raise HTTPException(404, f"Dataset '{dataset}' not found")

    where_clause = ""
    params = []
    if filter_col and filter_val is not None:
        # Validate column exists
        cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{dataset}")').fetchall()]
        if filter_col not in cols:
            conn.close()
            raise HTTPException(400, f"Column '{filter_col}' not found")
        where_clause = f'WHERE CAST("{filter_col}" AS VARCHAR) ILIKE ?'
        params.append(f"%{filter_val}%")

    order_clause = ""
    if sort_col:
        cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{dataset}")').fetchall()]
        if sort_col not in cols:
            conn.close()
            raise HTTPException(400, f"Column '{sort_col}' not found")
        order_clause = f'ORDER BY "{sort_col}" {sort_dir.upper()}'

    total = conn.execute(f'SELECT COUNT(*) FROM "{dataset}" {where_clause}', params).fetchone()[0]
    offset = (page - 1) * page_size

    rows = conn.execute(
        f'SELECT * FROM "{dataset}" {where_clause} {order_clause} LIMIT {page_size} OFFSET {offset}',
        params
    ).fetchdf()

    conn.close()
    return {
        "dataset": dataset,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if total > 0 else 1,
        "rows": rows.to_dict(orient="records"),
    }


# ─── Insert ───────────────────────────────────────────────────────────────────

class RowData(BaseModel):
    data: Dict[str, Any]


@app.post("/datasets/{dataset}/rows", summary="Insert a new row")
def insert_row(dataset: str, body: RowData):
    conn = get_conn()
    datasets = get_all_datasets(conn)
    if dataset not in datasets:
        conn.close()
        raise HTTPException(404, f"Dataset '{dataset}' not found")

    cols = conn.execute(f'PRAGMA table_info("{dataset}")').fetchall()
    col_names = [c[1] for c in cols]

    # Auto-assign _row_id
    max_id = conn.execute(f'SELECT MAX(_row_id) FROM "{dataset}"').fetchone()[0] or 0
    new_id = int(max_id) + 1

    row = {"_row_id": new_id}
    for col in col_names:
        if col == "_row_id":
            continue
        row[col] = body.data.get(col, None)

    placeholders = ", ".join(["?" for _ in col_names])
    col_str = ", ".join([f'"{c}"' for c in col_names])
    values = [row.get(c) for c in col_names]

    conn.execute(f'INSERT INTO "{dataset}" ({col_str}) VALUES ({placeholders})', values)
    conn.close()
    return {"status": "inserted", "_row_id": new_id, "row": row}


# ─── Update ───────────────────────────────────────────────────────────────────

@app.put("/datasets/{dataset}/rows/{row_id}", summary="Update a row by _row_id")
def update_row(dataset: str, row_id: int, body: RowData):
    conn = get_conn()
    datasets = get_all_datasets(conn)
    if dataset not in datasets:
        conn.close()
        raise HTTPException(404, f"Dataset '{dataset}' not found")

    existing = conn.execute(f'SELECT * FROM "{dataset}" WHERE _row_id = ?', [row_id]).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, f"Row {row_id} not found")

    updates = {k: v for k, v in body.data.items() if k != "_row_id"}
    if not updates:
        conn.close()
        raise HTTPException(400, "No fields to update")

    set_clause = ", ".join([f'"{k}" = ?' for k in updates])
    values = list(updates.values()) + [row_id]
    conn.execute(f'UPDATE "{dataset}" SET {set_clause} WHERE _row_id = ?', values)
    conn.close()
    return {"status": "updated", "_row_id": row_id}


# ─── Delete ───────────────────────────────────────────────────────────────────

@app.delete("/datasets/{dataset}/rows/{row_id}", summary="Delete a row by _row_id")
def delete_row(dataset: str, row_id: int):
    conn = get_conn()
    datasets = get_all_datasets(conn)
    if dataset not in datasets:
        conn.close()
        raise HTTPException(404, f"Dataset '{dataset}' not found")

    existing = conn.execute(f'SELECT _row_id FROM "{dataset}" WHERE _row_id = ?', [row_id]).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, f"Row {row_id} not found")

    conn.execute(f'DELETE FROM "{dataset}" WHERE _row_id = ?', [row_id])
    conn.close()
    return {"status": "deleted", "_row_id": row_id}


# ─── Delete Dataset ───────────────────────────────────────────────────────────

@app.delete("/datasets/{dataset}", summary="Drop an entire dataset")
def delete_dataset(dataset: str):
    conn = get_conn()
    datasets = get_all_datasets(conn)
    if dataset not in datasets:
        conn.close()
        raise HTTPException(404, f"Dataset '{dataset}' not found")
    conn.execute(f'DROP TABLE IF EXISTS "{dataset}"')
    conn.execute("DELETE FROM _byod_meta WHERE dataset_name = ?", [dataset])
    conn.close()
    return {"status": "deleted", "dataset": dataset}


# ─── SQL Query ────────────────────────────────────────────────────────────────

class QueryBody(BaseModel):
    sql: str


@app.post("/query", summary="Execute a read-only SQL query across datasets")
def run_query(body: QueryBody):
    sql = body.sql.strip()
    # Basic safety: only allow SELECT statements
    if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
        raise HTTPException(400, "Only SELECT statements are allowed")

    conn = get_conn()
    try:
        result = conn.execute(sql).fetchdf()
        conn.close()
        return {
            "rows": result.to_dict(orient="records"),
            "count": len(result),
            "columns": list(result.columns),
        }
    except Exception as e:
        conn.close()
        raise HTTPException(400, f"Query error: {str(e)}")


# ─── Export ───────────────────────────────────────────────────────────────────

@app.get("/datasets/{dataset}/export", summary="Export dataset as CSV")
def export_csv(dataset: str):
    conn = get_conn()
    datasets = get_all_datasets(conn)
    if dataset not in datasets:
        conn.close()
        raise HTTPException(404, f"Dataset '{dataset}' not found")

    df = conn.execute(f'SELECT * EXCLUDE (_row_id) FROM "{dataset}"').fetchdf()
    conn.close()

    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{dataset}.csv"'}
    )


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"service": "BYOD API", "docs": "/docs", "version": "1.0.0"}
