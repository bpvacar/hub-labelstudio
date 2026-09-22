# Imagen de las herramientas del HUB: CLI `hub`, migraciones,
# servicio de historial y generación del certificado. Todo lo que no es
# Label Studio en sí corre aquí, no en el servidor.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl \
 && rm -rf /var/lib/apt/lists/*

# requirements.txt es el lock completo (pip freeze) de label-studio-sdk==2.1.1,
# pillow==12.3.0 y psycopg[binary]==3.3.5. Se regenera a propósito, no solo.
COPY imagen/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.txt && pip check

# Mismo uid que Label Studio y nginx dentro de sus contenedores.
RUN useradd --uid 1001 --gid 0 --no-create-home --home-dir /app hub

WORKDIR /app
COPY tools/ tools/
COPY historial/ historial/
COPY ops/ ops/
COPY configs/ configs/
COPY vocabularios/ vocabularios/

# Un comando por script de tools/: hub.py → hub, migrar_labeler.py → migrar-labeler.
RUN for f in /app/tools/*.py; do \
      n=$(basename "$f" .py | tr _ -); \
      printf '#!/bin/sh\nexec python %s "$@"\n' "$f" > "/usr/local/bin/$n"; \
      chmod +x "/usr/local/bin/$n"; \
    done \
 && chmod +x ops/certificado.sh

ENV PYTHONUNBUFFERED=1
USER 1001
