#!/bin/sh
# Respaldo nocturno, dentro de su propio contenedor (postgres:16-alpine).
# pg_dump de la base entera (proyectos, anotaciones, usuarios y el esquema
# historial) + el media de Label Studio. Los datos fuente no: el RAID tiene su
# respaldo y datos/ se sube con rsync desde otro lado.
#
#   RESPALDO_HORA=0330  RESPALDO_DIAS=14  RESPALDO_AHORA=1 (corre una vez al arrancar)
set -eu

hora="${RESPALDO_HORA:-0330}"
dias="${RESPALDO_DIAS:-14}"
dest=/backups

respaldar() {
  stamp="$(date +%Y%m%d-%H%M%S)"
  pg_dump -h db -U labelstudio -d labelstudio -Fc -f "$dest/db-$stamp.dump.part"
  mv "$dest/db-$stamp.dump.part" "$dest/db-$stamp.dump"
  tar -C /lsdata -czf "$dest/media-$stamp.tar.gz.part" media
  mv "$dest/media-$stamp.tar.gz.part" "$dest/media-$stamp.tar.gz"
  find "$dest" -name 'db-*.dump' -mtime +"$dias" -delete
  find "$dest" -name 'media-*.tar.gz' -mtime +"$dias" -delete
  find "$dest" -name '*.part' -delete
  date +%s > "$dest/.ultimo"
  echo "$(date -Iseconds) respaldo ok: db-$stamp.dump"
}

[ "${RESPALDO_AHORA:-0}" = 1 ] && respaldar

hecho=""
while true; do
  hoy="$(date +%Y%m%d)"
  if [ "$(date +%H%M)" = "$hora" ] && [ "$hecho" != "$hoy" ]; then
    respaldar || echo "$(date -Iseconds) RESPALDO FALLÓ" >&2
    hecho="$hoy"
  fi
  sleep 30
done
