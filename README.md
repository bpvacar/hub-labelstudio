# HUB · Label Studio

A biodiversity data labeling platform built on Label Studio 1.23.0, with a **species
search tool** for camera traps. Everything runs in containers: the server only
provides Docker, the data storage, and a `.env` file.

- **Five labeling modes** (audio, camera traps, video, image, text), each with its own
  template and vocabulary.
- **Scientific name as the key**: annotators see the common name, and the annotation
  stores the scientific name, consistently across all modalities.
- **Append-only identification history**: who identified what, when, and what changed,
  which Label Studio Community does not keep.
- **Species search**: a grid of crops ranked by similarity (INQUIRE-Search + BioCLIP 2)
  to confirm detections in bulk, without retraining any model.

The code, interface, and command-line tools are in Spanish.

## Getting started

```bash
cp .env.example .env            # fill in passwords, paths, and the server IP
docker compose run --rm certificado
docker compose up -d
```

Label Studio is served at `https://<server>:8081` with the `LS_ADMIN_EMAIL` account.
API tokens (`LS_API_KEY`, `LS_API_KEY_MATLAB`) are issued to service accounts, not to
the admin account:

```bash
docker compose exec -T -e CUENTA=scripts@hub.local -e NOMBRE="Scripts" \
  app python label_studio/manage.py shell < ops/cuenta_servicio.py
```

See `.env.example` for all variables. Required variables make `docker compose` fail
with `falta en .env` ("missing in .env") when they are not set.

## One project = one mode + one folder

| Mode | What is labeled | Data |
|---|---|---|
| `audio` | Spectrogram segments: class (bird, frog, …) + species per segment, certainty, clip conditions | wav, flac, mp3, ogg |
| `camtrap` | `animal`/`human`/`vehicle` box + species per box, individuals per photo | jpg, png |
| `video` | Tracked boxes + species, behavior on the timeline | mp4, webm |
| `imagen` | Species for the whole image | jpg, png, webp |
| `texto` | Entities (species, place, date, count) + whether the document is a presence record | txt |

Projects are managed with `hub`, which runs inside the `tools` container. The `hub`
script at the repository root is a one-line wrapper around
`docker compose run --rm tools hub …`:

```bash
hub modos
hub lista

# create a project: config + storage + history webhook + sync
hub nuevo audio "Recordings 2025" --datos datos/audio/recordings --especies aves.csv

hub sync 8                                    # when new files arrive in the folder
hub birdnet 8 detections.csv --min-conf 0.25  # BirdNET detections as pre-annotations

# vocabulary
hub especies 8 more-species.csv
hub plantilla 8 audio                         # after editing configs/audio.xml
hub plantilla 7 camtrap --especies camtrap-especies.csv
hub cientificos 7 --seco                      # old annotations → scientific names
hub historial                                 # repair the history webhooks
```

`--datos` takes `datos/...` (the `LS_DATOS` folder) or `dgxraid/...` (the RAID), both
read-only. Files are **not uploaded to Label Studio**: it reads them where they are. CSV
files passed to `hub` go in `entrada/`.

`hub birdnet` expects the columns `archivo, start_s, end_s, scientific_name,
confidence`.

## Scientific names: show the common name, store the scientific one

A vocabulary is a CSV with columns `valor,cientifico,rango,confianza,ayuda`. `valor` is
what the annotator sees; `cientifico` is what the annotation stores (the `alias` of the
`Taxonomy` tag). Camera traps and audio therefore share the same key. `falso positivo`
(false positive) has no scientific name: it is not a taxon.

**With aliases, Label Studio no longer protects species that are in use** (it validates
against `value`, while the annotation stores the alias). Every config change made with
`hub` goes through `actualizar_config`, which refuses to remove a species in use.
Editing a project's config by hand in the web interface is not recommended.

`vocabularios/camtrap-especies.csv` keeps the exact label strings from the previous
labeling tool. Low-confidence mappings say "REVISAR" (review) in the help text.

## Migrating from the MATLAB Ground Truth Labeler

`tools/migrar_labeler.py` loads a SQLite database (`labels.db`) exported from the MATLAB
Ground Truth Labeler into a `camtrap` project:

- each photo becomes a task;
- MegaDetector boxes become *predictions*;
- boxes with a human-assigned species become annotations by the migration service
  account, so their origin is visible in the interface.

Boxes that nobody reviewed stay as *predictions*, not as negatives.
`tools/importar_anotaciones.py` later adds whatever a migration missed: it does not
create tasks and does not modify photos that already have an annotation.

```bash
docker compose run --rm tools migrar-labeler --limite 200 --titulo "test"
docker compose run --rm tools importar-anotaciones export.json --seco
```

## Identification history

Label Studio Community keeps only the latest version of each annotation. The `historial`
service writes every version to `historial.evento`, append-only (a trigger blocks
UPDATE/DELETE/TRUNCATE, even for the superuser): who, when, version, previous version,
and `cambios_especie` (species changes). There are two paths: a per-project webhook
(immediate; Label Studio sends it with a 1 s timeout and does not retry) and a
reconciliation every 5 minutes against `task_completion`.

Limits: importing annotations through the API does not trigger the webhook, and two
edits within the same 5-minute window while the webhook is down leave only the last one
recorded.

```sql
-- who changed which species
SELECT registrado_en, actualizada_por_id, anotacion_id, cambios_especie
FROM historial.evento WHERE cambios_especie <> '[]' ORDER BY id DESC LIMIT 20;
-- one row per region with a species, per version
SELECT * FROM historial.identificacion WHERE tarea_id = 10739;
```

`docker compose exec db psql -U labelstudio -d labelstudio` opens a database shell.

## Species search

INQUIRE-Search applied to labeling: one embedding per MegaDetector box, a species list,
and a grid of crops ranked by similarity. The query combines the scientific name with
the boxes already labeled as that species, without retraining anything, and whatever is
confirmed is written to Label Studio through the API, under the name of the person who
confirmed it.

### Model selection

```bash
mkdir -p modelos resultados        # if missing, Docker creates them as root
docker compose --profile gpu build embeddings
docker compose run --rm embeddings python buscador/comparar_modelos.py
```

Results go to `resultados/comparacion-modelos/` (`reporte.md`, `resultados.json`, and a
contact sheet to inspect the crops). The script is read-only: it writes neither to Label
Studio nor to `labels.db`.

BioCLIP 2 was compared against SigLIP SO400M, the model used by INQUIRE-Search, on 2,970
boxes of 31 species from an Amazonian biological station, over 10 repetitions. Metric:
mAP (%) when ranking all candidates by similarity to each species; a random ranking
scores 3.2.

| Condition | BioCLIP 2 | SigLIP SO400M |
|---|---|---|
| Name only (no examples) | **53.4** | 42.1 |
| 5 labeled examples | **57.5** | 47.4 |
| Species proposed by 5-NN (accuracy) | **62.9** | 57.2 |
| All examples, on a station with no labels | **55.0** | 45.4 |

BioCLIP 2 is also 3.4 times faster (497 vs. 148 crops/s on an A100) and uses 2.8 GB of
GPU memory vs. 6.2 GB. Both models make most of their errors between pairs of similar
species.

To keep the results from being inflated, an *event* groups photos from the same station
taken less than 30 minutes apart, and examples and candidates never share an event:
otherwise a crop would find its near-duplicate from the same burst. The harder
per-station split holds out entire stations.

### Crop index

```bash
docker compose run --rm embeddings python buscador/calcular_embeddings.py
```

Crops each box (a 448 px square with a 15 % margin) and computes its embedding. The run
can be resumed: existing crops are not redone.

| Output | Contents |
|---|---|
| `recortes/<detection_id>.jpg` | One crop per box; the grid serves them without reopening the original photo |
| `resultados/embeddings/<model>/` | `vectores.npy` (float32, normalized), `indice.csv`, and `meta.json` |

Boxes in the `person` category are excluded by default; `--categorias animal person
vehicle` includes them.

### The grid

```bash
docker compose --profile buscador up -d buscador     # https://<server>:4200
```

Users sign in with their **personal Label Studio token** (*Account & Settings* →
*Personal Access Token*). There are no separate accounts: what is confirmed is recorded
in Label Studio under the name of the person who confirmed it.

| Element | Function |
|---|---|
| Species list | Each species with its number of identified detections. Filter by typing |
| Grid | Detections ranked by similarity, with their rank and similarity score |
| Show | **Unidentified**, **already identified** (for auditing), or **all**. Identified detections show the species name and a distinct border |
| Cutoff line | `L` places it at the cursor: confirms everything above it at once |
| Panel | Right-click, `Z`, or double-click: the full image with the box highlighted and the photo's metadata |
| Free text | Search as in INQUIRE ("animal carrying young"). Loads the model on CPU |

The panel shows what is needed to decide without opening another tool: who identified
the detection and when, station, date and time, deployment, camera, coordinates,
MegaDetector category and confidence, the other detections in the same photo, the rest of
the event (same station, ±30 min), and a link to the task in Label Studio.

Shortcuts: arrow keys to move, `space` selects, `A` confirms, `R` rejects, `L` sets the
cutoff line, `Enter` confirms everything above it, `Z` or right-click opens the panel,
`Esc` closes it. `shift` + click selects a range.

On confirmation, the box is added to the photo's annotation (or one is created), with
the scientific name, and the history records it. **Rejecting does not write to Label
Studio**: it is stored in `datos-buscador/rechazos.db` only so the box does not show up
again for that species.

The service **does not use a GPU**: embeddings are precomputed, and the model is only
loaded, on CPU, for free-text searches. It needs the photo-to-task map produced by
`buscador/mapa_tareas.py`, which must be regenerated when new photos are added. It uses
its own certificate in `certs-buscador/`:

```bash
CERT_DIR=/certs-buscador CERT_UID=$HOST_UID docker compose run --rm certificado
```

### Command-line search

```bash
docker compose run --rm embeddings python buscador/buscar.py "Panthera onca"
docker compose run --rm embeddings python buscador/buscar.py "Tapirus terrestris" --estacion E01
docker compose run --rm embeddings python buscador/buscar.py "animal carrying young" --solo-texto
```

Writes a numbered contact sheet and a `top.csv` (photo, station, date, similarity) to
`resultados/busquedas/<query>/`. It only searches **unlabeled** boxes; labeled ones serve
as examples. Labels are read from Label Studio (annotated tasks are exported and each
region is matched to its MegaDetector box by IoU ≥ 0.5); `--refrescar` fetches them
again.

With examples, the score is the mean similarity to the 3 most similar examples; without
examples (or with `--solo-texto`), it is the similarity to the scientific name. The
difference is clear for species with few labels: with only 7 examples, the first page
improves markedly over text-only search.

## Services

| Service | Image | Function |
|---|---|---|
| `db` | `postgres:16-alpine` | Label Studio database + `historial` schema |
| `app` · `nginx` | `heartexlabs/label-studio:1.23.0` | uWSGI (8 processes) behind nginx. 8081 public HTTPS, 127.0.0.1:18085 local HTTP |
| `historial-init` | `postgres:16-alpine` | Schema, role, and permissions (`historial/init.sql`); runs and exits on every `up` |
| `historial` | `labelstudio-hub-tools:local` | Webhook + reconciliation + `/especies-usadas` |
| `respaldo` | `postgres:16-alpine` | `pg_dump` + media at 03:30, kept 14 days; healthcheck fails if the last backup is older than 26 h |
| `tools` | `labelstudio-hub-tools:local` | `hub` and the migration tools. `tools` profile, does not stay running |
| `certificado` | `labelstudio-hub-tools:local` | Self-signed certificate. `tools` profile |
| `embeddings` | `labelstudio-hub-gpu:local` | Crop embeddings (BioCLIP 2, SigLIP) on **one** GPU. `gpu` profile, on demand |
| `buscador` | `labelstudio-hub-gpu:local` | The species search grid, on port 4200 and **without a GPU**. `buscador` profile |

### Images

Both images are built from a complete lock file (`pip freeze`) and run `pip check`:
`imagen/requirements.txt` for `tools` and `imagen/requirements-gpu.txt` for the GPU image
(torch 2.11 cu128, open_clip 3.3). The recipe to regenerate each lock file is in its
Dockerfile.

- **A single GPU, selected by UUID** (`GPU_EMBEDDINGS`; see `nvidia-smi -L`). The UUID is
  used instead of the index because CUDA's device order may not match `nvidia-smi`.
- **The GPU image is separate from `tools`**, because the `hub` wrapper rebuilds `tools`
  on every call and torch with CUDA weighs several GB.
- **Containers run with the server's uid** (`HOST_UID`) so that `resultados/` and
  `modelos/` belong to that user. That uid does not exist in the image's `/etc/passwd`,
  and torch fails in `getpass.getuser()` on import; that is why the image sets
  `USER=hub` and `TORCHINDUCTOR_CACHE_DIR`.
- **Model weights live in `modelos/`**, outside the image. `modelos/` and `resultados/`
  are listed in `.dockerignore` so each `build` does not send gigabytes of context.

## Design decisions

- **HTTPS only.** The university firewall blocks audio served over plain HTTP (503
  *Application Blocked* even when nginx returns 200), and without TLS the password
  travels in clear text. The certificate is self-signed; the long-term fix is a DNS name
  with a certificate issued by the institution.
- **Empty `LABEL_STUDIO_HOST`.** With a host set, sync stores the absolute URL in every
  task, and changing the IP, scheme, or DNS name breaks all of them. Left empty, URLs are
  relative.
- **Fixed IPs on the internal network (`192.168.240.0/24`).** nginx resolves `app` only
  once at startup; if `app` is recreated with a different IP, nginx sends traffic to
  whichever container inherits the old one. For the same reason, `respaldo` is run with
  `docker compose exec`, not `docker compose run`, which would collide with its IP.
- **Per-project webhooks.** Label Studio 1.23 does not accept organization-level webhooks
  through the API. The URL uses the alias `historial.labelstudio.internal` because Django
  requires a domain name.
- **Service accounts with JWT** (`scripts@hub.local`, `matlab@hub.local`) instead of the
  admin account: Label Studio records in `completed_by` the account that imported each
  annotation, which keeps the origin visible.

## Operations

```bash
docker compose ps
docker compose logs -f app

# manual backup
docker compose exec respaldo sh -c 'RESPALDO_AHORA=1 timeout 120 sh /respaldo.sh'

# regenerate the certificate (the fingerprint changes: browsers must accept it again)
docker compose run --rm certificado && docker compose restart nginx

# restore a backup
docker compose exec -T db pg_restore -U labelstudio -d labelstudio --clean < backups/db-….dump

# reach the interface over HTTP through an SSH tunnel
ssh -N -L 18085:127.0.0.1:18085 <server>     # then open http://localhost:18085
```

## Limitations

- **Community edition:** every user sees every project; there are no private per-site
  projects.
- **No export to Camtrap DP or Darwin Core yet.**
- **LiDAR, SAR, and multispectral data** have no mode: Label Studio does not support
  them.
- `.DAT` audio files from some recorders must be decoded to WAV before import.
