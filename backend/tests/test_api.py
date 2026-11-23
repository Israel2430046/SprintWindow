"""Pruebas de integración de la API, despiece, importación y PNG."""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.despiece import DespieceError, explode
from app.main import app, db


@pytest.fixture(scope="module")
def client():
    c = TestClient(app)
    c.put("/api/settings", json={"ga": {"tiempo_max_s": 4, "semilla": 1}})
    return c


def test_despiece_rules():
    v = explode("vertical", 1800, 2100, 2)
    assert v[0]["ancho"] == 89 and v[0]["alto"] == 2080 and v[0]["cantidad"] == 2 * 23
    e = explode("enrollable", 1500, 1600, 1)
    assert {p["key"] for p in e} == {"LIENZO", "FALDON"} and e[0]["ancho"] == 1470
    pj = explode("panel_japones", 2400, 2300, 1)
    assert pj[0]["cantidad"] == 4 and pj[0]["ancho"] <= 700
    with pytest.raises(DespieceError):
        explode("vertical", 0, 100, 1)


def test_import_lots_all_or_nothing(client):
    bad = "codigo_lote,alto_mm,ancho_mm,espesor_mm,color,textura,direccionalidad_veta,cantidad\n" \
          "OK-1,2440,1220,3,Blanco,Liso,No,3\nBAD-1,abc,1220,3,Blanco,Liso,quizas,-1\n"
    r = client.post("/api/lots/import", files={"file": ("inv.csv", bad.encode(), "text/csv")})
    assert r.status_code == 422
    assert len(r.json()["detail"]["errores"]) == 1
    assert not any(l["code"] == "OK-1" for l in client.get("/api/lots").json())
    good = bad.splitlines()[0] + "\nIMP-1,2440,1220,3,Blanco,Liso,No,3\n"
    r = client.post("/api/lots/import", files={"file": ("inv.csv", good.encode(), "text/csv")})
    assert r.status_code == 200 and r.json()["importados"] == 1


def test_import_xlsx(client):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Código lote", "Alto (mm)", "Ancho (mm)", "Espesor (mm)", "Color", "Textura", "Veta", "Cantidad"])
    ws.append(["XL-1", 2440, 1220, 3, "Gris", "Liso", "Sí", 2])
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/api/lots/import", files={"file": ("inv.xlsx", buf.getvalue(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    lot = next(l for l in client.get("/api/lots").json() if l["code"] == "XL-1")
    assert lot["directional"] is True


def test_queue_urgent_and_reorder(client):
    base = {"client": "c", "blind_type": "vertical", "width": 1000, "height": 1500, "quantity": 1,
            "color": "Blanco", "texture": "Liso", "thickness": 3}
    a = client.post("/api/orders", json={**base, "order_number": "Q-A"}).json()["id"]
    b = client.post("/api/orders", json={**base, "order_number": "Q-B"}).json()["id"]
    u = client.post("/api/orders", json={**base, "order_number": "Q-U", "priority": "Urgente"}).json()["id"]
    ids = [o["id"] for o in client.get("/api/orders").json()]
    assert ids.index(u) < ids.index(a) < ids.index(b)
    client.post(f"/api/orders/{b}/move?direction=top")
    assert client.get("/api/orders").json()[0]["id"] == b
    new = list(reversed([o["id"] for o in client.get("/api/orders").json()]))
    assert client.post("/api/orders/reorder", json={"ids": new}).status_code == 200
    assert [o["id"] for o in client.get("/api/orders").json()] == new
    assert client.post("/api/orders", json={**base, "order_number": "Q-A"}).status_code == 409
    for i in (a, b, u):
        client.delete(f"/api/orders/{i}")


def test_full_flow_run_png_zip_confirm(client):
    client.post("/api/demo")
    r = client.post("/api/runs?sync=true")
    rid = r.json()["run_id"]
    run = client.get(f"/api/runs/{rid}").json()
    assert run["status"] == "simulada"
    res = run["result"]
    m = res["metricas"]
    assert m["hojas"] == len(res["hojas"]) > 0
    assert 0 < m["aprovechamiento"] <= 1 and abs(m["aprovechamiento"] + m["merma"] + m["retazo"] - 1) < 1e-9
    assert m["ordenes_total"] == m["ordenes_cubiertas"] + m["ordenes_parciales"] + m["ordenes_pendientes"]

    png = client.get(f"/api/runs/{rid}/sheets/1.png")
    assert png.status_code == 200
    img = Image.open(io.BytesIO(png.content))
    assert img.width >= 1920 and img.height >= 1080                    # RNF-02
    assert 150 <= round(img.info["dpi"][0]) <= 300

    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/runs/{rid}/zip").content))
    pngs = [n for n in z.namelist() if n.endswith(".png")]
    assert len(pngs) == len(res["hojas"]) and "lista_de_corte.csv" in z.namelist()

    before = {l["id"]: l["quantity"] for l in client.get("/api/lots").json()}
    c = client.post(f"/api/runs/{rid}/confirm").json()
    assert c["hojas_descontadas"] == len(res["hojas"])
    after = {l["id"]: l for l in client.get("/api/lots").json()}
    used = {}
    for s in res["hojas"]:
        used[s["lot_id"]] = used.get(s["lot_id"], 0) + 1
    for lot_id, n in used.items():
        if lot_id in after:
            assert after[lot_id]["quantity"] == before[lot_id] - n
    assert sum(1 for l in after.values() if l["origin_run"] == rid) == c["retazos_registrados"]
    assert client.post(f"/api/runs/{rid}/confirm").status_code == 409
    estados = {o["order_number"]: o["status"] for o in client.get("/api/orders?all=true").json()}
    for o in res["ordenes"]:
        if o["estado"] == "cubierta":
            assert estados[o["order_number"]] == "cortada"


def test_settings_validation(client):
    assert client.put("/api/settings", json={"proceso": {"modo_corte": "laser"}}).status_code == 422
    assert client.put("/api/settings", json={"proceso": {"kerf_mm": 4}}).json()["proceso"]["kerf_mm"] == 4
    client.put("/api/settings", json={"proceso": {"kerf_mm": 3}})
