"""Estructuras de datos del motor de optimización (independientes de la BD)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


def norm(s: str | None) -> str:
    return " ".join((s or "").strip().split()).casefold()


@dataclass(frozen=True)
class MaterialKey:
    """Clave de homogeneidad de material (RN-01 / RF-07)."""
    color: str
    texture: str
    thickness: float

    @staticmethod
    def of(color: str, texture: str, thickness: float) -> "MaterialKey":
        return MaterialKey(norm(color), norm(texture), round(float(thickness), 3))

    def label(self) -> str:
        return f"{self.color.title()} / {self.texture.title()} / {self.thickness:g} mm"


@dataclass(frozen=True)
class Piece:
    uid: int                 # índice dentro del subproblema
    order_id: int
    order_number: str
    client: str
    blind_type: str
    name: str
    w: float                 # ancho (eje X de la hoja)
    h: float                 # alto (eje Y de la hoja = sentido de la veta)
    rotatable: bool
    queue_rank: int          # posición relativa de la orden en la cola del grupo
    priority_weight: float
    piece_key: str           # identifica el tipo de pieza dentro de la orden

    @property
    def area(self) -> float:
        return self.w * self.h


@dataclass(frozen=True)
class SheetLot:
    """Lote de hojas (o retazo) disponible para un subproblema."""
    lot_id: int
    code: str
    w: float
    h: float
    thickness: float
    color: str
    texture: str
    directional: bool
    is_offcut: bool
    quantity: int
    unit_cost: float | None = None

    @property
    def area(self) -> float:
        return self.w * self.h


@dataclass(frozen=True)
class ProcessParams:
    kerf: float = 3.0
    trim: float = 10.0
    offcut_min_long: float = 300.0
    offcut_min_short: float = 150.0
    cut_mode: str = "guillotina"          # "guillotina" | "libre"
    tolerance_window: int = 2             # RN-04 ventana de agrupamiento
    w1: float = 1.0                       # peso merma
    w2: float = 0.05                      # peso desplazamiento en cola
    w3: float = 0.5                       # peso bonificación retazos
    w_pending: float = 10.0               # penalización por piezas sin hoja


@dataclass(frozen=True)
class GAParams:
    population: int = 40
    max_generations: int = 300
    stagnation: int = 40
    time_limit_s: float = 25.0
    crossover: str = "OX"                 # "OX" | "PMX"
    p_crossover: float = 0.9
    p_mutation: float = 0.35
    elitism: float = 0.05
    tournament_k: int = 3
    seed: int | None = None
    # Extensiones del AG (sobre la base de la ERS, sección 4.3)
    p_group: float = 0.3                  # prob. de heredar hojas completas de un élite
    adaptive_mutation: bool = True        # la mutación crece con el estancamiento
    immigrants: float = 0.3               # fracción reemplazada al llegar a X/2 sin mejora


@dataclass
class Subproblem:
    key: MaterialKey
    pieces: list[Piece]
    lots: list[SheetLot]
    directional: bool


@dataclass
class Placement:
    piece_uid: int
    x: float
    y: float
    w: float
    h: float
    rotated: bool


@dataclass
class PackedSheet:
    lot: SheetLot
    instance: int                          # nº de hoja dentro del lote
    placements: list[Placement] = field(default_factory=list)
    free: list = field(default_factory=list)       # rects libres (coord. absolutas)
    offcuts: list = field(default_factory=list)    # retazos (coord. absolutas)

    @property
    def area(self) -> float:
        return self.lot.area

    def used_area(self) -> float:
        return sum(p.w * p.h for p in self.placements)

    def offcut_area(self) -> float:
        return sum(r[2] * r[3] for r in self.offcuts)


def to_dict(obj):
    return asdict(obj)
