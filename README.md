# SprintWindow · Sistema de Optimización de Corte y Planeación de Producción (Nesting 2D)

Implementación del documento **ERS v1.0** (`proyecto.pdf`): optimizador de corte de láminas de PVC con
inventario heterogéneo, resuelto con un **Algoritmo Genético** + decodificador constructivo
(Guillotine Recursive Split / Bottom-Left-Fill), con generación de planos **PNG** para operarios.

## Arranque rápido

```bash
./run.sh                 # crea .venv, instala dependencias y levanta http://localhost:8000
```

Abra `http://localhost:8000` y pulse **«Cargar datos de demostración»** (o importe los CSV de `muestras/`).

Flujo de 3 clics (RNF-03): **+ Agregar a la cola** → **⚡ Optimizar** → **⬇ Descargar planos (.zip)**.

Pruebas automatizadas:

```bash
cd backend && ../.venv/bin/python -m pytest -q
```

## Estructura

```
backend/app/
  main.py               API REST (FastAPI) + servidor del frontend
  db.py                 Persistencia SQLite (data/sprintwindow.db) y parámetros por defecto
  despiece.py           RF-06 explosión de materiales por tipo de persiana
  importer.py           RF-02 carga masiva .csv/.xlsx con validación todo-o-nada
  render.py             RF-11/12 planos PNG A4 300 DPI + ZIP con lista de corte
  demo.py               datos de demostración
  engine/
    model.py            estructuras (Piece, SheetLot, parámetros)
    packers.py          GuillotineBin (seccionadora) y MaxRectsBin-BL (CNC), retazos
    decoder.py          cromosoma → patrón multi-hoja; función de aptitud F(x)
    ga.py               AG: torneo k=3, elitismo 5 %, OX/PMX, mutaciones, criterios de parada
    optimizer.py        RF-07 partición por material, RF-10 métricas y comparativa
    validation.py       RNF-04 validador geométrico independiente (bloquea patrones inválidos)
backend/tests/          26 pruebas (geometría, reglas de negocio, operadores, rendimiento, API, PNG)
frontend/               SPA Vue 3 (vendorizado, funciona sin internet)
muestras/               CSV de ejemplo para inventario y órdenes
```

## Matriz de trazabilidad

| ID | Requerimiento | Implementación |
|----|---------------|----------------|
| RN-01 | Homogeneidad de material | Partición por (color, textura, espesor) normalizados — `optimizer.build_subproblems`; prueba `test_material_homogeneity_partition` |
| RN-02 | Rotación por textura | `Piece.rotatable = not direccional`; el decodificador y la mutación nunca rotan piezas de veta; validador lo verifica |
| RN-03 | Kerf | Piezas infladas `w+kerf, h+kerf` en área interna `W − 2·refilado − kerf`; separación ≥ kerf entre piezas y con el refilado |
| RN-04 | Cola FIFO / ventana | Término `w2·Σ Pᵢ·ΔTᵢ` con ΔTᵢ = retraso de la orden en posiciones menos la ventana de tolerancia; semillas FIFO; urgentes se insertan tras la última urgente |
| RN-05 | Guillotina / libre | `modo_corte`: `GuillotineBin` (cada corte de borde a borde) o `MaxRectsBin` BLF; verificación recursiva de guillotina en `validation.py` |
| RN-06 | Retazos recuperables | Rectángulos libres (fusionados) ≥ Lₘᵢₙ × Aₘᵢₙ → «Retazo recuperable» (rayado verde); al confirmar reingresan al inventario |
| RF-01 | Alta/edición de hojas | Pestaña Inventario; `POST/PUT /api/lots` |
| RF-02 | Carga masiva | `POST /api/lots/import` (.csv/.xlsx, alias de encabezados, validación por fila, todo-o-nada) |
| RF-03 | Retazos primero | El decodificador abre retazos (del menor que contenga la pieza) antes que hojas vírgenes |
| RF-04 | Captura de órdenes | Formulario con tipo de persiana, medidas, cantidad, color, textura, espesor y prioridad; importación CSV |
| RF-05 | Reordenamiento | Arrastrar y soltar + botones ▲▼; recalcula impacto con `POST /api/plan/preview` |
| RF-06 | Despiece | `despiece.py` (enrollable, vertical, panel japonés, horizontal, manual) con holguras editables |
| RF-07 | Agrupamiento | Subproblemas independientes por material; presupuesto de tiempo proporcional a piezas |
| RF-08 | AG | `ga.py` + `decoder.py` |
| RF-09 | Parámetros | Pestaña Configuración: kerf, refilado, retazo mínimo, pesos w₁ w₂ w₃, parámetros del AG, holguras |
| RF-10 | Simulación y comparativa | Métricas proyectadas (hojas, aprovechamiento, merma neta, retazo, costo, órdenes cubiertas/parciales/pendientes) vs. línea base FIFO sin optimizar; confirmar o descartar |
| RF-11 | PNG por hoja / ZIP | `GET /api/runs/{id}/sheets/{n}.png`, `GET /api/runs/{id}/zip` (incluye `lista_de_corte.csv` y `resumen.txt`) |
| RF-12 | Contenido del plano | Encabezado (corrida, hoja, W×H, color, textura, flecha de veta), hoja a escala con kerf visible, etiqueta por pieza (orden, tipo, medidas, ↻ rotada), color por orden, retazo verde, merma gris, pie con % aprovechamiento, % merma y total de piezas |
| §4 | Especificación del AG | Genes `(id_pieza, rotada)`; F(x) de la ERS; torneo determinístico k=3; elitismo 5 %; OX o PMX; swap/inversión/rotación; parada por Gₘₐₓ, estancamiento X o Tₘₐₓ |
| RNF-01 | ≤ 30 s (200 piezas, 50 hojas) | Decodificación ≈ 2 ms; `test_performance_200_pieces_50_sheets` |
| RNF-02 | PNG ≥ 1920×1080, 150–300 DPI | 3508×2480 px a 300 DPI (A4 horizontal) |
| RNF-03 | ≤ 3 clics | Agregar → Optimizar (botón fijo en la barra) → Descargar ZIP |
| RNF-04 | Integridad geométrica | `validation.assert_valid` se ejecuta en cada hoja antes de entregar el resultado + pruebas unitarias |
| RNF-05 | Arquitectura desacoplada | Frontend Vue / backend Python (FastAPI, Pillow) vía API REST |

## Convenciones y decisiones de diseño

* **Ejes**: *ancho* (W) = eje X de la hoja; *alto* (H) = eje Y = **sentido de la veta**. Una pieza con veta
  conserva su alto paralelo al alto de la hoja. Origen de coordenadas: esquina superior izquierda.
* **Función de aptitud** (minimizar):
  `F = w₁·Σ(A_merma,j / A_hoja,j) + w₂·Σ Pᵢ·ΔTᵢ − w₃·ΣA_retazo / ΣA_hoja + w_p·pendientes`.
  El término de retazos se normaliza por el área de hojas usadas para que sea comparable con la merma;
  `pendientes` penaliza las piezas que no caben por falta de stock (ponderadas por prioridad). La merma
  neta excluye el retazo recuperable; la «merma bruta» (1 − aprovechamiento) también se reporta.
* **Selección de hoja** en inventario heterogéneo: retazos primero; luego la hoja virgen más pequeña que
  cubra el área pendiente; al final, mejora local que sustituye cada hoja por otra más pequeña si contiene
  exactamente las mismas piezas.
* **Búsqueda local por hoja** (al finalizar): cada hoja se re-acomoda con 5 órdenes de inserción ×
  reglas de ajuste (BAF/BSSF/BLSF, o BL en modo libre) × reglas de partición de guillotina, y se conserva
  la variante con menor merma neta. Nunca empeora una hoja.
* **Calibración (demo, 2026-11-23)**: merma neta 4.76 % frente a un piso físico de 3.46 % (kerf 3 mm +
  refilado 10 mm, inevitable) → sólo ≈1.3 pp son evitables y corresponden a tiras más angostas que el
  retazo mínimo que ninguna pieza de la cola puede ocupar. Variar población, cruce, mutación o tiempo
  (hasta 120 s) no cambia el resultado (±0.1 pp): el AG converge. **La merma 0 % es físicamente
  imposible**: aun con kerf y refilado en 0 quedaría ≈1.5 %. Las palancas reales son de proceso: retazo
  mínimo (200×100 → merma evitable 0.6 pp, si la planta realmente reutiliza esas tiras), refilado y
  mezcla de medidas en la cola.
* **Garantía**: el AG nunca entrega una solución peor que la línea base FIFO (se compara al final).
* **Confirmación de corrida**: descuenta hojas, da de alta los retazos como lotes nuevos (con costo
  proporcional), registra las piezas cortadas por orden (órdenes *cortadas* o *parciales*) y marca como
  *obsoletas* las demás simulaciones.
* **Supuestos** (ajustables en Configuración): las holguras de despiece son valores de referencia y deben
  calibrarse con Ingeniería de Procesos de SprintWindow; los KPIs de la sección 6 (reducción de merma
  12–18 %, cero defectos, adherencia > 95 %) se validan en el piloto de 2 semanas — el sistema entrega la
  comparativa contra la línea base para apoyar esa medición.
