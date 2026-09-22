#!/usr/bin/env python3
"""Agrega anotaciones desde un JSON a un proyecto camtrap que ya tiene sus tareas.

Para rellenar lo que la migración no trajo. El caso real: `labels.db` se armó con
el export viejo del proyecto 2 de MATLAB (3 427 fotos con especie) en vez de la
copia `apr-6-2026` (3 759), así que a Label Studio le faltan 332 fotos etiquetadas.

Reglas: **no crea tareas y no toca fotos que ya tienen anotación.** Solo agrega
las que faltan, a nombre de la cuenta de servicio de MATLAB, como el resto de la
migración. Las cajas del JSON van en píxeles absolutos `[x1 y1 x2 y2]`, igual que
el JSON de MegaDetector:

    {"fuente": "proyecto2.mat",
     "fotos": [{"ruta": "original_db/…/foto.JPG",
                "cajas": [{"clase": "animal", "especie": "Jaguar",
                           "bbox": [x1, y1, x2, y2]}]}]}

La ruta se resuelve contra `labels.db` por el sufijo más largo que dé una sola
foto: los `.mat` traen tres raíces absolutas distintas y los basenames se repiten.

    docker compose run --rm tools importar-anotaciones proyecto2-apr6.json --seco
    docker compose run --rm tools importar-anotaciones proyecto2-apr6.json
"""
from __future__ import annotations

import argparse
import os
import json
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub  # noqa: E402

RAID_FOTOS = Path(os.environ.get("FOTOS_RAIZ", "/label-studio/files/fotos"))
CAJA = {"animal": "animal", "human": "human", "vehicle": "vehicle"}
SEGMENTOS = 7                      # sufijos de hasta 7 tramos de ruta


def indice_por_sufijo(con) -> list[dict[str, int]]:
    """indice[k]: sufijo de k tramos → image_id, solo cuando es único."""
    por_k = [defaultdict(set) for _ in range(SEGMENTOS)]
    for image_id, ruta in con.execute("SELECT image_id, path FROM image_path"):
        tramos = ruta.lower().replace("\\", "/").split("/")
        for k in range(1, SEGMENTOS):
            if k <= len(tramos):
                por_k[k]["/".join(tramos[-k:])].add(image_id)
    return [{s: next(iter(v)) for s, v in d.items() if len(v) == 1} for d in por_k]


def resolver(ruta: str, indice) -> int | None:
    tramos = ruta.lower().replace("\\", "/").split("/")
    for k in range(min(SEGMENTOS - 1, len(tramos)), 0, -1):
        hit = indice[k].get("/".join(tramos[-k:]))
        if hit:
            return hit
    return None


def tamano(fila) -> tuple[int, int] | None:
    if fila["width"] and fila["height"]:
        return fila["width"], fila["height"]
    try:
        with Image.open(RAID_FOTOS / fila["primary_path"]) as im:   # solo la cabecera
            return im.size
    except OSError:
        return None


def region(caja, especie_cientifica, ancho, alto) -> list[dict]:
    """Una caja = dos resultados con el mismo id: el rectángulo y su especie."""
    x1, y1, x2, y2 = caja["bbox"]
    geo = dict(x=100 * x1 / ancho, y=100 * y1 / alto,
               width=100 * (x2 - x1) / ancho, height=100 * (y2 - y1) / alto, rotation=0)
    comun = dict(id=uuid.uuid4().hex[:10], to_name="image",
                 original_width=ancho, original_height=alto)
    return [dict(comun, from_name="caja", type="rectanglelabels",
                 value={**geo, "rectanglelabels": [CAJA.get(caja.get("clase"), "animal")]}),
            dict(comun, from_name="especie", type="taxonomy",
                 value={**geo, "taxonomy": [[especie_cientifica]]})]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json", help="archivo en ~/labelstudio/entrada/")
    ap.add_argument("--proyecto", type=int, default=7)
    ap.add_argument("--db", default="/fuentes/etiquetas/labels.db")
    ap.add_argument("--vocabulario", default="camtrap-especies.csv")
    ap.add_argument("--seco", action="store_true", help="no escribe nada")
    ap.add_argument("--limite", type=int, default=0, help="solo las primeras N fotos")
    a = ap.parse_args()

    datos = json.loads(Path(a.json).read_text(encoding="utf-8"))
    fotos = datos["fotos"][:a.limite] if a.limite else datos["fotos"]

    cientifico = {r["valor"]: (r.get("cientifico") or r["valor"])
                  for r in hub.leer_vocab(a.vocabulario)}
    faltan = {c["especie"] for f in fotos for c in f["cajas"]} - set(cientifico)
    if faltan:
        sys.exit(f"especies que no están en {a.vocabulario}: {sorted(faltan)}")

    db = sqlite3.connect(f"file:{a.db}?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    indice = indice_por_sufijo(db)
    imagenes = {f["id"]: f for f in db.execute(
        "SELECT id, sha256, primary_path, width, height, status FROM image")}

    ls = hub.cliente()
    ls_matlab = hub.cliente("LS_API_KEY_MATLAB")
    print(f"leyendo las tareas del proyecto {a.proyecto}…", flush=True)
    tarea_de, anotadas = {}, set()
    for t in ls.tasks.list(project=a.proyecto, fields="task_only"):
        sha = (t.data or {}).get("sha256")
        if sha:
            tarea_de[sha] = t.id
        if getattr(t, "total_annotations", 0):
            anotadas.add(t.id)
    print(f"  {len(tarea_de):,} tareas, {len(anotadas):,} ya anotadas")

    st = Counter()
    pendientes = []
    for foto in fotos:
        image_id = resolver(foto["ruta"], indice)
        if not image_id:
            st["ruta_sin_match"] += 1
            continue
        fila = imagenes[image_id]
        if fila["status"] == "excluded":          # fotos de prueba de cámara
            st["foto_excluida"] += 1
            continue
        tarea = tarea_de.get(fila["sha256"])
        if not tarea:
            st["sin_tarea"] += 1
            continue
        if tarea in anotadas:
            st["ya_anotada"] += 1
            continue
        wh = tamano(fila)
        if not wh:
            st["sin_tamano"] += 1
            continue
        resultado = []
        for caja in foto["cajas"]:
            resultado += region(caja, cientifico[caja["especie"]], *wh)
        pendientes.append((tarea, resultado))
        st["por_crear"] += 1
        st["cajas"] += len(foto["cajas"])

    print(f"  fotos en el JSON       {len(fotos):>8,}")
    for clave in ("ya_anotada", "ruta_sin_match", "sin_tarea", "foto_excluida", "sin_tamano"):
        if st[clave]:
            print(f"  {clave:<22} {st[clave]:>8,}")
    print(f"  anotaciones por crear  {st['por_crear']:>8,}  ({st['cajas']:,} cajas)")

    if a.seco:
        print("\nSECO — no se escribió nada")
        return 0
    if not pendientes:
        return 0

    hechas = 0
    for tarea, resultado in pendientes:
        ls_matlab.annotations.create(id=tarea, result=resultado)
        hechas += 1
        if hechas % 100 == 0:
            print(f"  {hechas:,}/{len(pendientes):,}", flush=True)
    print(f"\n{hechas:,} anotaciones creadas en el proyecto {a.proyecto}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
