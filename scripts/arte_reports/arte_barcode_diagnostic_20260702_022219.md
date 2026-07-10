# Diagnóstico barcode Arte — 2026-07-02T02:22:19

- Integración: **tiendabellasartesjer.com** (id=1)
- DB: `arte` @ `http://localhost:8069`

## Configuración integración

- `id`: 1
- `name`: tiendabellasartesjer.com
- `state`: draft
- `validate_barcode`: False
- `prestashop_validate_barcode`: False
- `prestashop_skip_invalid_barcodes`: True

## Métricas

| Métrica | Valor |
|---------|------:|
| Variantes mapeadas (activas) | 18751 |
| Con external_barcode (EAN externo) | 10837 |
| Con barcode Odoo | 10882 |
| EAN externo sin barcode Odoo | 0 |
| Sin external_barcode | 7914 |
| **TPV escaneables** (pos+barcode) | **0** |
| Barcode inválido en Odoo | 7871 |
| Grupos EAN duplicados (externo) | 22 |
| Grupos barcode duplicados (Odoo) | 126 |

## Candidatos piloto TPV

| product_id | ps_code | barcode | nombre |
|-----------:|---------|---------|--------|
| 21891 | 5176-38541 | 8715046090015 | [00.006] Oleo Old Holland 40 ml (A1 Titanium white) |
| 27075 | 7890-0 | 738797990555 | [0000055-0] Set 6 acrílicos Golden Open mixing |
| 27076 | 7891-0 | 738797992443 | [0000075-0] Set 6 acrílicos Golden heavy body mixing |
| 27073 | 7888-0 | 738797092402 | [0000924-0] Set acrílicos Golden heavy body mixing |
| 27074 | 7889-0 | 738797092501 | [0000925-0] Set acrílicos Golden Open mixing |
| 26488 | 7609-0 | 7630002354691 | [0003.509] Portaminas Fixpencil Caran d´ache 3 mm |
| 20276 | 3820-0 | 9788883705601 | [001001204] Moleskine acuarela pocket |
| 24296 | 6787-0 | 9788862933094 | [001001701] Cuaderno japones Moleskine |
| 21750 | 4984-0 | 9788883705625 | [001001704] Moleskine acuarela large |
| 21753 | 4987-0 | 9788883701153 | [001002334] Moleskine dibujo large |
| 22287 | 5318-0 | 9788862930345 | [001002737] Moleskine cuaderno bocetos rojo |
| 21754 | 4988-0 | 9788862931939 | [001007101] Moleskine dibujo DIN A4 |
| 21751 | 4985-0 | 9788862931946 | [001007102] Moleskine acuarela DIN A4 |
| 23946 | 6523-0 | 4003198106000 | [002001159] Organizador rotuladores Tombow vacío |
| 23947 | 6524-0 | 4003198106024 | [002001160] Organizador rotuladores Tombow con 108 rotulador |
| 26258 | 7375-0 | 4009729066317 | [00210-14410] Afilalápices eléctrico |
| 23305 | 5989-0 | 4017505000509 | [01001036] Pincel Da Vinci Maestro Serie 10 36 |
| 22713 | 5752-41949 | 4017505218331 | [01007003] Pincel Miniature Da Vinci 70 (3) |
| 22714 | 5752-41950 | 4017505218348 | [01007004] Pincel Miniature Da Vinci 70 (4) |
| 22723 | 5753-41951 | 4017505218362 | [01007603] Pincel Miniatura Da Vinci 76 (3) |
