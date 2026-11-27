"""Decodificador cromosoma -> patrón de corte multi-hoja y función de aptitud.

Un cromosoma es una permutación de genes ``(id_pieza, rotada)``. El decodificador
recorre los genes en orden y coloca cada pieza con la estrategia *first-fit*
sobre las hojas ya abiertas (en orden de apertura = secuencia de corte). Si no
cabe en ninguna, abre una hoja nueva del inventario heterogéneo:

1. Primero retazos (RF-03), del más pequeño al más grande que la contenga.
2. Después hojas vírgenes, eligiendo la más pequeña cuya área útil cubra el
   área pendiente por colocar (anticipación); si ninguna la cubre, la mayor.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .model import PackedSheet, Piece, Placement, ProcessParams, SheetLot, Subproblem
from .packers import EPS, PACKERS, extract_offcuts

Gene = tuple  # (piece_uid, rotated: bool)


@dataclass
class _Bin:
    lot: SheetLot
    instance: int
    packer: object
    placed: list = field(default_factory=list)  # (uid, x, y, w, h, rot) coords internas


@dataclass
class Decoded:
    bins: list
    unplaced: list
    fitness: float = 0.0
    components: dict = field(default_factory=dict)


def inner_size(lot: SheetLot, pp: ProcessParams) -> tuple[float, float]:
    """Área interna donde se insertan las piezas infladas por el kerf."""
    return (lot.w - 2 * pp.trim - pp.kerf, lot.h - 2 * pp.trim - pp.kerf)


def piece_fits_lot(w: float, h: float, lot: SheetLot, pp: ProcessParams) -> bool:
    iw, ih = inner_size(lot, pp)
    return w + pp.kerf <= iw + EPS and h + pp.kerf <= ih + EPS


def _orients(p: Piece, rot: bool) -> tuple:
    if not p.rotatable:
        return (False,)
    return (rot, not rot)


def _dims(p: Piece, rot: bool) -> tuple[float, float]:
    return (p.h, p.w) if rot else (p.w, p.h)


class Decoder:
    def __init__(self, sub: Subproblem, pp: ProcessParams):
        self.sub = sub
        self.pp = pp
        self.pieces = sub.pieces
        self.packer_cls = PACKERS[pp.cut_mode]
        self.offcut_lots = sorted((l for l in sub.lots if l.is_offcut), key=lambda l: l.area)
        self.virgin_lots = sorted((l for l in sub.lots if not l.is_offcut), key=lambda l: l.area)
        self.total_piece_area = sum(p.area for p in self.pieces) or 1.0
        # Posición de cada orden en la cola del grupo
        self.order_rank: dict[int, int] = {}
        self.order_weight: dict[int, float] = {}
        for p in self.pieces:
            self.order_rank[p.order_id] = p.queue_rank
            self.order_weight[p.order_id] = p.priority_weight

    # ------------------------------------------------------------------ decode
    def _new_bin(self, lot: SheetLot, instance: int, variant=None) -> _Bin:
        iw, ih = inner_size(lot, self.pp)
        packer = self.packer_cls(iw, ih) if variant is None else self.packer_cls(iw, ih, *variant)
        return _Bin(lot, instance, packer)

    def _choose_lot(self, p: Piece, rot: bool, remaining: dict, need_area: float):
        orients = _orients(p, rot)
        for lot in self.offcut_lots:                   # RF-03: retazos primero
            if remaining[lot.lot_id] <= 0:
                continue
            for r in orients:
                w, h = _dims(p, r)
                if piece_fits_lot(w, h, lot, self.pp):
                    return lot, r
        fitting = []
        for lot in self.virgin_lots:
            if remaining[lot.lot_id] <= 0:
                continue
            for r in orients:
                w, h = _dims(p, r)
                if piece_fits_lot(w, h, lot, self.pp):
                    fitting.append((lot, r))
                    break
        if not fitting:
            return None, None
        for lot, r in fitting:                         # ordenadas por área ascendente
            iw, ih = inner_size(lot, self.pp)
            if iw * ih >= need_area * 1.05:
                return lot, r
        return fitting[-1]

    def decode(self, chrom: list, finalize: bool = False, deadline: float | None = None,
               eliminate: bool = True) -> Decoded:
        pp = self.pp
        k = pp.kerf
        pieces = self.pieces
        remaining = {l.lot_id: l.quantity for l in self.sub.lots}
        used_count: dict[int, int] = {}
        bins: list[_Bin] = []
        unplaced: list[int] = []

        suffix = [0.0] * (len(chrom) + 1)
        for i in range(len(chrom) - 1, -1, -1):
            suffix[i] = suffix[i + 1] + pieces[chrom[i][0]].area

        for idx, (uid, rot) in enumerate(chrom):
            p = pieces[uid]
            done = False
            for r in _orients(p, rot):
                w, h = _dims(p, r)
                for b in bins:
                    pos = b.packer.insert(w + k, h + k)
                    if pos is not None:
                        b.placed.append((uid, pos[0], pos[1], w, h, r))
                        done = True
                        break
                if done:
                    break
            if done:
                continue
            lot, r = self._choose_lot(p, rot, remaining, suffix[idx])
            if lot is None:
                unplaced.append(uid)
                continue
            remaining[lot.lot_id] -= 1
            used_count[lot.lot_id] = used_count.get(lot.lot_id, 0) + 1
            b = self._new_bin(lot, used_count[lot.lot_id])
            w, h = _dims(p, r)
            pos = b.packer.insert(w + k, h + k)
            assert pos is not None
            b.placed.append((uid, pos[0], pos[1], w, h, r))
            bins.append(b)

        if finalize:
            plain = None
            if eliminate:
                snap_bins, snap_rem = list(bins), dict(remaining)
                self._eliminate_sheets(bins, unplaced, remaining, deadline)
                if len(bins) != len(snap_bins) or any(a is not b for a, b in zip(bins, snap_bins)):
                    plain = self._finish_bins(snap_bins, snap_rem, unplaced)
            res = self._finish_bins(bins, remaining, unplaced)
            if plain is not None and plain.fitness < res.fitness:
                res = plain          # el vaciado nunca debe empeorar el resultado final
            self._renumber(res.bins)
            return res
        res = Decoded(bins, unplaced)
        self._evaluate(res)
        return res

    def _finish_bins(self, bins: list, remaining: dict, unplaced: list) -> Decoded:
        self._downsize(bins, remaining)
        self._polish(bins)
        res = Decoded(bins, list(unplaced))
        self._evaluate(res)
        return res

    def _repack(self, lot: SheetLot, placed: list, instance: int, variant=None):
        b = self._new_bin(lot, instance, variant)
        k = self.pp.kerf
        for uid, _x, _y, w, h, r in placed:
            p = self.pieces[uid]
            ok = False
            for rr in _orients(p, r):
                ww, hh = _dims(p, rr)
                pos = b.packer.insert(ww + k, hh + k)
                if pos is not None:
                    b.placed.append((uid, pos[0], pos[1], ww, hh, rr))
                    ok = True
                    break
            if not ok:
                return None
        return b

    def _downsize(self, bins: list, remaining: dict) -> None:
        """Mejora local: sustituye cada hoja por el retazo o la hoja virgen más
        pequeña disponible que contenga exactamente las mismas piezas. Los
        retazos se prueban primero (RF-03): pasar una hoja casi vacía a un
        retazo libera una hoja virgen completa."""
        candidates = self.offcut_lots + self.virgin_lots
        for i, b in enumerate(bins):
            for lot in candidates:
                if lot.area >= b.lot.area - EPS or remaining[lot.lot_id] <= 0:
                    continue
                nb = None
                for order in self._orderings(b.placed):
                    for variant in self.packer_cls.VARIANTS:
                        nb = self._repack(lot, order, 0, variant)
                        if nb is not None:
                            break
                    if nb is not None:
                        break
                if nb is not None:
                    remaining[lot.lot_id] -= 1
                    remaining[b.lot.lot_id] += 1
                    bins[i] = nb
                    break
        self._renumber(bins)

    @staticmethod
    def _renumber(bins: list) -> None:
        """Renumera instancias por lote según la secuencia de corte."""
        counter: dict[int, int] = {}
        for b in bins:
            counter[b.lot.lot_id] = counter.get(b.lot.lot_id, 0) + 1
            b.instance = counter[b.lot.lot_id]

    def _try_add(self, b: _Bin, extra: list):
        """Intenta meter ``extra`` (tuplas de ``placed``) en la hoja ``b``.
        Primero sobre el acomodo actual (barato); si no cabe, re-acomoda la hoja
        completa con varias órdenes de inserción × variantes del empaquetador."""
        k = self.pp.kerf
        packer = b.packer.clone()
        added = []
        for uid, _x, _y, w, h, r in extra:
            p = self.pieces[uid]
            pos = None
            for rr in _orients(p, r):
                ww, hh = _dims(p, rr)
                pos = packer.insert(ww + k, hh + k)
                if pos is not None:
                    added.append((uid, pos[0], pos[1], ww, hh, rr))
                    break
            if pos is None:
                break
        else:
            nb = _Bin(b.lot, b.instance, packer, b.placed + added)
            return nb
        items = b.placed + list(extra)
        for order in self._orderings(items):
            for variant in self.packer_cls.VARIANTS:
                nb = self._repack(b.lot, order, b.instance, variant)
                if nb is not None:
                    return nb
        return None

    def _fill(self, b: _Bin) -> float:
        return sum(w * h for _u, _x, _y, w, h, _r in b.placed) / b.lot.area

    def _eliminate_sheets(self, bins: list, unplaced: list, remaining: dict,
                          deadline: float | None = None) -> None:
        """Búsqueda local de *vaciado de hojas* (la que más reduce la merma real).

        Toma la hoja menos aprovechada e intenta repartir todas sus piezas en
        las demás hojas abiertas (sin abrir ninguna nueva). Si lo logra, la hoja
        se devuelve al inventario. Sólo se acepta el movimiento si la aptitud
        total (merma + cola + retazos) mejora. Se repite hasta que ninguna hoja
        pueda vaciarse o se agote el tiempo."""
        if len(bins) < 2:
            return
        k = self.pp.kerf
        cur = self._fitness_of(bins, unplaced)
        improved = True
        while improved and len(bins) > 1:
            improved = False
            for vi in sorted(range(len(bins)), key=lambda i: self._fill(bins[i])):
                if deadline is not None and time.perf_counter() > deadline:
                    return
                victim = bins[vi]
                others = [b for j, b in enumerate(bins) if j != vi]
                free_area = sum(b.packer.W * b.packer.H for b in others) - sum(
                    (w + k) * (h + k) for b in others for _u, _x, _y, w, h, _r in b.placed)
                need = sum((w + k) * (h + k) for _u, _x, _y, w, h, _r in victim.placed)
                if need > free_area + EPS:
                    continue
                trial = list(others)
                ok = True
                for item in sorted(victim.placed, key=lambda t: -t[3] * t[4]):
                    a = (item[3] + k) * (item[4] + k)
                    # best-fit: primero las hojas con menos espacio libre que aún alcanza
                    cand = sorted(range(len(trial)), key=lambda j: trial[j].packer.W * trial[j].packer.H
                                  - sum((w + k) * (h + k) for _u, _x, _y, w, h, _r in trial[j].placed))
                    placed = False
                    for j in cand:
                        tb = trial[j]
                        if tb.packer.W * tb.packer.H - sum((w + k) * (h + k) for _u, _x, _y, w, h, _r in tb.placed) < a - EPS:
                            continue
                        nb = self._try_add(tb, [item])
                        if nb is not None:
                            trial[j] = nb
                            placed = True
                            break
                    if not placed:
                        ok = False
                        break
                if not ok:
                    continue
                f = self._fitness_of(trial, unplaced)
                if f < cur - 1e-9:
                    remaining[victim.lot.lot_id] += 1
                    bins[:] = trial
                    cur = f
                    improved = True
                    break

    def _fitness_of(self, bins: list, unplaced: list) -> float:
        r = Decoded(list(bins), list(unplaced))
        self._evaluate(r)
        return r.fitness

    def _orderings(self, placed: list) -> list:
        """Órdenes de inserción candidatos para re-acomodar una hoja."""
        dims = lambda t: (t[3], t[4])
        return [
            placed,
            sorted(placed, key=lambda t: (-dims(t)[1], -dims(t)[0])),       # alto desc
            sorted(placed, key=lambda t: (-dims(t)[0], -dims(t)[1])),       # ancho desc
            sorted(placed, key=lambda t: -dims(t)[0] * dims(t)[1]),         # área desc
            sorted(placed, key=lambda t: -max(dims(t))),                    # lado mayor desc
            sorted(placed, key=lambda t: (-min(dims(t)), -max(dims(t)))),  # lado menor desc
        ]

    def _waste(self, b: _Bin) -> float:
        used = sum(w * h for _u, _x, _y, w, h, _r in b.placed)
        off = sum(r[2] * r[3] for r in self.bin_offcuts(b))
        return b.lot.area - used - off

    def _polish(self, bins: list) -> None:
        """Búsqueda local por hoja: re-acomoda las mismas piezas con varias
        combinaciones (orden de inserción × regla de ajuste × regla de partición)
        y conserva la que deja menos merma neta, es decir, la que concentra el
        sobrante en retazos recuperables (RN-06). Nunca empeora una hoja."""
        for i, b in enumerate(bins):
            best, best_w = b, self._waste(b)
            if best_w <= EPS:
                continue
            for order in self._orderings(b.placed):
                for variant in self.packer_cls.VARIANTS:
                    nb = self._repack(b.lot, order, b.instance, variant)
                    if nb is None:
                        continue
                    w = self._waste(nb)
                    if w < best_w - 1.0:          # mejora de al menos 1 mm²
                        best, best_w = nb, w
            bins[i] = best

    # ---------------------------------------------------------------- fitness
    def bin_offcuts(self, b: _Bin) -> list:
        pp = self.pp
        free = b.packer.merged_free() if pp.cut_mode == "guillotina" else b.packer.free_rects()
        return extract_offcuts(free, pp.offcut_min_long, pp.offcut_min_short,
                               disjoint=(pp.cut_mode == "guillotina"))

    def _evaluate(self, res: Decoded) -> None:
        """Aptitud (a minimizar), normalizada por el área total de piezas:

        F = w1·[A_merma + (1 − w3)·A_retazo] / A_piezas
            + w2·Σ P_i·ΔT_i + w_p·pendientes − ε·Σ u_j² / n

        * Todo el material consumido que no se convierte en pieza cuesta: la
          merma completa y el retazo sólo parcialmente (w3 = valor de
          recuperación del retazo, 0 ≤ w3 < 1). Con la fórmula anterior el
          retazo *restaba*, lo que premiaba abrir hojas casi vacías.
        * El término −ε·Σu²/n (u = aprovechamiento de cada hoja) es el
          desempate clásico de bin packing: entre soluciones con el mismo
          consumo prefiere concentrar piezas y dejar una hoja casi vacía, lo
          que da al AG un gradiente hacia eliminar hojas completas.
        """
        pp = self.pp
        pieces = self.pieces
        sheet_area = used_area = offcut_area = 0.0
        fill_sq = 0.0
        completion: dict[int, int] = {}
        for bi, b in enumerate(res.bins):
            a = b.lot.area
            used = 0.0
            for uid, _x, _y, w, h, _r in b.placed:
                used += w * h
                oid = pieces[uid].order_id
                if completion.get(oid, -1) < bi:
                    completion[oid] = bi
            off = sum(r[2] * r[3] for r in self.bin_offcuts(b))
            sheet_area += a
            used_area += used
            offcut_area += off
            fill_sq += (used / a) ** 2
        merma_area = max(0.0, sheet_area - used_area - offcut_area)
        pending = 0.0
        never = len(res.bins) + 1
        for uid in res.unplaced:
            p = pieces[uid]
            pending += p.area * p.priority_weight
            completion[p.order_id] = never
        pending /= self.total_piece_area

        # ΔT_i: retraso de la orden i (en posiciones) respecto a su lugar en la
        # cola, descontando la ventana de tolerancia de agrupamiento (RN-04).
        orders = sorted(completion, key=lambda o: (completion[o], self.order_rank[o]))
        delay = 0.0
        for new_rank, oid in enumerate(orders):
            d = new_rank - self.order_rank[oid] - pp.tolerance_window
            if d > 0:
                delay += self.order_weight[oid] * d
        w3 = min(max(pp.w3, 0.0), 0.9)       # un retazo nunca vale tanto como no consumir la hoja
        consumo = (merma_area + (1.0 - w3) * offcut_area) / self.total_piece_area
        compact = (fill_sq / len(res.bins)) if res.bins else 0.0
        fitness = pp.w1 * consumo + pp.w2 * delay + pp.w_pending * pending - 0.02 * compact
        res.fitness = fitness
        res.components = {
            "merma": merma_area / sheet_area if sheet_area else 0.0,
            "desplazamiento": delay,
            "retazo": offcut_area / sheet_area if sheet_area else 0.0,
            "aprovechamiento": used_area / sheet_area if sheet_area else 0.0,
            "pendientes": pending, "hojas": len(res.bins),
        }

    # ------------------------------------------------------------- resultados
    def to_sheets(self, res: Decoded) -> list[PackedSheet]:
        pp = self.pp
        off = pp.trim + pp.kerf
        sheets = []
        for b in res.bins:
            ps = PackedSheet(lot=b.lot, instance=b.instance)
            for uid, x, y, w, h, r in b.placed:
                ps.placements.append(Placement(uid, round(x + off, 3), round(y + off, 3), w, h, bool(r)))
            ps.free = [(x + off, y + off, w, h) for x, y, w, h in b.packer.free_rects()]
            ps.offcuts = [(round(x + off, 3), round(y + off, 3), round(w, 3), round(h, 3))
                          for x, y, w, h in self.bin_offcuts(b)]
            sheets.append(ps)
        return sheets
