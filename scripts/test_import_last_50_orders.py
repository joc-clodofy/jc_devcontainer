#!/usr/bin/env python3
"""Force-import the last 50 WooCommerce orders and report pass/fail."""
import json
import traceback

INTEGRATION_ID = 1
ORDER_LIMIT = 50


def run(env):
    integration = env['sale.integration'].browse(INTEGRATION_ID)
    if not integration.exists():
        raise SystemExit(f'Integration id={INTEGRATION_ID} not found')

    adapter = integration._build_adapter()
    wc_orders = adapter.get(
        'orders',
        params={
            'lang': 'all',
            'per_page': ORDER_LIMIT,
            'orderby': 'id',
            'order': 'desc',
        },
        with_paging=False,
    )
    print(f'Integración: {integration.name} (id={integration.id})')
    print(f'Pedidos obtenidos de WooCommerce: {len(wc_orders)}\n')

    InputFile = env['sale.integration.input.file']
    Mapping = env['integration.sale.order.mapping']
    results = []

    for wc_order in wc_orders:
        ext_id = str(wc_order['id'])
        wc_status = wc_order.get('status', '?')
        wc_number = wc_order.get('number', ext_id)
        row = {
            'wc_id': ext_id,
            'wc_number': wc_number,
            'wc_status': wc_status,
            'result': '',
            'detail': '',
        }

        try:
            with env.cr.savepoint():
                mapping = Mapping.search([
                    ('integration_id', '=', integration.id),
                    ('external_id.code', '=', ext_id),
                ], limit=1)
                if mapping.odoo_id:
                    row['result'] = 'SKIP'
                    row['detail'] = f'Ya importado → {mapping.odoo_id.name}'
                    results.append(row)
                    continue

                input_file = InputFile.search([
                    ('si_id', '=', integration.id),
                    ('name', '=', ext_id),
                ], limit=1)

                if input_file and input_file.order_id:
                    row['result'] = 'SKIP'
                    row['detail'] = f'Input file con pedido {input_file.order_id.name}'
                    results.append(row)
                    continue

                if not input_file:
                    input_file = InputFile.with_context(
                        skip_create_order_from_input=True,
                    ).create({
                        'si_id': integration.id,
                        'name': ext_id,
                        'raw_data': json.dumps(wc_order, indent=4),
                        'update_required': True,
                    })
                else:
                    input_file.write({
                        'raw_data': json.dumps(wc_order, indent=4),
                        'update_required': True,
                    })

                sale_order = integration.with_company(
                    integration.company_id,
                ).create_order_from_input(input_file)

                row['result'] = 'OK'
                row['detail'] = sale_order.name
        except Exception as exc:
            row['result'] = 'FAIL'
            row['detail'] = f'{type(exc).__name__}: {exc}'
            row['trace'] = traceback.format_exc()

        results.append(row)

    ok = sum(1 for r in results if r['result'] == 'OK')
    skip = sum(1 for r in results if r['result'] == 'SKIP')
    fail = sum(1 for r in results if r['result'] == 'FAIL')

    print('=' * 90)
    print(f'RESUMEN: OK={ok}  SKIP={skip}  FAIL={fail}  TOTAL={len(results)}')
    print('=' * 90)

    for row in results:
        print(
            f"[{row['result']:4}] WC#{row['wc_number']:>8} "
            f"id={row['wc_id']:>8} status={row['wc_status']:<12} → {row['detail']}"
        )

    if fail:
        print('\n' + '=' * 90)
        print('DETALLE DE FALLOS')
        print('=' * 90)
        for row in results:
            if row['result'] != 'FAIL':
                continue
            print(f"\n--- WC#{row['wc_number']} (id={row['wc_id']}, status={row['wc_status']}) ---")
            print(row['detail'])
            if row.get('trace'):
                print(row['trace'])

    return results
