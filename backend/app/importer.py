"""RF-02: carga masiva desde .csv / .xlsx con validación previa (todo o nada)."""
from __future__ import annotations

import csv
import io
import unicodedata

from .despiece import BLIND_TYPES

LOT_ALIASES = {
    "code": ["codigo_lote", "codigo", "lote", "id_lote", "id", "code"],
    "height": ["alto_mm", "alto", "largo_mm", "largo", "height"],
    "width": ["ancho_mm", "ancho", "width"],
    "thickness": ["espesor_mm", "espesor", "calibre", "calibre_mm", "thickness"],
    "color": ["color"],
    "texture": ["textura", "texture", "acabado"],
    "directional": ["direccionalidad_veta", "veta", "direccional", "veta_direccional", "directional"],
    "quantity": ["cantidad", "cantidad_disponible", "qty", "quantity"],
    "unit_cost": ["costo_unitario", "costo", "unit_cost", "precio"],
    "is_offcut": ["es_retazo", "retazo", "is_offcut"],
}
LOT_REQUIRED = ["code", "height", "width", "thickness", "color", "texture", "quantity"]

ORDER_ALIASES = {
    "order_number": ["numero_orden", "num_orden", "orden", "no_orden", "order_number", "id_orden"],
    "client": ["cliente", "cliente_lote", "client"],
    "blind_type": ["tipo_persiana", "tipo", "blind_type"],
    "width": ["ancho_mm", "ancho", "width"],
    "height": ["alto_mm", "alto", "height"],
    "quantity": ["cantidad", "piezas", "quantity"],
    "color": ["color"],
    "texture": ["textura", "texture"],
    "thickness": ["espesor_mm", "espesor", "calibre", "thickness"],
    "priority": ["prioridad", "priority"],
}
ORDER_REQUIRED = ["order_number", "blind_type", "width", "height", "quantity", "color", "texture", "thickness"]

TRUE = {"1", "si", "sí", "s", "true", "verdadero", "x", "yes", "y"}
FALSE = {"0", "no", "n", "false", "falso", ""}


def _key(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return "_".join(s.strip().lower().replace("(", " ").replace(")", " ").replace("-", " ").split())


def read_table(filename: str, content: bytes) -> list[dict]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        header = [_key(h) for h in rows[0]]
        out = []
        for r in rows[1:]:
            if r is None or all(v in (None, "") for v in r):
                continue
            out.append({header[i]: ("" if v is None else v) for i, v in enumerate(r) if i < len(header)})
        return out
    if name.endswith(".csv") or name.endswith(".txt"):
        text = content.decode("utf-8-sig", errors="replace")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        out = []
        for r in reader:
            if all((v or "").strip() == "" for v in r.values() if isinstance(v, str)):
                continue
            out.append({_key(k): (v.strip() if isinstance(v, str) else v) for k, v in r.items() if k})
        return out
    raise ValueError("Formato no soportado: use .csv o .xlsx")


def _map(row: dict, aliases: dict) -> dict:
    out = {}
    for field, names in aliases.items():
        for n in names:
            if n in row and row[n] not in (None, ""):
                out[field] = row[n]
                break
    return out


def _num(v, field, errs, positive=True, integer=False):
    try:
        x = float(str(v).replace(",", ".")) if not isinstance(v, (int, float)) else float(v)
    except (TypeError, ValueError):
        errs.append(f"'{field}' no es numérico ({v!r})")
        return None
    if integer and abs(x - round(x)) > 1e-9:
        errs.append(f"'{field}' debe ser entero ({v!r})")
        return None
    if positive and x <= 0:
        errs.append(f"'{field}' debe ser mayor a cero")
        return None
    if x < 0:
        errs.append(f"'{field}' no puede ser negativo")
        return None
    return int(round(x)) if integer else x


def _bool(v, field, errs):
    s = _key(v) if not isinstance(v, bool) else ("si" if v else "no")
    if s in {_key(t) for t in TRUE}:
        return True
    if s in FALSE:
        return False
    errs.append(f"'{field}' debe ser Sí/No ({v!r})")
    return None


def validate_lots(rows: list[dict], existing_codes: set[str]) -> tuple[list[dict], list[str]]:
    items, errors, seen = [], [], set()
    if not rows:
        return [], ["El archivo no contiene filas de datos"]
    for i, raw in enumerate(rows, start=2):
        r = _map(raw, LOT_ALIASES)
        errs: list[str] = []
        missing = [f for f in LOT_REQUIRED if f not in r]
        if missing:
            errs.append("faltan columnas/valores: " + ", ".join(LOT_ALIASES[m][0] for m in missing))
        else:
            it = {
                "code": str(r["code"]).strip(),
                "height": _num(r["height"], "alto_mm", errs),
                "width": _num(r["width"], "ancho_mm", errs),
                "thickness": _num(r["thickness"], "espesor_mm", errs),
                "color": str(r["color"]).strip(),
                "texture": str(r["texture"]).strip(),
                "directional": _bool(r.get("directional", "no"), "direccionalidad_veta", errs),
                "quantity": _num(r["quantity"], "cantidad", errs, positive=False, integer=True),
                "unit_cost": (_num(r["unit_cost"], "costo_unitario", errs, positive=False)
                              if "unit_cost" in r else None),
                "is_offcut": _bool(r.get("is_offcut", "no"), "es_retazo", errs),
                "origin_run": None,
            }
            if it["code"] in seen:
                errs.append(f"código de lote duplicado en el archivo ({it['code']})")
            elif it["code"] in existing_codes:
                errs.append(f"el código de lote ya existe en inventario ({it['code']})")
            seen.add(it["code"])
            if not errs:
                items.append(it)
        if errs:
            errors.append(f"Fila {i}: " + "; ".join(errs))
    return (items if not errors else []), errors


def _blind(v) -> str | None:
    k = _key(v)
    for code, label in BLIND_TYPES.items():
        if k in (code, _key(label)):
            return code
    aliases = {"japones": "panel_japones", "panel": "panel_japones", "roller": "enrollable",
               "enrollable": "enrollable", "vertical": "vertical", "horizontal": "horizontal"}
    return aliases.get(k)


def validate_orders(rows: list[dict], existing_numbers: set[str]) -> tuple[list[dict], list[str]]:
    items, errors, seen = [], [], set()
    if not rows:
        return [], ["El archivo no contiene filas de datos"]
    for i, raw in enumerate(rows, start=2):
        r = _map(raw, ORDER_ALIASES)
        errs: list[str] = []
        missing = [f for f in ORDER_REQUIRED if f not in r]
        if missing:
            errs.append("faltan columnas/valores: " + ", ".join(ORDER_ALIASES[m][0] for m in missing))
        else:
            bt = _blind(r["blind_type"])
            if not bt or bt == "manual":
                errs.append(f"tipo de persiana no válido ({r['blind_type']!r}); use: "
                            + ", ".join(k for k in BLIND_TYPES if k != "manual"))
            pr = str(r.get("priority", "Normal")).strip().capitalize() or "Normal"
            if pr not in ("Normal", "Urgente"):
                errs.append(f"prioridad no válida ({pr}); use Normal o Urgente")
            it = {
                "order_number": str(r["order_number"]).strip(), "client": str(r.get("client", "")).strip(),
                "blind_type": bt, "width": _num(r["width"], "ancho_mm", errs),
                "height": _num(r["height"], "alto_mm", errs),
                "quantity": _num(r["quantity"], "cantidad", errs, integer=True),
                "color": str(r["color"]).strip(), "texture": str(r["texture"]).strip(),
                "thickness": _num(r["thickness"], "espesor_mm", errs), "priority": pr,
                "manual_pieces": None, "notes": None,
            }
            if it["order_number"] in seen or it["order_number"] in existing_numbers:
                errs.append(f"número de orden duplicado ({it['order_number']})")
            seen.add(it["order_number"])
            if not errs:
                items.append(it)
        if errs:
            errors.append(f"Fila {i}: " + "; ".join(errs))
    return (items if not errors else []), errors


LOT_TEMPLATE = ("codigo_lote,alto_mm,ancho_mm,espesor_mm,color,textura,direccionalidad_veta,cantidad,"
                "costo_unitario,es_retazo\n"
                "PVC-BL-122x244,2440,1220,3,Blanco,Liso,No,20,850,No\n"
                "PVC-NG-122x244,2440,1220,3,Nogal,Veta madera,Si,10,1100,No\n")
ORDER_TEMPLATE = ("numero_orden,cliente,tipo_persiana,ancho_mm,alto_mm,cantidad,color,textura,espesor_mm,"
                  "prioridad\n"
                  "OP-1001,Constructora Norte,vertical,1800,2100,2,Blanco,Liso,3,Normal\n"
                  "OP-1002,Hotel Plaza,panel_japones,2400,2300,1,Nogal,Veta madera,3,Urgente\n")
