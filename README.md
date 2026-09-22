# HUB · Label Studio

Plataforma de etiquetado de datos de biodiversidad sobre Label Studio 1.23.0, con un
**buscador por especie** para camera traps. Todo corre en contenedores: el servidor
solo aporta Docker, el almacenamiento de los datos y un archivo `.env`.

- **Cinco modos de etiquetado** (audio, camera traps, video, imagen, texto) con una
  plantilla y un vocabulario por modo.
- **Nombre científico como clave**: quien etiqueta ve el nombre común y la anotación
  guarda el científico, igual en todas las modalidades.
- **Historial de identificaciones** append-only: quién identificó qué, cuándo y qué
  cambió, algo que Label Studio Community no conserva.
- **Buscador por especie**: una grilla de recortes ordenados por parecido
  (INQUIRE-Search + BioCLIP 2) para confirmar detecciones en lote, sin reentrenar
  ningún modelo.

## Puesta en marcha

```bash
cp .env.example .env            # completar contraseñas, rutas e IP del servidor
docker compose run --rm certificado
docker compose up -d
```

Label Studio queda en `https://<servidor>:8081` con la cuenta de `LS_ADMIN_EMAIL`. Los
tokens de API (`LS_API_KEY`, `LS_API_KEY_MATLAB`) se emiten con cuentas de servicio,
no con la del administrador:

```bash
docker compose exec -T -e CUENTA=scripts@hub.local -e NOMBRE="Scripts" \
  app python label_studio/manage.py shell < ops/cuenta_servicio.py
```

Variables de `.env`: ver `.env.example`. Las obligatorias hacen fallar `docker compose`
con `falta en .env` si no están definidas.

## Un proyecto = un modo + una carpeta

| Modo | Qué se etiqueta | Datos |
|---|---|---|
| `audio` | Segmentos en el espectrograma: clase (Ave, Anuro, …) + especie por segmento, certeza, condiciones del clip | wav, flac, mp3, ogg |
| `camtrap` | Caja `animal`/`human`/`vehicle` + especie por caja, individuos por foto | jpg, png |
| `video` | Cajas con seguimiento + especie, comportamiento en la línea de tiempo | mp4, webm |
| `imagen` | Especie de la imagen entera | jpg, png, webp |
| `texto` | Entidades (especie, lugar, fecha, conteo) + si el documento es registro de presencia | txt |

Todo se administra con `hub`, que corre dentro del contenedor `tools`. El script `hub`
de la raíz es un wrapper de una línea sobre `docker compose run --rm tools hub …`:

```bash
hub modos
hub lista

# crear el proyecto: config + storage + webhook del historial + sync
hub nuevo audio "Grabaciones 2025" --datos datos/audio/grabaciones --especies aves.csv

hub sync 8                                    # si llegan más archivos a la carpeta
hub birdnet 8 detections.csv --min-conf 0.25  # detecciones de BirdNET como pre-anotación

# vocabulario
hub especies 8 mas-especies.csv
hub plantilla 8 audio                         # tras cambiar configs/audio.xml
hub plantilla 7 camtrap --especies camtrap-especies.csv
hub cientificos 7 --seco                      # anotaciones antiguas → nombre científico
hub historial                                 # reparar los webhooks del historial
```

`--datos` es `datos/...` (la carpeta `LS_DATOS`) o `dgxraid/...` (el RAID), ambos de
solo lectura. Los archivos **no se suben a Label Studio**: los lee de donde están. Los
CSV que recibe `hub` van en `entrada/`.

`hub birdnet` espera las columnas `archivo, start_s, end_s, scientific_name,
confidence`.

## Nombre científico: se ve el nombre común, se guarda el científico

Un vocabulario es un CSV `valor,cientifico,rango,confianza,ayuda`. `valor` es lo que ve
quien etiqueta; `cientifico` es lo que guarda la anotación (`alias` del `Taxonomy`). Así
camera traps y audio comparten la misma clave. `falso positivo` no tiene nombre
científico: no es un taxón.

**Con alias, Label Studio deja de proteger las especies en uso** (valida contra
`value`, mientras la anotación guarda el alias). Todo cambio de configuración hecho con
`hub` pasa por `actualizar_config`, que se niega a quitar una especie en uso. No se
recomienda editar la configuración de un proyecto a mano desde la interfaz.

`vocabularios/camtrap-especies.csv` conserva los nombres exactos del etiquetador
anterior. Los mapeos de confianza baja llevan "REVISAR" en la ayuda.

## Migración desde el Ground Truth Labeler de MATLAB

`tools/migrar_labeler.py` lleva a un proyecto `camtrap` una base SQLite (`labels.db`)
exportada del Ground Truth Labeler de MATLAB:

- cada foto es una tarea;
- las cajas de MegaDetector entran como *predictions*;
- las cajas con especie humana entran como anotación de la cuenta de servicio de la
  migración, para que la procedencia sea visible en la interfaz.

Las cajas que nadie revisó quedan como *predictions*, no como negativos.
`tools/importar_anotaciones.py` agrega después lo que una migración no haya traído: no
crea tareas y no modifica fotos que ya tienen anotación.

```bash
docker compose run --rm tools migrar-labeler --limite 200 --titulo "prueba"
docker compose run --rm tools importar-anotaciones export.json --seco
```

## Historial de identificaciones

Label Studio Community guarda solo la última versión de cada anotación. El servicio
`historial` escribe cada versión en `historial.evento`, append-only (un trigger bloquea
UPDATE/DELETE/TRUNCATE incluso al superusuario): quién, cuándo, versión, versión anterior
y `cambios_especie`. Dos vías: un webhook por proyecto (inmediato; Label Studio lo envía
con un timeout de 1 s y no reintenta) y una conciliación cada 5 min contra
`task_completion`.

Límites: importar anotaciones por API no dispara el webhook, y dos ediciones dentro de
la misma ventana de 5 min con el webhook caído dejan registrada solo la última.

```sql
-- quién cambió qué especie
SELECT registrado_en, actualizada_por_id, anotacion_id, cambios_especie
FROM historial.evento WHERE cambios_especie <> '[]' ORDER BY id DESC LIMIT 20;
-- una fila por región con especie, por versión
SELECT * FROM historial.identificacion WHERE tarea_id = 10739;
```

`docker compose exec db psql -U labelstudio -d labelstudio` abre la consola.

## Buscador por especie

INQUIRE-Search aplicado al etiquetado: un embedding por caja de MegaDetector, una lista
de especies y una grilla de recortes ordenados por parecido. La consulta combina el
nombre científico con las cajas ya etiquetadas de esa especie, sin reentrenar nada, y lo
confirmado se escribe en Label Studio por la API, a nombre de quien lo confirmó.

### Elección del modelo

```bash
mkdir -p modelos resultados        # si no existen, Docker los crea como root
docker compose --profile gpu build embeddings
docker compose run --rm embeddings python buscador/comparar_modelos.py
```

El resultado queda en `resultados/comparacion-modelos/` (`reporte.md`,
`resultados.json`, una hoja de contacto para revisar los recortes). El script solo lee:
no escribe en Label Studio ni en `labels.db`.

Se compararon BioCLIP 2 y SigLIP SO400M, el modelo de INQUIRE-Search, sobre 2 970 cajas
de 31 especies de una estación biológica amazónica, con 10 repeticiones. Métrica: mAP (%)
al ordenar todas las candidatas por parecido a cada especie; un orden al azar da 3,2.

| Condición | BioCLIP 2 | SigLIP SO400M |
|---|---|---|
| Solo el nombre (sin ejemplos) | **53,4** | 42,1 |
| 5 ejemplos etiquetados | **57,5** | 47,4 |
| Especie propuesta por 5-NN (exactitud) | **62,9** | 57,2 |
| Todos los ejemplos, en una estación sin etiquetas | **55,0** | 45,4 |

BioCLIP 2 es además 3,4 veces más rápido (497 frente a 148 recortes/s en una A100) y usa
2,8 GB de GPU frente a 6,2. Los errores de ambos modelos se concentran en pares de
especies parecidas.

Para que el resultado no salga inflado, un *evento* agrupa las fotos de la misma
estación separadas por menos de 30 min, y ejemplos y candidatas nunca comparten evento:
de lo contrario, un recorte encontraría a su gemelo de la misma ráfaga. La prueba por
estación, más exigente, deja fuera estaciones completas.

### El índice de recortes

```bash
docker compose run --rm embeddings python buscador/calcular_embeddings.py
```

Recorta cada caja (un cuadrado de 448 px con 15 % de margen) y calcula su embedding. Se
puede reanudar: los recortes existentes no se rehacen.

| Salida | Contenido |
|---|---|
| `recortes/<id_deteccion>.jpg` | Un recorte por caja; los sirve la grilla sin reabrir la foto original |
| `resultados/embeddings/<modelo>/` | `vectores.npy` (float32, normalizados), `indice.csv` y `meta.json` |

Las cajas de categoría `person` no entran por defecto; `--categorias animal person
vehicle` las incluye.

### La grilla

```bash
docker compose --profile buscador up -d buscador     # https://<servidor>:4200
```

Se ingresa con el **token personal de Label Studio** de cada persona (*Account &
Settings* → *Personal Access Token*). No hay cuentas aparte: lo confirmado queda en
Label Studio a nombre de quien lo confirmó.

| Elemento | Función |
|---|---|
| Lista de especies | Cada especie con cuántas detecciones tiene identificadas. Se filtra escribiendo |
| Grilla | Las detecciones ordenadas por parecido, con su posición y su similitud |
| Qué se muestra | **Sin identificar**, **ya identificadas** (para auditar) o **todas**. Las identificadas llevan el nombre de la especie y un borde distinto |
| Línea de corte | `L` la coloca en el cursor: confirma de una vez todo lo que quedó por encima |
| Panel | Clic derecho, `Z` o doble clic: imagen completa con la caja señalada y los datos de la foto |
| Texto libre | Búsqueda como en INQUIRE ("animal con cría"). Carga el modelo en CPU |

El panel muestra lo necesario para decidir sin abrir otra herramienta: quién identificó
la detección y cuándo, estación, fecha y hora, despliegue, cámara, coordenadas,
categoría y confianza de MegaDetector, las otras detecciones de la misma foto, el resto
del evento (misma estación, ±30 min) y un enlace a la tarea en Label Studio.

Atajos: flechas para moverse, `espacio` selecciona, `A` confirma, `R` descarta, `L` línea
de corte, `Enter` confirma todo lo que quedó por encima, `Z` o clic derecho abre el
panel, `Esc` lo cierra. `shift` + clic selecciona un rango.

Al confirmar, la caja se agrega a la anotación de la foto (o se crea), con el nombre
científico, y el historial la registra. **Descartar no escribe en Label Studio**: se
guarda en `datos-buscador/rechazos.db` solo para que la caja no vuelva a aparecer para
esa especie.

El servicio **no usa GPU**: los embeddings ya están calculados y el modelo solo se
carga, en CPU, para las búsquedas por texto libre. Necesita el mapa de foto a tarea que
genera `buscador/mapa_tareas.py`, que debe regenerarse cuando entran fotos nuevas. Usa su
propio certificado en `certs-buscador/`:

```bash
CERT_DIR=/certs-buscador CERT_UID=$HOST_UID docker compose run --rm certificado
```

### Búsqueda desde la línea de comandos

```bash
docker compose run --rm embeddings python buscador/buscar.py "Panthera onca"
docker compose run --rm embeddings python buscador/buscar.py "Tapirus terrestris" --estacion E01
docker compose run --rm embeddings python buscador/buscar.py "animal con cría" --solo-texto
```

Deja en `resultados/busquedas/<consulta>/` una hoja de contacto numerada y un `top.csv`
con foto, estación, fecha y similitud. Busca solo entre las cajas **sin etiquetar**; las
etiquetadas sirven de ejemplo. Las etiquetas se leen de Label Studio (se exportan las
tareas anotadas y cada región se empareja con su caja de MegaDetector por IoU ≥ 0,5);
`--refrescar` las vuelve a pedir.

Con ejemplos, el puntaje es la media de los 3 ejemplos más parecidos; sin ejemplos (o con
`--solo-texto`), la similitud con el nombre científico. En especies con pocas etiquetas
la diferencia es clara: con apenas 7 ejemplos, la primera página mejora de forma notable
frente a la búsqueda solo por texto.

## Servicios

| Servicio | Imagen | Función |
|---|---|---|
| `db` | `postgres:16-alpine` | Base de Label Studio + esquema `historial` |
| `app` · `nginx` | `heartexlabs/label-studio:1.23.0` | uWSGI (8 procesos) detrás de nginx. 8081 HTTPS público, 127.0.0.1:18085 HTTP local |
| `historial-init` | `postgres:16-alpine` | Esquema, rol y permisos (`historial/init.sql`); corre y termina en cada `up` |
| `historial` | `labelstudio-hub-tools:local` | Webhook + conciliación + `/especies-usadas` |
| `respaldo` | `postgres:16-alpine` | `pg_dump` + media a las 03:30, 14 días; healthcheck si el último tiene más de 26 h |
| `tools` | `labelstudio-hub-tools:local` | `hub` y las migraciones. Perfil `tools`, no queda corriendo |
| `certificado` | `labelstudio-hub-tools:local` | Certificado autofirmado. Perfil `tools` |
| `embeddings` | `labelstudio-hub-gpu:local` | Embeddings de recortes (BioCLIP 2, SigLIP) en **una** GPU. Perfil `gpu`, a demanda |
| `buscador` | `labelstudio-hub-gpu:local` | La grilla del buscador por especie, en el puerto 4200 y **sin GPU**. Perfil `buscador` |

### Imágenes

Ambas imágenes se construyen desde un lock completo (`pip freeze`) y ejecutan `pip
check`: `imagen/requirements.txt` para `tools` e `imagen/requirements-gpu.txt` para la
imagen GPU (torch 2.11 cu128, open_clip 3.3). La receta para regenerar cada lock está en
su Dockerfile.

- **Una sola GPU, identificada por UUID** (`GPU_EMBEDDINGS`; `nvidia-smi -L`). Se usa el
  UUID y no el índice porque el orden de CUDA puede no coincidir con el de `nvidia-smi`.
- **La imagen GPU es independiente de `tools`**, porque el wrapper `hub` reconstruye
  `tools` en cada llamada y torch con CUDA pesa varios GB.
- **Los contenedores corren con el uid del servidor** (`HOST_UID`) para que
  `resultados/` y `modelos/` le pertenezcan. Ese uid no existe en el `/etc/passwd` de la
  imagen y torch falla en `getpass.getuser()` al importarse; por eso la imagen define
  `USER=hub` y `TORCHINDUCTOR_CACHE_DIR`.
- **Los pesos viven en `modelos/`**, fuera de la imagen. `modelos/` y `resultados/`
  están en `.dockerignore` para no enviar gigabytes de contexto en cada `build`.

## Decisiones de diseño

- **Solo HTTPS.** El firewall de la universidad bloquea el audio servido por HTTP plano
  (503 *Application Blocked* aunque nginx responda 200), y sin TLS la contraseña viaja en
  claro. El certificado es autofirmado; a largo plazo corresponde un nombre DNS con
  certificado emitido por la institución.
- **`LABEL_STUDIO_HOST` vacío.** Con un host definido, el sync guarda la URL absoluta en
  cada tarea, y cambiar de IP, esquema o DNS las invalida todas. Vacío, son relativas.
- **IPs fijas en la red interna (`192.168.240.0/24`).** nginx resuelve `app` una sola vez
  al arrancar; si `app` se recrea con otra IP, nginx envía el tráfico a quien herede la
  anterior. Por la misma razón, `respaldo` se ejecuta con `docker compose exec`, no con
  `docker compose run`, que chocaría con su IP.
- **Webhooks por proyecto.** Label Studio 1.23 no acepta webhooks de organización por
  API. La URL usa el alias `historial.labelstudio.internal` porque Django exige un
  dominio.
- **Cuentas de servicio con JWT** (`scripts@hub.local`, `matlab@hub.local`) en lugar de
  la cuenta del administrador: Label Studio registra en `completed_by` la cuenta que
  importó cada anotación, y así la procedencia queda visible.

## Operación

```bash
docker compose ps
docker compose logs -f app

# respaldo manual
docker compose exec respaldo sh -c 'RESPALDO_AHORA=1 timeout 120 sh /respaldo.sh'

# regenerar el certificado (cambia la huella: hay que aceptarlo de nuevo en el navegador)
docker compose run --rm certificado && docker compose restart nginx

# restaurar un respaldo
docker compose exec -T db pg_restore -U labelstudio -d labelstudio --clean < backups/db-….dump

# acceder a la interfaz por HTTP a través de un túnel SSH
ssh -N -L 18085:127.0.0.1:18085 <servidor>     # y abrir http://localhost:18085
```

## Limitaciones

- **Community edition:** todos los usuarios ven todos los proyectos; no hay proyectos
  privados por sitio.
- **Aún no exporta a Camtrap DP ni a Darwin Core.**
- **LiDAR, SAR y multiespectral** no tienen modo: Label Studio no los soporta.
- Los archivos de audio `.DAT` de algunas grabadoras deben decodificarse a WAV antes de
  importarlos.
