# Diagnóstico del proyecto `Analisis_Steam_Indie`

Fecha: 2026-11-11

## 1. Resumen ejecutivo

La API de Steam **sí funciona** desde esta máquina. El problema no es de red ni
de credenciales: son varios casos borde que hacen que un script "ingenuo" falle
en cuanto empieza a recorrer varios juegos.

Evidencia recogida ejecutando consultas reales:

| Prueba | Resultado |
|---|---|
| `store.steampowered.com/api/appdetails?appids=10&cc=cr` | **200 OK**, ~0.4 s |
| `appids=10,20,30` (varios ids en `appdetails`) | **400 Bad Request** |
| Juego gratis con `filters=price_overview` (Dota 2, appid 570) | `success: true`, `data: []` |
| Juego retirado (GTA V Legacy, appid 271590) | `success: true`, `data: []` (sin precio) |
| `api.steampowered.com/ISteamApps/GetAppList/v2/` | **404** (método retirado) |
| `api.steampowered.com/IStoreService/GetAppList/v1/` | **403** (requiere API key) |
| `api.steampowered.com/IStoreBrowseService/GetItems/v1/` | **200 OK sin API key**, hasta 200 appids por petición |
| Ráfagas de ~200 consultas a `appdetails` | **HTTP 429** y bloqueo temporal |
| Imprimir `"₡4.700"` en consola Windows (cp1252) | `UnicodeEncodeError: 'charmap' codec can't encode '\u20a1'` |

El archivo `steam_precios_reales.csv` tenía **solo 2 bytes (CRLF)**: el intento
anterior no llegó a escribir ni el encabezado. Todo apunta a que el script murió
con la primera respuesta "rara" de Steam (probablemente un juego gratis o
retirado).

Con el script nuevo (`steam_precios.py`) ya se descargaron los **19.582 juegos
indie** en **0,8 minutos**, sin errores:

| Estado | Juegos |
|---|---|
| `ok` (con precio) | 14.290 |
| `gratis` | 1.921 |
| `sin_precio` (retirado / no vendible) | 2.144 |
| `no_encontrado` (appid ya no existe en la tienda) | 1.227 |
| **Total** | **19.582** |

## 2. Estado del repositorio

```
analisis_indie.ipynb       0 bytes   (vacío, sin commits de análisis)
test_spark.py              48 líneas (ETL de engagement, funciona)
steam_precios.py           nuevo: descarga de precios (bulk + single)
steam_precios_reales.csv   19.582 filas de precios (UTF-8, 1,7 MB)
steam.csv                  27.075 juegos del catálogo
steam-200k.csv             200.000 filas de comportamiento de usuarios
steamspy_tag_data.csv      29.022 appids x ~400 tags
```

- El código que consumía las APIs **no está en ningún commit**. El compañero
  probablemente lo ejecutó en local y nunca lo subió.
- `.gitignore` estaba vacío y existía además un `.gitgnore` (typo) con las
  reglas. Ya se unificó en `.gitignore` y se eliminó el archivo mal nombrado.
- Los CSV se commitearon, se borraron y se volvieron a commitear: el historial
  pesa ~37 MB aunque ahora estén ignorados.

## 3. Causas del fallo con las APIs (por probabilidad)

### 3.1 `data` puede ser una lista, no un diccionario

Con `filters=price_overview`, Steam responde así para un juego gratis o retirado:

```json
{"570": {"success": true, "data": []}}
```

Cualquier código como `data["price_overview"]` o `data.get("price_overview")`
lanza `TypeError: list indices must be integers` /
`AttributeError: 'list' object has no attribute 'get'`. Es el error clásico y
explica que el CSV quedara vacío.

Sin `filters`, un juego gratis devuelve `is_free: true` y un juego retirado
devuelve un diccionario sin `price_overview`: hay que tratar los tres casos.

### 3.2 No se pueden pedir varios appids en una sola llamada a `appdetails`

`?appids=10,20,30` devuelve **400**. El endpoint público de la tienda solo
acepta un appid por petición. Para descargar 19.000 juegos así se necesitan
19.000 peticiones, con el coste y el rate limit que eso implica.

### 3.3 Rate limiting (HTTP 429)

`store.steampowered.com` tolera ~2-4 peticiones/segundo en secuencia, pero
bloquea ráfagas con varios hilos. En las pruebas, tras ~130-200 consultas
agresivas empezó a responder 429 de forma masiva (86 de 200). El bloqueo se
libera solo en ~1 minuto. Reintentar cada 1-2 s durante el bloqueo solo alarga
el problema; hay que esperar ~60 s o respetar `Retry-After`.

**Por eso el script nuevo usa el endpoint en lote** (sección 4), que reduce
19.582 peticiones a 196.

### 3.4 Encoding de Windows

`cc=cr` devuelve colones: `"final_formatted": "₡4.700"` (`\u20a1`). La consola
de Windows usa cp1252 y **no puede imprimir ese carácter**; el script muere con
`UnicodeEncodeError` aunque la descarga haya sido correcta. Lo mismo pasa con
tildes/ñ si la consola no está en UTF-8. Solución: escribir archivos en UTF-8 y
no imprimir símbolos de moneda en consola.

### 3.5 Endpoints "de tutorial" que ya no sirven

- `ISteamApps/GetAppList/v2/` → 404 (método eliminado).
- `IStoreService/GetAppList/v1/` → 403 sin API key.

Si el compañero siguió un tutorial que usaba `GetAppList` o la API oficial sin
key, ese es el origen del "no puedo entrar a las APIs". Para precios y detalles
**no se necesita key**: basta `store.steampowered.com/api/appdetails` o el
endpoint en lote que se describe abajo.

### 3.6 El intérprete de Python del proyecto está mal elegido

`python` en el PATH es **3.14**, pero `pyspark` y `pandas` están instalados en
**3.12**:

```
python test_spark.py       -> ModuleNotFoundError: No module named 'pyspark'
py -3.12 test_spark.py     -> funciona
```

VS Code está configurado con `ms-python.python:system`, así que toma el 3.14.
Hay que seleccionar Python 3.12 o ejecutar siempre con `py -3.12`.

## 4. Solución incluida: `steam_precios.py`

El hallazgo importante es `IStoreBrowseService/GetItems/v1`, el endpoint que
usa la propia tienda de Steam:

- Funciona **sin API key** desde `api.steampowered.com`.
- Acepta **hasta 200 appids por petición** (probado: 200 OK, 300 da 400 por
  longitud de URL).
- Devuelve nombre, `is_free`, `unlisted` y
  `best_purchase_option` con `final_price_in_cents`,
  `original_price_in_cents`, `discount_pct` y `formatted_final_price`.
- Con `--mode single` queda el `appdetails` clásico como respaldo.

Uso (con el entorno virtual instalado en la sección 7):

```powershell
# Catálogo indie completo (~19.600 juegos) en menos de 1 minuto
.\.venv\Scripts\python.exe steam_precios.py --only-indie

# Muestra / pruebas
.\.venv\Scripts\python.exe steam_precios.py --only-indie --limit 500

# Juegos concretos, precios en USD
.\.venv\Scripts\python.exe steam_precios.py --appids 10,570,730 --cc us

# Respaldo con el endpoint público (1 appid por petición, más lento)
.\.venv\Scripts\python.exe steam_precios.py --only-indie --mode single

# Si se corta, se vuelve a lanzar el mismo comando y continúa donde quedó
# (--force borra el archivo y descarga todo de nuevo)
```

Características:

- **Reanudable**: salta los appids que ya tienen estado definitivo.
- Distingue **precio**, **gratis**, **sin precio** (retirado/región) y
  **no encontrado**; no confunde "gratis" con "sin precio".
- Reintentos con backoff; en 429 espera 60 s (o `Retry-After`).
- Lotes de 100 appids y 2 hilos por defecto (ajustable con `--batch-size` y
  `--workers`).
- Escribe el CSV en **UTF-8 con BOM** (Excel lo abre bien) y hace `flush` por
  lote, así no se pierde nada si se interrumpe.
- Detecta la moneda de la región (`cc`), con tabla de respaldo.
- Logs solo ASCII, para no chocar con cp1252.

Salida (`steam_precios_reales.csv`):

```
appid,name,currency,initial,final,discount_percent,final_formatted,is_free,status,fetched_at
10,Counter-Strike,CRC,470000,470000,0,"₡4.700,00",False,ok,...
570,Dota 2,CRC,0,0,0,,True,gratis,...
271590,"Grand Theft Auto V Legacy",,,,,,False,sin_precio,...
99999999,,,,,,,False,no_encontrado,...
```

`final`/`initial` vienen en **centavos** (igual que la API). Para colones,
dividir entre 100: `470000` = ₡4.700,00.

**Validación**: se compararon 6 appids contra `appdetails` y el precio en
centavos coincide exactamente en todos (incluido un juego con 70 % de
descuento).

## 5. Hallazgos sobre los datos (para el análisis)

- `steam.csv`: 27.075 juegos, sin appids duplicados; 19.421 tienen género
  *Indie* (16.232 por tags; la unión usada por el script da 19.582). 2.560 son
  gratis; mediana de precio USD 4,79.
- `steam-200k.csv`: 129.511 filas `purchase` y 70.489 `play`. Las de compra
  traen `Hours_Played = 1.0`; si no se filtran, **influyen 129.511 horas
  falsas** en el total. `test_spark.py` ya filtra `Action == "play"`, bien.
- La quinta columna del 200k es siempre 0: se puede descartar.
- Solo **2.243 de 5.153 nombres** de juego del 200k coinciden exactamente con
  `steam.csv` (43 %). Para cruzar ambos datasets hay que normalizar nombres
  (minúsculas, quitar `™`/`®`, `:`, `-`, etc.) o usar el appid.
- `steamspy_tag_data.csv`: 29.022 appids, todos los de `steam.csv` están
  presentes; matriz muy dispersa (casi todo 0), conviene quedarse con los tags
  más frecuentes.
- `test_spark.py` calcula el top de horas de **todo** Steam; si el objetivo es
  *indie*, falta unir con `steam.csv` por nombre/appid y filtrar el género.
- Del catálogo indie, 2.144 juegos ya no tienen precio visible y 1.227 appids
  ya no existen en la tienda: hay que decidir si se excluyen o se marcan como
  `NA` en el análisis.

## 6. Recomendaciones

1. Usar `steam_precios.py` y commitearlo. El CSV de salida ya está en
   `.gitignore`, pero como se commiteó antes sigue trackeado; si no quieren
   versionarlo: `git rm --cached steam_precios_reales.csv`.
2. Ejecutar siempre con el entorno virtual (`.venv`, Python 3.12); el `python`
   del sistema es 3.14 y no tiene PySpark.
3. No usar `GetAppList`; para precios usar el endpoint en lote o `appdetails`.
4. Documentar en el notebook que `final` está en centavos y que la moneda es
   CRC (si se usa `--cc cr`).
5. Si se quiere aligerar el repo, sacar los CSV del historial con
   `git filter-repo` (coordinar con el equipo).
6. Reinstalar dependencias con `requirements.txt` dentro del venv.
7. Completar el notebook con el pipeline: catálogo → filtro indie → precios
   reales → engagement del 200k → unión por nombre normalizado.

## 7. Entorno instalado y bloqueo de red actual (2026-11-12)

### Entorno virtual

Se creó `.venv` con **Python 3.12.1** en la raíz del proyecto y se instalaron:

| Paquete | Versión |
|---|---|
| pyspark | 4.2.0 |
| pandas | 2.3.0 |
| ipykernel | 7.4.0 |
| numpy, py4j, etc. | dependencias |

Verificado:

```
.\.venv\Scripts\python.exe test_spark.py     -> funciona
.\.venv\Scripts\python.exe steam_precios.py  -> funciona (sujeto a la red)
```

VS Code quedó configurado para usar `.venv\Scripts\python.exe` y se registró
el kernel de Jupyter **"Python 3.12 (Steam Indie)"**.

Para activarlo en PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python test_spark.py
```

Para reinstalarlo desde cero en otra máquina:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Java 21 ya estaba instalado (requisito de PySpark).

### La red actual bloquea Steam

El 2026-11-12 la red empezó a devolver **HTTP 403** en todos los dominios de
Steam. El cuerpo de la respuesta es una página del firewall:

```
<title>Application Control Violation</title>
<h1>FortiGate Application Control</h1>
<h3>Application Blocked</h3>
<p>You have attempted to use an application that violates your Internet usage policy.</p>
```

Evidencia:

| Destino | Resultado |
|---|---|
| `store.steampowered.com` (con `curl -k`) | 403 FortiGate |
| `api.steampowered.com` | 403 FortiGate |
| `steamcommunity.com` | 403 FortiGate |
| `example.com`, `httpbin.org` | 200 OK |
| Python 3.12/3.14 sin `-k` | `SSL: CERTIFICATE_VERIFY_FAILED` (FortiGate intercepta TLS) |

No es una caída de Steam ni un problema del script: es una política de la red
(campus/ISP). `steam_precios.py` ahora detecta los 403 de FortiGate y los
errores de certificado, e imprime un aviso claro en vez de marcar los juegos
como "no encontrados".

**Consecuencia**: los precios no se pueden actualizar desde esta red. El
archivo `steam_precios_reales.csv` ya tiene los **19.582 juegos descargados el
11-11**, así que el análisis puede continuar sin conexión. Para regenerarlo o
actualizarlo, ejecutar el script desde un hotspot móvil, otra red sin ese
bloqueo o un VPN permitido por la institución.
