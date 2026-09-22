#!/usr/bin/env python3
"""Calcula el embedding de cada caja de MegaDetector, para el buscador por especie.

Dos salidas, las dos reutilizables:

    recortes/<id>.jpg        el recorte cuadrado de cada caja, 448 px. Lo que va a
                             mostrar la grilla del buscador, sin volver a abrir la
                             foto de 2048x1536 del RAID.
    embeddings/<modelo>/     vectores.npy (float32, L2-normalizados, una fila por
                             caja), indice.csv (qué caja es cada fila) y meta.json.

Se puede reanudar: los recortes que ya existen no se vuelven a hacer. Los
embeddings sí se recalculan enteros, que son pocos minutos.

Las cajas de categoría `person` NO entran por defecto: el buscador es de especies
y no hace falta un índice de fotos de gente. `--categorias animal person vehicle`
las incluye.

    docker compose run --rm embeddings python buscador/calcular_embeddings.py
"""
from __future__ import annotations

import argparse
import os
import csv
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import embeber  # noqa: E402

RAID = Path(os.environ.get("FOTOS_RAIZ", "/label-studio/files/fotos"))


def leer_detecciones(db: str, categorias: list[str], limite: int) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    marcas = ",".join("?" * len(categorias))
    sql = f"""SELECT d.id AS deteccion, d.image_id AS foto, d.category AS categoria,
                     d.confidence AS confianza, d.x1, d.y1, d.x2, d.y2,
                     i.sha256, i.primary_path AS ruta, i.station AS estacion,
                     i.captured_at AS fecha, i.year AS anio
              FROM detection d JOIN image i ON i.id = d.image_id
              WHERE i.status != 'excluded' AND d.category IN ({marcas})
              ORDER BY d.image_id, d.id"""
    filas = con.execute(sql + (f" LIMIT {int(limite)}" if limite else ""), categorias).fetchall()
    return [dict(f) for f in filas]


def recortar_foto(cajas: list[dict], carpeta: Path) -> int:
    """Abre la foto UNA vez y saca todos sus recortes. Devuelve cuántos hizo."""
    pendientes = [c for c in cajas if not (carpeta / f"{c['deteccion']}.jpg").exists()]
    if not pendientes:
        return 0
    try:
        with Image.open(RAID / cajas[0]["ruta"]) as im:
            ancho, alto = im.size
            for c in pendientes:
                caja = embeber.cuadrado(c["x1"], c["y1"], c["x2"], c["y2"], ancho, alto)
                recorte = im.crop(caja).convert("RGB").resize((embeber.LADO,) * 2, Image.BICUBIC)
                destino = carpeta / f"{c['deteccion']}.jpg"
                temporal = destino.with_suffix(".tmp")
                recorte.save(temporal, format="JPEG", quality=92)
                temporal.rename(destino)
    except OSError as e:
        print(f"  foto ilegible {cajas[0]['ruta']}: {e}", file=sys.stderr)
        return 0
    return len(pendientes)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="/fuentes/etiquetas/labels.db")
    ap.add_argument("--recortes", default="/recortes")
    ap.add_argument("--salida", default="/resultados/embeddings")
    ap.add_argument("--modelo", default="bioclip-2", choices=list(embeber.MODELOS))
    ap.add_argument("--categorias", nargs="+", default=["animal", "vehicle"])
    ap.add_argument("--hilos", type=int, default=16)
    ap.add_argument("--lote", type=int, default=64)
    ap.add_argument("--limite", type=int, default=0, help="solo N cajas (prueba)")
    a = ap.parse_args()

    recortes = Path(a.recortes)
    recortes.mkdir(parents=True, exist_ok=True)
    salida = Path(a.salida) / a.modelo
    salida.mkdir(parents=True, exist_ok=True)

    cajas = leer_detecciones(a.db, a.categorias, a.limite)
    por_foto: dict[int, list[dict]] = {}
    for c in cajas:
        por_foto.setdefault(c["foto"], []).append(c)
    print(f"{len(cajas):,} cajas ({', '.join(a.categorias)}) en {len(por_foto):,} fotos",
          flush=True)

    t0 = time.time()
    hechos = fotos = 0
    with ThreadPoolExecutor(a.hilos) as ex:
        for n in ex.map(lambda cs: recortar_foto(cs, recortes), por_foto.values()):
            hechos += n
            fotos += 1
            if fotos % 5000 == 0:
                print(f"  {fotos:,}/{len(por_foto):,} fotos  "
                      f"{fotos / (time.time() - t0):.0f} fotos/s", flush=True)
    print(f"recortes: {hechos:,} nuevos en {time.time() - t0:.0f} s", flush=True)

    cajas = [c for c in cajas if (recortes / f"{c['deteccion']}.jpg").exists()]
    rutas = [str(recortes / f"{c['deteccion']}.jpg") for c in cajas]
    print(f"{len(cajas):,} cajas con recorte; embebiendo con {a.modelo}…", flush=True)

    m = embeber.cargar(a.modelo)
    t = time.time()
    E = embeber.imagenes(m, rutas, lote=a.lote)
    seg = time.time() - t
    embeber.liberar(m)
    np.save(salida / "vectores.npy", E)

    campos = ["deteccion", "foto", "sha256", "ruta", "categoria", "confianza",
              "estacion", "fecha", "anio", "x1", "y1", "x2", "y2"]
    with open(salida / "indice.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(cajas)
    (salida / "meta.json").write_text(json.dumps({
        "modelo": a.modelo, "arquitectura": embeber.MODELOS[a.modelo][0],
        "fecha": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "cajas": len(cajas), "dim": int(E.shape[1]), "categorias": a.categorias,
        "recortes_px": embeber.LADO, "margen": embeber.MARGEN,
        "segundos_embeddings": round(seg), "fuente": a.db,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{len(cajas):,} vectores de {E.shape[1]} dims en {seg:.0f} s "
          f"({len(cajas) / seg:.0f} recortes/s)")
    print(f"  -> {salida}/vectores.npy  ({E.nbytes / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
