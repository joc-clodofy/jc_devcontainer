-- Validación post-sync/dedupe arte
\pset pager off
\pset format unaligned
\pset tuples_only on
\pset fieldsep '|'

SELECT 'tpl_total' AS metric, COUNT(*)::text FROM product_template;
SELECT 'tpl_sin_mapping' AS metric, COUNT(*)::text FROM product_template pt
WHERE NOT EXISTS (
  SELECT 1 FROM integration_product_template_mapping m
  WHERE m.integration_id = 1 AND m.template_id = pt.id
);
SELECT 'tpl_mapping_total' AS metric, COUNT(*)::text FROM integration_product_template_mapping WHERE integration_id = 1;
SELECT 'tpl_mapping_mapped' AS metric, COUNT(*)::text FROM integration_product_template_mapping WHERE integration_id = 1 AND template_id IS NOT NULL;
SELECT 'tpl_dup_mapping_groups' AS metric, COUNT(*)::text FROM (
  SELECT template_id FROM integration_product_template_mapping
  WHERE integration_id = 1 AND template_id IS NOT NULL
  GROUP BY template_id HAVING COUNT(*) > 1
) x;
SELECT 'var_total' AS metric, COUNT(*)::text FROM product_product;
SELECT 'var_sin_mapping' AS metric, COUNT(*)::text FROM product_product pp
WHERE NOT EXISTS (
  SELECT 1 FROM integration_product_product_mapping m
  WHERE m.integration_id = 1 AND m.product_id = pp.id AND m.product_id IS NOT NULL
);
SELECT 'var_mapping_total' AS metric, COUNT(*)::text FROM integration_product_product_mapping WHERE integration_id = 1;
SELECT 'var_mapping_mapped' AS metric, COUNT(*)::text FROM integration_product_product_mapping WHERE integration_id = 1 AND product_id IS NOT NULL;
SELECT 'var_dup_mapping_groups' AS metric, COUNT(*)::text FROM (
  SELECT product_id FROM integration_product_product_mapping
  WHERE integration_id = 1 AND product_id IS NOT NULL
  GROUP BY product_id HAVING COUNT(*) > 1
) x;
SELECT 'var_mapping_null_product' AS metric, COUNT(*)::text FROM integration_product_product_mapping
WHERE integration_id = 1 AND product_id IS NULL;
SELECT 'var_con_mas_de_10_mappings' AS metric, COUNT(*)::text FROM (
  SELECT product_id FROM integration_product_product_mapping
  WHERE integration_id = 1 AND product_id IS NOT NULL
  GROUP BY product_id HAVING COUNT(*) > 10
) x;
SELECT 'tpl_mapped_sin_public_categ' AS metric, COUNT(*)::text
FROM product_template pt
WHERE EXISTS (SELECT 1 FROM integration_product_template_mapping m WHERE m.integration_id = 1 AND m.template_id = pt.id)
  AND NOT EXISTS (SELECT 1 FROM product_public_category_product_template_rel r WHERE r.product_template_id = pt.id);

-- ---------------------------------------------------------------------------
-- Barcodes (variantes mapeadas PS, integration_id = 1)
-- ---------------------------------------------------------------------------
SELECT 'var_mapped_active' AS metric, COUNT(*)::text
FROM integration_product_product_mapping m
JOIN product_product pp ON pp.id = m.product_id
WHERE m.integration_id = 1 AND m.product_id IS NOT NULL AND pp.active;

SELECT 'var_mapped_with_external_barcode' AS metric, COUNT(*)::text
FROM integration_product_product_mapping m
JOIN product_product pp ON pp.id = m.product_id
JOIN integration_product_product_external e ON e.id = m.external_product_id
WHERE m.integration_id = 1 AND m.product_id IS NOT NULL AND pp.active
  AND COALESCE(NULLIF(TRIM(e.external_barcode), ''), '') <> '';

SELECT 'var_mapped_with_odoo_barcode' AS metric, COUNT(*)::text
FROM integration_product_product_mapping m
JOIN product_product pp ON pp.id = m.product_id
WHERE m.integration_id = 1 AND m.product_id IS NOT NULL AND pp.active
  AND COALESCE(NULLIF(TRIM(pp.barcode), ''), '') <> '';

SELECT 'var_mapped_no_barcode' AS metric, COUNT(*)::text
FROM integration_product_product_mapping m
JOIN product_product pp ON pp.id = m.product_id
LEFT JOIN integration_product_product_external e ON e.id = m.external_product_id
WHERE m.integration_id = 1 AND m.product_id IS NOT NULL AND pp.active
  AND COALESCE(NULLIF(TRIM(pp.barcode), ''), '') = ''
  AND COALESCE(NULLIF(TRIM(e.external_barcode), ''), '') = '';

SELECT 'var_pos_scan_ready' AS metric, COUNT(*)::text
FROM integration_product_product_mapping m
JOIN product_product pp ON pp.id = m.product_id
JOIN product_template pt ON pt.id = pp.product_tmpl_id
WHERE m.integration_id = 1 AND m.product_id IS NOT NULL AND pp.active
  AND pt.available_in_pos
  AND COALESCE(NULLIF(TRIM(pp.barcode), ''), '') NOT IN ('', '0', '00');

SELECT 'var_dup_external_barcode_groups' AS metric, COUNT(*)::text FROM (
  SELECT e.external_barcode
  FROM integration_product_product_mapping m
  JOIN product_product pp ON pp.id = m.product_id
  JOIN integration_product_product_external e ON e.id = m.external_product_id
  WHERE m.integration_id = 1 AND m.product_id IS NOT NULL AND pp.active
    AND COALESCE(NULLIF(TRIM(e.external_barcode), ''), '') <> ''
  GROUP BY e.external_barcode HAVING COUNT(*) > 1
) x;

SELECT 'var_dup_odoo_barcode_groups' AS metric, COUNT(*)::text FROM (
  SELECT pp.barcode
  FROM integration_product_product_mapping m
  JOIN product_product pp ON pp.id = m.product_id
  WHERE m.integration_id = 1 AND m.product_id IS NOT NULL AND pp.active
    AND COALESCE(NULLIF(TRIM(pp.barcode), ''), '') <> ''
  GROUP BY pp.barcode HAVING COUNT(*) > 1
) x;

-- ---------------------------------------------------------------------------
-- Stock (variantes mapeadas, tracking none)
-- ---------------------------------------------------------------------------
SELECT 'var_mapped_tracking_none' AS metric, COUNT(*)::text
FROM integration_product_product_mapping m
JOIN product_product pp ON pp.id = m.product_id
JOIN product_template pt ON pt.id = pp.product_tmpl_id
WHERE m.integration_id = 1 AND m.product_id IS NOT NULL AND pp.active
  AND COALESCE(pt.tracking, 'none') = 'none';

SELECT 'var_mapped_with_internal_quant' AS metric, COUNT(DISTINCT pp.id)::text
FROM integration_product_product_mapping m
JOIN product_product pp ON pp.id = m.product_id
JOIN product_template pt ON pt.id = pp.product_tmpl_id
JOIN stock_quant sq ON sq.product_id = pp.id
JOIN stock_location sl ON sl.id = sq.location_id
WHERE m.integration_id = 1 AND m.product_id IS NOT NULL AND pp.active
  AND COALESCE(pt.tracking, 'none') = 'none'
  AND sl.usage = 'internal'
  AND sq.quantity <> 0;

SELECT 'integration_stock_location_lines' AS metric, COUNT(*)::text
FROM external_stock_location_line
WHERE integration_id = 1;
