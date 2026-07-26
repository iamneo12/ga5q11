"""
Persistent storage for incident agent runs.
Uses SQLite so state survives process restarts (mount a persistent volume
in production, e.g. Fly.io volumes).
"""
import sqlite3
import json
import os
import threading

DB_PATH = os.environ.get("INCIDENT_DB_PATH", "incidents.db")
_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            state TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS receipts (
            receipt_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            result TEXT NOT NULL
        )"""
    )
    return conn


def get_run(run_id: str):
    with _lock:
        conn = _conn()
        row = conn.execute(
            "SELECT request_hash, state FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {"request_hash": row[0], "state": json.loads(row[1])}


def save_run(run_id: str, request_hash: str, state: dict):
    with _lock:
        conn = _conn()
        conn.execute(
            """INSERT INTO runs (run_id, request_hash, state) VALUES (?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET state=excluded.state""",
            (run_id, request_hash, json.dumps(state)),
        )
        conn.commit()
        conn.close()


def get_receipt(receipt_id: str):
    with _lock:
        conn = _conn()
        row = conn.execute(
            "SELECT run_id, request_hash, result FROM receipts WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {"run_id": row[0], "request_hash": row[1], "result": json.loads(row[2])}


def save_receipt(receipt_id: str, run_id: str, request_hash: str, result: dict):
    with _lock:
        conn = _conn()
        conn.execute(
            "INSERT INTO receipts (receipt_id, run_id, request_hash, result) VALUES (?, ?, ?, ?)",
            (receipt_id, run_id, request_hash, json.dumps(result)),
        )
        conn.commit()
        conn.close()
