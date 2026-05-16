-- Limpia grupos duplicados en res_groups que chocan con la restricción
--   UNIQUE (category_id, name)  / mismo nombre EN en una misma aplicación.
--
-- Criterio: en cada grupo de duplicados (mismo category_id y mismo name->>'en_US')
-- se conserva UNA fila: prioridad a la que tiene xml_id en módulo "account",
-- luego cualquier xml_id, luego el id más bajo. El resto se elimina.
--
-- IMPORTANTE: haz BACKUP. Para Odoo, conviene parar el servidor o no usar la BD.
-- Uso:  psql -h HOST -U USER -d NOMBRE_BD -v ON_ERROR_STOP=1 -f cleanup_duplicate_res_groups.sql

BEGIN;

CREATE TEMP TABLE tmp_groups_to_remove ON COMMIT DROP AS
WITH ranked AS (
    SELECT
        rg.id,
        COUNT(*) OVER (
            PARTITION BY rg.category_id, COALESCE(rg.name->>'en_US', '')
        ) AS cnt,
        ROW_NUMBER() OVER (
            PARTITION BY rg.category_id, COALESCE(rg.name->>'en_US', '')
            ORDER BY
                (EXISTS (
                    SELECT 1
                    FROM ir_model_data imd
                    WHERE imd.model = 'res.groups'
                      AND imd.res_id = rg.id
                      AND imd.module = 'account'
                )) DESC,
                (EXISTS (
                    SELECT 1
                    FROM ir_model_data imd
                    WHERE imd.model = 'res.groups'
                      AND imd.res_id = rg.id
                )) DESC,
                rg.id ASC
        ) AS rn
    FROM res_groups rg
    WHERE rg.category_id IS NOT NULL
      AND rg.name ? 'en_US'
)
SELECT id FROM ranked WHERE cnt > 1 AND rn > 1;

-- Vista previa (comenta si ejecutas con -f y solo quieres borrar)
SELECT 'SE VAN A ELIMINAR LOS SIGUIENTES ids' AS msg;
SELECT g.id, g.category_id, g.name, imd.module, imd.name AS xml_id
FROM res_groups g
LEFT JOIN ir_model_data imd
  ON imd.model = 'res.groups' AND imd.res_id = g.id
WHERE g.id IN (SELECT id FROM tmp_groups_to_remove)
ORDER BY g.id;

DELETE FROM ir_model_data
WHERE model = 'res.groups'
  AND res_id IN (SELECT id FROM tmp_groups_to_remove);

DELETE FROM ir_model_access
WHERE group_id IN (SELECT id FROM tmp_groups_to_remove);

DELETE FROM rule_group_rel
WHERE group_id IN (SELECT id FROM tmp_groups_to_remove);

DELETE FROM res_groups
WHERE id IN (SELECT id FROM tmp_groups_to_remove);

COMMIT;
