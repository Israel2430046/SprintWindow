"""RNF-04: validación geométrica independiente de cada patrón de corte.

Se ejecuta sobre cada hoja antes de entregar un resultado; si detecta una
violación, la corrida se bloquea (excepción) en lugar de emitir un plano
inválido. Verifica:

* piezas dentro del área neta útil (refilado + kerf respecto al borde),
* separación ≥ kerf entre piezas (sin solapamientos),
* suma de áreas ≤ área neta útil,
* rotación prohibida en hojas con veta direccional (RN-02),
* dimensiones colocadas = dimensiones nominales (o permutadas si rotada),
* corte en guillotina factible cuando el modo es "guillotina" (RN-05).
"""
from __future__ import annotations

TOL = 1e-6


class LayoutError(AssertionError):
    pass


def _is_guillotine(rects: list[tuple], x0: float, y0: float, x1: float, y1: float, depth: int = 0) -> bool:
    if len(rects) <= 1:
        return True
    if depth > 400:
        return False
    for axis in (0, 1):
        # cortes candidatos: bordes finales de cada pieza (los cortes pasan por el kerf)
        edges = sorted({r[axis] + r[axis + 2] for r in rects})
        for c in edges:
            lo = [r for r in rects if r[axis] + r[axis + 2] <= c + TOL]
            hi = [r for r in rects if r[axis] >= c - TOL]
            if lo and hi and len(lo) + len(hi) == len(rects):
                if axis == 0:
                    return (_is_guillotine(lo, x0, y0, c, y1, depth + 1)
                            and _is_guillotine(hi, c, y0, x1, y1, depth + 1))
                return (_is_guillotine(lo, x0, y0, x1, c, depth + 1)
                        and _is_guillotine(hi, x0, c, x1, y1, depth + 1))
    return False


def validate_sheet(sheet: dict) -> list[str]:
    errs: list[str] = []
    W, H, k, t = sheet["w"], sheet["h"], sheet["kerf"], sheet["trim"]
    lo_x, lo_y, hi_x, hi_y = t + k, t + k, W - t - k, H - t - k
    pls = sheet["placements"]
    total = 0.0
    for i, p in enumerate(pls):
        name = f"pieza {i + 1} ({p['order_number']})"
        if p["x"] < lo_x - TOL or p["y"] < lo_y - TOL or p["x"] + p["w"] > hi_x + TOL or p["y"] + p["h"] > hi_y + TOL:
            errs.append(f"{name} fuera del área útil")
        nominal = (p["nominal_w"], p["nominal_h"])
        placed = (p["w"], p["h"])
        if p["rotated"]:
            if sheet["directional"]:
                errs.append(f"{name} rotada en hoja con veta direccional (RN-02)")
            if abs(placed[0] - nominal[1]) > TOL or abs(placed[1] - nominal[0]) > TOL:
                errs.append(f"{name} dimensiones rotadas inconsistentes")
        elif abs(placed[0] - nominal[0]) > TOL or abs(placed[1] - nominal[1]) > TOL:
            errs.append(f"{name} dimensiones inconsistentes")
        total += p["w"] * p["h"]
    for i in range(len(pls)):
        a = pls[i]
        for j in range(i + 1, len(pls)):
            b = pls[j]
            # separación mínima = kerf en al menos un eje
            sep_x = max(b["x"] - (a["x"] + a["w"]), a["x"] - (b["x"] + b["w"]))
            sep_y = max(b["y"] - (a["y"] + a["h"]), a["y"] - (b["y"] + b["h"]))
            if sep_x < k - TOL and sep_y < k - TOL:
                errs.append(f"piezas {i + 1} y {j + 1} solapadas o sin separación de kerf")
    net = max(0.0, (W - 2 * t) * (H - 2 * t))
    if total > net + TOL:
        errs.append("la suma de áreas de piezas excede el área neta útil")
    for o in sheet.get("offcuts", []):
        for i, p in enumerate(pls):
            if (o["x"] < p["x"] + p["w"] - TOL and p["x"] < o["x"] + o["w"] - TOL
                    and o["y"] < p["y"] + p["h"] - TOL and p["y"] < o["y"] + o["h"] - TOL):
                errs.append(f"retazo solapa con pieza {i + 1}")
    if sheet.get("cut_mode") == "guillotina":
        rects = [(p["x"], p["y"], p["w"], p["h"]) for p in pls]
        if not _is_guillotine(rects, 0, 0, W, H):
            errs.append("el patrón no es cortable en guillotina")
    return errs


def assert_valid(sheet: dict) -> None:
    errs = validate_sheet(sheet)
    if errs:
        raise LayoutError(f"Hoja {sheet.get('sheet_id')}: " + "; ".join(errs))
