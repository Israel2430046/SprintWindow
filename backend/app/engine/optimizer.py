"""Orquestador: partición por material (RF-07), AG por subproblema (RF-08) y
métricas proyectadas/comparativa contra la línea base (RF-10)."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

from ..despiece import BLIND_TYPES, explode
from .decoder import Decoded, Decoder, piece_fits_lot
from .ga import run_ga, seed_chromosomes
from .validation import assert_valid
from .model import GAParams, MaterialKey, Piece, ProcessParams, SheetLot, Subproblem


def params_from_settings(settings: dict) -> tuple[ProcessParams, GAParams]:
    p, w, g = settings["proceso"], settings["pesos"], settings["ga"]
    pp = ProcessParams(
        kerf=float(p["kerf_mm"]), trim=float(p["refilado_mm"]),
        offcut_min_long=float(p["retazo_min_largo_mm"]), offcut_min_short=float(p["retazo_min_ancho_mm"]),
        cut_mode=p["modo_corte"], tolerance_window=int(p["ventana_tolerancia"]),
        w1=float(w["w1_merma"]), w2=float(w["w2_cola"]), w3=float(w["w3_retazo"]),
        w_pending=float(w["w_pendientes"]),
    )
    gp = GAParams(
        population=int(g["poblacion"]), max_generations=int(g["generaciones_max"]),
        stagnation=int(g["estancamiento"]), time_limit_s=float(g["tiempo_max_s"]),
        crossover=g["cruce"], p_crossover=float(g["prob_cruce"]), p_mutation=float(g["prob_mutacion"]),
        elitism=float(g["elitismo"]), tournament_k=int(g["torneo_k"]),
        seed=(int(g["semilla"]) if g.get("semilla") not in (None, "") else None),
    )
    return pp, gp


def lot_from_row(r: dict) -> SheetLot:
    return SheetLot(lot_id=int(r["id"]), code=r["code"], w=float(r["width"]), h=float(r["height"]),
                    thickness=float(r["thickness"]), color=r["color"], texture=r["texture"],
                    directional=bool(r["directional"]), is_offcut=bool(r["is_offcut"]),
                    quantity=int(r["quantity"]),
                    unit_cost=(float(r["unit_cost"]) if r.get("unit_cost") not in (None, "") else None))


def order_pieces(order: dict, holguras: dict) -> list[dict]:
    """Piezas aún pendientes de corte de una orden (despiece - ya cortadas)."""
    cut = order.get("cut_pieces") or {}
    out = []
    for pc in explode(order["blind_type"], order.get("width"), order.get("height"), order["quantity"],
                      order.get("manual_pieces"), holguras):
        rem = pc["cantidad"] - int(cut.get(pc["key"], 0))
        if rem > 0:
            out.append({**pc, "cantidad": rem})
    return out


def build_subproblems(orders: list[dict], lots: list[dict], settings: dict):
    """RF-07: particiona cola e inventario por (color, textura, espesor)."""
    prio_w = {"Urgente": float(settings["pesos"]["peso_urgente"]), "Normal": float(settings["pesos"]["peso_normal"])}
    holg = settings.get("holguras") or {}
    lots_by_key: dict[MaterialKey, list[SheetLot]] = defaultdict(list)
    for r in lots:
        if int(r["quantity"]) > 0:
            lot = lot_from_row(r)
            lots_by_key[MaterialKey.of(lot.color, lot.texture, lot.thickness)].append(lot)

    orders_by_key: dict[MaterialKey, list[dict]] = defaultdict(list)
    for o in sorted(orders, key=lambda o: o["queue_pos"]):
        orders_by_key[MaterialKey.of(o["color"], o["texture"], o["thickness"])].append(o)

    subs: list[Subproblem] = []
    warnings: list[str] = []
    for key, olist in orders_by_key.items():
        glots = lots_by_key.get(key, [])
        flags = {l.directional for l in glots}
        directional = True in flags
        if len(flags) > 1:
            warnings.append(f"{key.label()}: lotes con direccionalidad mixta; se trata como textura "
                            f"direccional (sin rotación) por seguridad.")
        pieces: list[Piece] = []
        for rank, o in enumerate(olist):
            for pc in order_pieces(o, holg):
                for _ in range(pc["cantidad"]):
                    pieces.append(Piece(
                        uid=len(pieces), order_id=int(o["id"]), order_number=o["order_number"],
                        client=o.get("client") or "", blind_type=o["blind_type"], name=pc["nombre"],
                        w=float(pc["ancho"]), h=float(pc["alto"]), rotatable=not directional,
                        queue_rank=rank, priority_weight=prio_w.get(o.get("priority"), 1.0),
                        piece_key=pc["key"]))
        if pieces:
            subs.append(Subproblem(key=key, pieces=pieces, lots=glots, directional=directional))
    return subs, warnings


# ----------------------------------------------------------------- métricas

def _metrics(dec: Decoder, d: Decoded) -> dict:
    sheet_area = used = off = cost = 0.0
    for b in d.bins:
        a = b.lot.area
        u = sum(p[3] * p[4] for p in b.placed)
        o = sum(r[2] * r[3] for r in dec.bin_offcuts(b))
        sheet_area += a
        used += u
        off += o
        if b.lot.unit_cost:
            cost += b.lot.unit_cost * max(0.0, a - u - o) / a
    return {"hojas": len(d.bins), "area_hojas": sheet_area, "area_piezas": used, "area_retazos": off,
            "costo_merma": cost, "piezas_colocadas": sum(len(b.placed) for b in d.bins),
            "piezas_pendientes": len(d.unplaced)}


def _finish(m: dict) -> dict:
    a = m["area_hojas"]
    m["aprovechamiento"] = (m["area_piezas"] / a) if a else 0.0
    m["retazo"] = (m["area_retazos"] / a) if a else 0.0
    m["merma"] = (1 - m["aprovechamiento"] - m["retazo"]) if a else 0.0
    m["merma_bruta"] = (1 - m["aprovechamiento"]) if a else 0.0
    return m


def _sum(ms: list[dict]) -> dict:
    keys = ["hojas", "area_hojas", "area_piezas", "area_retazos", "costo_merma",
            "piezas_colocadas", "piezas_pendientes"]
    return {k: sum(m[k] for m in ms) for k in keys}


def _order_status(orders_total: dict, orders_placed: dict) -> dict:
    out = {}
    for oid, tot in orders_total.items():
        pl = orders_placed.get(oid, 0)
        out[oid] = "cubierta" if pl >= tot else ("parcial" if pl > 0 else "pendiente")
    return out


def optimize(orders: list[dict], lots: list[dict], settings: dict, run_id: str,
             progress: Callable[[dict], None] | None = None,
             cancelled: Callable[[], bool] | None = None, use_ga: bool = True) -> dict:
    t0 = time.perf_counter()
    pp, gp = params_from_settings(settings)
    subs, warnings = build_subproblems(orders, lots, settings)
    total_pieces = sum(len(s.pieces) for s in subs) or 1
    orders_by_id = {int(o["id"]): o for o in orders}

    sheets_out, groups_out, pending_out = [], [], []
    m_opt, m_base = [], []
    tot_by_order: dict[int, int] = defaultdict(int)
    placed_opt: dict[int, int] = defaultdict(int)
    placed_base: dict[int, int] = defaultdict(int)
    sheets_by_order: dict[int, set] = defaultdict(set)
    done_pieces = 0

    for gi, sub in enumerate(subs):
        dec = Decoder(sub, pp)
        for p in sub.pieces:
            tot_by_order[p.order_id] += 1
        budget = max(1.0, gp.time_limit_s * len(sub.pieces) / total_pieces)
        if not sub.lots:
            warnings.append(f"{sub.key.label()}: no hay hojas en inventario para este material (RN-01).")

        if use_ga and sub.lots:
            def prog(gen, fit, _gi=gi, _n=len(sub.pieces), _done=done_pieces):
                if progress:
                    progress({"grupo": _gi + 1, "grupos": len(subs), "material": sub.key.label(),
                              "generacion": gen, "fitness": round(fit, 5),
                              "avance": min(0.99, (_done + _n * min(1.0, gen / max(1, gp.max_generations)))
                                            / total_pieces)})
            res = run_ga(sub, pp, gp, time_limit=budget, progress=prog, cancelled=cancelled)
            best, baseline = res.decoded, res.baseline
            ginfo = {"generaciones": res.generations, "tiempo_s": round(res.elapsed, 2),
                     "criterio_parada": res.stop_reason, "evaluaciones": res.evaluations,
                     "historial": res.history}
        else:
            seed = seed_chromosomes(sub, pp.tolerance_window)[0]
            best = baseline = dec.decode(seed, finalize=True)
            ginfo = {"generaciones": 0, "tiempo_s": 0.0, "criterio_parada": "heuristica",
                     "evaluaciones": 1, "historial": [round(best.fitness, 6)]}
        done_pieces += len(sub.pieces)

        mo, mb = _metrics(dec, best), _metrics(dec, baseline)
        m_opt.append(mo)
        m_base.append(mb)
        for b in baseline.bins:
            for pl in b.placed:
                placed_base[sub.pieces[pl[0]].order_id] += 1

        for ps in dec.to_sheets(best):
            seq = len(sheets_out) + 1
            area = ps.area
            used, off = ps.used_area(), ps.offcut_area()
            pls = []
            for pl in ps.placements:
                pc = sub.pieces[pl.piece_uid]
                placed_opt[pc.order_id] += 1
                sheets_by_order[pc.order_id].add(seq)
                pls.append({"order_id": pc.order_id, "order_number": pc.order_number, "client": pc.client,
                            "blind_type": pc.blind_type, "blind_label": BLIND_TYPES.get(pc.blind_type, pc.blind_type),
                            "piece_name": pc.name, "piece_key": pc.piece_key,
                            "x": pl.x, "y": pl.y, "w": pl.w, "h": pl.h, "rotated": pl.rotated,
                            "nominal_w": pc.w, "nominal_h": pc.h})
            lot = ps.lot
            sheets_out.append({
                "seq": seq, "sheet_id": f"{lot.code}#{ps.instance}", "lot_id": lot.lot_id, "lot_code": lot.code,
                "instance": ps.instance, "w": lot.w, "h": lot.h, "color": lot.color, "texture": lot.texture,
                "thickness": lot.thickness, "directional": lot.directional, "is_offcut": lot.is_offcut,
                "unit_cost": lot.unit_cost, "group": sub.key.label(), "kerf": pp.kerf, "trim": pp.trim,
                "cut_mode": pp.cut_mode, "placements": pls,
                "offcuts": [{"x": r[0], "y": r[1], "w": r[2], "h": r[3]} for r in ps.offcuts],
                "metrics": {"aprovechamiento": used / area, "retazo": off / area,
                            "merma": max(0.0, 1 - (used + off) / area), "piezas": len(pls)},
            })
            assert_valid(sheets_out[-1])          # RNF-04: bloquea patrones inválidos

        for uid in best.unplaced:
            pc = sub.pieces[uid]
            if not sub.lots:
                reason = "Sin hojas del material (color/textura/espesor)"
            elif not any(piece_fits_lot(w, h, l, pp) for l in sub.lots
                         for (w, h) in ([(pc.w, pc.h), (pc.h, pc.w)] if pc.rotatable else [(pc.w, pc.h)])):
                reason = "La pieza excede las dimensiones útiles de toda hoja disponible"
            else:
                reason = "Inventario insuficiente"
            pending_out.append({"order_id": pc.order_id, "order_number": pc.order_number, "piece_name": pc.name,
                                "piece_key": pc.piece_key, "w": pc.w, "h": pc.h, "material": sub.key.label(),
                                "reason": reason})

        g = _finish(dict(mo))
        groups_out.append({"material": sub.key.label(), "color": sub.key.color, "texture": sub.key.texture,
                           "thickness": sub.key.thickness, "directional": sub.directional,
                           "piezas": len(sub.pieces), "lotes": len(sub.lots),
                           "metricas": g, "linea_base": _finish(dict(mb)),
                           "fitness": round(best.fitness, 6), "fitness_base": round(baseline.fitness, 6),
                           "componentes": best.components, **ginfo})

    st_opt = _order_status(tot_by_order, placed_opt)
    st_base = _order_status(tot_by_order, placed_base)
    gm = _finish(_sum(m_opt)) if m_opt else _finish(_sum([{k: 0 for k in ["hojas", "area_hojas", "area_piezas", "area_retazos", "costo_merma", "piezas_colocadas", "piezas_pendientes"]}]))
    bm = _finish(_sum(m_base)) if m_base else dict(gm)
    for m, st in ((gm, st_opt), (bm, st_base)):
        vals = list(st.values())
        m["ordenes_cubiertas"] = vals.count("cubierta")
        m["ordenes_parciales"] = vals.count("parcial")
        m["ordenes_pendientes"] = vals.count("pendiente")
        m["ordenes_total"] = len(vals)

    orders_out = []
    for oid in sorted(tot_by_order, key=lambda i: orders_by_id[i]["queue_pos"]):
        o = orders_by_id[oid]
        orders_out.append({"order_id": oid, "order_number": o["order_number"], "client": o.get("client"),
                           "blind_type": o["blind_type"], "priority": o.get("priority"),
                           "queue_pos": o["queue_pos"], "estado": st_opt[oid],
                           "piezas_total": tot_by_order[oid], "piezas_colocadas": placed_opt.get(oid, 0),
                           "hojas": sorted(sheets_by_order.get(oid, set()))})

    ahorro = None
    if bm["merma"] > 0:
        ahorro = (bm["merma"] - gm["merma"]) / bm["merma"]
    return {
        "run_id": run_id, "metricas": gm, "linea_base": bm, "reduccion_merma": ahorro,
        "grupos": groups_out, "hojas": sheets_out, "ordenes": orders_out, "pendientes": pending_out,
        "advertencias": warnings, "tiempo_total_s": round(time.perf_counter() - t0, 2),
        "parametros": {"proceso": settings["proceso"], "pesos": settings["pesos"], "ga": settings["ga"]},
    }
