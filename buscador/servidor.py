#!/usr/bin/env python3
"""El buscador por especie: la grilla, servida como aplicación web.

Se elige una especie, la grilla muestra las detecciones sin identificar ordenadas
por parecido, y lo confirmado se registra en Label Studio **a nombre de quien lo
confirma**: el buscador no guarda identificaciones propias. Lo único que guarda es
qué detecciones se descartaron para qué especie, que Label Studio no tiene dónde
anotar.

La identidad proviene del token personal de Label Studio: la sesión del buscador no
es una cuenta aparte.

    docker compose up -d buscador          # perfil `buscador`
    https://IP_DEL_SERVIDOR:4200

Todo el cálculo es sobre los embeddings ya hechos (`calcular_embeddings.py`).
El modelo solo se carga si alguien busca por texto libre, y en CPU: la GPU del
servidor compartido no se queda ocupada.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buscar  # noqa: E402  (índice, etiquetas de Label Studio, IoU)
import embeber  # noqa: E402

WEB = Path(__file__).resolve().parent / "web"
# La URL pública, para poder abrir la tarea en Label Studio desde el panel.
PUBLICO = os.environ.get("LS_PUBLICO", "https://localhost:8081").rstrip("/")
RAID = Path(os.environ.get("FOTOS_RAIZ", "/label-studio/files/fotos"))
TOP = 3                    # el puntaje es la media de los TOP ejemplos más parecidos
IOU_MISMA_CAJA = 0.8       # para no duplicar una región que ya está en la anotación
REFRESCO = 600             # s entre relecturas de las identificaciones de Label Studio
CADUCA_SESION = 12 * 3600
VENTANA_EVENTO = 30 * 60   # s: fotos del mismo sitio más cercanas que esto son un evento
HERMANAS = 24              # cuántas detecciones del evento se muestran en el panel


# ------------------------------------------------------------------- estado --
class Estado:
    """Todo lo que se carga una vez y se comparte entre pedidos."""

    def __init__(self, a):
        self.a = a
        carpeta = Path(a.indice) / a.modelo
        self.filas, vectores = buscar.cargar_indice(carpeta)
        self.vectores = np.asarray(vectores, dtype=np.float32)
        self.meta = json.loads((carpeta / "meta.json").read_text(encoding="utf-8"))
        self.por_deteccion = {f["deteccion"]: i for i, f in enumerate(self.filas)}

        db = sqlite3.connect(f"file:{a.db}?mode=ro&immutable=1", uri=True)
        db.row_factory = sqlite3.Row
        self.taxones = [dict(r) for r in db.execute(
            "SELECT label, scientific_name, name_en, kind, confidence FROM taxon"
            " WHERE kind IN ('species', 'group', 'false_positive') ORDER BY label")]
        self.info = {str(r["id"]): dict(r) for r in db.execute(
            "SELECT id, width, height, captured_at, camera_make, camera_model, station,"
            " deployment, latitude, longitude, year FROM image")}
        self.tamano = {k: (v["width"], v["height"]) for k, v in self.info.items()}
        self._calcular_eventos()

        self.tareas = {}
        mapa = Path(a.indice).parent / f"tareas-p{a.proyecto}.csv"
        if mapa.exists():
            with open(mapa, encoding="utf-8") as f:
                self.tareas = {r["sha256"]: int(r["tarea"]) for r in csv.DictReader(f)}
        else:
            print(f"AVISO: falta {mapa}; sin él no se puede escribir en Label Studio.\n"
                  "       docker compose run --rm embeddings python buscador/mapa_tareas.py",
                  file=sys.stderr)

        self.rechazos_db = Path(a.datos) / "rechazos.db"
        self.rechazos_db.parent.mkdir(parents=True, exist_ok=True)
        with self._rechazos() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS rechazo (
                deteccion TEXT NOT NULL, cientifico TEXT NOT NULL,
                quien TEXT, cuando TEXT NOT NULL,
                PRIMARY KEY (deteccion, cientifico))""")

        self.candado = threading.Lock()
        self.identificacion: dict[str, dict] = {}   # detección → especie, quién, cuándo
        self.etiquetas: dict[str, str] = {}         # detección → especie (para el ranking)
        self.sesiones: dict[str, dict] = {}
        self.modelo = None
        self.refrescar_etiquetas()
        threading.Thread(target=self._refrescar_cada, daemon=True).start()

    # ---------------------------------------------------------------- datos --
    def _calcular_eventos(self):
        """Evento = detecciones del mismo sitio separadas por menos de media hora.

        Es la unidad con la que mira un biólogo: la ráfaga entera de la cámara, donde
        el mismo animal suele verse mejor en otra foto.
        """
        por_sitio = defaultdict(list)
        for i, f in enumerate(self.filas):
            por_sitio[f["estacion"] or str(Path(f["ruta"]).parent)].append(i)
        self.evento = [None] * len(self.filas)
        self.eventos: dict[int, list[int]] = defaultdict(list)
        n = 0
        for indices in por_sitio.values():
            previo = None
            for i in sorted((i for i in indices if self.filas[i]["fecha"]),
                            key=lambda i: self.filas[i]["fecha"]):
                t = datetime.fromisoformat(self.filas[i]["fecha"]).timestamp()
                if previo is None or t - previo > VENTANA_EVENTO:
                    n += 1
                self.evento[i] = n
                previo = t
            sin_fecha = [i for i in indices if not self.filas[i]["fecha"]]
            if sin_fecha:                      # sin hora no se puede separar la ráfaga
                n += 1
                for i in sin_fecha:
                    self.evento[i] = n
        for i, ev in enumerate(self.evento):
            self.eventos[ev].append(i)

    def _rechazos(self):
        return sqlite3.connect(self.rechazos_db, timeout=30)

    def refrescar_etiquetas(self) -> int:
        """Relee las anotaciones de Label Studio: son la verdad, no una copia."""
        try:
            nuevas = buscar.identificaciones_de_label_studio(self.a.proyecto, self.filas)
        except Exception as e:                                   # noqa: BLE001
            print(f"no se pudieron leer las identificaciones: {e}", file=sys.stderr)
            return len(self.etiquetas)
        with self.candado:
            self.identificacion = nuevas
            self.etiquetas = {d: i["cientifico"] for d, i in nuevas.items()}
        return len(nuevas)

    def _refrescar_cada(self):
        while True:
            time.sleep(REFRESCO)
            self.refrescar_etiquetas()

    def rechazos_de(self, cientifico: str) -> set[str]:
        with self._rechazos() as con:
            return {r[0] for r in con.execute(
                "SELECT deteccion FROM rechazo WHERE cientifico = ?", (cientifico,))}

    def cargar_modelo(self):
        with self.candado:
            if self.modelo is None:
                print("cargando el modelo para búsqueda por texto (CPU)…", flush=True)
                self.modelo = embeber.cargar(self.a.modelo, "cpu")
        return self.modelo

    # -------------------------------------------------------------- consulta --
    def taxon_de(self, consulta: str) -> dict | None:
        q = buscar.sin_tildes(consulta)
        for t in self.taxones:
            if any(v and buscar.sin_tildes(v) == q
                   for v in (t["label"], t["scientific_name"], t["name_en"])):
                return t
        return None

    def buscar(self, consulta: str, *, solo_texto=False, estacion=None, anio=None,
               identificadas="no", limite=600) -> dict:
        taxon = self.taxon_de(consulta)
        cientifico = (taxon or {}).get("scientific_name") or (taxon or {}).get("label")
        with self.candado:
            etiquetas = dict(self.etiquetas)
            identificacion = dict(self.identificacion)
        rechazadas = self.rechazos_de(cientifico) if taxon else set()

        def entra(f) -> bool:
            ya = etiquetas.get(f["deteccion"])
            if identificadas == "no":
                if ya or f["deteccion"] in rechazadas:
                    return False
            elif identificadas == "si":
                # la vista de auditoría: lo que ya se identificó con esta especie
                if not ya or (cientifico and ya != cientifico):
                    return False
            return ((not estacion or f["estacion"] == estacion)
                    and (not anio or f["anio"] == anio))

        candidatas = [i for i, f in enumerate(self.filas) if entra(f)]
        if not candidatas:
            return {"modo": "sin candidatas", "ejemplos": 0, "total": 0, "resultados": []}

        idx = np.array(candidatas)
        E = self.vectores[idx]
        ejemplos = [self.por_deteccion[d] for d, c in etiquetas.items()
                    if c == cientifico and d in self.por_deteccion] if taxon else []
        if ejemplos and not solo_texto:
            modo = "ejemplos"
            sims = E @ self.vectores[np.array(ejemplos)].T
            k = min(TOP, len(ejemplos))
            puntaje = np.partition(sims, -k, axis=1)[:, -k:].mean(axis=1)
        else:
            modo = "texto"
            m = self.cargar_modelo()
            frases = buscar.frases(taxon, consulta)
            puntaje = E @ embeber.texto(m, frases)

        orden = np.argsort(-puntaje)[:limite]
        resultados = []
        for pos, o in enumerate(orden, 1):
            f = self.filas[idx[o]]
            resultados.append({"puesto": pos, "similitud": round(float(puntaje[o]), 4),
                               "deteccion": f["deteccion"], "estacion": f["estacion"],
                               "fecha": f["fecha"], "ruta": f["ruta"],
                               "confianza": f["confianza"],
                               "identificacion": identificacion.get(f["deteccion"])})
        return {"modo": modo, "ejemplos": len(ejemplos), "total": len(candidatas),
                "taxon": taxon, "resultados": resultados}

    def _miniatura(self, i: int) -> dict:
        f = self.filas[i]
        with self.candado:
            ident = self.identificacion.get(f["deteccion"])
        return {"deteccion": f["deteccion"], "foto": f["foto"], "fecha": f["fecha"],
                "confianza": f["confianza"], "identificacion": ident}

    def detalle(self, deteccion: str) -> dict | None:
        """Todo lo que un biólogo necesita para decidir sin salir de la grilla."""
        i = self.por_deteccion.get(deteccion)
        if i is None:
            return None
        f = self.filas[i]
        img = self.info.get(f["foto"], {})
        with self.candado:
            ident = self.identificacion.get(deteccion)
        misma_foto = [self._miniatura(j) for j in self.eventos.get(self.evento[i], [])
                      if self.filas[j]["foto"] == f["foto"] and j != i]
        evento = [self._miniatura(j) for j in self.eventos.get(self.evento[i], [])
                  if self.filas[j]["foto"] != f["foto"]][:HERMANAS]
        tarea = self.tareas.get(f["sha256"])
        return {
            "deteccion": deteccion,
            "identificacion": ident,
            "foto": {
                "id": f["foto"], "ruta": f["ruta"], "sha256": f["sha256"],
                "estacion": f["estacion"], "fecha": f["fecha"], "anio": f["anio"],
                "despliegue": img.get("deployment"),
                "camara": " ".join(x for x in (img.get("camera_make"),
                                               img.get("camera_model")) if x) or None,
                "latitud": img.get("latitude"), "longitud": img.get("longitude"),
                "ancho": img.get("width"), "alto": img.get("height"),
            },
            "caja": {"categoria": f["categoria"], "confianza": f["confianza"],
                     "x1": f["x1"], "y1": f["y1"], "x2": f["x2"], "y2": f["y2"]},
            "evento": {"id": self.evento[i], "detecciones": len(self.eventos.get(self.evento[i], [])),
                       "misma_foto": misma_foto, "otras_fotos": evento},
            "label_studio": (f"{PUBLICO}/projects/{self.a.proyecto}/data?task={tarea}"
                             if tarea else None),
        }

    def resumen_especies(self) -> list[dict]:
        with self.candado:
            cuenta = Counter(self.etiquetas.values())
        salida = []
        for t in self.taxones:
            clave = t["scientific_name"] or t["label"]
            salida.append({"valor": t["label"], "cientifico": t["scientific_name"],
                           "ingles": t["name_en"], "confianza": t["confidence"],
                           "etiquetadas": cuenta.get(clave, 0)})
        return sorted(salida, key=lambda e: -e["etiquetadas"])


# ------------------------------------------------------- Label Studio (API) --
def api(ruta: str, token: str, metodo="GET", cuerpo=None, timeout=120):
    url = os.environ.get("LS_URL", "http://nginx:8085") + ruta
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    pedido = urllib.request.Request(url, data=datos, method=metodo)
    pedido.add_header("Content-Type", "application/json")
    pedido.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(pedido, timeout=timeout) as r:
        cuerpo = r.read()
        return json.loads(cuerpo) if cuerpo else {}


def acceso(refresh: str) -> str:
    """El token personal de Label Studio es un refresh; la API quiere el access."""
    url = os.environ.get("LS_URL", "http://nginx:8085") + "/api/token/refresh"
    pedido = urllib.request.Request(url, data=json.dumps({"refresh": refresh}).encode(),
                                    method="POST")
    pedido.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(pedido, timeout=30) as r:
        return json.loads(r.read())["access"]


def ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def region(fila, cientifico: str, ancho: int, alto: int) -> list[dict]:
    """Una caja = dos resultados con el mismo id: el rectángulo y su especie."""
    x1, y1 = float(fila["x1"]), float(fila["y1"])
    x2, y2 = float(fila["x2"]), float(fila["y2"])
    geo = dict(x=100 * x1 / ancho, y=100 * y1 / alto,
               width=100 * (x2 - x1) / ancho, height=100 * (y2 - y1) / alto, rotation=0)
    clase = {"animal": "animal", "person": "human", "vehicle": "vehicle"}.get(
        fila["categoria"], "animal")
    comun = dict(id=secrets.token_hex(5), to_name="image",
                 original_width=ancho, original_height=alto)
    return [dict(comun, from_name="caja", type="rectanglelabels",
                 value={**geo, "rectanglelabels": [clase]}),
            dict(comun, from_name="especie", type="taxonomy",
                 value={**geo, "taxonomy": [[cientifico]]})]


def escribir_en_label_studio(estado: Estado, token: str, deteccion: str,
                             cientifico: str) -> str:
    """Agrega la caja con su especie a la anotación de la foto. Devuelve qué pasó."""
    fila = estado.filas[estado.por_deteccion[deteccion]]
    tarea = estado.tareas.get(fila["sha256"])
    if not tarea:
        return "sin tarea en Label Studio"
    ancho, alto = estado.tamano.get(fila["foto"], (None, None))
    if not ancho:
        with Image.open(RAID / fila["ruta"]) as im:
            ancho, alto = im.size
    nueva = region(fila, cientifico, ancho, alto)
    caja = (float(fila["x1"]), float(fila["y1"]), float(fila["x2"]), float(fila["y2"]))

    t = api(f"/api/tasks/{tarea}", token)
    anotaciones = t.get("annotations") or []
    if not anotaciones:
        api(f"/api/tasks/{tarea}/annotations/", token, "POST",
            {"result": nueva, "was_cancelled": False})
        return "creada"

    an = anotaciones[0]
    for r in an.get("result", []):
        if r.get("type") != "taxonomy":
            continue
        v, w, h = r["value"], r.get("original_width"), r.get("original_height")
        if not (w and h):
            continue
        otra = (v["x"] * w / 100, v["y"] * h / 100,
                (v["x"] + v["width"]) * w / 100, (v["y"] + v["height"]) * h / 100)
        if buscar.iou(caja, otra) >= IOU_MISMA_CAJA:
            return "ya tenía especie asignada"
    api(f"/api/annotations/{an['id']}", token, "PATCH",
        {"result": (an.get("result") or []) + nueva})
    return "agregada"


# ---------------------------------------------------------------- servidor --
class Manejador(BaseHTTPRequestHandler):
    estado: Estado = None                                   # se setea en servir()
    protocol_version = "HTTP/1.1"

    def log_message(self, formato, *args):                  # menos ruido
        if not self.path.startswith(("/recorte/", "/estilos", "/app.js")):
            print(f"{self.address_string()} {formato % args}", flush=True)

    # -------------------------------------------------------------- utilidad --
    def responder(self, codigo: int, cuerpo: bytes, tipo="application/json",
                  cabeceras: dict | None = None):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        for k, v in (cabeceras or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(cuerpo)

    def json(self, codigo: int, datos, cabeceras=None):
        self.responder(codigo, json.dumps(datos, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8", cabeceras)

    def cuerpo(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def sesion(self) -> dict | None:
        galletas = dict(p.strip().split("=", 1) for p in
                        self.headers.get("Cookie", "").split(";") if "=" in p)
        s = self.estado.sesiones.get(galletas.get("sesion", ""))
        if s and s["expira"] > time.time():
            return s
        return None

    def archivo(self, ruta: Path, tipo: str, cache=0):
        if not ruta.is_file():
            return self.json(404, {"error": "no existe"})
        datos = ruta.read_bytes()
        cab = {"Cache-Control": f"max-age={cache}"} if cache else None
        self.responder(200, datos, tipo, cab)

    # ----------------------------------------------------------------- rutas --
    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        e = self.estado

        if url.path in ("/", "/index.html"):
            return self.archivo(WEB / "index.html", "text/html; charset=utf-8")
        if url.path == "/app.js":
            return self.archivo(WEB / "app.js", "text/javascript; charset=utf-8")
        if url.path == "/estilos.css":
            return self.archivo(WEB / "estilos.css", "text/css; charset=utf-8")
        if url.path == "/api/salud":
            return self.json(200, {"ok": True, "cajas": len(e.filas)})

        sesion = self.sesion()
        if url.path.startswith(("/api/", "/recorte/", "/foto/")) and not sesion:
            return self.json(401, {"error": "sesión"})

        if url.path == "/api/estado":
            with e.candado:
                etiquetadas = len(e.etiquetas)
            return self.json(200, {
                "usuario": sesion["usuario"], "proyecto": e.a.proyecto,
                "indice": {"cajas": len(e.filas), "modelo": e.meta["modelo"],
                           "fecha": e.meta["fecha"], "etiquetadas": etiquetadas},
                "especies": e.resumen_especies(),
                "estaciones": sorted({f["estacion"] for f in e.filas if f["estacion"]}),
                "anios": sorted({f["anio"] for f in e.filas if f["anio"]}),
            })
        if url.path == "/api/buscar":
            if not q.get("consulta"):
                return self.json(400, {"error": "falta la consulta"})
            t0 = time.time()
            r = e.buscar(q["consulta"], solo_texto=q.get("texto") == "1",
                         estacion=q.get("estacion") or None, anio=q.get("anio") or None,
                         identificadas=q.get("identificadas", "no"),
                         limite=int(q.get("n", 600)))
            r["ms"] = round((time.time() - t0) * 1000)
            return self.json(200, r)
        if url.path.startswith("/api/deteccion/"):
            d = e.detalle(Path(url.path).name)
            return self.json(200, d) if d else self.json(404, {"error": "esa detección no existe"})
        if url.path.startswith("/recorte/"):
            return self.archivo(Path(e.a.recortes) / Path(url.path).name,
                                "image/jpeg", cache=86400)
        if url.path.startswith("/foto/"):
            return self.foto(Path(url.path).name.removesuffix(".jpg"))
        return self.json(404, {"error": "no existe"})

    def do_POST(self):
        url = urlparse(self.path)
        e = self.estado
        datos = self.cuerpo()

        if url.path == "/api/sesion":
            ficha = (datos.get("token") or "").strip()
            try:
                token = acceso(ficha)
                quien = api("/api/current-user/whoami", token)
            except urllib.error.HTTPError:
                return self.json(401, {"error": "Label Studio no reconoce ese token"})
            sid = secrets.token_urlsafe(24)
            e.sesiones[sid] = {"refresh": ficha, "usuario": quien.get("email", "?"),
                               "expira": time.time() + CADUCA_SESION}
            return self.json(200, {"usuario": quien.get("email")},
                             {"Set-Cookie": f"sesion={sid}; Path=/; HttpOnly; SameSite=Lax"})

        sesion = self.sesion()
        if not sesion:
            return self.json(401, {"error": "sesión"})
        detecciones = [str(d) for d in datos.get("detecciones", [])]
        especie = datos.get("especie") or ""

        if url.path == "/api/aceptar":
            if not e.tareas:
                return self.json(400, {"error": "falta el mapa de tareas: ejecutar mapa_tareas.py"})
            token = acceso(sesion["refresh"])
            hechas, fallas = [], []

            def una(d):
                try:
                    return d, escribir_en_label_studio(e, token, d, especie), None
                except Exception as ex:                       # noqa: BLE001
                    return d, None, str(ex)

            with ThreadPoolExecutor(max_workers=6) as pool:
                for d, que, error in pool.map(una, detecciones):
                    if error:
                        fallas.append({"deteccion": d, "error": error})
                    else:
                        hechas.append({"deteccion": d, "resultado": que})
                        with e.candado:
                            e.etiquetas[d] = especie
                            e.identificacion[d] = {"cientifico": especie,
                                                   "por": sesion["usuario"],
                                                   "cuando": ahora_iso()}
            return self.json(200, {"aceptadas": hechas, "fallas": fallas})

        if url.path == "/api/rechazar":
            ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with e._rechazos() as con:
                con.executemany(
                    "INSERT OR REPLACE INTO rechazo (deteccion, cientifico, quien, cuando)"
                    " VALUES (?,?,?,?)",
                    [(d, especie, sesion["usuario"], ahora) for d in detecciones])
            return self.json(200, {"rechazadas": len(detecciones)})

        if url.path == "/api/refrescar":
            return self.json(200, {"etiquetadas": e.refrescar_etiquetas()})
        return self.json(404, {"error": "no existe"})

    # ------------------------------------------------------------------ foto --
    def foto(self, deteccion: str):
        """La imagen completa con la detección señalada, para los casos dudosos."""
        e = self.estado
        i = e.por_deteccion.get(deteccion)
        if i is None:
            return self.json(404, {"error": "esa detección no existe"})
        f = e.filas[i]
        try:
            with Image.open(RAID / f["ruta"]) as im:
                im = im.convert("RGB")
                caja = [float(f["x1"]), float(f["y1"]), float(f["x2"]), float(f["y2"])]
                escala = min(1.0, 1400 / max(im.size))
                if escala < 1:
                    im = im.resize((round(im.width * escala), round(im.height * escala)))
                    caja = [c * escala for c in caja]
                ImageDraw.Draw(im).rectangle(caja, outline=(95, 211, 243), width=3)
                from io import BytesIO
                buf = BytesIO()
                im.save(buf, format="JPEG", quality=85)
        except OSError as ex:
            return self.json(404, {"error": f"no se pudo abrir la imagen: {ex}"})
        self.responder(200, buf.getvalue(), "image/jpeg", {"Cache-Control": "max-age=3600"})


def servir(a):
    Manejador.estado = Estado(a)
    servidor = ThreadingHTTPServer((a.host, a.puerto), Manejador)
    esquema = "http"
    cert = Path(a.certificados) / "cert.pem"
    if cert.exists():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, Path(a.certificados) / "cert.key")
        servidor.socket = ctx.wrap_socket(servidor.socket, server_side=True)
        esquema = "https"
    print(f"buscador en {esquema}://{a.host}:{a.puerto}  "
          f"({len(Manejador.estado.filas):,} cajas, "
          f"{len(Manejador.estado.etiquetas):,} etiquetadas)", flush=True)
    servidor.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--puerto", type=int, default=int(os.environ.get("PUERTO", 4200)))
    ap.add_argument("--indice", default="/resultados/embeddings")
    ap.add_argument("--recortes", default="/recortes")
    ap.add_argument("--datos", default="/datos-buscador")
    ap.add_argument("--certificados", default="/certs")
    ap.add_argument("--db", default="/fuentes/etiquetas/labels.db")
    ap.add_argument("--modelo", default="bioclip-2")
    ap.add_argument("--proyecto", type=int, default=7)
    servir(ap.parse_args())
    return 0


if __name__ == "__main__":
    sys.exit(main())
