"""RF-06 Explosión de materiales (despiece) por tipo de persiana.

Traduce el tipo y la medida final (ancho x alto de la persiana terminada, en mm)
a las piezas rectangulares que deben cortarse en lámina de PVC, aplicando las
holguras de ensamble configuradas por Ingeniería (editables en Configuración).
Convención: ``ancho`` = eje X de la hoja, ``alto`` = eje Y (sentido de la veta).
"""
from __future__ import annotations

import math

BLIND_TYPES = {
    "enrollable": "Persiana enrollable",
    "vertical": "Persiana vertical",
    "panel_japones": "Panel japonés",
    "horizontal": "Persiana horizontal (tablillas)",
    "manual": "Despiece manual / pieza a medida",
}

DEFAULT_HOLGURAS = {
    "enrollable": {
        "descuento_ancho": 30.0,     # mecanismo y soportes laterales
        "extra_alto": 250.0,         # vuelta de enrollado sobre el tubo
        "alto_faldon": 60.0,         # faldón / cubierta del contrapeso
    },
    "vertical": {
        "ancho_lama": 89.0,
        "traslape": 10.0,
        "descuento_alto": 20.0,      # holgura a piso y carro
    },
    "panel_japones": {
        "ancho_max_panel": 700.0,
        "traslape": 50.0,
        "descuento_alto": 15.0,
        "min_paneles": 2,
    },
    "horizontal": {
        "alto_tablilla": 50.0,
        "paso": 42.0,                # separación entre tablillas
        "descuento_ancho": 10.0,
    },
}


class DespieceError(ValueError):
    pass


def _h(holguras: dict | None, tipo: str) -> dict:
    base = dict(DEFAULT_HOLGURAS.get(tipo, {}))
    if holguras and tipo in holguras:
        base.update({k: v for k, v in holguras[tipo].items() if v is not None})
    return base


def explode(blind_type: str, width: float | None, height: float | None, quantity: int,
            manual_pieces: list | None = None, holguras: dict | None = None) -> list[dict]:
    """Devuelve una lista de piezas ``{key, nombre, ancho, alto, cantidad}``."""
    if blind_type not in BLIND_TYPES:
        raise DespieceError(f"Tipo de persiana desconocido: {blind_type}")
    q = int(quantity or 0)
    if q <= 0:
        raise DespieceError("La cantidad debe ser mayor a cero")

    if blind_type == "manual":
        out = []
        for i, mp in enumerate(manual_pieces or []):
            w, h, c = float(mp.get("ancho", 0)), float(mp.get("alto", 0)), int(mp.get("cantidad", 1))
            if w <= 0 or h <= 0 or c <= 0:
                raise DespieceError(f"Pieza manual #{i + 1} con dimensiones o cantidad inválidas")
            out.append({"key": f"M{i + 1}", "nombre": mp.get("nombre") or f"Pieza {i + 1}",
                        "ancho": round(w, 1), "alto": round(h, 1), "cantidad": c * q})
        if not out:
            raise DespieceError("El despiece manual requiere al menos una pieza")
        return out

    W, H = float(width or 0), float(height or 0)
    if W <= 0 or H <= 0:
        raise DespieceError("Ancho y alto de la persiana deben ser mayores a cero")
    hg = _h(holguras, blind_type)

    if blind_type == "enrollable":
        aw = W - hg["descuento_ancho"]
        if aw <= 0:
            raise DespieceError("Ancho insuficiente para el descuento de mecanismo")
        return [
            {"key": "LIENZO", "nombre": "Lienzo", "ancho": round(aw, 1),
             "alto": round(H + hg["extra_alto"], 1), "cantidad": q},
            {"key": "FALDON", "nombre": "Faldón", "ancho": round(aw, 1),
             "alto": round(hg["alto_faldon"], 1), "cantidad": q},
        ]

    if blind_type == "vertical":
        util = hg["ancho_lama"] - hg["traslape"]
        if util <= 0:
            raise DespieceError("El traslape debe ser menor al ancho de lama")
        n = max(1, math.ceil(W / util))
        return [{"key": "LAMA", "nombre": "Lama", "ancho": round(hg["ancho_lama"], 1),
                 "alto": round(H - hg["descuento_alto"], 1), "cantidad": n * q}]

    if blind_type == "panel_japones":
        t, mx = hg["traslape"], hg["ancho_max_panel"]
        if mx <= t:
            raise DespieceError("El ancho máximo de panel debe superar el traslape")
        n = max(int(hg["min_paneles"]), math.ceil((W - t) / (mx - t)))
        pw = (W + (n - 1) * t) / n
        return [{"key": "PANEL", "nombre": "Panel", "ancho": round(pw, 1),
                 "alto": round(H - hg["descuento_alto"], 1), "cantidad": n * q}]

    if blind_type == "horizontal":
        n = max(1, math.ceil(H / hg["paso"]))
        aw = W - hg["descuento_ancho"]
        if aw <= 0:
            raise DespieceError("Ancho insuficiente")
        return [{"key": "TABLILLA", "nombre": "Tablilla", "ancho": round(aw, 1),
                 "alto": round(hg["alto_tablilla"], 1), "cantidad": n * q}]

    raise DespieceError(blind_type)  # pragma: no cover
