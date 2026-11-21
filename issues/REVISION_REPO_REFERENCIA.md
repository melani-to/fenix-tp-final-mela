# Revisión del repo de referencia: `agustinalomoro6/Proyecto_Fenix`

Clonado en: `%LOCALAPPDATA%\Temp\opencode\Proyecto_Fenix`
Ejecutado en Windows con Python 3.13: `pip install -r requirements.txt && pip install -e . && pytest -q`

**Resultado de la suite: `3 failed, 194 passed`**

---

## Veredicto

Base **muy buena** (≈80 % de la consigna) pero **no está terminado ni "correcto" tal cual**:
hay una función rota del flujo nominal, tres pruebas en rojo, cero evidencia real sobre
LocalStack y varios entregables obligatorios ausentes. Sirve como referencia, no como plantilla
para copiar tal cual.

---

## ✅ Lo que está bien hecho (cumple la consigna)

| Área | Detalle verificado |
|---|---|
| **HMAC** (`utils/crypto.py`) | `HMAC-SHA256(str(t)+b"."+raw_body)`, cabecera `t=..,v1=..` estricta (exactamente 2 claves, sin duplicadas), `v1` 64 hex minúsculas, ventana `abs(now-t) <= 300`, `compare_digest`, errores sin secretos, secreto ≥16 chars |
| **Receptor** (`services/receiver.py`) | Lee **raw bytes** → 413 >16 KiB → 401 (antes de parsear) → 400 JSON roto → 422 contrato/inventario → **202 sólo después de `send_message` confirmado**; `run_in_threadpool` para disco y Boto3; 503 si SQS falla (nunca "accepted") |
| **Contrato** (`services/events.py`) | Campos exactos (`set(event) != FIELDS`), `schema_version` con `type(...) is not int` (rechaza `True`), `simulate_failures` 0–5 no-bool, `event_type` cerrado a los 2 valores, alias resueltos sólo contra inventario y con tipo coincidente, devuelve copia |
| **SQS** (`services/sqs.py`) | DLQ primero + `RedrivePolicy maxReceiveCount="3"`, cola con `VisibilityTimeout=45` y retención 345600, `receive` con `MaxNumberOfMessages=1` y `WaitTimeSeconds=10`, `retry_later = min(30, 2**ApproximateReceiveCount)` (2, 4, 8…), ACK con `ReceiptHandle` |
| **Worker** (`services/worker.py`) | Parseo y validación **por mensaje dentro de `try`** (un corrupto no tumba el worker), handler → `acknowledge` → log recién después del SDK, fallo ⇒ log sólo del tipo de excepción + `retry_later`, si falla el cambio de visibilidad sigue **sin ACK**, `NotImplementedError` se propaga, fallo de transporte ⇒ log + `sleep(2)`, `--once`, `finally` sin `return/break/continue` |
| **Reparación** (`remediation.py`) | Convergente: `describe_instances` antes y después, `start_instances` (nunca `reboot`), estados `pending/stopping` ⇒ retryable, `terminated` ⇒ permanente; S3 lee `get_bucket_tagging` (sólo `NoSuchTagSet` = vacío), mezcla `{**actual, **esperado}`, relee para verificar; `classify()` separa throttling/5xx (retry) de otros (permanent) |
| **Ledger / idempotencia** | `attempts` persistido **antes** de simular la falla; `repair_done` y `notified` persistidos por etapa; duplicado completo ⇒ `return` sin efectos; contenido distinto con mismo `event_id` ⇒ `PermanentError` sin tocar el ledger; reaviso sin repetir reparación; escritura atómica con `os.replace` y JSON corrupto que **no** se pisa |
| **Notificador** (`notifications.py`) | `timeout=(2,5)`, 429 ⇒ transitorio con `Retry-After` acotado, 5xx ⇒ transitorio, resto 4xx ⇒ permanente, Telegram exige `ok is True`, Slack exige cuerpo `ok`, `from None` para no filtrar la URL con token, modo `console` para desarrollo |
| **Logger** (`logging_json.py`) | Formateador con lista cerrada `event_id, message_id, resource_id, attempt, action, status` **rellenados con `None` si no aplican**, UTC, `QueueHandler/QueueListener`, rotación 2 MB + `gzip`, un archivo por proceso (`nombre-pid`) |
| **Clientes** (`config/settings.py`) | Rechaza cualquier endpoint que no sea `http://localhost:4566` (sin credenciales, usuario ni puerto distintos), exige las 3 variables, timeouts `connect=2/read=15` |
| **CLI** | `init-queues / setup-demo / fault / send / worker --once`, firma bytes crudos sin reserializar, `finally` sólo con `listener.stop()`, error controlado ⇒ código 1, argparse ⇒ 2 |
| **Tests unitarios** | 194 pasan: crypto (ventana, duplicados, fugas), receiver (401/400/413/422/503/202), events, handle (duplicados, conflictos, fallos simulados, ledger corrupto, aviso corto), remediation (convergencia, estados), worker (ACK ordenado, poison, fallo de receive), notifications (429/5xx/tokens) |

---

## ❌ Hallazgos — bloqueantes

### B1. `cli fault` está roto: `inject_fault` sigue en `NotImplementedError`
`src/fenix/services/demo.py:230` → `raise NotImplementedError("I6-A: inyectar y comprobar fallo")`
y `cli.py:50` lo llama directamente. **El flujo nominal de la sección 5.6**
(`python -m fenix.cli fault node-01`) falla, y con él F02/F03 (nodo detenido / bucket sin `Owner`).

### B2. La suite queda en rojo: 3 pruebas fallan
```
FAILED tests/test_scenarios.py::test_duplicate_is_idempotent        (I6-B)
FAILED tests/test_scenarios.py::test_ack_is_after_success           (I6-C)
FAILED tests/test_scenarios.py::test_redrive_with_localstack        (I6-D)
3 failed, 194 passed
```
Son exactamente las pruebas de I6: duplicados, orden del ACK y redrive real.

### B3. Cero evidencia de integración sobre LocalStack
Todas las pruebas usan mocks/monkeypatch. `test_redrive_with_localstack` es un stub.
La consigna exige **evidencia real sobre LocalStack para F01, F02, F03, F08, F10 y F13** — no existe.

---

## ⚠️ Hallazgos — requisitos de la consigna faltantes

| # | Requisito (consigna) | Estado en el repo |
|---|---|---|
| 1 | §5.2.5 *"Registrar `status=acknowledged` después de que el SDK confirme"* | `worker.py:46` loguea `action="ack"` **sin campo `status`** → el formateador emite `status: null` |
| 2 | §3.2.5 ledger con `event_id`, `repair_result`, `tags` | Guarda `result` (no `repair_result`), **no guarda `tags`**, y `event_id` sólo como clave externa del dict, no como campo |
| 3 | §5.4/I4 *"test que fuerce rotación con tamaño reducido y lea el .gz"* | **No existe** (F16). `maxBytes` está fijo en 2 MB |
| 4 | §4.2.6 demostrar `DEBUG` y `CRITICAL` | **No se usan en ningún archivo** ni en tests (sólo INFO/WARNING/ERROR) |
| 5 | §3.3 *"registrar conflicto"* al reutilizar `event_id` | Sólo lanza `PermanentError`; el log queda genérico (`fallo_procesando / status=PermanentError`), sin mensaje de conflicto ni verificación de que el ledger original siga intacto |
| 6 | §5.3.2.b espera acotada *"hasta 30 segundos"* | `_retry_after` acota a **60 s** (y sólo se informa, no se espera) |
| 7 | F13 requiere evidencia de integración | `test_receiver` lo resuelve con monkeypatch (503 simulado), no con SQS real caído |

---

## 📦 Hallazgos — entregables y documentación (§5.8)

- **Falta `requirements-lock.txt`** (`pip freeze`) — obligatorio.
- **README incompleto**: tiene 10 secciones contra las 18 exigidas. Falta:
  diagrama **Mermaid**, contratos de entrada/salida, ejecución por terminal completa,
  **reglas de reintento y DLQ**, decisiones de idempotencia,
  **matriz de pruebas F01–F16 con esperado y obtenido**, evidencia del canal elegido
  (Telegram/Slack real), **limpieza del laboratorio**, vínculo al video con marcas por
  integrante y **declaración de ayuda externa e IA**.
- **`.gitignore` no ignora `tokens/`** (exigido en §5.8.1.a junto con `.env`, `data/`, `logs/`).
- **No hay carpeta `evidencias/`** con: log nominal JSON, log de fallo, `.gz` legible y
  resultado final consultado con Boto3.
- `examples/*.json` con `occurred_at: "2026-10-22T18:00:00Z"` (la consigna pide actualizarlo
  en cada ensayo; la CLI siempre firma con timestamp nuevo — ok, pero la evidencia debe mostrarlo).
- `NOTIFY_PROVIDER=console` por defecto: falta al menos **un envío real** al canal elegido.

---

## 🧭 Cómo usar este repo desde tu proyecto

1. **Tomarlo como referencia de estilo y lógica**, no clonarlo tal cual: tu entrega debe poder
   explicarse individualmente (§2.2 permite IA si se declara y si cada integrante modifica/explica su código).
2. Si adoptás código, adaptalo a **tu** plantilla: los IDs de TODO (`I1-A … I6-D`) de tu
   `fenix_template.zip` son los mismos que usan, así que la correspondencia es directa
   (ver `issues/ANEXO.md`).
3. Corregí antes que nada los **3 bloqueantes** (B1–B3): sin `inject_fault`, sin suite verde y
  sin pruebas sobre LocalStack no se puede mostrar el circuito completo.
4. Completá los 7 requisitos faltantes de la tabla y los entregables de §5.8.

### Orden sugerido
1. `inject_fault` (I6-A) + tests I6-B/C → suite verde.
2. Evidencia real sobre LocalStack: F01, F02, F03, F08, F10, F13.
3. `status=acknowledged`, campos del ledger (`repair_result`, `tags`), conflicto registrado.
4. Test F16 de rotación gzip + demostración DEBUG/CRITICAL.
5. `requirements-lock.txt`, `.gitignore` (`tokens/`), README completo con Mermaid y matriz F01–F16.
6. Un envío real a Telegram/Slack + `evidencias/`.
