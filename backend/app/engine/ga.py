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
                 generations: int, elapsed: float, stop_reason: str, evaluations: int,
                 lower_bound: dict | None = None):
        self.lower_bound = lower_bound or {}
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
    """AG memético con gen de heurística.

    Individuo = (permutación de genes (id_pieza, rotada), h), donde ``h`` es el
    índice de la regla de acomodo del decodificador (ajuste × partición). La
    permutación evoluciona con OX/PMX + swap/inversión/rotación como dicta la
    ERS; ``h`` se hereda de uno de los padres y muta con baja probabilidad, de
    modo que el AG también aprende *qué regla de acomodo* conviene a cada mezcla
    de piezas.

    Fase memética (lamarckiana): cada vez que mejora el mejor individuo se le
    aplica la búsqueda local (vaciado de hojas, cambio a retazos, re-acomodo) y
    el patrón mejorado se reescribe como cromosoma (piezas en el orden hoja por
    hoja), que reemplaza al peor de la población. El mejor patrón pulido visto
    durante toda la corrida es el que se entrega.
    """
    t0 = time.perf_counter()
    T = gp.time_limit_s if time_limit is None else time_limit
    rng = random.Random(gp.seed)
    dec = Decoder(sub, pp)
    n = len(sub.pieces)
    rotatable = [p.rotatable for p in sub.pieces]
    n_var = len(dec.packer_cls.VARIANTS)
    cache: dict[tuple, float] = {}

    seeds = seed_chromosomes(sub, pp.tolerance_window)
    baseline = dec.decode(seeds[0], finalize=True, eliminate=False)    # línea base FIFO sin optimizar
    lb = dec.sheet_lower_bound()
    n_impossible = n - lb["piezas_colocables"]

    def at_optimum() -> bool:
        """Parada por óptimo: todas las piezas colocables están en hoja y el
        número de hojas iguala la cota inferior, así que no existe solución
        con menos hojas."""
        return (gp.stop_at_bound and final_best is not None
                and len(final_best.unplaced) <= n_impossible
                and 0 < len(final_best.bins) <= lb["hojas"])

    def key(ind) -> tuple:
        return (tuple(ind[0]), ind[1])

    def raw(ind) -> Decoded:
        return dec.decode(ind[0], variant=ind[1], best_fit=True)

    def evaluate(pop: list) -> list[float]:
        keys = [key(c) for c in pop]
        uniq: dict[tuple, int] = {}
        for i, k in enumerate(keys):
            if k not in cache:
                uniq.setdefault(k, i)       # evita evaluar duplicados
        for k, i in uniq.items():
            cache[k] = raw(pop[i]).fitness
        return [cache[k] for k in keys]

    # Población inicial: cada semilla heurística × varias reglas de acomodo.
    size = max(4, gp.population)
    pop = []
    for s in seeds:
        for v in range(n_var):
            pop.append((list(s), v))
    fits0 = evaluate(pop)
    pop = [pop[i] for i in sorted(range(len(pop)), key=lambda i: fits0[i])][: max(2, size // 2)]
    while len(pop) < size:
        base, v = rng.choice(pop[: max(2, size // 4)])
        c = list(base)
        if rng.random() < 0.25:
            rng.shuffle(c)          # diversidad: algunos individuos aleatorios
            v = rng.randrange(n_var)
        else:
            for _ in range(rng.randint(1, max(1, n // 10))):
                c = mutate(c, rotatable, rng)
        pop.append((c, v))

    fits = evaluate(pop)
    n_elite = max(1, math.ceil(gp.elitism * size))
    best_i = min(range(size), key=lambda i: fits[i])
    best, best_fit = pop[best_i], fits[best_i]
    history = [round(best_fit, 6)]

    # Mejor patrón pulido (búsqueda local) encontrado en toda la corrida.
    ls_deadline = t0 + max(T, 1.0)
    final_best: Decoded | None = None
    final_ind = best
    ls_done: set = set()

    def local_search(ind) -> None:
        nonlocal final_best, final_ind, pop, fits
        k = key(ind)
        if k in ls_done or time.perf_counter() > ls_deadline:
            return
        ls_done.add(k)
        d = dec.decode(ind[0], finalize=True, deadline=ls_deadline, variant=ind[1], best_fit=True)
        if final_best is None or d.fitness < final_best.fitness - 1e-12:
            final_best, final_ind = d, ind
        # Lamarck: el patrón mejorado vuelve a la población como cromosoma.
        child = ([(t[0], bool(t[5])) for b in d.bins for t in b.placed]
                 + [(u, False) for u in d.unplaced], ind[1])
        if key(child) != k and len(child[0]) == n:
            f = evaluate([child])[0]
            worst = max(range(len(pop)), key=lambda i: fits[i])
            if f < fits[worst]:
                pop[worst], fits[worst] = child, f

    local_search(best)

    # ---- Elitismo a nivel de hoja (herencia de grupos) -------------------
    # En bin packing lo valioso de una solución son sus *hojas bien llenas*
    # (grupos de piezas que encajan). De los élites se guardan esas hojas y
    # un hijo puede heredarlas completas: sus piezas pasan al frente de la
    # permutación (en el mismo orden y rotación), y el decodificador vuelve a
    # formar esas hojas antes de acomodar el resto.
    elite_sheets: list = []       # [(variante, [lista de genes por hoja buena])]

    def refresh_elite_sheets() -> None:
        elite_sheets.clear()
        seen_e: set = set()
        for i in sorted(range(len(pop)), key=lambda i: fits[i]):
            k = key(pop[i])
            if k in seen_e:
                continue
            seen_e.add(k)
            d = raw(pop[i])
            groups = []
            for b in d.bins:
                fill = sum(t[3] * t[4] for t in b.placed) / b.lot.area
                if fill >= 0.75:
                    groups.append([(t[0], bool(t[5])) for t in b.placed])
            if groups:
                elite_sheets.append((pop[i][1], groups))
            if len(seen_e) >= max(2, n_elite):
                break

    def inherit_sheets(perm: list, v: int):
        if not elite_sheets:
            return perm, v
        ev, groups = rng.choice(elite_sheets)
        take = [g for g in groups if rng.random() < 0.5] or [rng.choice(groups)]
        front, used = [], set()
        for g in take:
            for gene in g:
                if gene[0] not in used:
                    used.add(gene[0])
                    front.append(gene)
        return front + [g for g in perm if g[0] not in used], ev

    def random_individual():
        c = list(rng.choice(seeds))
        if rng.random() < 0.5:
            rng.shuffle(c)
        else:
            for _ in range(rng.randint(1, max(1, n // 5))):
                c = mutate(c, rotatable, rng)
        return (c, rng.randrange(n_var))

    refresh_elite_sheets()
    stagn = 0
    gen = 0
    stop = "generaciones"
    immigrated = False
    cx = order_crossover if gp.crossover.upper() == "OX" else pmx_crossover
    while True:
        if at_optimum():
            stop = "óptimo (cota)"
            break
        if gen >= gp.max_generations:
            stop = "G_max"
            break
        if stagn >= gp.stagnation:
            stop = "estancamiento"
            break
        if time.perf_counter() - t0 >= T * 0.8:     # 20 % del tiempo se reserva al pulido final
            stop = "T_max"
            break
        if cancelled and cancelled():
            stop = "cancelado"
            break
        if n < 2:
            stop = "trivial"
            break
        gen += 1
        # Mutación adaptativa: con estancamiento s la probabilidad sube hasta 2.5×.
        p_mut = gp.p_mutation
        if gp.adaptive_mutation and gp.stagnation > 0:
            p_mut = min(0.95, gp.p_mutation * (1.0 + 1.5 * stagn / gp.stagnation))
        order = sorted(range(size), key=lambda i: fits[i])
        new_pop = [pop[i] for i in order[:n_elite]]          # elitismo clásico (top %)
        keys_in = {key(c) for c in new_pop}
        tries = 0
        while len(new_pop) < size:
            tries += 1
            p1 = tournament(pop, fits, gp.tournament_k, rng)
            p2 = tournament(pop, fits, gp.tournament_k, rng)
            if rng.random() < gp.p_crossover:
                perm = cx(p1[0], p2[0], rng)
                v = p1[1] if rng.random() < 0.5 else p2[1]
            else:
                perm, v = list(p1[0]), p1[1]
            if rng.random() < gp.p_group:
                perm, v = inherit_sheets(perm, v)
            if rng.random() < p_mut:
                perm = mutate(perm, rotatable, rng)
                if rng.random() < 0.15:
                    v = rng.randrange(n_var)        # mutación del gen de heurística
            child = (perm, v)
            # Sin duplicados: una copia exacta se vuelve a mutar (diversidad).
            if key(child) in keys_in and tries < size * 4:
                child = (mutate(perm, rotatable, rng), v)
                if key(child) in keys_in:
                    continue
            keys_in.add(key(child))
            new_pop.append(child)
        pop = new_pop
        fits = evaluate(pop)
        gi = min(range(size), key=lambda i: fits[i])
        if fits[gi] < best_fit - 1e-9:
            best, best_fit = pop[gi], fits[gi]
            stagn = 0
            immigrated = False
            local_search(best)
        else:
            stagn += 1
        # Inmigrantes: a mitad del estancamiento se renueva la parte peor de la
        # población (se conservan los élites) para escapar del óptimo local.
        if gp.immigrants > 0 and not immigrated and stagn >= max(2, gp.stagnation // 2):
            immigrated = True
            order = sorted(range(size), key=lambda i: fits[i])
            n_imm = int(size * gp.immigrants)
            for i in order[size - n_imm:]:
                pop[i] = random_individual()
            fits = evaluate(pop)
        if gen % 5 == 0 or stagn == 0:
            refresh_elite_sheets()
        history.append(round(best_fit, 6))
        if progress:
            progress(gen, best_fit)

    # Pulido final de otros élites distintos mientras quede tiempo: distintos
    # órdenes de corte llevan a distintos óptimos locales.
    for i in sorted(range(len(pop)), key=lambda i: fits[i])[:4]:
        local_search(pop[i])
    for s in seeds[1:]:
        local_search((s, best[1]))
    decoded = final_best if final_best is not None else dec.decode(best[0], finalize=True, variant=best[1])
    best_perm = final_ind[0]
    if baseline.fitness < decoded.fitness:   # nunca entregar algo peor que la línea base
        decoded, best_perm = baseline, seeds[0]
    return GAResult(best_perm, decoded, baseline, history, gen, time.perf_counter() - t0, stop, len(cache), lb)
