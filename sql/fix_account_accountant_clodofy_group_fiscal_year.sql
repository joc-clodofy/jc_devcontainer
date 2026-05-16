-- Conflicto al instalar account_accountant:
--   duplicate key res_groups_name_uniq al fusionar traducción de group_fiscal_year.
--
-- Causa: grupo + xml_id de account_accountant_clodofy.group_fiscal_year (módulo
-- ya no instalable) duplica el nombre del grupo oficial en la misma categoría.
--
-- Borra solo ese xml_id y su res_groups, en un solo paso (PostgreSQL CTE).

WITH doomed AS (
    SELECT imd.res_id AS gid
    FROM ir_model_data imd
    WHERE imd.model = 'res.groups'
      AND imd.module = 'account_accountant_clodofy'
      AND imd.name = 'group_fiscal_year'
      AND NOT EXISTS (SELECT 1 FROM ir_model_access a WHERE a.group_id = imd.res_id)
      AND NOT EXISTS (SELECT 1 FROM res_groups_users_rel u WHERE u.gid = imd.res_id)
),
del_xml AS (
    DELETE FROM ir_model_data imd
    USING doomed d
    WHERE imd.model = 'res.groups'
      AND imd.res_id = d.gid
      AND imd.module = 'account_accountant_clodofy'
      AND imd.name = 'group_fiscal_year'
    RETURNING imd.res_id
)
DELETE FROM res_groups rg
USING del_xml x
WHERE rg.id = x.res_id;
