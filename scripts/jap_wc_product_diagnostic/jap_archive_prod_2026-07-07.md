# Productos archivados en producción — Happy Japan

**Fecha:** 2026-07-07 06:41 UTC
**URL:** https://odoo17-happy-japan.n3.clodofy.cloud
**Base de datos:** `odoo_odoo17_happy_japan`
**Acción:** ARCHIVADO (active=False)
**Total productos:** 94

## Cómo revertir

Para reactivar un producto archivado, en Odoo ir a Inventario → Productos,
activar el filtro "Archivados", buscar por ID o referencia, y pulsar "Desarchivar".

Vía XML-RPC (ejemplo para un ID):

```python
models.execute_kw(db, uid, password, 'product.template', 'write',
    [[PRODUCT_ID], {'active': True}])
```

## Listado completo

| # | ID archivado | Referencia | Nombre archivado | ID conservado | Motivo |
|---|-------------|------------|------------------|---------------|--------|
| 1 | 1274 | `0888 200050` | PLACA 1 AGUJA &quot;b&quot; dk 888 | 3174 | legacy+import: import con pedidos, archivar legacy |
| 2 | 3177 | `0888 200060` | PLACA 1 AGUJA "c" dk 888/HIGHLEAD/GOLDEN WHEEL | 1300 | legacy+import: archivar copia import |
| 3 | 3198 | `103H7823-0416` | MOTOR MOVIMIENTO "Y" SWF | 1945 | legacy+import: archivar copia import |
| 4 | 3203 | `5.04.112A` | PATA/PRENSATELAS "A" MEC-VAL | 2109 | legacy+import: archivar copia import |
| 5 | 2547 | `522080-271538` | TOPE GUIA | 1624 | ambos legacy: archivar id mayor |
| 6 | 2137 | `720` | ENGRANE HEMBRA EJE ACOPLAMIENTO RULETA PF-491/591/ZOJE/TTY | 1564 | ambos legacy: archivar id mayor |
| 7 | 3079 | `8.01.114` | BUJE MEC-VAL CS-87 | 2090 | legacy+import: archivar copia import |
| 8 | 3182 | `91-059186-04/004` | DIENTE PF-1293 "B" | 1434 | legacy+import: archivar copia import |
| 9 | 3183 | `91-150526-04/001` | PLACA AGUJA PF-491 "B" | 1483 | legacy+import: archivar copia import |
| 10 | 3180 | `91-150739-24/001` | PLACA AGUJA PF-591 "B" tty | 1404 | legacy+import: archivar copia import |
| 11 | 3185 | `91-150739-24/003` | PLACA AGUJA PEQUEÑA PF-591 "A" tty | 1560 | legacy+import: archivar copia import |
| 12 | 3184 | `91-150739-24/004` | PLACA AGUJA PF-591 "C" tty | 1559 | legacy+import: archivar copia import |
| 13 | 3088 | `C0066` | CUCHILLA VERTICAL KM10&quot; | 3235 | ambas import: conservar más nuevo |
| 14 | 3219 | `CORREADENTADA` | CORREA DENTADA MOTOR "Y" LASER | 2672 | legacy+import: archivar copia import |
| 15 | 3225 | `DUO-HCS3-1201-40` | DUO COMBO HAPPY HCS3.  COMBO ESPECIAL DE DOS CABEZALES DE HCS3, UNIDOS SOBRE UNA | 2885 | legacy+import: archivar copia import |
| 16 | 2491 | `EL6001392` | RODILLO ALIMENTAC. MAQUINA REBAJAR 50MM GRANO 2 | 2489 | ambos legacy: archivar id mayor |
| 17 | 3166 | `EPM00980` | ROTARY SERVOMOTOR EJE X,Y. YASKAWA 200W PARA MÁQUINA HAPPY, MODELO HCU. | 1027 | legacy+import: archivar copia import |
| 18 | 3188 | `EPZ01410` | PANEL LCD 10" HAPPY | 1664 | legacy+import: archivar copia import |
| 19 | 424 | `EPZ01480` | PANEL TACTIL 7&apos;&apos; HAPPY HCH/HCS/HCD2/HCR2/HCR3 | 3143 | legacy+import: import con pedidos, archivar legacy |
| 20 | 3212 | `EPZ01490` | PANTALLA LCD HAPPY 7" | 2431 | legacy+import: archivar copia import |
| 21 | 3207 | `FC345` | BASTIDOR FAST CLAMPING 3,5"X4"(89X101,5MM) | 2225 | legacy+import: archivar copia import |
| 22 | 3206 | `FC45` | BASTIDOR FAST CLAMPING 4,5"X4,5" (114X114MM) | 2224 | legacy+import: archivar copia import |
| 23 | 3208 | `FC456` | BASTIDOR FAST CLAMPING 4,5"X6" (114,3X152,4MM) | 2226 | legacy+import: archivar copia import |
| 24 | 3205 | `FC65` | BASTIDOR FAST CLAMPING 6,5"X6,5" (165,1X165,1MM) | 2223 | legacy+import: archivar copia import |
| 25 | 3083 | `FRA44003` | Cap hold frame ass&apos;y (wide type) HAPPY | 3234 | ambas import: conservar más nuevo |
| 26 | 3130 | `FW-711445` | HILO DE LANA FISWOOLY 1000MT | 3131 | ambas import: conservar más nuevo |
| 27 | 3222 | `HCA04100` | CUCHILLA FIJA HAPPY "B" HCA/HCG/HCM | 2857 | legacy+import: archivar copia import |
| 28 | 3178 | `HCB79210` | MOTOR "Y" HAPPY HCS2 | 1360 | legacy+import: archivar copia import |
| 29 | 3168 | `HCB79600` | POTENCIOMETRO CAMBIO COLOR HCH-HCS2-HCU/HCU2 POTENTIOMETER ASS'Y HAPPY | 1088 | legacy+import: archivar copia import |
| 30 | 3163 | `HCB914022` | THREAD HOLDER ASS'Y HAPPY HCS/HCS2 | 1010 | legacy+import: archivar copia import |
| 31 | 3165 | `HCBU08040` | POSITIONING PLATE ASS'Y-HCS Upgrade Kit A HAPPY | 1021 | legacy+import: archivar copia import |
| 32 | 3164 | `HCBU13020` | LEVER FOR PRESSURE FOOT ASS'Y HAPPY HCH/HCS/HCS2 | 1016 | legacy+import: archivar copia import |
| 33 | 3162 | `HCBU14011` | THREAD CATCHER ASS'Y-HCS Upgrade kit a HAPPY | 1009 | legacy+import: archivar copia import |
| 34 | 3209 | `HCD05A13` | SOPORTE CUCHILLAS HAPPY HCD2 // THREAD CUTTING DEVICE BASE ASS'Y | 2314 | legacy+import: archivar copia import |
| 35 | 3217 | `HCD29540` | REAR COVER (10.4 in) HAPPY (SOPORTE LCD 10") | 2592 | legacy+import: archivar copia import |
| 36 | 3043 | `HCD29572` | CARCASA FRONTAL PANEL 10&quot; HAPPY | 3233 | ambas import: conservar más nuevo |
| 37 | 3189 | `HCD72800` | CABLE CONEXIÓN LCD 10" HAPPY | 1665 | legacy+import: archivar copia import |
| 38 | 3197 | `HCD79621` | MODULO CORE 7" PANEL LCD HAPPY | 1926 | legacy+import: archivar copia import |
| 39 | 3167 | `HCD79720` | DRIVER MOVIMIENTOS X, Y, A. PARA MÁQUINA DE BORDAR HAPPY MODELO HCU. SERVOPACK 2 | 1028 | legacy+import: archivar copia import |
| 40 | 3137 | `HCD81022` | LCD ATA CIRCUIT BOARD (Pb FREE) ASSY HAPPY &quot;HCS LCD COLOR&quot; | 3237 | ambas import: conservar más nuevo |
| 41 | 3170 | `HCD81092H` | PLACA DE POSICION "L" Y "C" HAPPY HCD2 (TIMING BOARD) | 1125 | legacy+import: archivar copia import |
| 42 | 3169 | `HCD81093` | CONJUNTO PLACA DE POSICION "L" Y "C" HAPPY HCD2 (TIMING BOARD) | 1121 | legacy+import: archivar copia import |
| 43 | 3211 | `HCDU37A41` | BASTIDOR PORTAL "X" HAPPY HCD2-X (400X1200). | 2428 | legacy+import: archivar copia import |
| 44 | 3146 | `HCHplus-701 S/G` | HAPPY HCHplus-701-30 S/G . | 436 | legacy+import: archivar copia import |
| 45 | 3176 | `HCR15B40` | SUJETAHILOS TIPO CLIP happy hcd2/hcr2 moderno/HOLDER ASS'Y (lower) | 1287 | legacy+import: archivar copia import |
| 46 | 3210 | `HCR16A46` | CONJUNTO TENSOR DETECCIÓN DE HILO HAPPY HCR2. (TENSOR NEGRO COMPLETO) | 2322 | legacy+import: archivar copia import |
| 47 | 3154 | `HCR3-1502-45` | HAPPY HCR3-X1502-45  Máquina de bordar, de 2 cabezas y 15 agujas, con una distan | 455 | legacy+import: archivar copia import |
| 48 | 3153 | `HCR3-1502-45 S/G` | HAPPY HCR3-X1502-45 S/G  Máquina de bordar, de 2 cabezas y 15 agujas, con una di | 454 | legacy+import: archivar copia import |
| 49 | 3144 | `HCR3-1504-45` | HAPPY HCR3-1504-45 - Máquina de bordar, de 4 cabezas y 15 agujas, con una distan | 425 | legacy+import: archivar copia import |
| 50 | 3145 | `HCR3-1504-45 S/G` | HAPPY HCR3-1504-45 S/G -Máquina de bordar, de 4 cabezas y 15 agujas, con una dis | 426 | legacy+import: archivar copia import |
| 51 | 3148 | `HCR3-1506-45` | HAPPY HCR3-1506-45  - Máquina de bordar, de 6 cabezas y 15 agujas, con una dista | 445 | legacy+import: archivar copia import |
| 52 | 444 | `HCR3-1506-45 S/G` | HAPPY HCR3-1506-45 S/G - Máquina de bordar, de 6 cabezas y 15 agujas, con una di | 3147 | legacy+import: import con pedidos, archivar legacy |
| 53 | 3161 | `HCR3-X1504-45` | HAPPY HCR3-X1504-45  -Máquina de bordar, de 4 cabezas y 15 agujas, con una dista | 984 | legacy+import: archivar copia import |
| 54 | 3149 | `HCR3-X1506 S/G` | HAPPY HCR3-X1506-45 S/G -Máquina de bordar, de 6 cabezas y 15 agujas, con una di | 446 | legacy+import: archivar copia import |
| 55 | 3150 | `HCR3-X1506-45` | HAPPY HCR3-X1506-45  -Máquina de bordar, de 6 cabezas y 15 agujas, con una dista | 447 | legacy+import: archivar copia import |
| 56 | 3151 | `HCR3-X1508 S/G` | HAPPY HCR3-X1508-45 S/G -Máquina de bordar, de 6 cabezas y 15 agujas, con una di | 450 | legacy+import: archivar copia import |
| 57 | 3152 | `HCR3-X1508-45` | HAPPY HCR3-X1508-45 - Máquina de bordar, de 8 cabezas y 15 agujas, con una dista | 451 | legacy+import: archivar copia import |
| 58 | 3229 | `HCSU29010` | DISPLAY MODULE 7" HCD3E/HCD2 HAPPY | 2977 | legacy+import: archivar copia import |
| 59 | 3160 | `HCU-1401-40 S/G` | HCU-1401-40W - Máquina de bordar de 1 cabezal y 14 agujas. - Prensatelas con reg | 979 | legacy+import: archivar copia import |
| 60 | 3200 | `HCU25100` | PLETINA SUJECIÓN CORREA EJE "Y" HAPPY HCU | 1948 | legacy+import: archivar copia import |
| 61 | 3199 | `HCU25110` | PLETINA CONEXIÓN CORREA EJE "Y" HAPPY HCU | 1947 | legacy+import: archivar copia import |
| 62 | 3156 | `HFR-W1502-120` | HAPPY HFR-W1502-120 - Campo de trabajo: 2 cabezales Y1200xX600 mm. - Máquina de  | 457 | legacy+import: archivar copia import |
| 63 | 456 | `HFR-W1502A-120` | HAPPY HFR-W1502A-120 - Campo de trabajo: 2 cabezales Y1200xX600 mm.; 1 cabezal Y | 3155 | legacy+import: import con pedidos, archivar legacy |
| 64 | 3201 | `HMG08130` | CUSHION "B" HAPPY BLANCO HCGB/HMF | 2015 | legacy+import: archivar copia import |
| 65 | 3173 | `M0483229` | THREAD DETECTING CIRCUIT BOARD ASS'Y HAPPY HCG SERIES | 1234 | legacy+import: archivar copia import |
| 66 | 3035 | `MF00A0834-B` | CUCHILLA MOVIL MITSUBISHI &quot;B&quot; LS2-150/180/190 | 3232 | ambas import: conservar más nuevo |
| 67 | 3186 | `MH 9x5` | 9"x5" MIGHTY HOOP PARA HAPPY 360 | 1583 | legacy+import: archivar copia import |
| 68 | 3187 | `NEO10` | BOBINA HILO POLYNEON 40 COLOR 1000M C/ | 1626 | legacy+import: archivar copia import |
| 69 | 2574 | `PISAIRDES` | PISTOLA AIRE | 1685 | ambos legacy: archivar id mayor |
| 70 | 3172 | `SBBD03005` | TORNILLO SUJECC. PRENSATELAS HAPPY HCD2/HCR2/HMF & HMG NOISE REDUCTION | 1202 | legacy+import: archivar copia import |
| 71 | 1772 | `SCH134KKD90` | AGUJAS 134KKD-90 | 1318 | ambos legacy: archivar id mayor |
| 72 | 3196 | `ST-M0404-HA360` | MIGHTYHOOP 4" X 4" ECO | 1725 | legacy+import: archivar copia import |
| 73 | 3193 | `ST-M0413-HA500` | MIGHTYHOOP 4" X 12,75" ECO | 1722 | legacy+import: archivar copia import |
| 74 | 968 | `ST-M0505-HA360` | MIGHTYHOOP 5,5&quot; ECO | 3157 | legacy+import: import con pedidos, archivar legacy |
| 75 | 3204 | `ST-M0505-W` | "MARCO" MIGHTYHOOP 5,5" ECO | 2206 | legacy+import: archivar copia import |
| 76 | 3194 | `ST-M0608-HA360` | MIGHTYHOOP 6" X 8" ECO | 1723 | legacy+import: archivar copia import |
| 77 | 3191 | `ST-M0803-HA360` | MIGHTYHOOP 7,75" X 2,75" ECO | 1719 | legacy+import: archivar copia import |
| 78 | 3231 | `ST-M0809-HA360` | MIGHTYHOOP 8,5 X 9" ECO | 2991 | legacy+import: archivar copia import |
| 79 | 3190 | `ST-M0813-HA500` | MIGHTYHOOP 8 X 13" ECO | 1716 | legacy+import: archivar copia import |
| 80 | 3230 | `ST-M0905-HA360` | MIGHTYHOOP 8,7 X 4,9" ECO | 2990 | legacy+import: archivar copia import |
| 81 | 3228 | `ST-M1010` | "MARCO" MIGHTYHOOP 10" ECO | 2950 | legacy+import: archivar copia import |
| 82 | 969 | `ST-M1010-HA360` | MIGHTYHOOP 10 X 10&quot; ECO | 3158 | legacy+import: import con pedidos, archivar legacy |
| 83 | 3159 | `ST-M1113-HA500` | MIGHTYHOOP 11 X 13" ECO | 970 | legacy+import: archivar copia import |
| 84 | 3226 | `ST-M1212-HA500` | MIDGHTYHOOP 12 X 12" ECO | 2948 | legacy+import: archivar copia import |
| 85 | 3220 | `ST-M1304-HA500` | MIGHTYHOOP 12,6" X 3,9" ECO | 2719 | legacy+import: archivar copia import |
| 86 | 3192 | `ST-M1316-HA500` | MIGHTYHOOP 12,5" X 15,5" ECO | 1721 | legacy+import: archivar copia import |
| 87 | 3227 | `ST-M1716-HA500` | MIGHTYHOOP 17 X 16" | 2949 | legacy+import: archivar copia import |
| 88 | 3195 | `ST-M6565-HA360` | MIGHTYHOOP 6,5" X 6,5" ECO | 1724 | legacy+import: archivar copia import |
| 89 | 3221 | `ST-M7272-HA500` | MIGHTYHOOP 7,25" ECO | 2720 | legacy+import: archivar copia import |
| 90 | 3224 | `TA-9A0802400012` | EXCENTRICO PARTIDO  PRENSATELAS (M) TAJIMA: (TFMX, TFMXII, TFGN,) | 2863 | legacy+import: archivar copia import |
| 91 | 3223 | `TA-9A0802500000` | EXCENTRICO PARTIDO  PRENSATELAS (F1) TAJIMA: (TFHX, TMFD, TFGN, TMLH-MIX, TMLHII | 2862 | legacy+import: archivar copia import |
| 92 | 3175 | `WPAA03000` | ARANDELA PRENSATELAS HAPPY HCD2/HCR2/HMF & HMG NOISE REDUCTION | 1281 | legacy+import: archivar copia import |
| 93 | 3213 | `hcd29582` | FRONT PANEL COVER 7" HAPPY | 2432 | legacy+import: archivar copia import |
| 94 | 3214 | `hcd29590` | REAR COVER PANEL 7" HAPPY | 2433 | legacy+import: archivar copia import |

## IDs archivados (lista plana)

```
1274, 3177, 3198, 3203, 2547, 2137, 3079, 3182, 3183, 3180, 3185, 3184, 3088, 3219, 3225, 2491, 3166, 3188, 424, 3212, 3207, 3206, 3208, 3205, 3083, 3130, 3222, 3178, 3168, 3163, 3165, 3164, 3162, 3209, 3217, 3043, 3189, 3197, 3167, 3137, 3170, 3169, 3211, 3146, 3176, 3210, 3154, 3153, 3144, 3145, 3148, 444, 3161, 3149, 3150, 3151, 3152, 3229, 3160, 3200, 3199, 3156, 456, 3201, 3173, 3035, 3186, 3187, 2574, 3172, 1772, 3196, 3193, 968, 3204, 3194, 3191, 3231, 3190, 3230, 3228, 969, 3159, 3226, 3220, 3192, 3227, 3195, 3221, 3224, 3223, 3175, 3213, 3214
```
