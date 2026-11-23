"""Conjunto de datos de demostración (inventario heterogéneo + cola de órdenes)."""

def _lot(code, h, w, color, texture, directional, qty, cost, offcut=False, t=3.0):
    return {"code": code, "height": h, "width": w, "thickness": t, "color": color, "texture": texture,
            "directional": directional, "quantity": qty, "unit_cost": cost, "is_offcut": offcut,
            "origin_run": None}


DEMO_LOTS = [
    _lot("BL-122x244", 2440, 1220, "Blanco", "Liso", False, 12, 850),
    _lot("BL-150x300", 3000, 1500, "Blanco", "Liso", False, 6, 1290),
    _lot("BL-RET-01", 900, 800, "Blanco", "Liso", False, 1, 205, offcut=True),
    _lot("BL-RET-02", 1300, 600, "Blanco", "Liso", False, 1, 225, offcut=True),
    _lot("NG-122x244", 2440, 1220, "Nogal", "Veta madera", True, 8, 1100),
    _lot("NG-122x300", 3000, 1220, "Nogal", "Veta madera", True, 4, 1350),
    _lot("GR-122x244", 2440, 1220, "Gris", "Grabado lineal", True, 6, 980),
    _lot("AR-122x244", 2440, 1220, "Arena", "Texturizado", False, 6, 920),
]


def _ord(num, client, bt, w, h, q, color, texture, prio="Normal", manual=None):
    return {"order_number": num, "client": client, "blind_type": bt, "width": w, "height": h,
            "quantity": q, "color": color, "texture": texture, "thickness": 3.0, "priority": prio,
            "manual_pieces": manual, "notes": None}


DEMO_ORDERS = [
    _ord("OP-1001", "Constructora Norte", "vertical", 1800, 2100, 2, "Blanco", "Liso"),
    _ord("OP-1002", "Hotel Plaza", "panel_japones", 2400, 2300, 1, "Nogal", "Veta madera", "Urgente"),
    _ord("OP-1003", "Oficinas Delta", "enrollable", 1500, 1600, 3, "Blanco", "Liso"),
    _ord("OP-1004", "Residencial Lomas", "vertical", 2200, 1900, 1, "Nogal", "Veta madera"),
    _ord("OP-1005", "Clínica San José", "panel_japones", 3000, 2400, 1, "Blanco", "Liso"),
    _ord("OP-1006", "Depto. Roma 12", "enrollable", 1100, 1400, 2, "Gris", "Grabado lineal"),
    _ord("OP-1007", "Casa Fam. Ortiz", "manual", None, None, 1, "Blanco", "Liso", manual=[
        {"nombre": "Cenefa", "ancho": 1600, "alto": 150, "cantidad": 2},
        {"nombre": "Tapa lateral", "ancho": 120, "alto": 300, "cantidad": 4}]),
    _ord("OP-1008", "Restaurante Mar", "enrollable", 1150, 1900, 2, "Arena", "Texturizado"),
    _ord("OP-1009", "Constructora Norte", "vertical", 1200, 2300, 2, "Gris", "Grabado lineal"),
    _ord("OP-1010", "Hotel Plaza", "enrollable", 900, 1800, 4, "Nogal", "Veta madera"),
    _ord("OP-1011", "Colegio Andes", "panel_japones", 3600, 2500, 1, "Arena", "Texturizado"),
    _ord("OP-1012", "Oficinas Delta", "vertical", 1500, 2000, 1, "Blanco", "Liso", "Urgente"),
]
