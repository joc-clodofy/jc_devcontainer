-- Productos mapeados sin categoría pública (los 43 que el script toca)
\pset pager off
\pset format aligned

SELECT pt.id,
       pt.default_code,
       LEFT(pt.name->>'es_ES', COALESCE(NULLIF(pt.name->>'es_ES',''), pt.name::text, 60)) AS nombre,
       pt.available_in_pos,
       pt.sale_ok,
       pt.purchase_ok,
       (SELECT COUNT(*) FROM pos_category_product_template_rel r WHERE r.product_template_id = pt.id) AS n_pos_categ
FROM product_template pt
WHERE EXISTS (
    SELECT 1 FROM integration_product_template_mapping m
    WHERE m.integration_id = 1 AND m.template_id = pt.id
)
AND NOT EXISTS (
    SELECT 1 FROM product_public_category_product_template_rel r
    WHERE r.product_template_id = pt.id
)
ORDER BY pt.id;
