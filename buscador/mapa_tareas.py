#!/usr/bin/env python3
"""Mapea cada foto (sha256) a su tarea de Label Studio, y lo deja en un CSV.

El buscador necesita saber en qué tarea escribir cuando alguien acepta una caja.
Listar decenas de miles de tareas por la API toma varios minutos, así que se hace una vez y se
guarda; hay que rehacerlo solo cuando entran fotos nuevas al proyecto.

    docker compose run --rm embeddings python buscador/mapa_tareas.py
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
from pathlib import Path


def pedir(ruta: str, token: str | None = None, timeout: int = 900):
    url = os.environ.get("LS_URL", "http://nginx:8085") + ruta
    pedido = urllib.request.Request(url, method="GET")
    if token:
        pedido.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(pedido, timeout=timeout) as r:
        return json.loads(r.read())


def token_de(clave: str) -> str:
    url = os.environ.get("LS_URL", "http://nginx:8085") + "/api/token/refresh"
    datos = json.dumps({"refresh": clave}).encode()
    pedido = urllib.request.Request(url, data=datos, method="POST")
    pedido.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(pedido, timeout=60) as r:
        return json.loads(r.read())["access"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--proyecto", type=int, default=7)
    ap.add_argument("--salida", default="/resultados")
    a = ap.parse_args()

    clave = os.environ.get("LS_API_KEY")
    if not clave:
        sys.exit("falta LS_API_KEY")
    token = token_de(clave)

    filas, pagina, por_pagina = [], 1, 1000
    renovado = time.time()
    while True:
        if time.time() - renovado > 120:      # el access token de Label Studio dura poco
            token, renovado = token_de(clave), time.time()
        r = pedir(f"/api/tasks?project={a.proyecto}&page={pagina}"
                  f"&page_size={por_pagina}&fields=task_only", token)
        tareas = r["tasks"] if isinstance(r, dict) else r
        if not tareas:
            break
        for t in tareas:
            sha = (t.get("data") or {}).get("sha256")
            if sha:
                filas.append((sha, t["id"], t.get("total_annotations", 0)))
        print(f"  {len(filas):,} tareas…", flush=True)
        if len(tareas) < por_pagina:
            break
        pagina += 1

    destino = Path(a.salida) / f"tareas-p{a.proyecto}.csv"
    with open(destino, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sha256", "tarea", "anotaciones"])
        w.writerows(filas)
    print(f"\n{len(filas):,} tareas -> {destino}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
