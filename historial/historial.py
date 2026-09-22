#!/usr/bin/env python3
"""Servicio de historial de identificaciones.

Dos caminos que escriben en historial.evento (append-only):

1. Webhook. Label Studio llama a POST /webhook en cada anotación creada,
   actualizada o borrada. Es inmediato, pero Label Studio lo manda síncrono con
   timeout de 1 s y, si falla, solo lo deja en su log: un webhook perdido es un
   hueco silencioso.
2. Conciliación. Cada RECONCILIAR_CADA segundos compara public.task_completion
   contra la última versión registrada de cada anotación e inserta lo que falte.
   La primera pasada sobre un historial vacío es la instantánea inicial.

Lo que no puede reconstruir la conciliación son las versiones intermedias entre
dos pasadas si también se perdió su webhook. Por eso van los dos.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg.types.json import Jsonb

DSN = os.environ["HISTORIAL_DSN"]
TOKEN = os.environ["HISTORIAL_TOKEN"]
CADA = int(os.environ.get("RECONCILIAR_CADA", "300"))

log = logging.getLogger("historial")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def especies(resultado) -> dict[str, str]:
    """region_id → especie, de los resultados de tipo taxonomy."""
    out = {}
    for r in resultado or []:
        if r.get("type") == "taxonomy":
            ruta = (r.get("value") or {}).get("taxonomy") or [[]]
            out[r.get("id")] = ruta[0][-1] if ruta and ruta[0] else None
    return out


def cambios(antes, despues) -> list[dict]:
    a, d = especies(antes), especies(despues)
    return [{"region": k, "antes": a.get(k), "despues": d.get(k)}
            for k in sorted(set(a) | set(d), key=str) if a.get(k) != d.get(k)]


def registrar(cur, *, origen, accion, anotacion_id, tarea_id=None, proyecto_id=None,
              completada_por_id=None, actualizada_por_id=None, actualizada_en=None,
              resultado=None):
    cur.execute("SELECT resultado FROM historial.ultimo WHERE anotacion_id = %s", (anotacion_id,))
    fila = cur.fetchone()
    anterior = fila[0] if fila else None
    cur.execute(
        """INSERT INTO historial.evento
           (origen, accion, anotacion_id, tarea_id, proyecto_id, completada_por_id,
            actualizada_por_id, anotacion_actualizada_en, resultado, resultado_anterior,
            cambios_especie)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (origen, accion, anotacion_id, tarea_id, proyecto_id, completada_por_id,
         actualizada_por_id, actualizada_en,
         Jsonb(resultado) if resultado is not None else None,
         Jsonb(anterior) if anterior is not None else None,
         Jsonb(cambios(anterior, resultado))))


def _id(v):
    return v.get("id") if isinstance(v, dict) else v


def procesar_webhook(payload: dict) -> int:
    accion = payload.get("action")
    n = 0
    with psycopg.connect(DSN) as con, con.cursor() as cur:
        if accion in ("ANNOTATION_CREATED", "ANNOTATION_UPDATED", "ANNOTATIONS_CREATED"):
            anns = payload.get("annotation")
            for a in anns if isinstance(anns, list) else [anns]:
                if not a:
                    continue
                # Si la conciliación ya registró esta misma versión, no se duplica.
                cur.execute("""SELECT 1 FROM historial.ultimo WHERE anotacion_id = %s
                               AND anotacion_actualizada_en = %s""", (a["id"], a.get("updated_at")))
                if cur.fetchone():
                    continue
                registrar(cur, origen="webhook",
                          accion="actualizada" if accion == "ANNOTATION_UPDATED" else "creada",
                          anotacion_id=a["id"], tarea_id=_id(a.get("task")),
                          proyecto_id=_id(a.get("project")),
                          completada_por_id=_id(a.get("completed_by")),
                          actualizada_por_id=_id(a.get("updated_by")) or _id(a.get("completed_by")),
                          actualizada_en=a.get("updated_at"), resultado=a.get("result") or [])
                n += 1
        elif accion == "ANNOTATIONS_DELETED":
            for a in payload.get("annotations") or []:
                registrar(cur, origen="webhook", accion="borrada", anotacion_id=_id(a))
                n += 1
    return n


def reconciliar() -> int:
    with psycopg.connect(DSN) as con, con.cursor() as cur:
        cur.execute("SELECT NOT EXISTS (SELECT 1 FROM historial.evento)")
        vacio = cur.fetchone()[0]
        origen = "instantanea_inicial" if vacio else "reconciliacion"
        cur.execute("""
            SELECT a.id, a.task_id, a.project_id, a.completed_by_id,
                   coalesce(a.updated_by_id, a.completed_by_id), a.updated_at, a.result
            FROM public.task_completion a
            LEFT JOIN historial.ultimo u ON u.anotacion_id = a.id
            WHERE u.anotacion_id IS NULL
               OR u.accion = 'borrada'
               OR u.anotacion_actualizada_en IS DISTINCT FROM a.updated_at""")
        filas = cur.fetchall()
        for (aid, tid, pid, cby, uby, upd, res) in filas:
            registrar(cur, origen=origen, accion="creada" if vacio else "actualizada",
                      anotacion_id=aid, tarea_id=tid, proyecto_id=pid, completada_por_id=cby,
                      actualizada_por_id=uby, actualizada_en=upd, resultado=res or [])
        cur.execute("""
            SELECT u.anotacion_id FROM historial.ultimo u
            WHERE u.accion <> 'borrada'
              AND NOT EXISTS (SELECT 1 FROM public.task_completion a WHERE a.id = u.anotacion_id)""")
        borradas = [r[0] for r in cur.fetchall()]
        for aid in borradas:
            registrar(cur, origen="reconciliacion", accion="borrada", anotacion_id=aid)
    if filas or borradas:
        log.info("%s: %d versiones, %d borradas", origen, len(filas), len(borradas))
    return len(filas) + len(borradas)


def bucle_conciliacion():
    while True:
        try:
            reconciliar()
        except Exception:
            log.exception("falló la conciliación")
        time.sleep(CADA)


ESPECIES_USADAS = """
SELECT DISTINCT x
FROM (
    SELECT r FROM public.task_completion a, jsonb_array_elements(a.result) r
    WHERE a.project_id = %(p)s
    UNION ALL
    SELECT r FROM public.prediction p, jsonb_array_elements(p.result::jsonb) r
    WHERE p.project_id = %(p)s
) s,
LATERAL jsonb_array_elements(s.r->'value'->'taxonomy') camino,
LATERAL jsonb_array_elements_text(camino) x
WHERE s.r->>'type' = 'taxonomy'
ORDER BY x"""


class Handler(BaseHTTPRequestHandler):
    def _responder(self, codigo, cuerpo):
        datos = json.dumps(cuerpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        ruta, _, query = self.path.partition("?")
        if ruta == "/especies-usadas":
            # Para `hub`: con alias, Label Studio no protege las especies en uso,
            # y exportar un proyecto de 96 000 tareas para averiguarlo tarda minutos.
            if not hmac.compare_digest(self.headers.get("X-Historial-Token", ""), TOKEN):
                return self._responder(403, {"error": "token"})
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            try:
                with psycopg.connect(DSN) as con:
                    filas = con.execute(ESPECIES_USADAS, {"p": int(params["proyecto"])}).fetchall()
                return self._responder(200, {"especies": [f[0] for f in filas]})
            except (KeyError, ValueError):
                return self._responder(400, {"error": "falta ?proyecto=<id>"})
        if ruta != "/salud":
            return self._responder(404, {"error": "no existe"})
        try:
            with psycopg.connect(DSN, connect_timeout=3) as con:
                n = con.execute("SELECT count(*) FROM historial.evento").fetchone()[0]
            self._responder(200, {"ok": True, "eventos": n})
        except Exception as exc:
            self._responder(503, {"ok": False, "error": str(exc)})

    def do_POST(self):
        if self.path != "/webhook":
            return self._responder(404, {"error": "no existe"})
        if not hmac.compare_digest(self.headers.get("X-Historial-Token", ""), TOKEN):
            return self._responder(403, {"error": "token"})
        try:
            largo = int(self.headers.get("Content-Length", 0))
            n = procesar_webhook(json.loads(self.rfile.read(largo)))
            self._responder(200, {"registrados": n})
        except Exception as exc:
            log.exception("webhook no registrado; lo recoge la conciliación")
            self._responder(500, {"error": str(exc)})

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


if __name__ == "__main__":
    threading.Thread(target=bucle_conciliacion, daemon=True).start()
    log.info("historial escuchando en :8000, conciliación cada %ss", CADA)
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
