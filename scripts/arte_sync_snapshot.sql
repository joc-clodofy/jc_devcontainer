-- Snapshot métricas arte (antes/después sync categorías+TPV)
\pset pager off
\pset format unaligned
\pset tuples_only on
\pset fieldsep '|'

SELECT 'product_category_count' AS metric, COUNT(*)::text AS value FROM product_category;
SELECT 'pos_category_count' AS metric, COUNT(*)::text AS value FROM pos_category;
SELECT 'public_category_count' AS metric, COUNT(*)::text AS value FROM product_public_category;
SELECT 'tpl_mapping_count' AS metric, COUNT(*)::text AS value FROM integration_product_template_mapping WHERE integration_id = 1;
SELECT 'tpl_mapped_count' AS metric, COUNT(*)::text AS value FROM integration_product_template_mapping WHERE integration_id = 1 AND template_id IS NOT NULL;
SELECT 'tpl_total' AS metric, COUNT(*)::text AS value FROM product_template;
SELECT 'tpl_available_pos_true' AS metric, COUNT(*)::text AS value FROM product_template WHERE available_in_pos = true;
SELECT 'tpl_sale_ok_true' AS metric, COUNT(*)::text AS value FROM product_template WHERE sale_ok = true;
SELECT 'tpl_purchase_ok_true' AS metric, COUNT(*)::text AS value FROM product_template WHERE purchase_ok = true;
SELECT 'tpl_with_pos_categ' AS metric, COUNT(*)::text AS value FROM product_template pt
  WHERE EXISTS (SELECT 1 FROM pos_category_product_template_rel r WHERE r.product_template_id = pt.id);
SELECT 'tpl_mapped_with_pos_categ' AS metric, COUNT(*)::text AS value
FROM product_template pt
WHERE EXISTS (SELECT 1 FROM integration_product_template_mapping m WHERE m.integration_id = 1 AND m.template_id = pt.id)
  AND EXISTS (SELECT 1 FROM pos_category_product_template_rel r WHERE r.product_template_id = pt.id);
SELECT 'tpl_mapped_available_pos' AS metric, COUNT(*)::text AS value
FROM product_template pt
WHERE EXISTS (SELECT 1 FROM integration_product_template_mapping m WHERE m.integration_id = 1 AND m.template_id = pt.id)
  AND pt.available_in_pos = true;
SELECT 'tpl_mapped_no_public_categ' AS metric, COUNT(*)::text AS value
FROM product_template pt
WHERE EXISTS (SELECT 1 FROM integration_product_template_mapping m WHERE m.integration_id = 1 AND m.template_id = pt.id)
  AND NOT EXISTS (SELECT 1 FROM product_public_category_product_template_rel r WHERE r.product_template_id = pt.id);
