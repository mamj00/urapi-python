# URAPI Python SDK 0.0.1

Cliente de servidor para Python 3.11+, sin dependencias de runtime. Incluye un
catálogo generado de las 45 operaciones del contrato público; esto no acredita
que todos esos endpoints estén implementados o activos. No está publicado en PyPI.

## Instalar y usar

Tras construir el wheel local con los comandos de abajo, dentro del entorno virtual de tu servidor:

```sh
python -m pip install --no-index --no-deps /ruta/urapi_sdk-0.0.1-py3-none-any.whl
```

La clave API vive sólo en el servidor. Configura `URAPI_BASE_URL` con el origen
HTTPS verificado de tu API y `URAPI_API_KEY` con una clave del proyecto y scopes
necesarios. El siguiente ejemplo realiza **una** admisión. Antes de ejecutarlo,
persiste `operation_id`, cuerpo exacto, proyecto e `URAPI_INTENT` en tu registro
duradero de la operación. El SDK no genera ni persiste esa intención por ti.

```python
import os
from urapi import UrapiClient, UrapiApiError, UrapiClientError

client = UrapiClient(
    base_url=os.environ["URAPI_BASE_URL"],
    api_key=os.environ["URAPI_API_KEY"],
)
body = {
    "text": "Primer resultado TEST",
    "format": "png",
    "max_credits": "1.000",  # Techo autorizado del ejemplo, no precio.
    "retention_seconds": 3600,
}
try:
    result = client.request("createQR", {
        "body": body,
        "idempotency_key": os.environ["URAPI_INTENT"],
    })
    job_id = result.data["id"]
    # Persistir job_id en el registro duradero antes de continuar.
    print(job_id)  # No imprimir result.data completo ni enlaces privados.
except UrapiApiError as error:
    print(error.code, error.status, error.request_id, error.outcome_unknown)
except UrapiClientError as error:
    print(error.code, error.outcome_unknown)
```

Requiere `qr:write`, saldo y procesamiento activos. Para PDF usa `createPDF`,
scope `pdf:write` y este cuerpo, con **otra intención persistida**:

```python
body = {
    "source": {"type": "html", "html": "<h1>Primer PDF TEST</h1>"},
    "max_credits": "1.000",
    "retention_seconds": 3600,
}
result = client.request("createPDF", {
    "body": body,
    "idempotency_key": os.environ["URAPI_PDF_INTENT"],
})
```

Después de persistir el ID consulta explícitamente el trabajo con `jobs:read`:

```python
job = client.request("getJob", {"path": {"job_id": saved_job_id}}).data
```

Desde 0.0.1 puedes esperar explícitamente sobre ese **mismo ID persistido**:

```python
from urapi import wait_for_job, UrapiWaitError

try:
    final = wait_for_job(client, saved_job_id,
                         timeout_seconds=60, poll_interval_seconds=1, max_polls=60)
    print(final.data["state"])
except UrapiWaitError as error:
    print(error.code, error.polls, error.last_state)
    # Conservar saved_job_id; una futura espera consulta el mismo ID.
```

El helper sólo llama `getJob`: no cancela, crea ni reintenta peticiones fallidas.
Devuelve los estados terminales `succeeded`, `failed`, `cancelled`, `timed_out`;
`queued`, `running` y `retry_wait` continúan la espera. Comprueba status 200,
identidad exacta y estado conocido, sin validar el esquema completo de Job.
Los errores API/transporte detienen la espera y conservan su tipo, salvo que al
volver de la petición ya se haya agotado el presupuesto o cancelado la espera.

Opciones: `timeout_seconds` 0.01–600 (predeterminado 60),
`poll_interval_seconds` 0.001–60 (1), `max_polls` entero 1–10000 (60).
El presupuesto usa reloj monotónico y se comprueba antes/después de GET y sueño.
Cada GET recibe un timeout de socket `min(presupuesto_restante, 120)` segundos,
que reemplaza el timeout predeterminado del cliente para esa consulta. Si quedan
menos de 10 ms se detiene sin otra petición. No acorta el intervalo entre GET
para introducir una última consulta antes de caducar.

Opcionalmente pasa `cancel_event=threading.Event()`; otro hilo puede hacer
`event.set()` para interrumpir el sueño o detenerse al volver del GET. **No
interrumpe DNS ni I/O activo** y el presupuesto no es un deadline duro de red.
Un resultado recibido después del presupuesto se descarta como `wait_timeout`.
`UrapiWaitError` expone sólo código fijo, número de GET iniciados (`polls`) y
último estado válido (`last_state`), sin incluir ID, respuesta o credenciales.
Códigos: `invalid_wait_options`, `invalid_job_id`, `invalid_job_response`,
`wait_timeout`, `wait_aborted`, `poll_limit_exceeded`.

Tu orquestador debe conservar el ID tras un timeout. Un trabajo fallido
o cancelado es un resultado terminal, no una invitación a repetir admisiones.
Si la respuesta de admisión se pierde (`outcome_unknown=True`), conserva cuerpo
e intención; no inventes otra clave para el mismo trabajo. La retención del
mapping de idempotencia no es eterna: aplica el contrato y tu registro duradero.

`getFileDownload` devuelve metadatos de un enlace privado; este cliente no sigue
ese enlace ni descarga binarios. Usa un cliente separado, HTTPS validado, sin
clave API ni cookies y sin redirects. No registres el enlace.

Para el perfil sintético local exclusivamente, permite HTTP explícitamente:

```python
client = UrapiClient(
    base_url="http://127.0.0.1:4100",
    api_key=os.environ["URAPI_API_KEY"],  # Sólo formato ur_test_… válido.
    allow_http_loopback_for_tests=True,
)
```

Esto no arranca servicios ni acredita su disponibilidad. `localhost`, otras IP,
claves LIVE y HTTP sin opt-in se rechazan. HTTPS usa el almacén de confianza de
Python y validación de hostname; no existe opción de desactivar TLS.

## Interfaz y límites

`request(operation_id, input=None, options=None)` devuelve `ApiResult` con
`data`, `status`, `request_id`. El `repr` del resultado omite `data`; el contenido
sigue siendo privado y accesible por el consumidor. `input` admite sólo `path`,
`query`, `body`, `idempotency_key`; `options` sólo `timeout_seconds`.

- Rutas y parámetros proceden del manifiesto; no hay URL, headers o transporte
  arbitrarios por petición. Rechaza parámetros desconocidos, tipos incorrectos,
  límites/enum/pattern, UUID inválido y segmentos punto. `date-time` se transmite
  como texto y su validación semántica corresponde al backend.
- JSON de petición: tipos estándar, claves string, números finitos dentro del
  rango seguro ±(2^53−1), profundidad máxima 64, máximo 100000 nodos; sin ciclos.
  No genera modelos de los 73 esquemas ni valida completamente cuerpos o
  respuestas contra JSON Schema. El backend autoriza tenant/scopes/entorno y
  valida contenido, cuotas y reglas del recurso en cada llamada.
- Éxito: status declarado, MIME JSON y objeto JSON; lectura UTF-8 acotada,
  números no finitos y cuerpos truncados rechazados. No descomprime respuestas.
- Predeterminados: timeout de socket 10 s, petición 1 MiB, respuesta 2 MiB
  (también en errores). Configurables: 0.01–120 s, petición 256 B–8 MiB,
  respuesta 256 B–64 MiB. El timeout es por operación de socket: **no** es un
  deadline total; DNS y un servidor que envía lentamente pueden durar más.
- Conexión directa por llamada: sin proxies de entorno, redirects, cookies,
  reintentos, descarga firmada ni generación implícita de idempotencia.
- `UrapiApiError`: HTTP >=400, código/request_id saneados, status y
  `retry_after_seconds` sólo para Retry-After numérico (máximo 86400).
  `UrapiClientError`: configuración, entrada, tamaño, transporte, timeout o
  respuesta inválida. Mensajes/cadenas no contienen cuerpos ni causas crudas.
  `outcome_unknown` es conservador para mutaciones con transporte o respuesta
  inválidos, 408/409/5xx; no demuestra que el servidor admitió una operación.
  No vuelques atributos internos del cliente ni locales de traceback.

## Generación, pruebas y wheel

Desde la raíz del repositorio, usando un Python local con setuptools 84.0.0 y
wheel 0.48.0 ya disponibles (no se descargan dependencias):

```sh
python scripts/generate_sdk_python.py --check
python -m unittest discover -s tests -v
python -m pip wheel --no-index --no-build-isolation --no-deps . --wheel-dir dist
python tests/wheel_consumer.py dist/urapi_sdk-0.0.1-py3-none-any.whl
```

`--write` crea el manifiesto ausente. Reemplazar contenido diferente requiere
`--write SHA256_ACTUAL_DEL_MANIFIESTO`; conserva copia anterior y rechaza cambios
concurrentes o enlaces/junctions. No modifica el contrato. `--check` compara el
manifiesto completo con la fuente canónica. El marcador Private :: Do Not Upload impide publicación accidental en PyPI; este repositorio contiene fuente pública de preproducción.

Las pruebas usan servidores HTTP efímeros en 127.0.0.1 y claves sintéticas; el
consumidor instala el wheel en un directorio temporal aislado sin red de paquete.
No prueban autenticación real, PostgreSQL, procesamiento o disponibilidad de los
45 endpoints. El consumidor 0.0.1 comprueba GET del mismo ID desde `queued` hasta
`succeeded`. El wheel 0.0.0 anterior se conserva intacto; no contiene este helper.
Webhooks, modelos completos y distribución siguen pendientes.

## Distribución y licencia

Fuente de preproducción; publicar este repositorio no implica que el producto esté completo. No se ha publicado en npm/PyPI. No se añade una licencia de software: el titular no ha seleccionado una. Los tests usan datos sintéticos. No incluir credenciales en ejemplos, issues o commits.
