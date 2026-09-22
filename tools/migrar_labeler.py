#!/usr/bin/env python3
"""Migra el labeler de MATLAB (labels.db) a un proyecto camtrap de Label Studio.

Qué entra y cómo:

    image (no excluidas)      → una tarea por foto, con su metadato en `data`
    detection (MegaDetector)  → predictions, model_version "MegaDetector v5a"
    annotation (MATLAB)       → una anotación por foto, a nombre de la cuenta
                                "MATLAB (migrado)": caja + especie por caja

Lo que NO entra, a propósito:

- Las fotos `excluded` (fotos de prueba de cámara, `…-Test.JPG`).
- Las cajas con especie `ninguno`: son cajas que nadie revisó, no negativos.
  Quedan como predictions y la foto queda sin anotar.
- El checkbox `Etiquetada` sin especie: no es un juicio sobre el contenido.

`labels.db` se abre de solo lectura. Las fotos se leen del RAID, nunca se
copian. Se puede reanudar: `--proyecto N` salta las fotos cuyo sha256 ya
está en el proyecto.

    migrar-labeler                                   # proyecto nuevo
    migrar-labeler --limite 200 --titulo "prueba"
    migrar-labeler --proyecto 7                      # reanudar

Corre en el contenedor tools: `docker compose run --rm tools migrar-labeler`.
La especie se guarda con su nombre científico (el alias del vocabulario); el
string exacto de MATLAB se ve en la interfaz y queda en el vocabulario.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub  # noqa: E402

RAID_FOTOS = Path(os.environ.get("FOTOS_RAIZ", "/label-studio/files/fotos"))   # montaje del contenedor
RUTA_LS = str(RAID_FOTOS.relative_to(hub.DOC_ROOT))   # la misma carpeta, relativa a Label Studio
MODELO = "MegaDetector v5a"
CAJA = {"animal": "animal", "person": "human", "vehicle": "vehicle"}


def rid() -> str:
    return uuid.uuid4().hex[:10]


def rect(x1, y1, x2, y2, w, h) -> dict:
    """Píxeles absolutos [x1 y1 x2 y2] → porcentajes de Label Studio."""
    return dict(x=100 * x1 / w, y=100 * y1 / h, width=100 * (x2 - x1) / w,
                height=100 * (y2 - y1) / h, rotation=0)


def dims(row) -> tuple[int, int] | None:
    if row["width"] and row["height"]:
        return row["width"], row["height"]
    try:
        with Image.open(RAID_FOTOS / row["primary_path"]) as im:   # solo lee la cabecera
            return im.size
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("db", nargs="?", default="/fuentes/etiquetas/labels.db")
    ap.add_argument("--titulo", default="Camera traps (migrado)")
    ap.add_argument("--proyecto", type=int, help="reanudar sobre un proyecto existente")
    ap.add_argument("--limite", type=int, help="solo las primeras N fotos (prueba)")
    ap.add_argument("--desde", type=int, default=0, help="empezar en este image.id (prueba)")
    ap.add_argument("--lote", type=int, default=500)
    a = ap.parse_args()

    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    ls = hub.cliente()
    ls_matlab = hub.cliente("LS_API_KEY_MATLAB")

    cientifico = {r["valor"]: (r.get("cientifico") or r["valor"])
                  for r in hub.leer_vocab("camtrap-especies.csv")}
    faltan = {r[0] for r in db.execute(
        "SELECT DISTINCT t.label FROM annotation a JOIN taxon t ON t.id = a.taxon_id")} - set(cientifico)
    if faltan:
        sys.exit(f"etiquetas usadas que no están en el vocabulario: {sorted(faltan)}")

    ya = set()
    if a.proyecto:
        pid = a.proyecto
        for t in ls.tasks.list(project=pid, fields="all"):
            ya.add(t.data.get("sha256"))
        print(f"reanudando el proyecto {pid}: {len(ya):,} fotos ya importadas")
    else:
        p = ls.projects.create(
            title=a.titulo,
            description=("Fotos de camera trap de la estación biológica. "
                         "Cajas de MegaDetector v5a como pre-anotación; las anotaciones de "
                         "'MATLAB (migrado)' vienen del Ground Truth Labeler (hasta mar 2025)."),
            label_config=hub.armar_config("camtrap", None, None, 30),
            sampling="Sequential sampling",
            show_skip_button=True,
            enable_empty_annotation=True,
            show_collab_predictions=True,
            model_version=MODELO,
        )
        pid = p.id
        # Sin este storage Label Studio no sirve los archivos: /data/local-files
        # solo entrega rutas cubiertas por un storage del proyecto. No se sincroniza.
        ls.import_storage.local.create(
            project=pid, title="original_db (migrado, sin sync)",
            path=f"{hub.DOC_ROOT}/{RUTA_LS}/original_db",
            regex_filter=hub.MODOS["camtrap"]["regex"], use_blob_urls=True,
        )
        print(f"proyecto {pid} creado: {a.titulo} (webhook del historial "
              f"{hub.asegurar_webhook(ls, pid)})")

    attrs = defaultdict(dict)
    for r in db.execute("SELECT image_id, key, value FROM image_attr"):
        attrs[r["image_id"]][r["key"]] = r["value"]
    dets = defaultdict(list)
    for r in db.execute("SELECT * FROM detection ORDER BY image_id, confidence DESC"):
        dets[r["image_id"]].append(r)
    anns = defaultdict(list)
    for r in db.execute("""SELECT a.*, t.label AS especie, d.category AS categoria
                           FROM annotation a JOIN taxon t ON t.id = a.taxon_id
                           LEFT JOIN detection d ON d.id = a.detection_id"""):
        anns[r["image_id"]].append(r)

    imagenes = db.execute("SELECT * FROM image WHERE status != 'excluded' AND id >= ? ORDER BY id",
                          (a.desde,)).fetchall()
    if a.limite:
        imagenes = imagenes[:a.limite]

    total = sum(1 for img in imagenes if img["sha256"] not in ya)
    n_tareas = n_pred = n_ann = n_sin_dims = 0
    lote, t0 = [], time.time()

    def enviar():
        nonlocal lote
        if lote:
            # La cuenta MATLAB importa: `completed_by` queda a su nombre.
            ls_matlab.projects.import_tasks(id=pid, request=lote)
            lote = []

    for img in imagenes:
        if img["sha256"] in ya:
            continue
        at = attrs.get(img["id"], {})
        tarea = {"data": {
            "image": f"/data/local-files/?d={quote(f'{RUTA_LS}/{img['primary_path']}')}",
            "sha256": img["sha256"],
            "ruta": img["primary_path"],
            "estacion": img["station"],
            "despliegue": img["deployment"],
            "fecha": img["captured_at"],
            "anio": img["year"],
            "camara": " ".join(x for x in (img["camera_make"], img["camera_model"]) if x) or None,
            "lat": img["latitude"],
            "lon": img["longitude"],
            "codigo_archivo": at.get("filename_code"),
            "matlab_fuente": at.get("matlab_source"),
        }}

        d_img, a_img = dets.get(img["id"], []), anns.get(img["id"], [])
        wh = dims(img) if (d_img or a_img) else None
        if (d_img or a_img) and wh is None:
            n_sin_dims += 1
        elif wh:
            w, h = wh
            if d_img:
                result = []
                for d in d_img:
                    result.append(dict(
                        id=rid(), from_name="caja", to_name="image", type="rectanglelabels",
                        original_width=w, original_height=h, score=d["confidence"],
                        value={**rect(d["x1"], d["y1"], d["x2"], d["y2"], w, h),
                               "rectanglelabels": [CAJA[d["category"]]]}))
                tarea["predictions"] = [dict(model_version=MODELO, result=result,
                                             score=d_img[0]["confidence"])]
                n_pred += len(result)
            if a_img:
                result = []
                for an in a_img:
                    i, geo = rid(), rect(an["x1"], an["y1"], an["x2"], an["y2"], w, h)
                    comun = dict(id=i, to_name="image", original_width=w, original_height=h)
                    result.append(dict(comun, from_name="caja", type="rectanglelabels",
                                       value={**geo, "rectanglelabels":
                                              [CAJA.get(an["categoria"], "animal")]}))
                    result.append(dict(comun, from_name="especie", type="taxonomy",
                                       value={**geo, "taxonomy": [[cientifico[an["especie"]]]]}))
                tarea["annotations"] = [dict(result=result, lead_time=0)]
                n_ann += 1

        lote.append(tarea)
        n_tareas += 1
        if len(lote) >= a.lote:
            enviar()
            el = time.time() - t0
            print(f"  {n_tareas:,}/{total:,}  {n_tareas / el:.0f} fotos/s",
                  flush=True)
    enviar()

    print(f"\nproyecto {pid}: {n_tareas:,} tareas, {n_pred:,} cajas de MegaDetector, "
          f"{n_ann:,} fotos con anotación MATLAB")
    if n_sin_dims:
        print(f"ojo: {n_sin_dims} fotos con cajas sin poder leer su tamaño; entraron sin cajas")


if __name__ == "__main__":
    main()
