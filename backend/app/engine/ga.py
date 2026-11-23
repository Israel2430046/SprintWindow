"""Algoritmo Genético (sección 4 de la ERS).

* Codificación: permutación de genes (id_pieza, rotada) + decodificador.
* Selección: torneo determinístico k=3 con elitismo (top 5 %).
* Cruce: OX (Order Crossover) o PMX (Partially Mapped Crossover).
* Mutación: swap, inversión de subsecuencia y alternancia de rotación
  (sólo para piezas rotables, es decir, direccionalidad_veta = False).
* Parada: G_max, estancamiento de X generaciones o tiempo límite T_max.
"""
from __future__ import annotations

import math
import random
import time
from typing import Callable

from .decoder import Decoded, Decoder
from .model import GAParams, ProcessParams, Subproblem

# ----------------------------------------------------------- operadores

def order_crossover(p1: list, p2: list, rng: random.Random) -> list:
    """OX: copia un segmento de p1 y completa con el orden relativo de p2."""
    n = len(p1)
    if n < 2:
        return list(p1)
    a, b = sorted(rng.sample(range(n), 2))
    child = [None] * n
    child[a:b + 1] = p1[a:b + 1]
    taken = {g[0] for g in p1[a:b + 1]}
    fill = [g for g in p2 if g[0] not in taken]
    j = 0
    for i in list(range(b + 1, n)) + list(range(0, a)):
        child[i] = fill[j]
        j += 1
    return child


def pmx_crossover(p1: list, p2: list, rng: random.Random) -> list:
    """PMX: segmento de p1, resto de p2 resolviendo conflictos por mapeo."""
    n = len(p1)
    if n < 2:
        return list(p1)
    a, b = sorted(rng.sample(range(n), 2))
    child = [None] * n
    child[a:b + 1] = p1[a:b + 1]
    seg_ids = {g[0] for g in p1[a:b + 1]}
    pos_in_p2 = {g[0]: i for i, g in enumerate(p2)}
    for i in range(a, b + 1):
        g = p2[i]
        if g[0] in seg_ids:
            continue
        j = i
        while a <= j <= b:
            j = pos_in_p2[p1[j][0]]
        child[j] = g
    for i in range(n):
        if child[i] is None:
            child[i] = p2[i]
    return child


def mutate(chrom: list, rotatable: list[bool], rng: random.Random) -> list:
    c = list(chrom)
    n = len(c)
    if n < 2:
        return c
    ops = ["swap", "inversion"]
    if any(rotatable):
        ops.append("rotation")
    op = rng.choice(ops)
    if op == "swap":
        i, j = rng.sample(range(n), 2)
        c[i], c[j] = c[j], c[i]
    elif op == "inversion":
        i, j = sorted(rng.sample(range(n), 2))
        c[i:j + 1] = reversed(c[i:j + 1])
    else:
        idx = [i for i in range(n) if rotatable[c[i][0]]]
        for _ in range(max(1, len(idx) // 20)):
            i = rng.choice(idx)
            c[i] = (c[i][0], not c[i][1])
    return c


def tournament(pop: list, fits: list, k: int, rng: random.Random) -> list:
    """Torneo determinístico: gana siempre el de menor costo."""
    best = min(rng.sample(range(len(pop)), min(k, len(pop))), key=lambda i: fits[i])
    return pop[best]


# ---------------------------------------------------------- semillas

def seed_chromosomes(sub: Subproblem, tol: int) -> list[list]:
    P = sub.pieces
    ids = list(range(len(P)))

    def chrom(order, rot_fn=lambda p: False):
        return [(i, bool(P[i].rotatable and rot_fn(P[i]))) for i in order]

    tall = lambda p: p.w > p.h    # rota para dejar el lado largo vertical
    seeds = [
        chrom(sorted(ids, key=lambda i: (P[i].queue_rank, -P[i].area))),                 # FIFO estricto
        chrom(sorted(ids, key=lambda i: (P[i].queue_rank // (tol + 1), -P[i].area))),    # FIFO por ventanas
        chrom(sorted(ids, key=lambda i: (P[i].queue_rank // (tol + 1), -max(P[i].w, P[i].h))), tall),
        chrom(sorted(ids, key=lambda i: -P[i].area)),                                     # área decreciente
        chrom(sorted(ids, key=lambda i: (-P[i].h, -P[i].w))),
        chrom(sorted(ids, key=lambda i: (-max(P[i].w, P[i].h), -P[i].area)), tall),
    ]
    uniq, seen = [], set()
    for s in seeds:
        t = tuple(s)
        if t not in seen:
            seen.add(t)
            uniq.append(s)
    return uniq


class GAResult:
    def __init__(self, best: list, decoded: Decoded, baseline: Decoded, history: list,
                 generations: int, elapsed: float, stop_reason: str, evaluations: int):
        self.best = best
        self.decoded = decoded
        self.baseline = baseline
        self.history = history
        self.generations = generations
        self.elapsed = elapsed
        self.stop_reason = stop_reason
        self.evaluations = evaluations


def run_ga(sub: Subproblem, pp: ProcessParams, gp: GAParams, time_limit: float | None = None,
           progress: Callable[[int, float], None] | None = None,
           cancelled: Callable[[], bool] | None = None) -> GAResult:
    t0 = time.perf_counter()
    T = gp.time_limit_s if time_limit is None else time_limit
    rng = random.Random(gp.seed)
    dec = Decoder(sub, pp)
    n = len(sub.pieces)
    rotatable = [p.rotatable for p in sub.pieces]
    cache: dict[tuple, float] = {}

    seeds = seed_chromosomes(sub, pp.tolerance_window)
    baseline = dec.decode(seeds[0], finalize=True)    # línea base FIFO sin optimizar

    def evaluate(pop: list) -> list[float]:
        keys = [tuple(c) for c in pop]
        todo = [i for i, k in enumerate(keys) if k not in cache]
        # evita evaluar duplicados dentro de la misma generación
        uniq: dict[tuple, int] = {}
        for i in todo:
            uniq.setdefault(keys[i], i)
        idxs = list(uniq.values())
        vals = [dec.decode(pop[i]).fitness for i in idxs]
        for i, v in zip(idxs, vals):
            cache[keys[i]] = v
        return [cache[k] for k in keys]

    size = max(4, gp.population)
    pop = [list(s) for s in seeds][:size]
    while len(pop) < size:
        base = rng.choice(seeds)
        c = list(base)
        if rng.random() < 0.25:
            rng.shuffle(c)          # diversidad: algunos individuos aleatorios
        else:
            for _ in range(rng.randint(1, max(1, n // 10))):
                c = mutate(c, rotatable, rng)
        pop.append(c)

    fits = evaluate(pop)
    n_elite = max(1, math.ceil(gp.elitism * size))
    best_i = min(range(size), key=lambda i: fits[i])
    best, best_fit = pop[best_i], fits[best_i]
    history = [round(best_fit, 6)]
    stagn = 0
    gen = 0
    stop = "generaciones"
    while True:
        if gen >= gp.max_generations:
            stop = "G_max"
            break
        if stagn >= gp.stagnation:
            stop = "estancamiento"
            break
        if time.perf_counter() - t0 >= T:
            stop = "T_max"
            break
        if cancelled and cancelled():
            stop = "cancelado"
            break
        if n < 2:
            stop = "trivial"
            break
        gen += 1
        order = sorted(range(size), key=lambda i: fits[i])
        new_pop = [pop[i] for i in order[:n_elite]]
        while len(new_pop) < size:
            p1 = tournament(pop, fits, gp.tournament_k, rng)
            p2 = tournament(pop, fits, gp.tournament_k, rng)
            if rng.random() < gp.p_crossover:
                cx = order_crossover if gp.crossover.upper() == "OX" else pmx_crossover
                child = cx(p1, p2, rng)
            else:
                child = list(p1)
            if rng.random() < gp.p_mutation:
                child = mutate(child, rotatable, rng)
            new_pop.append(child)
        pop = new_pop
        fits = evaluate(pop)
        gi = min(range(size), key=lambda i: fits[i])
        if fits[gi] < best_fit - 1e-9:
            best, best_fit = pop[gi], fits[gi]
            stagn = 0
        else:
            stagn += 1
        history.append(round(best_fit, 6))
        if progress:
            progress(gen, best_fit)

    decoded = dec.decode(best, finalize=True)
    if baseline.fitness < decoded.fitness:   # nunca entregar algo peor que la línea base
        decoded, best = baseline, seeds[0]
    return GAResult(best, decoded, baseline, history, gen, time.perf_counter() - t0, stop, len(cache))
