-- Historial de identificaciones: registro append-only de cada versión de cada
-- anotación. Label Studio Community solo guarda la última acción; hace falta saber
-- quién verificó cada observación y el historial si la especie cambió.
--
-- Idempotente. Lo corre `historial-init` con el superusuario en cada arranque.
-- Variable psql: :pass = contraseña del rol historial.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'historial') THEN
    CREATE ROLE historial LOGIN;
  END IF;
END $$;
ALTER ROLE historial PASSWORD :'pass';

CREATE SCHEMA IF NOT EXISTS historial;

CREATE TABLE IF NOT EXISTS historial.evento (
    id                      bigserial PRIMARY KEY,
    registrado_en           timestamptz NOT NULL DEFAULT now(),
    origen                  text NOT NULL
                            CHECK (origen IN ('webhook', 'instantanea_inicial', 'reconciliacion')),
    accion                  text NOT NULL
                            CHECK (accion IN ('creada', 'actualizada', 'borrada')),
    anotacion_id            integer NOT NULL,
    tarea_id                integer,
    proyecto_id             integer,
    completada_por_id       integer,       -- quién hizo la anotación
    actualizada_por_id      integer,       -- quién hizo ESTA versión
    anotacion_actualizada_en timestamptz,  -- updated_at de Label Studio
    resultado               jsonb,         -- la versión (NULL si se borró)
    resultado_anterior      jsonb,         -- la versión previa registrada
    cambios_especie         jsonb          -- [{region, antes, despues}]
);
CREATE INDEX IF NOT EXISTS evento_anotacion ON historial.evento (anotacion_id, id DESC);
CREATE INDEX IF NOT EXISTS evento_tarea ON historial.evento (tarea_id);

-- Append-only de verdad: ni el propio servicio puede reescribir el pasado.
CREATE OR REPLACE FUNCTION historial.solo_agregar() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'historial.evento es append-only (% bloqueado)', TG_OP;
END $$;
DROP TRIGGER IF EXISTS evento_sin_cambios ON historial.evento;
CREATE TRIGGER evento_sin_cambios BEFORE UPDATE OR DELETE ON historial.evento
  FOR EACH ROW EXECUTE FUNCTION historial.solo_agregar();
DROP TRIGGER IF EXISTS evento_sin_truncate ON historial.evento;
CREATE TRIGGER evento_sin_truncate BEFORE TRUNCATE ON historial.evento
  FOR EACH STATEMENT EXECUTE FUNCTION historial.solo_agregar();

-- Última versión conocida de cada anotación: contra esto concilia el servicio.
CREATE OR REPLACE VIEW historial.ultimo AS
SELECT DISTINCT ON (anotacion_id) *
FROM historial.evento
ORDER BY anotacion_id, id DESC;

-- Una fila por (versión, región con especie). La vista que usa un export.
CREATE OR REPLACE VIEW historial.identificacion AS
SELECT e.id AS evento_id, e.registrado_en, e.origen, e.accion,
       e.anotacion_id, e.tarea_id, e.proyecto_id,
       e.actualizada_por_id, u.email AS actualizada_por,
       e.anotacion_actualizada_en,
       r->>'id' AS region_id,
       r->'value'->'taxonomy'->0->>-1 AS especie
FROM historial.evento e
LEFT JOIN public.htx_user u ON u.id = e.actualizada_por_id
CROSS JOIN LATERAL jsonb_array_elements(coalesce(e.resultado, '[]'::jsonb)) r
WHERE r->>'type' = 'taxonomy';

GRANT USAGE ON SCHEMA historial TO historial;
GRANT SELECT, INSERT ON historial.evento TO historial;
GRANT USAGE ON SEQUENCE historial.evento_id_seq TO historial;
GRANT SELECT ON historial.ultimo, historial.identificacion TO historial;
-- Lectura mínima de Label Studio, para conciliar y para los correos.
GRANT USAGE ON SCHEMA public TO historial;
GRANT SELECT ON public.task_completion, public.prediction, public.htx_user TO historial;
