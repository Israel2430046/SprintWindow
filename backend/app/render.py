"""RF-11 / RF-12 / RNF-02: generación de planos de corte en PNG.

Lienzo A4 horizontal a 300 DPI (3508 x 2480 px, supera el mínimo 1920 x 1080).
Contenido: encabezado (corrida, hoja, W x H, color, textura, veta), hoja a escala
con piezas (borde = contorno real de corte, la separación visible es el kerf),
etiquetas por pieza, color por orden, retazos con rayado verde, merma con
rayado gris, leyenda de órdenes y resumen numérico al pie.
"""
from __future__ import annotations

import io
import math
import os
import zipfile
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

CANVAS_W, CANVAS_H, DPI = 3508, 2480, 300
MARGIN = 70
HEADER_H = 300
FOOTER_H = 230
LEGEND_W = 760

INK = (28, 33, 40)
MUTED = (95, 104, 115)
SHEET_BG = (236, 237, 239)
MERMA_LINE = (150, 154, 160)
RETAZO_BG = (220, 243, 222)
RETAZO_LINE = (46, 160, 67)
GRAIN = (140, 98, 57)

# Paleta por orden (tonos claros con buen contraste para texto oscuro).
ORDER_COLORS = [
    (125, 180, 237), (247, 182, 110), (163, 214, 128), (232, 148, 160), (186, 160, 232),
    (110, 207, 200), (240, 214, 102), (214, 160, 120), (150, 195, 160), (230, 160, 220),
    (160, 175, 205), (250, 150, 120), (130, 200, 235), (200, 210, 110), (190, 190, 190),
]

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF",
             os.path.join(os.path.dirname(__file__), "fonts")]


@lru_cache(maxsize=64)
def font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def order_color(idx: int) -> tuple:
    return ORDER_COLORS[idx % len(ORDER_COLORS)]


def _darker(c, f=0.55):
    return tuple(int(v * f) for v in c)


def _hatch(size, bg, line, spacing=22, width=3, reverse=False):
    w, h = size
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    for s in range(-h, w + h, spacing):
        if reverse:
            d.line([(s, h), (s + h, 0)], fill=line, width=width)
        else:
            d.line([(s, 0), (s + h, h)], fill=line, width=width)
    return img


def _text_size(draw, text, f):
    b = draw.textbbox((0, 0), text, font=f)
    return b[2] - b[0], b[3] - b[1]


def _fit_lines(draw, lines, max_w, max_h, max_size=46, min_size=15):
    """Mayor tamaño de fuente con el que todas las líneas caben."""
    for size in range(max_size, min_size - 1, -2):
        fb, fr = font(size, True), font(size)
        ws, hs = [], []
        for i, ln in enumerate(lines):
            w, h = _text_size(draw, ln, fb if i == 0 else fr)
            ws.append(w)
            hs.append(h)
        gap = int(size * 0.35)
        tot = sum(hs) + gap * (len(lines) - 1)
        if max(ws) <= max_w and tot <= max_h:
            return size, tot, gap
    return None


def _draw_lines_centered(img, box, lines, color=INK, max_size=46, min_size=15):
    """Dibuja etiqueta centrada en ``box``; si no cabe horizontal prueba girada 90°."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0 - 12, y1 - y0 - 12
    draw = ImageDraw.Draw(img)
    fit = _fit_lines(draw, lines, bw, bh, max_size, min_size)
    if fit:
        size, tot, gap = fit
        y = (y0 + y1 - tot) / 2
        for i, ln in enumerate(lines):
            f = font(size, i == 0)
            w, h = _text_size(draw, ln, f)
            draw.text(((x0 + x1 - w) / 2, y - draw.textbbox((0, 0), ln, font=f)[1]), ln, font=f, fill=color)
            y += h + gap
        return True
    fit = _fit_lines(draw, lines, bh, bw, max_size, min_size)   # girada
    if fit:
        size, tot, gap = fit
        tmp = Image.new("RGBA", (int(bh), int(tot) + 8), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        y = 4
        for i, ln in enumerate(lines):
            f = font(size, i == 0)
            w, h = _text_size(td, ln, f)
            td.text(((bh - w) / 2, y - td.textbbox((0, 0), ln, font=f)[1]), ln, font=f, fill=color + (255,))
            y += h + gap
        rot = tmp.rotate(90, expand=True)
        px = int((x0 + x1 - rot.width) / 2)
        py = int((y0 + y1 - rot.height) / 2)
        img.paste(rot, (px, py), rot)
        return True
    return False


def _arrow(draw, x, y0, y1, color, width=8, head=26):
    draw.line([(x, y0 + head), (x, y1 - head)], fill=color, width=width)
    draw.polygon([(x, y0), (x - head * 0.7, y0 + head * 1.2), (x + head * 0.7, y0 + head * 1.2)], fill=color)
    draw.polygon([(x, y1), (x - head * 0.7, y1 - head * 1.2), (x + head * 0.7, y1 - head * 1.2)], fill=color)


def _fmt(v: float) -> str:
    return f"{v:g}" if abs(v - round(v)) > 1e-6 else f"{int(round(v))}"


def render_sheet(sheet: dict, run_id: str, order_index: dict | None = None,
                 created_at: str | None = None) -> bytes:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), "white")
    d = ImageDraw.Draw(img)
    W, H = sheet["w"], sheet["h"]
    m = sheet["metrics"]
    if order_index is None:
        order_index = {}
        for p in sheet["placements"]:
            order_index.setdefault(p["order_id"], len(order_index))

    # ------------------------------------------------------------ encabezado
    d.rectangle([0, 0, CANVAS_W, HEADER_H - 40], fill=(244, 246, 249))
    d.line([(0, HEADER_H - 40), (CANVAS_W, HEADER_H - 40)], fill=(200, 205, 212), width=3)
    d.text((MARGIN, 38), "PLANO DE CORTE · SprintWindow", font=font(58, True), fill=INK)
    d.text((MARGIN, 120), f"Corrida: {run_id}    Hoja: {sheet['sheet_id']}  (secuencia {sheet['seq']})"
           + ("    [RETAZO]" if sheet.get("is_offcut") else ""), font=font(42, True), fill=INK)
    d.text((MARGIN, 185), f"Dimensiones: {_fmt(W)} × {_fmt(H)} mm (W × H)    Color: {sheet['color']}    "
           f"Textura: {sheet['texture']}    Espesor: {_fmt(sheet['thickness'])} mm", font=font(38), fill=INK)
    modo = "Guillotina (borde a borde)" if sheet.get("cut_mode") == "guillotina" else "Anidado libre (CNC)"
    d.text((MARGIN, 236 - 6), f"Kerf: {_fmt(sheet['kerf'])} mm   Refilado: {_fmt(sheet['trim'])} mm   "
           f"Corte: {modo}" + (f"   Generado: {created_at}" if created_at else ""), font=font(30), fill=MUTED)
    # indicador de veta
    gx = CANVAS_W - MARGIN - 520
    if sheet.get("directional"):
        _arrow(d, gx + 40, 40, HEADER_H - 70, GRAIN, width=10, head=28)
        d.text((gx + 100, 70), "VETA DIRECCIONAL", font=font(38, True), fill=GRAIN)
        d.text((gx + 100, 125), "Sentido paralelo al alto (H)", font=font(30), fill=GRAIN)
        d.text((gx + 100, 168), "Piezas SIN rotación (RN-02)", font=font(30), fill=GRAIN)
    else:
        d.text((gx + 20, 80), "ACABADO UNIFORME", font=font(38, True), fill=MUTED)
        d.text((gx + 20, 135), "Sin veta direccional:", font=font(30), fill=MUTED)
        d.text((gx + 20, 178), "rotación 90° permitida", font=font(30), fill=MUTED)

    # ---------------------------------------------------------- zona dibujo
    area_x0, area_y0 = MARGIN + 110, HEADER_H + 70
    area_x1, area_y1 = CANVAS_W - LEGEND_W - MARGIN, CANVAS_H - FOOTER_H - 40
    scale = min((area_x1 - area_x0) / W, (area_y1 - area_y0) / H)
    sw, sh = W * scale, H * scale
    ox, oy = area_x0 + ((area_x1 - area_x0) - sw) / 2, area_y0

    def tx(x):
        return ox + x * scale

    def ty(y):
        return oy + y * scale

    # cotas de la hoja
    d.line([(tx(0), oy - 30), (tx(W), oy - 30)], fill=INK, width=3)
    for xx in (tx(0), tx(W)):
        d.line([(xx, oy - 45), (xx, oy - 15)], fill=INK, width=3)
    lbl = f"W = {_fmt(W)} mm"
    tw, th = _text_size(d, lbl, font(34, True))
    d.rectangle([tx(W / 2) - tw / 2 - 10, oy - 52, tx(W / 2) + tw / 2 + 10, oy - 8], fill="white")
    d.text((tx(W / 2) - tw / 2, oy - 52), lbl, font=font(34, True), fill=INK)
    d.line([(ox - 40, ty(0)), (ox - 40, ty(H))], fill=INK, width=3)
    for yy in (ty(0), ty(H)):
        d.line([(ox - 55, yy), (ox - 25, yy)], fill=INK, width=3)
    vl = Image.new("RGBA", (420, 50), (255, 255, 255, 0))
    ImageDraw.Draw(vl).text((10, 2), f"H = {_fmt(H)} mm", font=font(34, True), fill=INK + (255,))
    vl = vl.rotate(90, expand=True)
    img.paste(vl, (int(ox - 40 - 25 - vl.width + 30), int(ty(H / 2) - vl.height / 2)), vl)

    # fondo de la hoja = merma (rayado gris) — después se cubre con retazos y piezas
    box = (int(tx(0)), int(ty(0)), int(math.ceil(tx(W))), int(math.ceil(ty(H))))
    hatch_g = _hatch((box[2] - box[0], box[3] - box[1]), SHEET_BG, MERMA_LINE, spacing=26, width=2)
    img.paste(hatch_g, box[:2])
    d.rectangle(box, outline=INK, width=5)
    # línea de refilado
    t = sheet["trim"]
    if t > 0:
        d.rectangle([tx(t), ty(t), tx(W - t), ty(H - t)], outline=(120, 120, 120), width=2)

    # retazos recuperables (rayado verde)
    for r in sheet["offcuts"]:
        rb = (int(tx(r["x"])), int(ty(r["y"])), int(tx(r["x"] + r["w"])), int(ty(r["y"] + r["h"])))
        if rb[2] - rb[0] < 2 or rb[3] - rb[1] < 2:
            continue
        img.paste(_hatch((rb[2] - rb[0], rb[3] - rb[1]), RETAZO_BG, RETAZO_LINE, spacing=30, width=3,
                         reverse=True), rb[:2])
        d.rectangle(rb, outline=RETAZO_LINE, width=4)
        lines = ["RETAZO RECUPERABLE", f"{_fmt(r['w'])} × {_fmt(r['h'])} mm"]
        if not _draw_lines_centered(img, rb, lines, color=(20, 100, 35), max_size=36):
            _draw_lines_centered(img, rb, ["RETAZO"], color=(20, 100, 35), max_size=28)

    # piezas
    unlabeled = []
    for i, p in enumerate(sheet["placements"]):
        col = order_color(order_index.get(p["order_id"], 0))
        pb = (tx(p["x"]), ty(p["y"]), tx(p["x"] + p["w"]), ty(p["y"] + p["h"]))
        d.rectangle(pb, fill=col, outline=_darker(col, 0.35), width=3)
        dims = f"{_fmt(p['nominal_w'])} × {_fmt(p['nominal_h'])} mm"
        rot = "↻ ROTADA 90°" if p["rotated"] else None
        full = [f"Orden {p['order_number']}", p["blind_label"], f"{p['piece_name']} · {dims}"] + ([rot] if rot else [])
        oneline = [f"{p['order_number']} · {p['blind_label']} · {p['piece_name']} {dims}"
                   + (" · ↻ 90°" if rot else "")]
        compact = [f"{p['order_number']}", dims] + (["↻ 90°"] if rot else [])
        if not (_draw_lines_centered(img, pb, full, min_size=24)
                or _draw_lines_centered(img, pb, oneline, max_size=40, min_size=22)
                or _draw_lines_centered(img, pb, compact, max_size=30, min_size=18)):
            _draw_lines_centered(img, pb, [f"{i + 1}"], max_size=24, min_size=10)
            unlabeled.append((i + 1, p))

    # flecha de veta sobre el borde derecho de la hoja
    if sheet.get("directional"):
        ax = tx(W) + 45
        _arrow(d, ax, ty(0) + 10, ty(H) - 10, GRAIN, width=7, head=22)

    # --------------------------------------------------------------- leyenda
    lx = CANVAS_W - LEGEND_W - MARGIN + 60
    ly = HEADER_H + 40
    d.text((lx, ly), "ÓRDENES EN LA HOJA", font=font(36, True), fill=INK)
    ly += 64
    seen = {}
    for p in sheet["placements"]:
        seen.setdefault(p["order_id"], {"p": p, "n": 0})
        seen[p["order_id"]]["n"] += 1
    for oid, info in seen.items():
        if ly > CANVAS_H - FOOTER_H - 330:
            d.text((lx, ly), "…", font=font(30), fill=MUTED)
            ly += 40
            break
        p = info["p"]
        col = order_color(order_index.get(oid, 0))
        d.rectangle([lx, ly + 4, lx + 54, ly + 54], fill=col, outline=_darker(col, 0.35), width=3)
        d.text((lx + 74, ly), f"{p['order_number']}  ({info['n']} pz)", font=font(30, True), fill=INK)
        sub = f"{p['blind_label']}" + (f" · {p['client']}" if p.get("client") else "")
        d.text((lx + 74, ly + 36), sub[:38], font=font(24), fill=MUTED)
        ly += 86
    ly += 20
    # simbología
    d.text((lx, ly), "SIMBOLOGÍA", font=font(32, True), fill=INK)
    ly += 52
    img.paste(_hatch((54, 50), RETAZO_BG, RETAZO_LINE, 14, 3, True), (lx, ly))
    d.rectangle([lx, ly, lx + 54, ly + 50], outline=RETAZO_LINE, width=3)
    d.text((lx + 74, ly + 8), "Retazo recuperable", font=font(28), fill=INK)
    ly += 66
    img.paste(_hatch((54, 50), SHEET_BG, MERMA_LINE, 12, 2), (lx, ly))
    d.rectangle([lx, ly, lx + 54, ly + 50], outline=INK, width=2)
    d.text((lx + 74, ly + 8), "Merma / kerf / refilado", font=font(28), fill=INK)
    ly += 66
    d.text((lx, ly), "↻ = pieza rotada 90°", font=font(28), fill=INK)
    ly += 46
    if unlabeled:
        d.text((lx, ly), "Piezas numeradas:", font=font(28, True), fill=INK)
        ly += 42
        for n, p in unlabeled:
            if ly > CANVAS_H - FOOTER_H - 50:
                d.text((lx, ly), "…", font=font(24), fill=MUTED)
                break
            d.text((lx, ly), f"{n}: {p['order_number']} {p['piece_name']} {_fmt(p['nominal_w'])}×"
                   f"{_fmt(p['nominal_h'])}{' ↻' if p['rotated'] else ''}", font=font(24), fill=INK)
            ly += 34

    # ------------------------------------------------------------------ pie
    fy = CANVAS_H - FOOTER_H
    d.rectangle([0, fy, CANVAS_W, CANVAS_H], fill=(244, 246, 249))
    d.line([(0, fy), (CANVAS_W, fy)], fill=(200, 205, 212), width=3)
    stats = [
        ("% Aprovechamiento", f"{m['aprovechamiento'] * 100:.1f} %", (31, 120, 60)),
        ("% Merma", f"{m['merma'] * 100:.1f} %", (150, 60, 40)),
        ("% Retazo recuperable", f"{m['retazo'] * 100:.1f} %", (46, 130, 67)),
        ("Total de piezas en la hoja", f"{m['piezas']}", INK),
    ]
    colw = (CANVAS_W - 2 * MARGIN) / len(stats)
    for i, (k, v, c) in enumerate(stats):
        x = MARGIN + i * colw
        d.text((x, fy + 40), k, font=font(34), fill=MUTED)
        d.text((x, fy + 92), v, font=font(78, True), fill=c)

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(DPI, DPI), optimize=False, compress_level=6)
    return buf.getvalue()


def global_order_index(result: dict) -> dict:
    idx: dict = {}
    for o in result.get("ordenes", []):
        idx.setdefault(o["order_id"], len(idx))
    return idx


def sheet_filename(run_id: str, sheet: dict) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in sheet["sheet_id"])
    return f"{run_id}_H{sheet['seq']:02d}_{safe}.png"


def build_zip(result: dict, png_getter, created_at: str | None = None) -> bytes:
    """ZIP con todos los PNG + lista de corte CSV + resumen."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for s in result["hojas"]:
            z.writestr(sheet_filename(result["run_id"], s), png_getter(s))
        lines = ["secuencia;hoja;lote;orden;cliente;tipo_persiana;pieza;ancho_mm;alto_mm;x_mm;y_mm;rotada"]
        for s in result["hojas"]:
            for p in s["placements"]:
                lines.append(";".join(str(v) for v in [
                    s["seq"], s["sheet_id"], s["lot_code"], p["order_number"], p.get("client") or "",
                    p["blind_label"], p["piece_name"], _fmt(p["nominal_w"]), _fmt(p["nominal_h"]),
                    _fmt(p["x"]), _fmt(p["y"]), "SI" if p["rotated"] else "NO"]))
        z.writestr("lista_de_corte.csv", "﻿" + "\n".join(lines))
        mg = result["metricas"]
        resumen = [
            f"Corrida: {result['run_id']}", f"Generado: {created_at or ''}",
            f"Hojas consumidas: {mg['hojas']}",
            f"Aprovechamiento global: {mg['aprovechamiento'] * 100:.2f} %",
            f"Merma neta: {mg['merma'] * 100:.2f} %",
            f"Retazo recuperable: {mg['retazo'] * 100:.2f} %",
            f"Aprovechamiento teórico máximo (cota kerf + refilado): {mg.get('aprovechamiento_teorico', 0) * 100:.2f} %",
            f"Adherencia a la cola: {mg.get('adherencia_cola', 1) * 100:.1f} %",
            f"Órdenes cubiertas: {mg['ordenes_cubiertas']} / {mg['ordenes_total']}"
            f" (parciales {mg['ordenes_parciales']}, pendientes {mg['ordenes_pendientes']})",
        ]
        for s in result["hojas"]:
            for r in s["offcuts"]:
                resumen.append(f"Retazo: hoja {s['sheet_id']} -> {_fmt(r['w'])} x {_fmt(r['h'])} mm")
        z.writestr("resumen.txt", "\n".join(resumen))
    return buf.getvalue()
