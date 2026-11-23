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
    def _new_bin(self, lot: SheetLot, instance: int) -> _Bin:
        iw, ih = inner_size(lot, self.pp)
        return _Bin(lot, instance, self.packer_cls(iw, ih))

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

    def decode(self, chrom: list, finalize: bool = False) -> Decoded:
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
            self._downsize(bins, remaining)
        res = Decoded(bins, unplaced)
        self._evaluate(res)
        return res

    def _repack(self, lot: SheetLot, placed: list, instance: int):
        b = self._new_bin(lot, instance)
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
        """Mejora local: sustituye cada hoja virgen por la más pequeña disponible
        que contenga exactamente las mismas piezas."""
        for i, b in enumerate(bins):
            if b.lot.is_offcut:
                continue
            for lot in self.virgin_lots:
                if lot.area >= b.lot.area - EPS or remaining[lot.lot_id] <= 0:
                    continue
                nb = self._repack(lot, b.placed, 0)
                if nb is not None:
                    remaining[lot.lot_id] -= 1
                    remaining[b.lot.lot_id] += 1
                    bins[i] = nb
                    break
        # Renumerar instancias por lote según la secuencia de corte
        counter: dict[int, int] = {}
        for b in bins:
            counter[b.lot.lot_id] = counter.get(b.lot.lot_id, 0) + 1
            b.instance = counter[b.lot.lot_id]

    # ---------------------------------------------------------------- fitness
    def bin_offcuts(self, b: _Bin) -> list:
        pp = self.pp
        free = b.packer.merged_free() if pp.cut_mode == "guillotina" else b.packer.free_rects()
        return extract_offcuts(free, pp.offcut_min_long, pp.offcut_min_short,
                               disjoint=(pp.cut_mode == "guillotina"))

    def _evaluate(self, res: Decoded) -> None:
        """F(x) = w1·Σ(A_merma/A_hoja) + w2·Σ P_i·ΔT_i − w3·ΣA_retazo/ΣA_hoja + w_p·pendientes"""
        pp = self.pp
        pieces = self.pieces
        merma_sum = 0.0
        sheet_area = 0.0
        offcut_area = 0.0
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
            merma_sum += max(0.0, a - used - off) / a
            sheet_area += a
            offcut_area += off
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
        retazo = offcut_area / sheet_area if sheet_area else 0.0
        fitness = pp.w1 * merma_sum + pp.w2 * delay - pp.w3 * retazo + pp.w_pending * pending
        res.fitness = fitness
        res.components = {
            "merma": merma_sum, "desplazamiento": delay, "retazo": retazo,
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
