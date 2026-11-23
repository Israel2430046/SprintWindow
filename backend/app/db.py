"""Persistencia en SQLite (archivo local, sin servidor de BD)."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from .despiece import DEFAULT_HOLGURAS

DATA_DIR = os.environ.get("NESTING_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "data"))
DATA_DIR = os.path.abspath(DATA_DIR)

DEFAULT_SETTINGS = {
    "proceso": {
        "kerf_mm": 3.0,                 # RN-03
        "refilado_mm": 10.0,            # margen perimetral
        "retazo_min_largo_mm": 300.0,   # RN-06 L_min
        "retazo_min_ancho_mm": 150.0,   # RN-06 A_min
        "modo_corte": "guillotina",     # RN-05: guillotina | libre
        "ventana_tolerancia": 2,        # RN-04 (posiciones de cola)
    },
    "pesos": {
        "w1_merma": 1.0,
        "w2_cola": 0.05,                # peso de prioridad de cola vs. ahorro de material
        "w3_retazo": 0.5,
        "w_pendientes": 10.0,
        "peso_urgente": 3.0,
        "peso_normal": 1.0,
    },
    "ga": {
        "poblacion": 40,
        "generaciones_max": 400,
        "estancamiento": 80,
        "tiempo_max_s": 25.0,
        "cruce": "OX",
        "prob_cruce": 0.9,
        "prob_mutacion": 0.35,
        "elitismo": 0.05,
        "torneo_k": 3,
        "semilla": None,
    },
    "holguras": DEFAULT_HOLGURAS,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    height REAL NOT NULL,
    width REAL NOT NULL,
    thickness REAL NOT NULL,
    color TEXT NOT NULL,
    texture TEXT NOT NULL,
    directional INTEGER NOT NULL DEFAULT 0,
    quantity INTEGER NOT NULL DEFAULT 0,
    unit_cost REAL,
    is_offcut INTEGER NOT NULL DEFAULT 0,
    origin_run TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT NOT NULL UNIQUE,
    client TEXT,
    blind_type TEXT NOT NULL,
    width REAL,
    height REAL,
    quantity INTEGER NOT NULL,
    color TEXT NOT NULL,
    texture TEXT NOT NULL,
    thickness REAL NOT NULL,
    priority TEXT NOT NULL DEFAULT 'Normal',
    queue_pos INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pendiente',
    manual_pieces TEXT,
    cut_pieces TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT
);
"""

_lock = threading.RLock()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DB:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(DATA_DIR, "sprintwindow.db")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        with _lock:
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    # ---------------------------------------------------------------- lotes
    @staticmethod
    def _lot(r) -> dict:
        d = dict(r)
        d["directional"] = bool(d["directional"])
        d["is_offcut"] = bool(d["is_offcut"])
        return d

    def list_lots(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM lots ORDER BY is_offcut DESC, color, texture, code").fetchall()
        return [self._lot(r) for r in rows]

    def get_lot(self, lot_id: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
        return self._lot(r) if r else None

    LOT_FIELDS = ["code", "height", "width", "thickness", "color", "texture", "directional",
                  "quantity", "unit_cost", "is_offcut", "origin_run"]

    def insert_lot(self, data: dict, c=None) -> int:
        vals = [data.get(f) for f in self.LOT_FIELDS]
        vals[6] = int(bool(vals[6]))
        vals[9] = int(bool(vals[9]))
        sql = f"INSERT INTO lots ({','.join(self.LOT_FIELDS)}, created_at) VALUES ({','.join('?' * len(vals))}, ?)"
        if c is not None:
            return c.execute(sql, vals + [now()]).lastrowid
        with self.conn() as cc:
            return cc.execute(sql, vals + [now()]).lastrowid

    def insert_lots(self, items: list[dict]) -> int:
        with self.conn() as c:
            for it in items:
                self.insert_lot(it, c)
        return len(items)

    def update_lot(self, lot_id: int, data: dict) -> None:
        fields = [f for f in self.LOT_FIELDS if f in data]
        vals = [int(bool(data[f])) if f in ("directional", "is_offcut") else data[f] for f in fields]
        with self.conn() as c:
            c.execute(f"UPDATE lots SET {','.join(f + '=?' for f in fields)} WHERE id=?", vals + [lot_id])

    def delete_lot(self, lot_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM lots WHERE id=?", (lot_id,))

    def code_exists(self, code: str, exclude_id: int | None = None) -> bool:
        with self.conn() as c:
            r = c.execute("SELECT id FROM lots WHERE code=? AND id IS NOT ?", (code, exclude_id)).fetchone()
        return r is not None

    # -------------------------------------------------------------- órdenes
    @staticmethod
    def _order(r) -> dict:
        d = dict(r)
        d["manual_pieces"] = json.loads(d["manual_pieces"]) if d["manual_pieces"] else None
        d["cut_pieces"] = json.loads(d["cut_pieces"]) if d["cut_pieces"] else {}
        return d

    def list_orders(self, active_only: bool = False) -> list[dict]:
        q = "SELECT * FROM orders"
        if active_only:
            q += " WHERE status IN ('pendiente','parcial')"
        with self.conn() as c:
            rows = c.execute(q + " ORDER BY queue_pos, id").fetchall()
        return [self._order(r) for r in rows]

    def get_order(self, oid: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        return self._order(r) if r else None

    ORDER_FIELDS = ["order_number", "client", "blind_type", "width", "height", "quantity", "color",
                    "texture", "thickness", "priority", "manual_pieces", "notes"]

    def _order_vals(self, data: dict, fields: list[str]) -> list:
        return [json.dumps(data[f]) if f == "manual_pieces" and data.get(f) is not None else data.get(f)
                for f in fields]

    def insert_order(self, data: dict, c=None) -> int:
        def _do(cc):
            active = cc.execute("SELECT id, priority FROM orders WHERE status IN ('pendiente','parcial') "
                                "ORDER BY queue_pos, id").fetchall()
            # Las urgentes se insertan después de la última urgente; las normales al final.
            if data.get("priority") == "Urgente":
                pos = sum(1 for r in active if r["priority"] == "Urgente")
            else:
                pos = len(active)
            ids = [r["id"] for r in active]
            fields = self.ORDER_FIELDS
            vals = self._order_vals(data, fields)
            new_id = cc.execute(
                f"INSERT INTO orders ({','.join(fields)}, queue_pos, status, cut_pieces, created_at) "
                f"VALUES ({','.join('?' * len(fields))}, ?, 'pendiente', '{{}}', ?)", vals + [0, now()]).lastrowid
            ids.insert(pos, new_id)
            for i, oid in enumerate(ids):
                cc.execute("UPDATE orders SET queue_pos=? WHERE id=?", (i, oid))
            return new_id
        if c is not None:
            return _do(c)
        with self.conn() as cc:
            return _do(cc)

    def insert_orders(self, items: list[dict]) -> int:
        with self.conn() as c:
            for it in items:
                self.insert_order(it, c)
        return len(items)

    def update_order(self, oid: int, data: dict) -> None:
        fields = [f for f in self.ORDER_FIELDS + ["status", "cut_pieces"] if f in data]
        vals = []
        for f in fields:
            v = data[f]
            if f in ("manual_pieces", "cut_pieces") and v is not None:
                v = json.dumps(v)
            vals.append(v)
        with self.conn() as c:
            c.execute(f"UPDATE orders SET {','.join(f + '=?' for f in fields)} WHERE id=?", vals + [oid])

    def delete_order(self, oid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM orders WHERE id=?", (oid,))
        self._compact_queue()

    def _compact_queue(self) -> None:
        with self.conn() as c:
            rows = c.execute("SELECT id FROM orders ORDER BY CASE WHEN status IN ('pendiente','parcial') "
                             "THEN 0 ELSE 1 END, queue_pos, id").fetchall()
            for i, r in enumerate(rows):
                c.execute("UPDATE orders SET queue_pos=? WHERE id=?", (i, r["id"]))

    def reorder(self, ids: list[int]) -> None:
        with self.conn() as c:
            active = [r["id"] for r in c.execute(
                "SELECT id FROM orders WHERE status IN ('pendiente','parcial') ORDER BY queue_pos, id")]
            if sorted(active) != sorted(ids):
                raise ValueError("La lista debe contener exactamente las órdenes activas de la cola")
            for i, oid in enumerate(ids):
                c.execute("UPDATE orders SET queue_pos=? WHERE id=?", (i, oid))
        self._compact_queue()

    def order_number_exists(self, num: str, exclude_id: int | None = None) -> bool:
        with self.conn() as c:
            return c.execute("SELECT 1 FROM orders WHERE order_number=? AND id IS NOT ?",
                             (num, exclude_id)).fetchone() is not None

    # ---------------------------------------------------------- configuración
    def get_settings(self) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key='config'").fetchone()
        s = copy.deepcopy(DEFAULT_SETTINGS)
        if r:
            stored = json.loads(r["value"])
            for sec, vals in stored.items():
                if isinstance(vals, dict) and isinstance(s.get(sec), dict):
                    if sec == "holguras":
                        for t, hv in vals.items():
                            s[sec].setdefault(t, {}).update(hv)
                    else:
                        s[sec].update(vals)
        return s

    def save_settings(self, s: dict) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO settings(key,value) VALUES('config',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(s),))

    # --------------------------------------------------------------- corridas
    def next_run_id(self) -> str:
        day = datetime.now().strftime("%Y%m%d")
        with self.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM runs WHERE id LIKE ?", (f"R-{day}-%",)).fetchone()[0]
        return f"R-{day}-{n + 1:03d}"

    def create_run(self, run_id: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO runs(id, created_at, status) VALUES(?,?,?)", (run_id, now(), "ejecutando"))

    def update_run(self, run_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
        with self.conn() as c:
            c.execute("UPDATE runs SET status=?, result=COALESCE(?, result), error=? WHERE id=?",
                      (status, json.dumps(result) if result is not None else None, error, run_id))

    def get_run(self, run_id: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT id, created_at, status, error, result FROM runs "
                             "ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            res = json.loads(d.pop("result")) if d.get("result") else None
            d["metricas"] = res["metricas"] if res else None
            out.append(d)
        return out
