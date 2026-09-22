#!/usr/bin/env python3
"""Compara modelos de embeddings para el buscador por especie de camera traps.

La pregunta: al pedir "muéstrame todas las X", ¿qué modelo pone más X arriba?
Se simula el buscador sobre las cajas que ya tienen especie humana en labels.db, las
mismas que se migraron a Label Studio.

1. Evento = fotos de la misma estación separadas por menos de 30 min. Las fotos
   de una ráfaga son casi idénticas: si un recorte pudiera encontrar a su gemelo
   de la misma ráfaga, cualquier modelo parecería excelente.
2. Muestra: hasta --por-especie cajas de cada especie con al menos --min-eventos
   eventos, repartidas entre eventos para no llenarla con una sola ráfaga.
3. En cada repetición los eventos se parten en dos lados: ejemplos (lo que ya
   está etiquetado) y candidatas (lo que se busca). Un evento no está en los dos.
4. Por especie se ordenan TODAS las candidatas, de todas las especies, y se mide
   average precision (AP):
     texto   nombre científico e inglés, sin ningún ejemplo (zero-shot)
     1/5/20  N ejemplos; puntaje = media de los 3 ejemplos más parecidos
     todos   todos los ejemplos de ese lado
   Además, la especie que propondría 5-NN, que es lo que vería quien etiqueta
   como prediction en Label Studio.
5. Lo mismo partiendo por estación: la prueba más dura, una cámara que no tiene
   nada etiquetado.

Solo lee: no escribe en Label Studio ni en labels.db.

    docker compose run --rm embeddings python buscador/comparar_modelos.py
"""
from __future__ import annotations

import argparse
import os
import csv
import json
import math
import random
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import embeber  # noqa: E402

RAID = Path(os.environ.get("FOTOS_RAIZ", "/label-studio/files/fotos"))   # montaje del contenedor
VENTANA = 30 * 60        # s: fotos del mismo sitio más cercanas que esto son un evento
N_EJEMPLOS = (1, 5, 20)
TOP = 3                  # puntaje por ejemplos = media de los TOP más parecidos
K_VECINOS = 5
CONDICIONES = ("texto", *map(str, N_EJEMPLOS), "todos")
PLANTILLAS = (
    "a photo of {cientifico}.",
    "a camera trap photo of {cientifico}.",
    "a photo of {cientifico} with common name {ingles}.",
    "a photo of a {ingles}.",
    "a camera trap photo of a {ingles}.",
)


# ------------------------------------------------------------------- datos --
def leer_cajas(db: str) -> list[dict]:
    # immutable: el archivo está en WAL y montado de solo lectura; nadie lo escribe.
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    filas = con.execute("""
        SELECT a.id AS anotacion, a.image_id AS foto, a.x1, a.y1, a.x2, a.y2,
               t.label AS especie, t.scientific_name AS cientifico,
               t.name_en AS ingles, t.confidence AS confianza,
               i.primary_path AS ruta, i.station AS estacion, i.captured_at AS fecha
        FROM annotation a
        JOIN taxon t ON t.id = a.taxon_id
        JOIN image i ON i.id = a.image_id
        WHERE i.status != 'excluded'
        ORDER BY a.id""").fetchall()
    return [dict(f) for f in filas]


def agrupar(cajas: list[dict]) -> None:
    """Agrega `sitio` y `evento` a cada caja.

    Sitio = estación, o la carpeta si la foto no la tiene. Sin fecha no se pueden
    separar las ráfagas, así que las fotos sin fecha de un sitio son un solo
    evento: la opción conservadora.
    """
    por_sitio = defaultdict(list)
    for c in cajas:
        c["sitio"] = c["estacion"] or str(Path(c["ruta"]).parent)
        por_sitio[c["sitio"]].append(c)
    n = 0
    for cs in por_sitio.values():
        previo = None
        for c in sorted((c for c in cs if c["fecha"]), key=lambda c: c["fecha"]):
            t = datetime.fromisoformat(c["fecha"]).timestamp()
            if previo is None or t - previo > VENTANA:
                n += 1
            c["evento"] = n
            previo = t
        sin_fecha = [c for c in cs if not c["fecha"]]
        if sin_fecha:
            n += 1
            for c in sin_fecha:
                c["evento"] = n


def repartir(items: list, clave, n: int, rng: random.Random) -> list:
    """Hasta n items, uno por grupo por vuelta: cubre tantos grupos como pueda."""
    por_grupo = defaultdict(list)
    for it in items:
        por_grupo[clave(it)].append(it)
    colas = [rng.sample(v, len(v)) for v in por_grupo.values()]
    rng.shuffle(colas)
    salida = []
    while len(salida) < n and any(colas):
        for cola in colas:
            if cola and len(salida) < n:
                salida.append(cola.pop())
    return salida


def muestrear(cajas, por_especie, min_eventos, rng):
    por_especie_cajas = defaultdict(list)
    for c in cajas:
        por_especie_cajas[c["especie"]].append(c)
    muestra, descartadas = [], {}
    for esp in sorted(por_especie_cajas):
        cs = por_especie_cajas[esp]
        eventos = len({c["evento"] for c in cs})
        if eventos < min_eventos:
            descartadas[esp] = (len(cs), eventos)
            continue
        muestra += repartir(cs, lambda c: c["evento"], por_especie, rng)
    return muestra, descartadas


# ---------------------------------------------------------------- recortes --
def recortar(c: dict, carpeta: Path) -> str | None:
    destino = carpeta / f"{c['anotacion']}.jpg"
    if destino.exists():
        return str(destino)
    try:
        with Image.open(RAID / c["ruta"]) as im:
            caja = embeber.cuadrado(c["x1"], c["y1"], c["x2"], c["y2"], *im.size)
            recorte = im.crop(caja).convert("RGB").resize((embeber.LADO,) * 2, Image.BICUBIC)
        temporal = destino.with_suffix(".tmp")
        recorte.save(temporal, format="JPEG", quality=92)
        temporal.rename(destino)
    except OSError as e:
        print(f"  sin recorte para la anotación {c['anotacion']}: {e}", file=sys.stderr)
        return None
    return str(destino)


def hoja_de_contacto(muestra, rutas, destino: Path, por_fila=10, lado=112):
    """Los primeros recortes de cada especie, para revisar a ojo que el recorte es bueno."""
    por_esp = defaultdict(list)
    for c, r in zip(muestra, rutas):
        if len(por_esp[c["especie"]]) < por_fila:
            por_esp[c["especie"]].append(r)
    especies = sorted(por_esp)
    hoja = Image.new("RGB", (por_fila * lado, len(especies) * lado), "white")
    dibujo = ImageDraw.Draw(hoja)
    for i, esp in enumerate(especies):
        for j, r in enumerate(por_esp[esp]):
            with Image.open(r) as im:
                hoja.paste(im.resize((lado, lado)), (j * lado, i * lado))
        dibujo.rectangle((0, i * lado, lado * 3, i * lado + 14), fill="black")
        # La fuente por defecto de Pillow no tiene tildes.
        nombre = unicodedata.normalize("NFKD", esp).encode("ascii", "ignore").decode()
        dibujo.text((3, i * lado + 1), nombre[:34], fill="white")
    hoja.save(destino, quality=85)


# -------------------------------------------------------------- evaluación --
def average_precision(puntajes: np.ndarray, positivos: np.ndarray) -> float:
    rel = positivos[np.argsort(-puntajes, kind="stable")]
    n = rel.sum()
    if n == 0:
        return math.nan
    precision = np.cumsum(rel) / np.arange(1, len(rel) + 1)
    return float((precision * rel).sum() / n)


def por_ejemplos(E_candidatas: np.ndarray, E_ejemplos: np.ndarray) -> np.ndarray:
    sims = E_candidatas @ E_ejemplos.T
    k = min(TOP, E_ejemplos.shape[0])
    return np.partition(sims, -k, axis=1)[:, -k:].mean(axis=1)


def vecinos(E_candidatas, E_soporte, etiquetas_soporte, k=K_VECINOS) -> list[str]:
    """Especie propuesta por voto de los k vecinos, pesado por similitud."""
    sims = E_candidatas @ E_soporte.T
    idx = np.argpartition(-sims, k, axis=1)[:, :k]
    propuesta = []
    for fila, vs in enumerate(idx):
        votos = defaultdict(float)
        for v in vs:
            votos[etiquetas_soporte[v]] += sims[fila, v]
        propuesta.append(max(votos, key=votos.get))
    return propuesta


def partir(grupo: list, etiqueta: np.ndarray, rng: random.Random) -> np.ndarray:
    """grupo → 0 (ejemplos) o 1 (candidatas).

    Se decide por grupo, no por caja: una foto con un jaguar y un falso positivo
    queda entera de un lado. Las especies con menos grupos se reparten primero,
    para que cada una tenga grupos en los dos lados.
    """
    grupos_de = defaultdict(set)
    for g, e in zip(grupo, etiqueta):
        grupos_de[e].add(g)
    lado = {}
    for esp in sorted(grupos_de, key=lambda e: (len(grupos_de[e]), e)):
        cuenta = Counter(lado[g] for g in grupos_de[esp] if g in lado)
        libres = sorted(grupos_de[esp] - lado.keys(), key=str)
        rng.shuffle(libres)
        for g in libres:
            lado[g] = 0 if cuenta[0] <= cuenta[1] else 1
            cuenta[lado[g]] += 1
    return np.array([lado[g] for g in grupo])


def evaluar(muestra, emb, txt, clave, repeticiones, rng):
    etiqueta = np.array([c["especie"] for c in muestra])
    grupo = [c[clave] for c in muestra]
    especies = sorted(set(etiqueta))
    ap = defaultdict(list)                     # (modelo, condición, especie) → [AP]
    base = defaultdict(list)                   # especie → [fracción de positivos]
    aciertos = defaultdict(lambda: [0, 0])     # (modelo, especie) → [bien, total]
    for _ in range(repeticiones):
        lado = partir(grupo, etiqueta, rng)
        cand, sop = np.flatnonzero(lado == 1), np.flatnonzero(lado == 0)
        for esp in especies:
            ejemplos = sop[etiqueta[sop] == esp]
            positivos = etiqueta[cand] == esp
            if len(ejemplos) == 0 or not positivos.any():
                continue
            base[esp].append(positivos.mean())
            # Los mismos ejemplos para todos los modelos, de eventos distintos si se puede.
            elegidos = {n: np.array(repartir(list(ejemplos), lambda i: grupo[i], n, rng))
                        for n in N_EJEMPLOS if len(ejemplos) >= n}
            for m, E in emb.items():
                Ec = E[cand]
                if esp in txt[m]:
                    ap[(m, "texto", esp)].append(average_precision(Ec @ txt[m][esp], positivos))
                for n, sel in elegidos.items():
                    ap[(m, str(n), esp)].append(average_precision(por_ejemplos(Ec, E[sel]), positivos))
                ap[(m, "todos", esp)].append(average_precision(por_ejemplos(Ec, E[ejemplos]), positivos))
        # Una especie sin ejemplos de este lado no se puede proponer: no se cuenta.
        con_ejemplos = set(etiqueta[sop])
        for m, E in emb.items():
            for real, prop in zip(etiqueta[cand], vecinos(E[cand], E[sop], etiqueta[sop])):
                if real not in con_ejemplos:
                    continue
                aciertos[(m, real)][0] += int(real == prop)
                aciertos[(m, real)][1] += 1
    return resumir(ap, base, aciertos, list(emb), especies)


def resumir(ap, base, aciertos, modelos, especies) -> dict:
    """mAP por condición, siempre sobre las mismas especies para todos los modelos."""
    global_ = {}
    for cond in CONDICIONES:
        comunes = [e for e in especies if all(ap.get((m, cond, e)) for m in modelos)]
        global_[cond] = {
            "especies": len(comunes),
            "azar": float(np.mean([np.mean(base[e]) for e in comunes])) if comunes else None,
            **{m: float(np.mean([np.nanmean(ap[(m, cond, e)]) for e in comunes])) if comunes else None
               for m in modelos},
        }
    evaluadas = [e for e in especies if aciertos.get((modelos[0], e), [0, 0])[1]]
    global_["5-NN"] = {
        "especies": len(evaluadas),
        **{m: float(np.mean([aciertos[(m, e)][0] / aciertos[(m, e)][1] for e in evaluadas]))
           for m in modelos},
    }
    por_especie = {
        e: {m: {cond: (float(np.nanmean(ap[(m, cond, e)])) if ap.get((m, cond, e)) else None)
                for cond in CONDICIONES}
               | {"5-NN": (aciertos[(m, e)][0] / aciertos[(m, e)][1]) if aciertos[(m, e)][1] else None}
            for m in modelos}
        for e in especies
    }
    return {"global": global_, "por_especie": por_especie}


# ----------------------------------------------------------------- reporte --
def pct(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f}"


def reporte(meta, eventos, sitios, info_especies, modelos) -> str:
    lineas = [
        "# Comparación de modelos para el buscador por especie",
        "",
        f"Generado {meta['fecha']} en {meta['gpu']}. Semilla {meta['semilla']}, "
        f"{meta['repeticiones']} repeticiones por partición.",
        "",
        f"Muestra: **{meta['recortes']:,} cajas** de **{meta['especies']} especies** "
        f"(hasta {meta['por_especie']} por especie, con al menos {meta['min_eventos']} eventos). "
        f"Evento = misma estación, fotos a menos de {VENTANA // 60} min.",
        "",
        "Métrica: average precision (AP) de ordenar todas las candidatas por parecido a una "
        "especie, promediada entre especies (mAP), en %. `azar` es lo que daría un orden "
        "aleatorio. `5-NN` es la exactitud media por especie de la especie propuesta.",
        "",
    ]
    for titulo, res in (("Partición por evento", eventos),
                        ("Partición por estación (cámaras sin nada etiquetado)", sitios)):
        lineas += [f"## {titulo}", "",
                   "| Condición | Especies | Azar | " + " | ".join(modelos) + " |",
                   "|---|---|---|" + "---|" * len(modelos)]
        for cond, fila in res["global"].items():
            nombre = {"texto": "Solo texto", "todos": "Todos los ejemplos",
                      "5-NN": "Especie propuesta (5-NN)"}.get(cond, f"{cond} ejemplo(s)")
            lineas.append(f"| {nombre} | {fila['especies']} | {pct(fila.get('azar'))} | "
                          + " | ".join(pct(fila[m]) for m in modelos) + " |")
        lineas.append("")
    lineas += ["## Por especie (partición por evento)", "",
               "AP en %: texto / 5 ejemplos / todos. `conf.` = confianza del mapeo a nombre científico.",
               "",
               "| Especie | Científico | conf. | Cajas | Eventos | " + " | ".join(modelos) + " |",
               "|---|---|---|---|---|" + "---|" * len(modelos)]
    for esp, info in sorted(info_especies.items(), key=lambda kv: -kv[1]["cajas"]):
        celdas = []
        for m in modelos:
            r = eventos["por_especie"].get(esp, {}).get(m, {})
            celdas.append(" / ".join(pct(r.get(c)) for c in ("texto", "5", "todos")))
        lineas.append(f"| {esp} | {info['cientifico'] or '—'} | {info['confianza'] or '—'} | "
                      f"{info['cajas']} | {info['eventos']} | " + " | ".join(celdas) + " |")
    lineas += ["", "## Costo", "", "| Modelo | Dim | Recortes/s | Memoria GPU máx. |", "|---|---|---|---|"]
    for m in modelos:
        t = meta["tiempos"][m]
        lineas.append(f"| {m} | {t['dim']} | {t['por_segundo']:.0f} | {t['memoria_gb']:.1f} GB |")
    lineas += ["", f"Recortes desde el RAID: {meta['recortes_por_segundo']:.0f} fotos/s "
               f"con {meta['hilos']} hilos (decodificar la foto entera es lo caro)."]
    if meta["descartadas"]:
        lineas += ["", "Especies fuera de la muestra (cajas, eventos): "
                   + ", ".join(f"{e} ({c}, {ev})" for e, (c, ev) in sorted(meta["descartadas"].items()))
                   + "."]
    return "\n".join(lineas) + "\n"


# -------------------------------------------------------------------- main --
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="/fuentes/etiquetas/labels.db")
    ap.add_argument("--salida", default="/resultados/comparacion-modelos")
    ap.add_argument("--modelos", nargs="+", default=list(embeber.MODELOS), choices=list(embeber.MODELOS))
    ap.add_argument("--por-especie", type=int, default=150)
    ap.add_argument("--min-eventos", type=int, default=6)
    ap.add_argument("--repeticiones", type=int, default=10)
    ap.add_argument("--semilla", type=int, default=17)
    ap.add_argument("--hilos", type=int, default=16, help="lectura de fotos del RAID")
    a = ap.parse_args()

    rng = random.Random(a.semilla)
    salida = Path(a.salida)
    (salida / "recortes").mkdir(parents=True, exist_ok=True)

    cajas = leer_cajas(a.db)
    agrupar(cajas)
    muestra, descartadas = muestrear(cajas, a.por_especie, a.min_eventos, rng)
    print(f"{len(cajas):,} cajas con especie, {len({c['evento'] for c in cajas}):,} eventos, "
          f"{len({c['sitio'] for c in cajas})} sitios")
    print(f"muestra: {len(muestra):,} cajas de {len({c['especie'] for c in muestra})} especies "
          f"({len(descartadas)} especies con menos de {a.min_eventos} eventos quedan fuera)")

    t0 = time.time()
    with ThreadPoolExecutor(a.hilos) as ex:
        rutas = list(ex.map(lambda c: recortar(c, salida / "recortes"), muestra))
    seg_recortes = time.time() - t0
    muestra, rutas = zip(*[(c, r) for c, r in zip(muestra, rutas) if r])
    muestra, rutas = list(muestra), list(rutas)
    print(f"recortes: {len(rutas):,} en {seg_recortes:.0f} s")
    hoja_de_contacto(muestra, rutas, salida / "hoja-de-contacto.jpg")

    with open(salida / "muestra.csv", "w", newline="", encoding="utf-8") as f:
        campos = ["anotacion", "foto", "especie", "cientifico", "sitio", "evento", "fecha", "ruta",
                  "x1", "y1", "x2", "y2"]
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(muestra)

    info_especies = {}
    for c in muestra:
        i = info_especies.setdefault(c["especie"], {
            "cientifico": c["cientifico"], "ingles": c["ingles"], "confianza": c["confianza"],
            "cajas": 0, "eventos": set(), "sitios": set()})
        i["cajas"] += 1
        i["eventos"].add(c["evento"])
        i["sitios"].add(c["sitio"])

    emb, txt, tiempos = {}, {}, {}
    for nombre in a.modelos:
        print(f"{nombre}: cargando")
        m = embeber.cargar(nombre)
        torch.cuda.reset_peak_memory_stats()
        t = time.time()
        E = embeber.imagenes(m, rutas)
        seg = time.time() - t
        txt[nombre] = {}
        for esp, i in info_especies.items():
            if not i["cientifico"]:          # falso positivo: no es un taxón, no hay texto
                continue
            frases = [p.format(cientifico=i["cientifico"], ingles=i["ingles"])
                      for p in PLANTILLAS if i["ingles"] or "{ingles}" not in p]
            txt[nombre][esp] = embeber.texto(m, frases)
        tiempos[nombre] = {"dim": int(E.shape[1]), "por_segundo": len(rutas) / seg,
                           "memoria_gb": torch.cuda.max_memory_allocated() / 1e9}
        np.save(salida / f"embeddings-{nombre}.npy", E)
        emb[nombre] = E
        embeber.liberar(m)
        print(f"{nombre}: {len(rutas) / seg:.0f} recortes/s, dim {E.shape[1]}")

    print("evaluando por evento…")
    eventos = evaluar(muestra, emb, txt, "evento", a.repeticiones, rng)
    print("evaluando por estación…")
    sitios = evaluar(muestra, emb, txt, "sitio", a.repeticiones, rng)

    for i in info_especies.values():
        i["eventos"], i["sitios"] = len(i["eventos"]), len(i["sitios"])
    meta = {
        "fecha": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "gpu": torch.cuda.get_device_name(0), "semilla": a.semilla,
        "repeticiones": a.repeticiones, "por_especie": a.por_especie,
        "min_eventos": a.min_eventos, "recortes": len(rutas), "especies": len(info_especies),
        "recortes_por_segundo": len(rutas) / seg_recortes, "hilos": a.hilos,
        "tiempos": tiempos, "descartadas": descartadas,
    }
    (salida / "resultados.json").write_text(json.dumps(
        {"meta": meta, "por_evento": eventos, "por_estacion": sitios, "especies": info_especies},
        ensure_ascii=False, indent=1), encoding="utf-8")
    texto_reporte = reporte(meta, eventos, sitios, info_especies, a.modelos)
    (salida / "reporte.md").write_text(texto_reporte, encoding="utf-8")
    print("\n" + texto_reporte)
    return 0


if __name__ == "__main__":
    sys.exit(main())
