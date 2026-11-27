"""Empaquetadores de una sola hoja (decodificadores constructivos).

Ambos trabajan en *coordenadas internas* de la hoja: el área útil después de
retirar el refilado perimetral y el kerf de la línea de refilado. Las piezas se
insertan *infladas* por el kerf (w + kerf, h + kerf), con lo cual entre dos
piezas adyacentes, y entre una pieza y cualquier borde de refilado, siempre
queda al menos un kerf de separación (RN-03).

* ``GuillotineBin``  – Guillotine Recursive Split. Cada colocación parte el
  rectángulo libre con un único corte recto de extremo a extremo, por lo que el
  patrón resultante es siempre cortable en guillotina (RN-05, seccionadora).
* ``MaxRectsBin``    – Bottom-Left-Fill sobre rectángulos libres maximales
  (anidamiento libre para router CNC).
"""
from __future__ import annotations

EPS = 1e-6

Rect = tuple  # (x, y, w, h)


class GuillotineBin:
    """``fit``: baf (Best Area Fit), bssf (Best Short Side Fit), blsf (Best Long Side Fit).
    ``split``: maxarea (sobrante continuo mayor), minarea, horizontal, vertical."""
    __slots__ = ("W", "H", "free", "fit", "split")

    VARIANTS = [(f, s) for f in ("baf", "bssf", "blsf") for s in ("maxarea", "minarea", "horizontal", "vertical")]

    def __init__(self, W: float, H: float, fit: str = "baf", split: str = "maxarea"):
        self.W = W
        self.H = H
        self.fit = fit
        self.split = split
        self.free: list[Rect] = [(0.0, 0.0, W, H)] if W > EPS and H > EPS else []

    def clone(self) -> "GuillotineBin":
        c = GuillotineBin.__new__(GuillotineBin)
        c.W, c.H, c.fit, c.split, c.free = self.W, self.H, self.fit, self.split, list(self.free)
        return c

    def can_fit(self, w: float, h: float) -> bool:
        for fx, fy, fw, fh in self.free:
            if fw + EPS >= w and fh + EPS >= h:
                return True
        return False

    def insert(self, w: float, h: float):
        """Coloca un rectángulo (ya inflado). Devuelve (x, y) o None."""
        best_i = -1
        best_a = best_b = float("inf")
        fit = self.fit
        for i, (fx, fy, fw, fh) in enumerate(self.free):
            if fw + EPS >= w and fh + EPS >= h:
                area = fw * fh - w * h
                short = min(fw - w, fh - h)
                long_ = max(fw - w, fh - h)
                if fit == "baf":
                    a, b = area, short
                elif fit == "bssf":
                    a, b = short, long_
                else:
                    a, b = long_, short
                if a < best_a - EPS or (abs(a - best_a) <= EPS and b < best_b):
                    best_i, best_a, best_b = i, a, b
        if best_i < 0:
            return None
        fx, fy, fw, fh = self.free.pop(best_i)
        rw, bh = fw - w, fh - h
        # Regla de partición. Por defecto "Maximizar área": se elige el corte
        # que deja el sobrante continuo más grande (favorece retazos, RN-06).
        horiz_best = max(fw * bh, rw * h)   # corte horizontal a todo lo ancho
        vert_best = max(rw * fh, w * bh)    # corte vertical a todo lo alto
        sp = self.split
        if sp == "maxarea":
            horizontal = horiz_best >= vert_best
        elif sp == "minarea":
            horizontal = horiz_best < vert_best
        else:
            horizontal = sp == "horizontal"
        if horizontal:
            bottom = (fx, fy + h, fw, bh)
            right = (fx + w, fy, rw, h)
        else:
            right = (fx + w, fy, rw, fh)
            bottom = (fx, fy + h, w, bh)
        for r in (bottom, right):
            if r[2] > EPS and r[3] > EPS:
                self.free.append(r)
        return fx, fy

    def merged_free(self) -> list[Rect]:
        """Une rectángulos libres adyacentes que comparten una arista completa
        (sobrantes contiguos separados sólo por cortes virtuales)."""
        return merge_adjacent(self.free)

    def free_rects(self) -> list[Rect]:
        return list(self.free)


def merge_adjacent(rects: list[Rect]) -> list[Rect]:
    out = list(rects)
    changed = True
    while changed:
        changed = False
        for i in range(len(out)):
            a = out[i]
            for j in range(i + 1, len(out)):
                b = out[j]
                m = None
                if abs(a[1] - b[1]) <= EPS and abs(a[3] - b[3]) <= EPS:
                    if abs(a[0] + a[2] - b[0]) <= EPS:
                        m = (a[0], a[1], a[2] + b[2], a[3])
                    elif abs(b[0] + b[2] - a[0]) <= EPS:
                        m = (b[0], a[1], a[2] + b[2], a[3])
                elif abs(a[0] - b[0]) <= EPS and abs(a[2] - b[2]) <= EPS:
                    if abs(a[1] + a[3] - b[1]) <= EPS:
                        m = (a[0], a[1], a[2], a[3] + b[3])
                    elif abs(b[1] + b[3] - a[1]) <= EPS:
                        m = (a[0], b[1], a[2], a[3] + b[3])
                if m is not None:
                    out[i] = m
                    out.pop(j)
                    changed = True
                    break
            if changed:
                break
    return out


def _intersects(a: Rect, b: Rect) -> bool:
    return (a[0] < b[0] + b[2] - EPS and b[0] < a[0] + a[2] - EPS
            and a[1] < b[1] + b[3] - EPS and b[1] < a[1] + a[3] - EPS)


def _contains(a: Rect, b: Rect) -> bool:
    """¿a contiene a b?"""
    return (b[0] >= a[0] - EPS and b[1] >= a[1] - EPS
            and b[0] + b[2] <= a[0] + a[2] + EPS and b[1] + b[3] <= a[1] + a[3] + EPS)


def subtract_rect(free: list[Rect], used: Rect) -> list[Rect]:
    """Resta ``used`` de una lista de rectángulos libres maximales (MaxRects)."""
    ux, uy, uw, uh = used
    keep: list[Rect] = []
    new: list[Rect] = []
    for f in free:
        if not _intersects(f, used):
            keep.append(f)
            continue
        fx, fy, fw, fh = f
        if ux > fx + EPS:
            new.append((fx, fy, ux - fx, fh))
        if ux + uw < fx + fw - EPS:
            new.append((ux + uw, fy, fx + fw - (ux + uw), fh))
        if uy > fy + EPS:
            new.append((fx, fy, fw, uy - fy))
        if uy + uh < fy + fh - EPS:
            new.append((fx, uy + uh, fw, fy + fh - (uy + uh)))
    # Poda: sólo los rectángulos nuevos pueden quedar contenidos en otros.
    pruned_new: list[Rect] = []
    for i, r in enumerate(new):
        dominated = False
        for k in keep:
            if _contains(k, r):
                dominated = True
                break
        if not dominated:
            for j, s in enumerate(new):
                if i != j and _contains(s, r) and (not _contains(r, s) or j < i):
                    dominated = True
                    break
        if not dominated:
            pruned_new.append(r)
    return keep + pruned_new


class MaxRectsBin:
    """``fit``: bl (Bottom-Left), baf, bssf, blsf."""
    __slots__ = ("W", "H", "free", "fit")

    VARIANTS = [(f, None) for f in ("bl", "baf", "bssf", "blsf")]

    def __init__(self, W: float, H: float, fit: str = "bl", split=None):
        self.W = W
        self.H = H
        self.fit = fit
        self.free: list[Rect] = [(0.0, 0.0, W, H)] if W > EPS and H > EPS else []

    def clone(self) -> "MaxRectsBin":
        c = MaxRectsBin.__new__(MaxRectsBin)
        c.W, c.H, c.fit, c.free = self.W, self.H, self.fit, list(self.free)
        return c

    def can_fit(self, w: float, h: float) -> bool:
        for fx, fy, fw, fh in self.free:
            if fw + EPS >= w and fh + EPS >= h:
                return True
        return False

    def insert(self, w: float, h: float):
        best = None
        best_y = best_x = float("inf")
        fit = self.fit
        for fx, fy, fw, fh in self.free:
            if fw + EPS >= w and fh + EPS >= h:
                if fit == "bl":
                    # Bottom-Left: menor "techo" (y + h), luego menor x.
                    a, b = fy + h, fx
                elif fit == "baf":
                    a, b = fw * fh - w * h, min(fw - w, fh - h)
                elif fit == "bssf":
                    a, b = min(fw - w, fh - h), max(fw - w, fh - h)
                else:
                    a, b = max(fw - w, fh - h), min(fw - w, fh - h)
                if a < best_y - EPS or (abs(a - best_y) <= EPS and b < best_x):
                    best, best_y, best_x = (fx, fy), a, b
        if best is None:
            return None
        self.free = subtract_rect(self.free, (best[0], best[1], w, h))
        return best

    def free_rects(self) -> list[Rect]:
        return list(self.free)


PACKERS = {"guillotina": GuillotineBin, "libre": MaxRectsBin}


def extract_offcuts(free: list[Rect], min_long: float, min_short: float, disjoint: bool) -> list[Rect]:
    """Clasifica sobrantes rectangulares continuos como retazos (RN-06).

    Para Guillotina los rectángulos libres ya son disjuntos. Para MaxRects se
    eligen vorazmente los más grandes y se restan del resto para no contar dos
    veces la misma área.
    """
    def qualifies(r: Rect) -> bool:
        return max(r[2], r[3]) + EPS >= min_long and min(r[2], r[3]) + EPS >= min_short

    if disjoint:
        return sorted((r for r in free if qualifies(r)), key=lambda r: -r[2] * r[3])
    out: list[Rect] = []
    cand = [r for r in free if qualifies(r)]
    pool = list(free)
    while cand:
        best = max(cand, key=lambda r: r[2] * r[3])
        out.append(best)
        pool = subtract_rect(pool, best)
        cand = [r for r in pool if qualifies(r)]
    return out
