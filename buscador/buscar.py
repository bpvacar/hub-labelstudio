#!/usr/bin/env python3
"""Busca cajas sin revisar parecidas a una especie, y arma una hoja de contacto.

Es el buscador sin interfaz: sirve para ver la calidad del ranking sobre los datos
reales antes de construir la grilla. Cada búsqueda deja una hoja de contacto con los
recortes en orden y un CSV con la foto, la estación y la fecha de cada uno.

    buscar "Jaguar"                        # ejemplos etiquetados + nombre científico
    buscar "Panthera onca" --n 300
    buscar "Tapir Amazónico" --estacion E01 --anio 2016
    buscar "animal cargando una cría" --solo-texto      # consulta libre, como INQUIRE

Qué se busca: las cajas de `animal`/`vehicle` que **nadie ha etiquetado todavía**.
Las que ya tienen especie quedan fuera, y también sirven de ejemplo.

De dónde salen los ejemplos: de Label Studio, que es donde viven las etiquetas. La
lista se guarda en caché; `--refrescar` la vuelve a pedir. `--fuente sqlite` usa
`labels.db` en vez de Label Studio (más viejo: no tiene lo etiquetado desde la
migración).

    docker compose run --rm embeddings python buscador/buscar.py "Jaguar"
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import embeber  # noqa: E402

IOU_MIN = 0.5
TOP = 3          # el puntaje por ejemplos es la media de los TOP más parecidos
PLANTILLAS = (
    "a photo of {cientifico}.",
    "a camera trap photo of {cientifico}.",
    "a photo of {cientifico} with common name {ingles}.",
    "a photo of a {ingles}.",
    "a camera trap photo of a {ingles}.",
)


# ------------------------------------------------------------------ índice --
def cargar_indice(carpeta: Path):
    filas = list(csv.DictReader(open(carpeta / "indice.csv", encoding="utf-8")))
    vectores = np.load(carpeta / "vectores.npy", mmap_mode="r")
    if len(filas) != len(vectores):
        sys.exit(f"indice.csv tiene {len(filas):,} filas y vectores.npy {len(vectores):,}")
    return filas, vectores


# --------------------------------------------------------------- etiquetas --
def iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _api(ruta: str, metodo="GET", cuerpo=None, token=None, timeout=900):
    url = os.environ.get("LS_URL", "http://nginx:8085") + ruta
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    pedido = urllib.request.Request(url, data=datos, method=metodo)
    pedido.add_header("Content-Type", "application/json")
    if token:
        pedido.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(pedido, timeout=timeout) as r:
        return json.loads(r.read())


def identificaciones_de_label_studio(proyecto: int, filas) -> dict[str, dict]:
    """detección → identificación humana, emparejando cada región con su caja por IoU.

    Devuelve, por detección: el nombre científico, quién la identificó y cuándo. Label
    Studio guarda la región en porcentajes; la caja de MegaDetector, en píxeles.
    """
    clave = os.environ.get("LS_API_KEY")
    if not clave:
        sys.exit("falta LS_API_KEY: usar --fuente sqlite o definir el token")
    token = _api("/api/token/refresh", "POST", {"refresh": clave})["access"]
    tareas = _api(f"/api/projects/{proyecto}/export?exportType=JSON&download_all_tasks=false",
                  token=token)
    try:
        usuarios = {u["id"]: u.get("email") for u in _api("/api/users", token=token)}
    except Exception:                                            # noqa: BLE001
        usuarios = {}

    por_foto: dict[str, list] = {}
    for f in filas:
        por_foto.setdefault(f["sha256"], []).append(f)

    identificaciones, sin_caja = {}, 0
    for t in tareas:
        cajas = por_foto.get((t.get("data") or {}).get("sha256"))
        if not cajas:
            continue
        for an in t.get("annotations", []):
            autor = an.get("completed_by")
            autor = autor.get("id") if isinstance(autor, dict) else autor
            quien = usuarios.get(autor) or (autor if isinstance(autor, str) else None)
            cuando = an.get("updated_at") or an.get("created_at")
            for r in an.get("result", []):
                if r.get("type") != "taxonomy":
                    continue
                camino = (r.get("value") or {}).get("taxonomy") or [[]]
                especie = camino[0][-1] if camino and camino[0] else None
                v, w, h = r["value"], r.get("original_width"), r.get("original_height")
                if not (especie and w and h):
                    continue
                caja = (v["x"] * w / 100, v["y"] * h / 100,
                        (v["x"] + v["width"]) * w / 100, (v["y"] + v["height"]) * h / 100)
                mejor, mejor_iou = None, 0.0
                for f in cajas:
                    s = iou(caja, (float(f["x1"]), float(f["y1"]), float(f["x2"]), float(f["y2"])))
                    if s > mejor_iou:
                        mejor, mejor_iou = f["deteccion"], s
                if mejor and mejor_iou >= IOU_MIN:
                    identificaciones[mejor] = {"cientifico": especie, "por": quien,
                                               "cuando": cuando, "tarea": t.get("id")}
                else:
                    sin_caja += 1
    print(f"  {len(tareas):,} fotos anotadas en Label Studio → {len(identificaciones):,} "
          f"detecciones identificadas ({sin_caja:,} regiones sin caja de MegaDetector "
          "que les corresponda)")
    return identificaciones


def etiquetas_de_label_studio(proyecto: int, filas) -> dict[str, str]:
    """Solo detección → nombre científico, que es lo que necesita el ranking."""
    return {d: i["cientifico"]
            for d, i in identificaciones_de_label_studio(proyecto, filas).items()}


def etiquetas_de_sqlite(db: str) -> dict[str, str]:
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    filas = con.execute("""SELECT a.detection_id, coalesce(t.scientific_name, t.label)
                           FROM annotation a JOIN taxon t ON t.id = a.taxon_id
                           WHERE a.detection_id IS NOT NULL""").fetchall()
    return {str(d): e for d, e in filas}


def etiquetas(a, filas) -> dict[str, str]:
    cache = Path(a.salida) / f"etiquetas-{a.fuente}-p{a.proyecto}.csv"
    if cache.exists() and not a.refrescar:
        with open(cache, encoding="utf-8") as f:
            return {r["deteccion"]: r["cientifico"] for r in csv.DictReader(f)}
    print(f"leyendo las etiquetas de {a.fuente}…", flush=True)
    mapa = (etiquetas_de_sqlite(a.db) if a.fuente == "sqlite"
            else etiquetas_de_label_studio(a.proyecto, filas))
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["deteccion", "cientifico"])
        w.writerows(sorted(mapa.items()))
    return mapa


# ----------------------------------------------------------------- consulta --
def sin_tildes(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


def resolver_especie(consulta: str, db: str) -> dict | None:
    """La consulta contra el vocabulario: nombre en español, científico o inglés."""
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    q = sin_tildes(consulta)
    for r in con.execute("SELECT label, scientific_name, name_en, kind FROM taxon"):
        candidatos = [r["label"], r["scientific_name"], r["name_en"]]
        if any(c and sin_tildes(c) == q for c in candidatos):
            return dict(r)
    return None


def frases(taxon: dict | None, consulta: str) -> list[str]:
    if not taxon or not taxon["scientific_name"]:
        return [consulta]
    datos = {"cientifico": taxon["scientific_name"], "ingles": taxon["name_en"] or ""}
    return [p.format(**datos) for p in PLANTILLAS
            if datos["ingles"] or "{ingles}" not in p]


# ------------------------------------------------------------------- salida --
def hoja_de_contacto(rutas, destino: Path, por_fila=10, lado=128):
    filas = (len(rutas) + por_fila - 1) // por_fila
    hoja = Image.new("RGB", (por_fila * lado, filas * lado), "white")
    dibujo = ImageDraw.Draw(hoja)
    for i, ruta in enumerate(rutas):
        x, y = (i % por_fila) * lado, (i // por_fila) * lado
        try:
            with Image.open(ruta) as im:
                hoja.paste(im.resize((lado, lado)), (x, y))
        except OSError:
            continue
        dibujo.rectangle((x, y, x + 26, y + 13), fill="black")
        dibujo.text((x + 2, y + 1), str(i + 1), fill="white")
    hoja.save(destino, quality=88)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("consulta", help="especie del vocabulario, nombre científico o texto libre")
    ap.add_argument("--n", type=int, default=200, help="cuántas cajas devolver")
    ap.add_argument("--modelo", default="bioclip-2", choices=list(embeber.MODELOS))
    ap.add_argument("--indice", default="/resultados/embeddings")
    ap.add_argument("--recortes", default="/recortes")
    ap.add_argument("--salida", default="/resultados/busquedas")
    ap.add_argument("--db", default="/fuentes/etiquetas/labels.db")
    ap.add_argument("--fuente", default="label-studio", choices=["label-studio", "sqlite"])
    ap.add_argument("--proyecto", type=int, default=7)
    ap.add_argument("--refrescar", action="store_true", help="volver a leer las etiquetas")
    ap.add_argument("--solo-texto", action="store_true", help="ignorar los ejemplos")
    ap.add_argument("--estacion")
    ap.add_argument("--anio", type=int)
    a = ap.parse_args()

    filas, vectores = cargar_indice(Path(a.indice) / a.modelo)
    etiquetado = etiquetas(a, filas)
    taxon = resolver_especie(a.consulta, a.db)
    nombre = taxon["label"] if taxon else a.consulta
    cientifico = (taxon or {}).get("scientific_name")
    print(f"consulta: {a.consulta!r} → "
          + (f"{nombre} ({cientifico})" if taxon else "texto libre"))

    ejemplos = [i for i, f in enumerate(filas)
                if cientifico and etiquetado.get(f["deteccion"]) == cientifico]
    candidatas = [i for i, f in enumerate(filas)
                  if f["deteccion"] not in etiquetado
                  and (not a.estacion or f["estacion"] == a.estacion)
                  and (not a.anio or f["anio"] == str(a.anio))]
    if not candidatas:
        sys.exit("no quedan cajas sin etiquetar con esos filtros")

    E = np.asarray(vectores[candidatas], dtype=np.float32)
    if ejemplos and not a.solo_texto:
        modo = f"{len(ejemplos):,} ejemplos etiquetados"
        sims = E @ np.asarray(vectores[ejemplos], dtype=np.float32).T
        k = min(TOP, len(ejemplos))
        puntaje = np.partition(sims, -k, axis=1)[:, -k:].mean(axis=1)
    else:
        modo = "solo texto"
        m = embeber.cargar(a.modelo)
        q = embeber.texto(m, frases(taxon, a.consulta))
        embeber.liberar(m)
        puntaje = E @ q
    print(f"modo: {modo} · {len(candidatas):,} cajas sin revisar")

    orden = np.argsort(-puntaje)[:a.n]
    elegidas = [filas[candidatas[i]] for i in orden]
    rutas = [str(Path(a.recortes) / f"{f['deteccion']}.jpg") for f in elegidas]

    slug = re.sub(r"[^a-z0-9]+", "-", sin_tildes(a.consulta)).strip("-") or "consulta"
    carpeta = Path(a.salida) / slug
    carpeta.mkdir(parents=True, exist_ok=True)
    hoja_de_contacto(rutas, carpeta / "hoja.jpg")
    with open(carpeta / "top.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["puesto", "similitud", "deteccion", "foto", "estacion", "fecha", "ruta"])
        for puesto, (i, fila) in enumerate(zip(orden, elegidas), 1):
            w.writerow([puesto, round(float(puntaje[i]), 4), fila["deteccion"], fila["foto"],
                        fila["estacion"], fila["fecha"], fila["ruta"]])

    print(f"\n{len(elegidas)} cajas; similitud {puntaje[orden[0]]:.3f} … {puntaje[orden[-1]]:.3f}")
    print(f"  hoja de contacto  {carpeta / 'hoja.jpg'}")
    print(f"  detalle           {carpeta / 'top.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
