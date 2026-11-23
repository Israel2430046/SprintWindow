"""API REST + servidor del frontend.

Ejecutar:  uvicorn app.main:app --port 8000   (desde la carpeta backend)
"""
from __future__ import annotations

import os
import threading
import traceback
from collections import defaultdict
from typing import Literal, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import importer
from .db import DATA_DIR, DB, now
from .despiece import BLIND_TYPES, DEFAULT_HOLGURAS, DespieceError, explode
from .engine.optimizer import optimize, order_pieces
from .render import build_zip, global_order_index, render_sheet, sheet_filename

FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend"))

app = FastAPI(title="SprintWindow · Optimización de Corte 2D", version="1.0")
db = DB()

_progress: dict[str, dict] = {}
_cancel: set[str] = set()
_run_lock = threading.Lock()


# ================================================================== esquemas
class LotIn(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    height: float = Field(gt=0, description="Alto (mm), sentido de la veta")
    width: float = Field(gt=0, description="Ancho (mm)")
    thickness: float = Field(gt=0)
    color: str = Field(min_length=1)
    texture: str = Field(min_length=1)
    directional: bool = False
    quantity: int = Field(ge=0)
    unit_cost: Optional[float] = Field(default=None, ge=0)
    is_offcut: bool = False

    @field_validator("code", "color", "texture")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("no puede estar vacío")
        return v


class ManualPiece(BaseModel):
    nombre: str = ""
    ancho: float = Field(gt=0)
    alto: float = Field(gt=0)
    cantidad: int = Field(gt=0)


class OrderIn(BaseModel):
    order_number: str = Field(min_length=1, max_length=40)
    client: str = ""
    blind_type: str
    width: Optional[float] = None
    height: Optional[float] = None
    quantity: int = Field(gt=0)
    color: str = Field(min_length=1)
    texture: str = Field(min_length=1)
    thickness: float = Field(gt=0)
    priority: Literal["Normal", "Urgente"] = "Normal"
    manual_pieces: Optional[list[ManualPiece]] = None
    notes: Optional[str] = None

    @field_validator("blind_type")
    @classmethod
    def _bt(cls, v: str) -> str:
        if v not in BLIND_TYPES:
            raise ValueError(f"tipo de persiana no válido: {v}")
        return v


class ReorderIn(BaseModel):
    ids: list[int]


class DespieceIn(BaseModel):
    blind_type: str
    width: Optional[float] = None
    height: Optional[float] = None
    quantity: int = 1
    manual_pieces: Optional[list[ManualPiece]] = None


# ================================================================ utilidades
def _err(status: int, msg):
    raise HTTPException(status_code=status, detail=msg)


def _order_dict(o: OrderIn) -> dict:
    d = o.model_dump()
    if d.get("manual_pieces") is not None:
        d["manual_pieces"] = [dict(mp) for mp in d["manual_pieces"]]
    if d["blind_type"] != "manual":
        d["manual_pieces"] = None
    return d


def _validate_despiece(d: dict) -> list[dict]:
    try:
        return explode(d["blind_type"], d.get("width"), d.get("height"), d["quantity"],
                       d.get("manual_pieces"), db.get_settings().get("holguras"))
    except DespieceError as e:
        _err(422, str(e))


def _run_dir(run_id: str) -> str:
    p = os.path.join(DATA_DIR, "runs", run_id)
    os.makedirs(p, exist_ok=True)
    return p


# ================================================================ inventario
@app.get("/api/lots")
def list_lots():
    return db.list_lots()


@app.post("/api/lots", status_code=201)
def create_lot(lot: LotIn):
    if db.code_exists(lot.code):
        _err(409, f"Ya existe un lote con código {lot.code}")
    return {"id": db.insert_lot(lot.model_dump())}


@app.put("/api/lots/{lot_id}")
def update_lot(lot_id: int, lot: LotIn):
    if not db.get_lot(lot_id):
        _err(404, "Lote no encontrado")
    if db.code_exists(lot.code, lot_id):
        _err(409, f"Ya existe un lote con código {lot.code}")
    db.update_lot(lot_id, lot.model_dump())
    return {"ok": True}


@app.delete("/api/lots/{lot_id}")
def delete_lot(lot_id: int):
    db.delete_lot(lot_id)
    return {"ok": True}


@app.post("/api/lots/import")
async def import_lots(file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = importer.read_table(file.filename or "", content)
    except Exception as e:
        _err(422, {"errores": [f"No se pudo leer el archivo: {e}"]})
    items, errors = importer.validate_lots(rows, {l["code"] for l in db.list_lots()})
    if errors:
        _err(422, {"errores": errors})
    return {"importados": db.insert_lots(items)}


@app.get("/api/lots/template.csv")
def lots_template():
    return Response(importer.LOT_TEMPLATE, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=plantilla_inventario.csv"})


# =================================================================== órdenes
@app.get("/api/blind-types")
def blind_types():
    return BLIND_TYPES


@app.get("/api/orders")
def list_orders(all: bool = False):
    orders = db.list_orders(active_only=not all)
    holg = db.get_settings().get("holguras")
    for o in orders:
        try:
            o["despiece"] = explode(o["blind_type"], o["width"], o["height"], o["quantity"],
                                    o["manual_pieces"], holg)
            o["piezas_pendientes"] = sum(p["cantidad"] for p in order_pieces(o, holg))
        except DespieceError as e:
            o["despiece"], o["piezas_pendientes"], o["error"] = [], 0, str(e)
        o["piezas_total"] = sum(p["cantidad"] for p in o["despiece"])
    return orders


@app.post("/api/orders", status_code=201)
def create_order(order: OrderIn):
    d = _order_dict(order)
    if db.order_number_exists(d["order_number"]):
        _err(409, f"Ya existe la orden {d['order_number']}")
    _validate_despiece(d)
    return {"id": db.insert_order(d)}


@app.put("/api/orders/{oid}")
def update_order(oid: int, order: OrderIn):
    cur = db.get_order(oid)
    if not cur:
        _err(404, "Orden no encontrada")
    d = _order_dict(order)
    if db.order_number_exists(d["order_number"], oid):
        _err(409, f"Ya existe la orden {d['order_number']}")
    _validate_despiece(d)
    db.update_order(oid, d)
    return {"ok": True}


@app.delete("/api/orders/{oid}")
def delete_order(oid: int):
    db.delete_order(oid)
    return {"ok": True}


@app.post("/api/orders/reorder")
def reorder(body: ReorderIn):
    try:
        db.reorder(body.ids)
    except ValueError as e:
        _err(422, str(e))
    return {"ok": True}


@app.post("/api/orders/{oid}/move")
def move_order(oid: int, direction: Literal["up", "down", "top", "bottom"]):
    ids = [o["id"] for o in db.list_orders(active_only=True)]
    if oid not in ids:
        _err(404, "La orden no está en la cola activa")
    i = ids.index(oid)
    ids.pop(i)
    j = {"up": max(0, i - 1), "down": min(len(ids), i + 1), "top": 0, "bottom": len(ids)}[direction]
    ids.insert(j, oid)
    db.reorder(ids)
    return {"ok": True}


@app.post("/api/orders/import")
async def import_orders(file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = importer.read_table(file.filename or "", content)
    except Exception as e:
        _err(422, {"errores": [f"No se pudo leer el archivo: {e}"]})
    items, errors = importer.validate_orders(rows, {o["order_number"] for o in db.list_orders()})
    if not errors:
        holg = db.get_settings().get("holguras")
        for it in items:
            try:
                explode(it["blind_type"], it["width"], it["height"], it["quantity"], None, holg)
            except DespieceError as e:
                errors.append(f"Orden {it['order_number']}: {e}")
    if errors:
        _err(422, {"errores": errors})
    return {"importados": db.insert_orders(items)}


@app.get("/api/orders/template.csv")
def orders_template():
    return Response(importer.ORDER_TEMPLATE, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=plantilla_ordenes.csv"})


@app.post("/api/despiece")
def despiece_preview(body: DespieceIn):
    d = body.model_dump()
    if d.get("manual_pieces") is not None:
        d["manual_pieces"] = [dict(mp) for mp in d["manual_pieces"]]
    return _validate_despiece(d)


# ============================================================= configuración
@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.put("/api/settings")
def put_settings(s: dict):
    cur = db.get_settings()
    errs = []
    for sec in ("proceso", "pesos", "ga", "holguras"):
        if sec in s and isinstance(s[sec], dict):
            if sec == "holguras":
                for t, vals in s[sec].items():
                    if t in cur["holguras"] and isinstance(vals, dict):
                        cur["holguras"][t].update({k: v for k, v in vals.items() if k in DEFAULT_HOLGURAS[t]})
            else:
                cur[sec].update({k: v for k, v in s[sec].items() if k in cur[sec]})
    p, g = cur["proceso"], cur["ga"]
    if p["modo_corte"] not in ("guillotina", "libre"):
        errs.append("modo_corte debe ser 'guillotina' o 'libre'")
    for k in ("kerf_mm", "refilado_mm", "retazo_min_largo_mm", "retazo_min_ancho_mm"):
        try:
            if float(p[k]) < 0:
                errs.append(f"{k} no puede ser negativo")
        except (TypeError, ValueError):
            errs.append(f"{k} debe ser numérico")
    if str(g["cruce"]).upper() not in ("OX", "PMX"):
        errs.append("cruce debe ser OX o PMX")
    for k, lo in (("poblacion", 4), ("generaciones_max", 1), ("estancamiento", 1), ("torneo_k", 2)):
        try:
            if int(g[k]) < lo:
                errs.append(f"{k} debe ser ≥ {lo}")
        except (TypeError, ValueError):
            errs.append(f"{k} debe ser entero")
    try:
        if not (0.5 <= float(g["tiempo_max_s"]) <= 600):
            errs.append("tiempo_max_s debe estar entre 0.5 y 600")
    except (TypeError, ValueError):
        errs.append("tiempo_max_s debe ser numérico")
    if errs:
        _err(422, errs)
    db.save_settings(cur)
    return cur


@app.post("/api/settings/reset")
def reset_settings():
    from .db import DEFAULT_SETTINGS
    db.save_settings(DEFAULT_SETTINGS)
    return db.get_settings()


# ============================================================ optimización
@app.post("/api/plan/preview")
def plan_preview():
    """RF-05: impacto rápido (heurística FIFO, sin AG) tras reordenar la cola."""
    orders = db.list_orders(active_only=True)
    res = optimize(orders, db.list_lots(), db.get_settings(), "PREVIEW", use_ga=False)
    return {"metricas": res["metricas"], "ordenes": res["ordenes"], "advertencias": res["advertencias"]}


def _worker(run_id: str, orders, lots, settings):
    try:
        def prog(info):
            _progress[run_id] = info
        res = optimize(orders, lots, settings, run_id, progress=prog, cancelled=lambda: run_id in _cancel)
        res["created_at"] = now()
        db.update_run(run_id, "simulada", res)
    except Exception as e:  # pragma: no cover
        traceback.print_exc()
        db.update_run(run_id, "error", error=str(e))
    finally:
        _progress.pop(run_id, None)
        _cancel.discard(run_id)


@app.post("/api/runs", status_code=202)
def start_run(sync: bool = False):
    orders = db.list_orders(active_only=True)
    if not orders:
        _err(422, "La cola de órdenes está vacía")
    with _run_lock:
        run_id = db.next_run_id()
        db.create_run(run_id)
    _progress[run_id] = {"avance": 0.0}
    args = (run_id, orders, db.list_lots(), db.get_settings())
    if sync:
        _worker(*args)
    else:
        threading.Thread(target=_worker, args=args, daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/runs")
def list_runs():
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    r = db.get_run(run_id)
    if not r:
        _err(404, "Corrida no encontrada")
    r["progreso"] = _progress.get(run_id)
    return r


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    _cancel.add(run_id)
    return {"ok": True}


def _png(run: dict, sheet: dict) -> bytes:
    res = run["result"]
    path = os.path.join(_run_dir(run["id"]), sheet_filename(run["id"], sheet))
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    data = render_sheet(sheet, run["id"], global_order_index(res), res.get("created_at"))
    with open(path, "wb") as f:
        f.write(data)
    return data


def _finished(run_id: str) -> dict:
    r = db.get_run(run_id)
    if not r:
        _err(404, "Corrida no encontrada")
    if not r["result"]:
        _err(409, "La corrida aún no tiene resultados")
    return r


@app.get("/api/runs/{run_id}/sheets/{seq}.png")
def sheet_png(run_id: str, seq: int, download: bool = False):
    r = _finished(run_id)
    sheets = {s["seq"]: s for s in r["result"]["hojas"]}
    if seq not in sheets:
        _err(404, "Hoja no encontrada")
    data = _png(r, sheets[seq])
    headers = {}
    if download:
        headers["Content-Disposition"] = f"attachment; filename={sheet_filename(run_id, sheets[seq])}"
    return Response(data, media_type="image/png", headers=headers)


@app.get("/api/runs/{run_id}/zip")
def run_zip(run_id: str):
    r = _finished(run_id)
    data = build_zip(r["result"], lambda s: _png(r, s), r["result"].get("created_at"))
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=planos_{run_id}.zip"})


@app.post("/api/runs/{run_id}/confirm")
def confirm_run(run_id: str):
    """Confirma la orden de corte: descuenta inventario, da de alta retazos
    recuperables (RN-06 / RF-03) y registra las piezas cortadas por orden."""
    r = _finished(run_id)
    if r["status"] != "simulada":
        _err(409, f"Sólo se puede confirmar una corrida simulada (estado actual: {r['status']})")
    res = r["result"]
    lots = {l["id"]: l for l in db.list_lots()}
    need = defaultdict(int)
    for s in res["hojas"]:
        need[s["lot_id"]] += 1
    for lot_id, n in need.items():
        if lot_id not in lots or lots[lot_id]["quantity"] < n:
            _err(409, "El inventario cambió desde la simulación; vuelva a optimizar.")
    cut = defaultdict(lambda: defaultdict(int))
    for s in res["hojas"]:
        for p in s["placements"]:
            cut[p["order_id"]][p["piece_key"]] += 1

    new_offcuts = 0
    holg = db.get_settings().get("holguras")
    with db.conn() as c:
        for lot_id, n in need.items():
            c.execute("UPDATE lots SET quantity = quantity - ? WHERE id=?", (n, lot_id))
        for s in res["hojas"]:
            src = lots[s["lot_id"]]
            for k, o in enumerate(s["offcuts"], start=1):
                cost = None
                if src.get("unit_cost"):
                    cost = round(src["unit_cost"] * (o["w"] * o["h"]) / (src["width"] * src["height"]), 2)
                db.insert_lot({"code": f"RET-{run_id[2:]}-{s['seq']:02d}-{k}", "height": round(o["h"], 1),
                               "width": round(o["w"], 1), "thickness": src["thickness"], "color": src["color"],
                               "texture": src["texture"], "directional": src["directional"], "quantity": 1,
                               "unit_cost": cost, "is_offcut": True, "origin_run": run_id}, c)
                new_offcuts += 1
        c.execute("DELETE FROM lots WHERE quantity <= 0 AND is_offcut = 1")
        c.execute("UPDATE runs SET status='obsoleta' WHERE status='simulada' AND id<>?", (run_id,))
        c.execute("UPDATE runs SET status='confirmada' WHERE id=?", (run_id,))
    for oid, pieces in cut.items():
        o = db.get_order(oid)
        if not o:
            continue
        cp = dict(o["cut_pieces"] or {})
        for k, n in pieces.items():
            cp[k] = cp.get(k, 0) + n
        o["cut_pieces"] = cp
        remaining = sum(p["cantidad"] for p in order_pieces(o, holg))
        db.update_order(oid, {"cut_pieces": cp, "status": "cortada" if remaining == 0 else "parcial"})
    db._compact_queue()
    return {"ok": True, "retazos_registrados": new_offcuts, "hojas_descontadas": sum(need.values())}


@app.post("/api/runs/{run_id}/discard")
def discard_run(run_id: str):
    r = db.get_run(run_id)
    if not r:
        _err(404, "Corrida no encontrada")
    if r["status"] == "confirmada":
        _err(409, "No se puede descartar una corrida confirmada")
    db.update_run(run_id, "descartada")
    return {"ok": True}


# ========================================================== datos de ejemplo
@app.post("/api/demo")
def load_demo():
    """Carga un conjunto de datos de demostración (inventario + cola)."""
    from .demo import DEMO_LOTS, DEMO_ORDERS
    codes = {l["code"] for l in db.list_lots()}
    nums = {o["order_number"] for o in db.list_orders()}
    lots = [l for l in DEMO_LOTS if l["code"] not in codes]
    orders = [o for o in DEMO_ORDERS if o["order_number"] not in nums]
    db.insert_lots(lots)
    db.insert_orders(orders)
    return {"lotes": len(lots), "ordenes": len(orders)}


# ================================================================= frontend
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
