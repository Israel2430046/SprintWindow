"""Benchmark del motor de corte frente a la cota teórica.

Uso (desde backend/):
    python3 -m tools.benchmark [--tiempo 10] [--modo guillotina|libre] [--semillas 8]

Reporta, para el conjunto de demostración y para instancias aleatorias:
hojas, aprovechamiento, retazo, merma neta y la brecha contra el
aprovechamiento teórico máximo (cota de área: kerf + refilado).
"""
from __future__ import annotations

import argparse
import copy
import random
import time

from app.db import DEFAULT_SETTINGS
from app.demo import DEMO_LOTS, DEMO_ORDERS
from app.engine.decoder import Decoder
from app.engine.ga import run_ga
from app.engine.model import GAParams, MaterialKey, Piece, ProcessParams, SheetLot, Subproblem
from app.engine.optimizer import optimize, theoretical_bound

SIZES = [(89, 1800), (600, 1500), (900, 1200), (300, 700), (1200, 400), (450, 450),
         (637, 2285), (870, 2050), (1070, 1650), (1470, 60), (89, 2280), (500, 1100)]


def random_instance(seed: int, directional: bool) -> Subproblem:
    rng = random.Random(seed)
    pieces = []
    for o in range(rng.randint(10, 25)):
        w, h = rng.choice(SIZES)
        for _ in range(rng.randint(1, 8)):
            pieces.append(Piece(len(pieces), o, f"O{o}", "c", "x", "p", w, h, not directional, o, 1.0, "K"))
    lots = [SheetLot(1, "A", 1220, 2440, 3, "b", "l", directional, False, 60),
            SheetLot(2, "B", 1500, 3000, 3, "b", "l", directional, False, 30),
            SheetLot(3, "R", 800, 900, 3, "b", "l", directional, True, 2)]
    return Subproblem(MaterialKey.of("b", "l", 3), pieces, lots, directional)


def demo(tiempo: float, modo: str) -> None:
    lots = [{**l, "id": i + 1} for i, l in enumerate(DEMO_LOTS)]
    orders = [{**o, "id": i + 1, "queue_pos": i + 1, "cut_pieces": None} for i, o in enumerate(DEMO_ORDERS)]
    st = copy.deepcopy(DEFAULT_SETTINGS)
    st["ga"]["tiempo_max_s"] = tiempo
    st["ga"]["semilla"] = 1
    st["proceso"]["modo_corte"] = modo
    r = optimize(orders, lots, st, "BENCH")
    print(f"DEMO ({modo}, {r['tiempo_total_s']} s)")
    print(f"  {'grupo':32} {'hojas':>9} {'aprov':>7} {'teórico':>8} {'retazo':>7} {'merma':>6}")
    for g in r["grupos"]:
        m, b = g["metricas"], g["linea_base"]
        print(f"  {g['material']:32} {b['hojas']:3} → {m['hojas']:3} {m['aprovechamiento']:7.1%} "
              f"{m['aprovechamiento_teorico']:8.1%} {m['retazo']:7.1%} {m['merma']:6.1%}")
    m, b = r["metricas"], r["linea_base"]
    print(f"  {'TOTAL':32} {b['hojas']:3} → {m['hojas']:3} {m['aprovechamiento']:7.1%} "
          f"{m['aprovechamiento_teorico']:8.1%} {m['retazo']:7.1%} {m['merma']:6.1%}")
    print(f"  Material no aprovechado vs. línea base: −{r['reduccion_desperdicio_total']:.1%}")


def aleatorio(tiempo: float, modo: str, semillas: int) -> None:
    pp = ProcessParams(cut_mode=modo)
    S = P = R = N = LB_S = LB_P = 0.0
    t0 = time.time()
    for seed in range(semillas):
        for d in (False, True):
            sub = random_instance(seed, d)
            res = run_ga(sub, pp, GAParams(seed=1, time_limit_s=tiempo, max_generations=400, stagnation=80))
            dec = Decoder(sub, pp)
            S += sum(b.lot.area for b in res.decoded.bins)
            P += sum(w * h for b in res.decoded.bins for _u, _x, _y, w, h, _r in b.placed)
            R += sum(o[2] * o[3] for b in res.decoded.bins for o in dec.bin_offcuts(b))
            N += len(res.decoded.bins)
            tb = theoretical_bound(sub, pp)
            LB_S += tb["area_hojas_min"]
            LB_P += tb["area_piezas"]
    print(f"ALEATORIO ({modo}, {2 * semillas} instancias, {time.time() - t0:.0f} s): hojas={N:.0f} "
          f"aprov={P / S:.2%} retazo={R / S:.2%} merma={(S - P - R) / S:.2%} teórico={LB_P / LB_S:.2%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiempo", type=float, default=10.0)
    ap.add_argument("--modo", default="guillotina", choices=["guillotina", "libre"])
    ap.add_argument("--semillas", type=int, default=8)
    ap.add_argument("--solo-demo", action="store_true")
    a = ap.parse_args()
    demo(a.tiempo, a.modo)
    if not a.solo_demo:
        aleatorio(a.tiempo / 2, a.modo, a.semillas)
