"""Pruebas del motor: geometría, reglas de negocio, operadores genéticos y rendimiento."""
import random
import time

import pytest

from app.engine.decoder import Decoder
from app.engine.ga import mutate, order_crossover, pmx_crossover, run_ga, seed_chromosomes
from app.engine.model import GAParams, MaterialKey, Piece, ProcessParams, SheetLot, Subproblem
from app.engine.optimizer import optimize
from app.engine.packers import GuillotineBin, MaxRectsBin, extract_offcuts
from app.engine.validation import _is_guillotine, validate_sheet


def make_sub(n_orders=20, per=5, directional=False, seed=0, lots=None):
    rng = random.Random(seed)
    pieces = []
    for o in range(n_orders):
        w, h = rng.choice([(89, 1800), (600, 1500), (900, 1200), (300, 700), (1200, 400), (450, 450)])
        for _ in range(per):
            pieces.append(Piece(len(pieces), o, f"O{o}", "c", "vertical", "p", w, h, not directional, o,
                                3.0 if o % 7 == 0 else 1.0, "K"))
    lots = lots or [SheetLot(1, "A", 1220, 2440, 3, "b", "l", directional, False, 40),
                    SheetLot(2, "B", 1500, 3000, 3, "b", "l", directional, False, 10),
                    SheetLot(3, "R", 800, 900, 3, "b", "l", directional, True, 3)]
    return Subproblem(MaterialKey.of("b", "l", 3), pieces, lots, directional)


def sheets_of(sub, pp, chrom=None):
    dec = Decoder(sub, pp)
    chrom = chrom or seed_chromosomes(sub, pp.tolerance_window)[0]
    res = dec.decode(chrom, finalize=True)
    out = []
    for ps in dec.to_sheets(res):
        pls = []
        for pl in ps.placements:
            pc = sub.pieces[pl.piece_uid]
            pls.append({"order_number": pc.order_number, "x": pl.x, "y": pl.y, "w": pl.w, "h": pl.h,
                        "rotated": pl.rotated, "nominal_w": pc.w, "nominal_h": pc.h})
        out.append({"sheet_id": ps.lot.code, "w": ps.lot.w, "h": ps.lot.h, "kerf": pp.kerf, "trim": pp.trim,
                    "directional": ps.lot.directional, "cut_mode": pp.cut_mode, "placements": pls,
                    "offcuts": [{"x": r[0], "y": r[1], "w": r[2], "h": r[3]} for r in ps.offcuts]})
    return res, out


# ------------------------------------------------------------- empaquetadores
@pytest.mark.parametrize("cls", [GuillotineBin, MaxRectsBin])
def test_packer_no_overlap_random(cls):
    rng = random.Random(42)
    for _ in range(30):
        b = cls(1000, 2000)
        placed = []
        for _ in range(60):
            w, h = rng.randint(20, 400), rng.randint(20, 600)
            pos = b.insert(w, h)
            if pos:
                placed.append((pos[0], pos[1], w, h))
        for i, a in enumerate(placed):
            assert a[0] >= -1e-9 and a[1] >= -1e-9 and a[0] + a[2] <= 1000 + 1e-6 and a[1] + a[3] <= 2000 + 1e-6
            for c in placed[i + 1:]:
                assert not (a[0] < c[0] + c[2] - 1e-6 and c[0] < a[0] + a[2] - 1e-6
                            and a[1] < c[1] + c[3] - 1e-6 and c[1] < a[1] + a[3] - 1e-6)
        if cls is GuillotineBin:
            assert _is_guillotine(placed, 0, 0, 1000, 2000)


def test_offcuts_do_not_overlap_maxrects():
    b = MaxRectsBin(1000, 1000)
    b.insert(400, 300)
    b.insert(200, 700)
    offs = extract_offcuts(b.free_rects(), 200, 100, disjoint=False)
    assert offs
    for i, a in enumerate(offs):
        for c in offs[i + 1:]:
            assert not (a[0] < c[0] + c[2] and c[0] < a[0] + a[2] and a[1] < c[1] + c[3] and c[1] < a[1] + a[3])


# ----------------------------------------------------- reglas de negocio
@pytest.mark.parametrize("mode", ["guillotina", "libre"])
@pytest.mark.parametrize("directional", [False, True])
def test_layouts_valid_kerf_trim_rotation(mode, directional):
    """RN-02, RN-03, RN-05 y RNF-04 sobre soluciones del AG."""
    sub = make_sub(directional=directional, seed=3)
    pp = ProcessParams(cut_mode=mode, kerf=4, trim=12)
    res = run_ga(sub, pp, GAParams(population=20, max_generations=15, time_limit_s=10, seed=1))
    dec = Decoder(sub, pp)
    for ps in dec.to_sheets(res.decoded):
        sheet = {"sheet_id": ps.lot.code, "w": ps.lot.w, "h": ps.lot.h, "kerf": pp.kerf, "trim": pp.trim,
                 "directional": ps.lot.directional, "cut_mode": mode,
                 "offcuts": [{"x": r[0], "y": r[1], "w": r[2], "h": r[3]} for r in ps.offcuts],
                 "placements": [{"order_number": "x", "x": p.x, "y": p.y, "w": p.w, "h": p.h, "rotated": p.rotated,
                                 "nominal_w": sub.pieces[p.piece_uid].w, "nominal_h": sub.pieces[p.piece_uid].h}
                                for p in ps.placements]}
        assert validate_sheet(sheet) == []
        if directional:
            assert not any(p.rotated for p in ps.placements)


def test_validator_detects_overlap_and_kerf():
    base = {"sheet_id": "T", "w": 1000, "h": 1000, "kerf": 3, "trim": 10, "directional": True, "cut_mode": "libre",
            "offcuts": []}
    p = lambda x, y, w, h, rot=False: {"order_number": "o", "x": x, "y": y, "w": w, "h": h, "rotated": rot,
                                       "nominal_w": h if rot else w, "nominal_h": w if rot else h}
    ok = dict(base, placements=[p(13, 13, 100, 100), p(116, 13, 100, 100)])
    assert validate_sheet(ok) == []
    tight = dict(base, placements=[p(13, 13, 100, 100), p(114, 13, 100, 100)])     # 1 mm < kerf
    assert any("kerf" in e for e in validate_sheet(tight))
    outside = dict(base, placements=[p(5, 13, 100, 100)])
    assert any("fuera" in e for e in validate_sheet(outside))
    rotated = dict(base, placements=[p(13, 13, 200, 100, rot=True)])
    assert any("RN-02" in e for e in validate_sheet(rotated))
    non_guillotine = dict(base, cut_mode="guillotina", directional=False, placements=[
        p(13, 13, 600, 200), p(616, 13, 200, 600), p(416, 416, 600, 200), p(13, 216, 200, 600), p(216, 216, 197, 197)])
    assert any("guillotina" in e for e in validate_sheet(non_guillotine))


def test_material_homogeneity_partition():
    """RN-01 / RF-07: nunca se asigna una pieza a una hoja de otro color/textura/espesor."""
    lots = [
        {"id": 1, "code": "B", "height": 2440, "width": 1220, "thickness": 3, "color": "Blanco", "texture": "Liso",
         "directional": False, "is_offcut": False, "quantity": 5, "unit_cost": 100},
        {"id": 2, "code": "N", "height": 2440, "width": 1220, "thickness": 3, "color": "Nogal", "texture": "Veta",
         "directional": True, "is_offcut": False, "quantity": 5, "unit_cost": 100},
        {"id": 3, "code": "B4", "height": 2440, "width": 1220, "thickness": 4, "color": "Blanco", "texture": "Liso",
         "directional": False, "is_offcut": False, "quantity": 5, "unit_cost": 100},
    ]
    o = lambda i, c, t, th: {"id": i, "order_number": f"O{i}", "client": "", "blind_type": "vertical", "width": 900,
                             "height": 1500, "quantity": 1, "color": c, "texture": t, "thickness": th,
                             "priority": "Normal", "queue_pos": i, "manual_pieces": None, "cut_pieces": {}}
    orders = [o(1, "blanco ", "LISO", 3), o(2, "Nogal", "Veta", 3), o(3, "Blanco", "Liso", 4), o(4, "Rojo", "Liso", 3)]
    settings = __import__("app.db", fromlist=["DEFAULT_SETTINGS"]).DEFAULT_SETTINGS
    res = optimize(orders, lots, settings, "T", use_ga=False)
    lot_by_id = {l["id"]: l for l in lots}
    ord_by_id = {x["id"]: x for x in orders}
    for s in res["hojas"]:
        lot = lot_by_id[s["lot_id"]]
        for p in s["placements"]:
            od = ord_by_id[p["order_id"]]
            assert od["color"].strip().lower() == lot["color"].lower()
            assert od["texture"].strip().lower() == lot["texture"].lower()
            assert od["thickness"] == lot["thickness"]
    estados = {x["order_number"]: x["estado"] for x in res["ordenes"]}
    assert estados["O4"] == "pendiente"
    assert any("Sin hojas" in p["reason"] for p in res["pendientes"])


def test_offcuts_consumed_first():
    """RF-03: los retazos se abren antes que hojas nuevas."""
    sub = make_sub(n_orders=2, per=1, lots=[SheetLot(1, "A", 1220, 2440, 3, "b", "l", False, False, 5),
                                           SheetLot(2, "R", 1300, 1300, 3, "b", "l", False, True, 1)])
    sub.pieces[:] = [Piece(0, 0, "O0", "", "x", "p", 500, 500, True, 0, 1, "K")]
    res = Decoder(sub, ProcessParams()).decode([(0, False)])
    assert res.bins[0].lot.is_offcut


def test_fifo_queue_respected_in_baseline():
    """RN-04: la secuencia de hojas respeta el orden de la cola fuera de la ventana."""
    sub = make_sub(n_orders=12, per=4, seed=5)
    pp = ProcessParams(tolerance_window=0)
    res = run_ga(sub, pp, GAParams(population=20, max_generations=30, time_limit_s=10, seed=2))
    assert res.decoded.components["desplazamiento"] <= res.baseline.components["desplazamiento"] + 5


def test_ga_never_worse_than_baseline():
    sub = make_sub(seed=9)
    pp = ProcessParams()
    res = run_ga(sub, pp, GAParams(population=24, max_generations=40, time_limit_s=10, seed=4))
    assert res.decoded.fitness <= res.baseline.fitness + 1e-9
    assert res.history == sorted(res.history, reverse=True)      # el mejor nunca empeora (elitismo)


# --------------------------------------------------------------- operadores
@pytest.mark.parametrize("cx", [order_crossover, pmx_crossover])
def test_crossover_preserves_permutation(cx):
    rng = random.Random(7)
    n = 50
    for _ in range(200):
        p1 = [(i, rng.random() < .5) for i in rng.sample(range(n), n)]
        p2 = [(i, rng.random() < .5) for i in rng.sample(range(n), n)]
        child = cx(p1, p2, rng)
        assert sorted(g[0] for g in child) == list(range(n))


def test_mutation_respects_grain():
    rng = random.Random(1)
    rot = [False] * 30
    c = [(i, False) for i in range(30)]
    for _ in range(500):
        c = mutate(c, rot, rng)
        assert sorted(g[0] for g in c) == list(range(30))
        assert not any(g[1] for g in c)


# --------------------------------------------------------------- rendimiento
def test_performance_200_pieces_50_sheets():
    """RNF-01: ≤ 30 s para 200 piezas y 50 hojas en inventario."""
    sub = make_sub(n_orders=40, per=5, seed=11)
    assert len(sub.pieces) == 200 and sum(l.quantity for l in sub.lots) >= 50
    t = time.perf_counter()
    res = run_ga(sub, ProcessParams(), GAParams(time_limit_s=25, seed=1))
    assert time.perf_counter() - t <= 30
    assert not res.decoded.unplaced


@pytest.mark.parametrize("mode", ["guillotina", "libre"])
@pytest.mark.parametrize("directional", [False, True])
def test_polish_never_worse_and_valid(mode, directional):
    """La búsqueda local por hoja conserva piezas, reglas y nunca sube la merma."""
    sub = make_sub(directional=directional, seed=5)
    pp = ProcessParams(cut_mode=mode, kerf=3, trim=10)
    dec = Decoder(sub, pp)
    chrom = seed_chromosomes(sub, pp.tolerance_window)[0]
    raw = dec.decode(chrom, finalize=False)
    dec._downsize(raw.bins, {l.lot_id: 0 for l in sub.lots})   # mismas hojas que finalize
    before = [(sorted(t[0] for t in b.placed), dec._waste(b)) for b in raw.bins]
    dec._polish(raw.bins)
    for (uids, w0), b in zip(before, raw.bins):
        assert sorted(t[0] for t in b.placed) == uids
        assert dec._waste(b) <= w0 + 1e-6
    _res, sheets = sheets_of(sub, pp, chrom)
    for s in sheets:
        assert validate_sheet(s) == []


@pytest.mark.parametrize("mode", ["guillotina", "libre"])
@pytest.mark.parametrize("directional", [False, True])
def test_sheet_elimination_never_worse_and_valid(mode, directional):
    """El vaciado de hojas no aumenta el nº de hojas, no pierde piezas y
    produce patrones válidos (kerf, refilado, guillotina, veta)."""
    pp = ProcessParams(cut_mode=mode)
    for seed in range(3):
        sub = make_sub(n_orders=12, per=4, directional=directional, seed=seed)
        dec = Decoder(sub, pp)
        chrom = seed_chromosomes(sub, pp.tolerance_window)[0]
        plain = dec.decode(chrom, finalize=True, eliminate=False)
        better = dec.decode(chrom, finalize=True)
        assert len(better.bins) <= len(plain.bins)
        assert better.fitness <= plain.fitness + 1e-9
        assert sorted(t[0] for b in better.bins for t in b.placed) == sorted(
            t[0] for b in plain.bins for t in b.placed)
        _res, sheets = sheets_of(sub, pp, chrom)
        for s in sheets:
            assert validate_sheet(s) == []


def test_theoretical_bound_above_actual():
    from app.engine.optimizer import theoretical_bound
    pp = ProcessParams()
    sub = make_sub(n_orders=10, per=4, seed=3)
    res = Decoder(sub, pp).decode(seed_chromosomes(sub, pp.tolerance_window)[0], finalize=True)
    used = sum(w * h for b in res.bins for _u, _x, _y, w, h, _r in b.placed)
    actual = used / sum(b.lot.area for b in res.bins)
    tb = theoretical_bound(sub, pp)
    teorico = tb["area_piezas"] / tb["area_hojas_min"]
    assert actual <= teorico < 1.0


@pytest.mark.parametrize("mode", ["guillotina", "libre"])
@pytest.mark.parametrize("directional", [False, True])
def test_memetic_ga_layout_valid(mode, directional):
    """El patrón que entrega el AG memético (gen de heurística + búsqueda local
    lamarckiana) es válido y contiene todas las piezas exactamente una vez."""
    pp = ProcessParams(cut_mode=mode)
    sub = make_sub(n_orders=10, per=4, directional=directional, seed=5)
    res = run_ga(sub, pp, GAParams(population=16, max_generations=20, time_limit_s=5, seed=3))
    uids = sorted([t[0] for b in res.decoded.bins for t in b.placed] + res.decoded.unplaced)
    assert uids == list(range(len(sub.pieces)))
    assert res.decoded.fitness <= res.baseline.fitness + 1e-9
    dec = Decoder(sub, pp)
    for ps in dec.to_sheets(res.decoded):
        pls = [{"order_number": sub.pieces[pl.piece_uid].order_number, "x": pl.x, "y": pl.y, "w": pl.w,
                "h": pl.h, "rotated": pl.rotated, "nominal_w": sub.pieces[pl.piece_uid].w,
                "nominal_h": sub.pieces[pl.piece_uid].h} for pl in ps.placements]
        sheet = {"sheet_id": ps.lot.code, "w": ps.lot.w, "h": ps.lot.h, "kerf": pp.kerf, "trim": pp.trim,
                 "directional": ps.lot.directional, "cut_mode": mode, "placements": pls,
                 "offcuts": [{"x": r[0], "y": r[1], "w": r[2], "h": r[3]} for r in ps.offcuts]}
        assert validate_sheet(sheet) == []
