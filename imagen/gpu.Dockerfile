# Imagen GPU del buscador por especie: embeddings de recortes de camera trap con
# open_clip (BioCLIP 2, SigLIP). Va aparte de imagen/Dockerfile a propósito:
# torch con CUDA pesa varios GB y `hub` reconstruye la imagen de herramientas en
# cada llamada.
#
# El lock se regenera a propósito, no solo, en un contenedor desechable:
#
#   docker run --rm python:3.12-slim sh -c 'pip install -q --index-url \
#     https://download.pytorch.org/whl/cu128 torch torchvision && pip install -q \
#     open_clip_torch transformers sentencepiece && pip check >&2 && pip freeze'
#
# cu128 funciona con el driver del DGX (580, CUDA 13.0).
FROM python:3.12-slim

COPY imagen/requirements-gpu.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --root-user-action=ignore \
      --extra-index-url https://download.pytorch.org/whl/cu128 \
      -r /tmp/requirements.txt \
 && pip check

WORKDIR /app
COPY buscador/ buscador/

# Los pesos se descargan una sola vez a ./modelos (bind mount), nunca a la imagen.
# El contenedor corre con el uid del servidor, que no existe en /etc/passwd de la
# imagen: sin USER, torch revienta en getpass.getuser() al importar.
ENV PYTHONUNBUFFERED=1 \
    USER=hub \
    HF_HOME=/modelos/huggingface \
    TORCH_HOME=/modelos/torch \
    TORCHINDUCTOR_CACHE_DIR=/modelos/torchinductor \
    XDG_CACHE_HOME=/modelos/cache
