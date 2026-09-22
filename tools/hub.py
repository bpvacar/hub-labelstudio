#!/usr/bin/env python3
"""hub — proyectos de etiquetado del HUB en Label Studio.

Un proyecto = un modo (audio, camtrap, video, imagen, texto) + una carpeta de
datos. Los archivos no se suben: Label Studio los lee de donde están.

    hub modos
    hub lista
    hub nuevo audio "Mecheros 2024 — piloto" --datos datos/audio/mecheros
    hub nuevo camtrap "Estación 2019" --datos "dgxraid/fotos/2019"
    hub sync 3
    hub especies 3 aves.csv
    hub plantilla 7 camtrap          # tras cambiar configs/camtrap.xml
    hub cientificos 7 --seco         # anotaciones viejas → nombre científico
    hub birdnet 3 detections.csv --min-conf 0.25
    hub historial                    # webhook del historial en todos los proyectos

Corre dentro del contenedor `tools` (docker compose run --rm tools hub …; el
wrapper ~/labelstudio/hub lo hace). Los CSV se leen de ~/labelstudio/entrada/.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from label_studio_sdk import LabelStudio

RAIZ = Path(__file__).resolve().parent.parent          # /app en la imagen
CONFIGS = RAIZ / "configs"
VOCAB = RAIZ / "vocabularios"
DOC_ROOT = "/label-studio/files"                        # LOCAL_FILES_DOCUMENT_ROOT, mismo montaje que app
HISTORIAL_BASE = "http://historial.labelstudio.internal:8000"   # alias de red en compose.yml
HISTORIAL_URL = f"{HISTORIAL_BASE}/webhook"

# Qué archivos entran en cada modo. Los `._*` son resource forks de macOS: tienen
# extensión ".jpg" pero no son imágenes.
SIN_FORKS = r"(?!(?:.*/)?\._)"
MODOS = {
    "audio":   dict(regex=rf"(?i)^{SIN_FORKS}.*\.(wav|flac|mp3|ogg|m4a)$",
                    clases="clases-audio.csv", especies="sin-especies.csv",
                    desc="Segmentos en el espectrograma: clase + especie por segmento"),
    "camtrap": dict(regex=rf"(?i)^{SIN_FORKS}.*\.(jpe?g|png)$",
                    clases=None, especies="camtrap-especies.csv",
                    desc="Caja (animal/human/vehicle) + especie por caja"),
    "video":   dict(regex=rf"(?i)^{SIN_FORKS}.*\.(mp4|webm)$",
                    clases="clases-video.csv", especies="camtrap-especies.csv",
                    desc="Cajas con seguimiento + comportamiento en la línea de tiempo"),
    "imagen":  dict(regex=rf"(?i)^{SIN_FORKS}.*\.(jpe?g|png|webp)$",
                    clases=None, especies="sin-especies.csv",
                    desc="Especie de la imagen entera, sin cajas"),
    "texto":   dict(regex=rf"(?i)^{SIN_FORKS}.*\.txt$",
                    clases=None, especies="sin-especies.csv",
                    desc="Entidades (especie, lugar, fecha) en documentos"),
}


# ------------------------------------------------------------------ conexión --
def var(nombre: str) -> str:
    v = os.environ.get(nombre)
    if not v:
        sys.exit(f"falta la variable {nombre} (¿se está corriendo fuera del contenedor tools?)")
    return v


def url_interna() -> str:
    """nginx por la red interna del compose; el 8081 público es solo HTTPS."""
    return os.environ.get("LS_URL", "http://nginx:8085")


def cliente(clave: str = "LS_API_KEY") -> LabelStudio:
    return LabelStudio(base_url=url_interna(), api_key=var(clave))


def http(clave: str = "LS_API_KEY") -> httpx.Client:
    """Cliente HTTP crudo, para endpoints que la SDK no expone cómodos (export)."""
    r = httpx.post(f"{url_interna()}/api/token/refresh", json={"refresh": var(clave)}, timeout=30)
    r.raise_for_status()
    return httpx.Client(base_url=url_interna(), timeout=600,
                        headers={"Authorization": f"Bearer {r.json()['access']}"})


def ruta_local(ruta_ls: str) -> Path:
    """datos/... y dgxraid/...: el contenedor los monta donde los ve Label Studio."""
    raiz = ruta_ls.split("/", 1)[0]
    if raiz not in ("datos", "dgxraid"):
        sys.exit(f"la ruta tiene que empezar con datos/ o dgxraid/, no {raiz}/")
    return Path(DOC_ROOT) / ruta_ls


# ----------------------------------------------------------------- plantillas --
def leer_vocab(nombre_o_ruta: str) -> list[dict[str, str]]:
    """CSV con columnas valor[,cientifico,rango,confianza,ayuda] o valor[,color].

    `valor` es lo que se ve en la interfaz; `cientifico`, si está, es lo que se
    guarda en la anotación (alias de Label Studio). La taxonomía tiene que ser
    consistente entre modalidades: en camera traps se ve el nombre en español y
    se guarda el científico, igual que en audio.
    """
    p = Path(nombre_o_ruta)
    if not p.exists():
        p = VOCAB / nombre_o_ruta
    if not p.exists():
        sys.exit(f"no existe {nombre_o_ruta} (ni en /entrada ni en vocabularios/)")
    with p.open(newline="", encoding="utf-8") as f:
        filas = [r for r in csv.DictReader(f) if r.get("valor", "").strip()]
    if not filas:
        sys.exit(f"{p}: vocabulario vacío (hace falta una columna 'valor')")
    return filas


def xml_choices(filas, sangria="        ") -> str:
    out = []
    for r in filas:
        attrs = f'value="{escape(r["valor"].strip(), quote=True)}"'
        if (r.get("cientifico") or "").strip():
            attrs += f' alias="{escape(r["cientifico"].strip(), quote=True)}"'
        if r.get("ayuda"):
            attrs += f' hint="{escape(r["ayuda"], quote=True)}"'
        out.append(f"{sangria}<Choice {attrs}/>")
    return "\n".join(out)


def xml_labels(filas, sangria="    ") -> str:
    out = []
    for r in filas:
        bg = f' background="{r["color"]}"' if r.get("color") else ""
        out.append(f'{sangria}<Label value="{escape(r["valor"].strip(), quote=True)}"{bg}/>')
    return "\n".join(out)


def armar_config(modo: str, especies: str | list | None, clases: str | list | None,
                 fps: float) -> str:
    """Plantilla del modo + vocabularios. Cada vocabulario es un CSV o ya una lista de filas."""
    m = MODOS[modo]
    filas = lambda v, defecto: v if isinstance(v, list) else leer_vocab(v or defecto)
    xml = (CONFIGS / f"{modo}.xml").read_text(encoding="utf-8")
    xml = xml.replace("{{ESPECIES}}", xml_choices(filas(especies, m["especies"])))
    if "{{CLASES}}" in xml:
        xml = xml.replace("{{CLASES}}", xml_labels(filas(clases, m["clases"])))
    xml = xml.replace("{{FPS}}", f"{fps:g}")
    ET.fromstring(xml)  # que falle aquí y no a mitad de la creación
    return xml


def especies_usadas(proyecto_id: int) -> set[str]:
    """Especies en anotaciones o predicciones del proyecto, vía el servicio historial.

    Se pregunta al historial (SQL directo) y no a la API de Label Studio: exportar
    las 96 465 tareas del proyecto 7 revienta el timeout de uWSGI.
    """
    r = httpx.get(f"{HISTORIAL_BASE}/especies-usadas", params={"proyecto": proyecto_id},
                  headers={"X-Historial-Token": var("HISTORIAL_TOKEN")}, timeout=120)
    r.raise_for_status()
    return set(r.json()["especies"])


def actualizar_config(ls, proyecto_id: int, xml: str):
    """Único punto por donde `hub` cambia un label_config.

    Con alias, Label Studio deja de proteger las especies en uso: valida contra
    `value`, pero la anotación guarda el `alias`, así que acepta quitar una
    especie que 15 000 anotaciones usan (verificado el 17 sep 2026). Esta
    verificación la reemplaza: cada especie usada tiene que seguir existiendo
    como value o como alias.
    """
    nuevas = especies_de_config(xml)
    disponibles = {r["valor"] for r in nuevas} | {r["cientifico"] for r in nuevas if r["cientifico"]}
    huerfanas = especies_usadas(proyecto_id) - disponibles
    if huerfanas:
        sys.exit(f"el cambio dejaría sin especie a anotaciones existentes: {sorted(huerfanas)}")
    ls.projects.update(id=proyecto_id, label_config=xml)


def especies_de_config(label_config: str) -> list[dict[str, str]]:
    raiz = ET.fromstring(label_config)
    return [dict(valor=c.get("value"), cientifico=c.get("alias", ""), ayuda=c.get("hint", ""))
            for c in raiz.findall(".//Taxonomy[@name='especie']/Choice")]


# ------------------------------------------------------------------- comandos --
def cmd_modos(_):
    for k, m in MODOS.items():
        print(f"{k:8}  {m['desc']}")


def cmd_lista(a):
    ls = cliente()
    for p in ls.projects.list():
        print(f"{p.id:>4}  {p.title:<40} {p.task_number or 0:>7} tareas  "
              f"{p.finished_task_number or 0:>7} anotadas  "
              f"{p.total_predictions_number or 0:>7} predicciones")


def cmd_nuevo(a):
    local = ruta_local(a.datos)
    if not local.is_dir():
        sys.exit(f"no existe: {a.datos}")
    config = armar_config(a.modo, a.especies, a.clases, a.fps)

    ls = cliente()
    proyecto = ls.projects.create(
        title=a.titulo,
        description=a.descripcion or f"Modo {a.modo}. Datos: {a.datos}",
        label_config=config,
        sampling="Sequential sampling",
        show_skip_button=True,
        enable_empty_annotation=True,
        show_collab_predictions=True,       # las predicciones aparecen como pre-anotación
    )
    storage = ls.import_storage.local.create(
        project=proyecto.id,
        title=a.datos,
        path=str(local),
        regex_filter=MODOS[a.modo]["regex"],
        use_blob_urls=True,                 # un archivo = una tarea
        recursive_scan=True,
    )
    print(f"proyecto {proyecto.id} creado: {a.titulo} (webhook del historial "
          f"{asegurar_webhook(ls, proyecto.id)})")
    if not a.sin_sync:
        ls.import_storage.local.sync(id=storage.id)
        print(f"sincronizando {a.datos} … (corre en segundo plano; `hub lista` para ver el avance)")


def cmd_sync(a):
    ls = cliente()
    storages = ls.import_storage.local.list(project=a.proyecto)
    if not storages:
        sys.exit(f"el proyecto {a.proyecto} no tiene storage local")
    for s in storages:
        ls.import_storage.local.sync(id=s.id)
        print(f"sync lanzado: {s.title}")


def _agregar_especies(ls, proyecto_id: int, nuevas: list[dict[str, str]]) -> int:
    """Agrega opciones al Taxonomy `especie` sin borrar ninguna.

    Label Studio rechaza un config que quite etiquetas ya usadas; agregar es
    siempre seguro. Una especie ya está si coincide su valor o su científico.
    El placeholder "Sin identificar" se quita solo mientras el proyecto no
    tenga anotaciones.
    """
    p = ls.projects.get(id=proyecto_id)
    raiz = ET.fromstring(p.label_config)
    tax = raiz.find(".//Taxonomy[@name='especie']")
    if tax is None:
        sys.exit(f"el proyecto {proyecto_id} no tiene un Taxonomy 'especie'")
    existentes = {c.get("value") for c in tax.findall("Choice")}
    existentes |= {c.get("alias") for c in tax.findall("Choice") if c.get("alias")}
    cambios = 0
    for r in nuevas:
        v, sci = r["valor"].strip(), (r.get("cientifico") or "").strip()
        if v in existentes or (sci and sci in existentes):
            continue
        el = ET.SubElement(tax, "Choice", value=v)
        if sci:
            el.set("alias", sci)
        if r.get("ayuda"):
            el.set("hint", r["ayuda"])
        existentes |= {v, sci} - {""}
        cambios += 1
    placeholder = [c for c in tax.findall("Choice") if c.get("value") == "Sin identificar"]
    if placeholder and len(tax.findall("Choice")) > 1 and not p.finished_task_number:
        tax.remove(placeholder[0])
        cambios += 1
    if cambios:
        actualizar_config(ls, proyecto_id, ET.tostring(raiz, encoding="unicode"))
    return cambios


def cmd_plantilla(a):
    """Vuelve a aplicar la plantilla del modo a un proyecto existente.

    Conserva el vocabulario que el proyecto ya tiene (especies con su alias,
    clases, fps), así que sirve para propagar un cambio de diseño de
    configs/*.xml sin perder las especies que agregó `hub birdnet` o `hub especies`.
    Con --especies, el vocabulario del CSV se mezcla encima: actualiza alias y
    ayuda de las especies que ya están y agrega las nuevas.
    """
    ls = cliente()
    p = ls.projects.get(id=a.proyecto)
    viejo = ET.fromstring(p.label_config)
    especies = especies_de_config(p.label_config)
    if a.especies:
        por_valor = {r["valor"]: r for r in especies}
        for r in leer_vocab(a.especies):
            por_valor[r["valor"]] = r
        especies = list(por_valor.values())
    clases = [dict(valor=l.get("value"), color=l.get("background", ""))
              for l in viejo.findall(".//Labels[@name='clase']/Label")] or None
    video = viejo.find(".//Video")
    fps = float(video.get("framerate", 30)) if video is not None else 30.0
    actualizar_config(ls, a.proyecto, armar_config(a.modo, especies or None, clases, fps))
    con_alias = sum(1 for r in especies if (r.get("cientifico") or "").strip())
    print(f"plantilla {a.modo} aplicada al proyecto {a.proyecto} "
          f"({len(especies)} especies, {con_alias} con nombre científico, "
          f"{len(clases or [])} clases)")


def cmd_especies(a):
    ls = cliente()
    n = _agregar_especies(ls, a.proyecto, leer_vocab(a.csv))
    print(f"{n} cambios en el vocabulario del proyecto {a.proyecto}")


def cmd_cientificos(a):
    """Reescribe las anotaciones que guardan el nombre visible en vez del científico.

    Usa el vocabulario del propio proyecto (valor → alias). Cada anotación se
    actualiza con el token de quien la hizo cuando ese token existe (MATLAB),
    así `updated_by` no pasa a ser la cuenta de scripts. El historial registra
    la versión anterior de cada una: el texto original no se pierde.
    """
    ls = cliente()
    p = ls.projects.get(id=a.proyecto)
    mapa = {r["valor"]: r["cientifico"] for r in especies_de_config(p.label_config)
            if r.get("cientifico")}
    if not mapa:
        sys.exit("el vocabulario del proyecto no tiene nombres científicos; "
                 "primero `hub plantilla <proyecto> <modo> --especies <csv>`")

    with http() as c:
        tareas = c.get(f"/api/projects/{a.proyecto}/export",
                       params={"exportType": "JSON", "download_all_tasks": "false"}).json()
    usuarios = {u.id: u.email for u in ls.users.list()}
    clientes = {"matlab@hub.local": cliente("LS_API_KEY_MATLAB")} \
        if os.environ.get("LS_API_KEY_MATLAB") else {}

    pendientes, cuenta, sin_mapa = [], Counter(), Counter()
    for t in tareas:
        for an in t.get("annotations", []):
            nuevo, cambio = [], False
            for r in an["result"]:
                if r.get("type") == "taxonomy":
                    ruta = r["value"].get("taxonomy") or []
                    ruta2 = [[mapa.get(x, x) for x in camino] for camino in ruta]
                    for camino in ruta:
                        for x in camino:
                            (cuenta if x in mapa else sin_mapa)[x] += 1
                    if ruta2 != ruta:
                        r = {**r, "value": {**r["value"], "taxonomy": ruta2}}
                        cambio = True
                nuevo.append(r)
            if cambio:
                autor = an.get("completed_by")
                autor = usuarios.get(autor.get("id") if isinstance(autor, dict) else autor)
                pendientes.append((an["id"], nuevo, autor))

    print(f"{len(pendientes):,} anotaciones por reescribir; {sum(cuenta.values()):,} regiones")
    for v, n in cuenta.most_common(8):
        print(f"  {n:>6}  {v}  →  {mapa[v]}")
    ya = {k: v for k, v in sin_mapa.items() if k not in mapa.values()}
    if ya:
        print(f"sin nombre científico en el vocabulario (se dejan igual): {dict(ya)}")
    if a.seco or not pendientes:
        return

    def escribir(item):
        aid, result, autor = item
        clientes.get(autor, ls).annotations.update(id=aid, result=result)

    hechas = 0
    with ThreadPoolExecutor(max_workers=a.hilos) as pool:
        for _ in pool.map(escribir, pendientes):
            hechas += 1
            if hechas % 1000 == 0:
                print(f"  {hechas:,}/{len(pendientes):,}", flush=True)
    print(f"{hechas:,} anotaciones reescritas")


ACCIONES_HISTORIAL = ["ANNOTATION_CREATED", "ANNOTATIONS_CREATED",
                      "ANNOTATION_UPDATED", "ANNOTATIONS_DELETED"]


def asegurar_webhook(ls, proyecto_id: int) -> str:
    """Webhook del historial en un proyecto. Idempotente.

    Label Studio 1.23 solo acepta webhooks por proyecto desde la API (el
    `perform_create` exige `project`), así que va uno por proyecto. Si alguno
    falta igual no hay hueco: la conciliación del historial lee la base.
    """
    datos = dict(url=HISTORIAL_URL, actions=ACCIONES_HISTORIAL, send_payload=True,
                 send_for_all_actions=False, is_active=True,
                 headers={"X-Historial-Token": var("HISTORIAL_TOKEN")})
    for w in ls.webhooks.list(project=str(proyecto_id)):
        if w.url == HISTORIAL_URL:
            ls.webhooks.update(id=w.id, **datos)
            return "actualizado"
    ls.webhooks.create(project=proyecto_id, **datos)
    return "creado"


def cmd_historial(a):
    """Registra (o repara) el webhook del historial en todos los proyectos."""
    ls = cliente()
    for p in ls.projects.list():
        print(f"{p.id:>4}  {p.title:<40} webhook {asegurar_webhook(ls, p.id)}")
    r = httpx.get(f"{HISTORIAL_BASE}/salud", timeout=5)
    print(f"historial: {r.json()}")


def _tareas_por_archivo(ls, proyecto_id: int) -> dict[str, int]:
    """ruta relativa (y basename) → id de tarea, para emparejar CSVs externos."""
    idx: dict[str, int] = {}
    dup: set[str] = set()
    for t in ls.tasks.list(project=proyecto_id, fields="all"):
        url = next(iter(t.data.values()))
        d = parse_qs(urlparse(url).query).get("d", [""])[0]
        for clave in (d, Path(d).name):
            if clave in idx and idx[clave] != t.id:
                dup.add(clave)
            idx[clave] = t.id
    for clave in dup:
        idx.pop(clave, None)                    # un basename repetido no empareja
    return idx


def cmd_birdnet(a):
    """Detecciones de BirdNET → predictions del modo audio.

    El CSV necesita: archivo, start_s, end_s, scientific_name, confidence
    (common_name es opcional y va de ayuda). `archivo` puede ser el nombre del
    WAV o la ruta relativa. run_birdnet_tbs.py escribe record_id en vez de
    archivo: hay que unirlo antes con out/pull_list.csv.
    """
    ls = cliente()
    idx = _tareas_por_archivo(ls, a.proyecto)
    por_tarea: dict[int, list[dict]] = defaultdict(list)
    especies: dict[str, str] = {}
    sin_tarea, bajo_umbral = set(), 0

    with open(a.csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("scientific_name", "").startswith("__"):
                continue                                  # filas de error del pipeline
            conf = float(r["confidence"] or 0)
            if conf < a.min_conf:
                bajo_umbral += 1
                continue
            archivo = r["archivo"]
            tid = idx.get(archivo) or idx.get(Path(archivo).name)
            if tid is None:
                sin_tarea.add(archivo)
                continue
            sci = r["scientific_name"].strip()
            especies.setdefault(sci, r.get("common_name", "").strip())
            por_tarea[tid].append(dict(start=float(r["start_s"]), end=float(r["end_s"]),
                                       sci=sci, conf=conf))

    if especies:
        n = _agregar_especies(ls, a.proyecto,
                              [dict(valor=k, ayuda=v) for k, v in sorted(especies.items())])
        print(f"{n} cambios en el vocabulario")

    lote = []
    for tid, dets in por_tarea.items():
        result = []
        for d in dets:
            rid = uuid.uuid4().hex[:10]
            base = dict(start=d["start"], end=d["end"], channel=0)
            result.append(dict(id=rid, from_name="clase", to_name="audio", type="labels",
                               origin="prediction", score=d["conf"],
                               value={**base, "labels": ["Ave"]}))
            result.append(dict(id=rid, from_name="especie", to_name="audio", type="taxonomy",
                               origin="prediction", score=d["conf"],
                               value={**base, "taxonomy": [[d["sci"]]]}))
        lote.append(dict(task=tid, result=result, model_version=a.modelo,
                         score=max(d["conf"] for d in dets)))

    for i in range(0, len(lote), 500):
        ls.projects.import_predictions(id=a.proyecto, request=lote[i:i + 500])
    ls.projects.update(id=a.proyecto, model_version=a.modelo)
    print(f"{sum(len(v) for v in por_tarea.values())} detecciones en {len(lote)} tareas "
          f"({bajo_umbral} bajo min_conf={a.min_conf})")
    if sin_tarea:
        print(f"ojo: {len(sin_tarea)} archivos del CSV no tienen tarea, p.ej. {sorted(sin_tarea)[:3]}")


# ------------------------------------------------------------------------ cli --
def main():
    ap = argparse.ArgumentParser(prog="hub", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("modos", help="modos disponibles").set_defaults(f=cmd_modos)
    sub.add_parser("lista", help="proyectos y avance").set_defaults(f=cmd_lista)

    n = sub.add_parser("nuevo", help="crear proyecto + storage + sync")
    n.add_argument("modo", choices=MODOS)
    n.add_argument("titulo")
    n.add_argument("--datos", required=True,
                   help="carpeta bajo datos/ (~/labelstudio/datos) o dgxraid/ (/mnt/DGX0Raid)")
    n.add_argument("--especies", help="CSV valor,cientifico,rango,confianza,ayuda (default según modo)")
    n.add_argument("--clases", help="CSV valor,color para audio/video")
    n.add_argument("--fps", type=float, default=30.0, help="solo video")
    n.add_argument("--descripcion")
    n.add_argument("--sin-sync", action="store_true")
    n.set_defaults(f=cmd_nuevo)

    s = sub.add_parser("sync", help="volver a escanear la carpeta del proyecto")
    s.add_argument("proyecto", type=int)
    s.set_defaults(f=cmd_sync)

    t = sub.add_parser("plantilla", help="reaplicar configs/<modo>.xml conservando el vocabulario")
    t.add_argument("proyecto", type=int)
    t.add_argument("modo", choices=MODOS)
    t.add_argument("--especies", help="CSV que se mezcla encima del vocabulario actual")
    t.set_defaults(f=cmd_plantilla)

    e = sub.add_parser("especies", help="agregar especies al vocabulario de un proyecto")
    e.add_argument("proyecto", type=int)
    e.add_argument("csv")
    e.set_defaults(f=cmd_especies)

    c = sub.add_parser("cientificos", help="reescribir anotaciones al nombre científico")
    c.add_argument("proyecto", type=int)
    c.add_argument("--seco", action="store_true", help="solo contar, no escribir")
    c.add_argument("--hilos", type=int, default=6, help="escrituras en paralelo")
    c.set_defaults(f=cmd_cientificos)

    b = sub.add_parser("birdnet", help="importar detecciones de BirdNET como predictions")
    b.add_argument("proyecto", type=int)
    b.add_argument("csv")
    b.add_argument("--min-conf", type=float, default=0.1)
    b.add_argument("--modelo", default="BirdNET-Analyzer V2.4")
    b.set_defaults(f=cmd_birdnet)

    sub.add_parser("historial", help="registrar el webhook del historial").set_defaults(f=cmd_historial)

    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
