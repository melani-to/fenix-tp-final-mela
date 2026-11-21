# TP Final 1 — Sistema de Autorreparación de Infraestructura ("Proyecto Fénix")
### Tareas a realizar para resolverlo

> **Estado (2026-11-20): implementación completa.** Todos los `TODO`/`NotImplementedError`
> de la plantilla fueron resueltos (I1…I6), la matriz F01–F16 está automatizada y el
> entorno corre sobre LocalStack 4.14.0. Ver `README.md` §11 para esperado/obtenido.

Fuente: `issues/tpf.pdf` (impresión del enunciado de Notion).
Anexo reconstruido por rol: `issues/ANEXO.md` · Revisión del repo de referencia: `issues/REVISION_REPO_REFERENCIA.md`.

---

## 0. Qué hay que construir (resultado mínimo)

Circuito completo, ejecutable por API contra LocalStack:

```
alerta HTTP firmada (HMAC) → receptor FastAPI → SQS estándar (+DLQ) → worker
   → repara EC2 mock (detenido → running) o S3 (restaura etiquetas)
   → aviso a Telegram/Slack → ACK (delete_message) → ledger en data/events.json
```

Dos reparaciones pequeñas y verificables:
1. `node-01` (EC2 mock): `stopped → start_instances → comprobar running`.
2. `bucket-01` (S3): restaurar etiquetas obligatorias preservando las ajenas.

Invariantes: repetir el mismo evento o reiniciar el worker **no** repite acciones ya verificadas;
los fallidos reintentan con backoff y llegan a la DLQ.

---

## 1. Preparación (Hito 1)

- [ ] Descargar `fenix_template.zip` de Notion e instalar el esqueleto en `D:\yamil\tpf` (repo nuevo, Git propio).
- [ ] Elegir una versión de Python 3.11+ igual para todo el grupo.
- [ ] `python -m venv .venv` → activar → `pip install -e .` (PowerShell: `.venv\Scripts\Activate.ps1`).
- [ ] `cp .env.example .env` y generar `WEBHOOK_SECRET` con `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- [ ] Completar `requirements.txt`, `pyproject.toml` y generar `requirements-lock.txt` con `pip freeze`.
- [ ] Levantar LocalStack en Docker (`compose.yaml`), con endpoint explícito `http://localhost:4566`.
  - El helper de clientes debe **rechazar destinos fuera de loopback:4566** (nunca AWS real).
- [ ] Elegir proveedor de avisos: **Telegram** (BotFather → `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`) o **Slack** (`SLACK_WEBHOOK_URL`), solo uno, en `.env`.
- [ ] Definir la tabla de integrantes/roles en `README.md` (I1…I6, apellido/nombre, correo, módulos, pruebas defendidas).

## 2. Contratos y validación (I1) — Hito 1

- [ ] `src/fenix/utils/errors.py` y `validation.py`: excepciones del dominio (heredan de `Exception`), `identifier`, `utc_text`, helper `ttl_seconds`.
- [ ] `src/fenix/services/events.py` → `events.validate(evento, inventario)`:
  - [ ] JSON estricto, **todos** los campos obligatorios:
    `schema_version=1` (entero), `event_id`, `resource_id` (`[a-z0-9-]{3,32}`),
    `event_type` ∈ {`instance.stopped`, `bucket.tags_missing`}, `occurred_at` UTC explícito,
    `simulate_failures` entero 0..5 (**nunca bool**).
  - [ ] Rechazar: objeto incorrecto (p. ej. lista JSON), claves extra, claves ausentes,
    `event_type` como bool/entero/desconocido, `resource_id` ajeno al inventario → `ValidationError`.
  - [ ] Salida: copia validada del evento (no la original).
  - [ ] Respetar conflictos: mismo `event_id` con contenido distinto → registrar conflicto y
    conservarlo para DLQ **sin cambiar el primer registro**.
- [ ] Evidencia I1: 1 evento correcto + 3 rechazos **sin publicación en SQS**.
- [ ] Defensa: por qué una lista JSON no es un evento válido y por qué se revalida en el worker.

## 3. Mensajería e inventario cloud (I2) — Hito 2

- [ ] `src/fenix/services/sqs.py`:
  - [ ] `ensure_queues`: crear/reutilizar **DLQ + cola estándar** con `RedrivePolicy` (`maxReceiveCount=3`),
    conservar el `QueueUrl` devuelto por el SDK.
  - [ ] `retry_later`: backoff `demora = min(30, 2 ** numero_de_recepcion)` (2, 4, 8 s) vía
    `change_message_visibility` sobre el `ReceiptHandle` actual.
  - [ ] Long polling **10 s**, una entrega, `VisibilityTimeout=45`.
- [ ] `src/fenix/services/demo.py` → `setup_demo`:
  - [ ] Instancia mock EC2 con **SG exclusivo sin puertos de entrada**, bucket S3, etiquetados por grupo
    (`ManagedBy=ppa2`, `Project=fenix`, `GroupId=<grupo>`, `Owner=<grupo>`).
  - [ ] Guardar el inventario en `data/inventory.json` tras cada creación (IDs: `InstanceId`, `security_group_id`, `expected_tags`; `BucketName`).
  - [ ] Idempotencia: recuperar recursos existentes por `tags`/`ClientToken` (no duplicar).
- [ ] Evidencia I2: dos `init-queues` y dos `setup-demo` sin duplicados + lectura real de un mensaje en la DLQ.
- [ ] Defensa: `MessageId` vs `ReceiptHandle`, y qué es la visibilidad.

## 4. Webhooks y reparación (I3) — Hito 2

- [ ] `src/fenix/utils/crypto.py`: `HMAC-SHA256(secret, str(t).encode() + b"." + raw_body)`,
  cabecera `X-Webhook-Signature: t=<epoch>,v1=<64 hex>`, `hmac.compare_digest`, ventana **±300 s**,
  rechazar claves duplicadas y formato ambiguo.
- [ ] `src/fenix/services/receiver.py`:
  - [ ] Leer **raw bytes** y limitar tamaño a **16 KiB** (ya está en el esqueleto); faltan `verify`,
    errores HTTP y publicación confirmada.
  - [ ] Tabla de respuestas: 401 firma ausente/malformada/incorrecta/fuera de ventana ·
    413 cuerpo >16 KiB · 400 JSON roto/UTF-8 inválido con firma válida · 422 contrato incorrecto ·
    503 SQS no disponible (nunca prometer aceptación) · **202** con `event_id`/`message_id` solo tras
    `send_message` confirmado.
  - [ ] Boto3 es bloqueante → `run_in_threadpool` de FastAPI (sin bloquear el bucle).
- [ ] `src/fenix/services/remediation.py` → `repair`:
  - [ ] EC2: `describe` → `stopped` → `start_instances` → comprobar `running`.
  - [ ] S3: leer tags → mezclar `expected_tags` conservando las ajenas → escribir → verificar.
  - [ ] **Convergencia sobre estado deseado** (no `reboot`): consultar estado real antes y después.
  - [ ] Resolver recursos solo desde el inventario por alias (`node-01`, `bucket-01`); un ID externo en un evento **nunca** se repara.
- [ ] Evidencia I3: 401 con firma inválida + las dos reparaciones ejecutadas por API.

## 5. Observabilidad y comunicación (I4) — Hito 3

- [ ] Revisar `utils/logging_json.py` (entregado) y completar `services/notifications.py → notify` para **un** proveedor.
  - [ ] Telegram: POST JSON `chat_id`/`text`, exigir HTTP satisfactorio y `ok=true`, sin `parse_mode`.
  - [ ] Slack: POST `{"text": ...}`, validar HTTP 200 y cuerpo textual.
  - [ ] Timeouts: conexión 2 s / lectura 5 s. `Timeout`, conexión, 429 y 5xx → fallo transitorio
    (leer `Retry-After`, espera acotada ≤30 s); 400/401/403 → diagnóstico de configuración.
  - [ ] Un fallo de aviso **no borra el mensaje**: queda `notified` pendiente y se reintenta en la próxima recepción.
  - [ ] **Nunca** registrar tokens/URLs privadas (capturar la excepción y loguear solo tipo/contexto público).
- [ ] Asegurar que cada paso de I3 e I5 registra contexto (`event_id`, `message_id`, `resource_id`, `attempt`, `action`, `status`; no aplicables → `null`).
- [ ] Test de rotación con tamaño reducido que lea el `.gz`; sin logs de megabytes.
- [ ] Evidencia I4: líneas JSON correlacionadas, histórico comprimido y **un aviso real** en el canal elegido.
- [ ] Defensa: las dos colas distintas (SQS vs `queue.Queue`) y por qué una URL privada no entra en logs.

## 6. Integración y worker (I5) — Hito 2/3

- [ ] `src/fenix/services/worker.py` + `remediation.handle`, revisar `src/fenix/cli.py`.
- [ ] Algoritmo del worker (sección 5.2 del enunciado):
  - [ ] Recibir con long polling 10 s / 1 entrega / visibilidad 45 s; `sin Messages` **no** es error.
  - [ ] Fallo de transporte al consultar → registrar contexto seguro, esperar 2 s y reintentar (sin fabricar ACK).
  - [ ] Por mensaje, dentro de `try`: leer `Body` → `json.loads` → exigir objeto → **revalidar contrato**
    → resolver recursos desde inventario/manifiesto.
  - [ ] Ejecutar por etapas cortas y **persistir en disco antes de continuar**:
    `data/events.json` con `attempts`, `event_id`, `repair_done`, `repair_result`, `notified`, `tags`.
  - [ ] Solo cuando el handler completó todo → `delete_message(ReceiptHandle actual)` y registrar
    `status=acknowledged` **después** de confirmar el SDK.
  - [ ] Fallo → registrar tipo de excepción y `attempt`, conservar mensaje y backoff sobre el `ReceiptHandle`;
    si falla el cambio de visibilidad, seguir sin ACK.
  - [ ] La `RedrivePolicy` (no un `send_message` nuevo) deriva a DLQ con `maxReceiveCount=3`.
  - [ ] Modo `--once`: una consulta y terminar; Ctrl+C y cierre en `finally` controlado (sin ACK ahí).
- [ ] Defensa I5: ubicar el `ACK` y explicar una caída entre efecto externo y guardado local (F12).

## 7. Pruebas y diagnóstico (I6) — Hito 3

- [ ] Completar/ampliar `src/fenix/cli.py → inject_fault` y `tests/test_fenix.py` con la matriz completa.
- [ ] Stub de `notify` para simular 429 y fallo permanente sin enviar spam real; registrar qué funciones se probó.
- [ ] Verificar que ningún test borre recursos de otro grupo.
- [ ] Automatizar: **F04/F05/F06/F07, F09, F11/F12, F16**.
- [ ] Evidencia de integración sobre LocalStack: **F01/F02/F03/F08/F10/F13**.
- [ ] Evidencia final: ejecución reproducible + una falla que **efectivamente** llega a la DLQ (leer un mensaje real sin eliminarlo antes de documentarlo).

### Matriz mínima de pruebas (F01–F16)

| ID | Preparación | Esperado |
|---|---|---|
| F01 | `init-queues` + `setup-demo` dos veces | mismas colas/recursos, sin duplicados |
| F02 | nodo detenido + evento firmado válido | `running` verificado, ledger completo, aviso y ACK |
| F03 | bucket sin `Owner` | etiquetas restauradas y ajenas preservadas |
| F04 | firma ausente/alterada y timestamp de hace 1 h | 401 y ningún mensaje nuevo |
| F05 | mismos datos JSON con bytes distintos tras firmar | 401 por HMAC inválida |
| F06 | JSON roto firmado; luego lista JSON firmada | 400 y 422, sin publicación |
| F07 | alias ajeno y claves extra | rechazo de esquema, sin cambios cloud |
| F08 | JSON corrupto inyectado directo a SQS | el worker sigue; mensaje termina en DLQ |
| F09 | `simulate_failures=2`, ID nuevo | backoff 2 y 4; después reparación y ACK |
| F10 | falla persistente, ID nuevo | sin ACK; derivación automática a DLQ |
| F11 | repetir evento completo y reiniciar worker | sin nueva reparación ni aviso |
| F12 | cortar tras `repair_done` y antes de `notify` | al reiniciar se retoma solo el aviso |
| F13 | SQS detenido al recibir webhook | 503, sin aceptación falsa |
| F14 | mock de aviso 429/timeout y luego éxito | sin repetir reparación; aviso reintentado |
| F15 | cambiar payload conservando `event_id` | conflicto; ledger y recursos intactos |
| F16 | forzar rotación, abrir `.gz`, parsear cada línea | JSON válido, UTC, contexto, sin secretos |

## 8. Condiciones técnicas obligatorias (revisión final)

- [ ] Funciona **por API** con LocalStack (no imprimir que ocurrió).
- [ ] Sin `BaseException`, sin `except: pass`, sin registrar éxito tras un fallo.
- [ ] Sin `return`/`break`/`continue` en `finally`.
- [ ] Toda operación de red con esperas acotadas; cierre controlado con código 1 solo en error de arranque;
      un mensaje corrupto no tumba el worker.
- [ ] Formateador con `event_id, message_id, resource_id, attempt, action, status` (`null` si no aplica);
      demostrar DEBUG/INFO/WARNING/ERROR/CRITICAL con casos o fixtures (sin elevar fallos normales a CRITICAL).
- [ ] `logger` no serializa árboles completos de `ExceptionGroup`.
- [ ] Secretos fuera de Git (`.env`, `.venv`, `tokens/`, `data/`, `logs/` ignorados).

## 9. Entregables (Hito 4)

- [ ] Repo público con: `src/`, `tests/`, `examples/`, `config/`, `README.md`, `requirements.txt`,
      `requirements-lock.txt`, `pyproject.toml`, `compose.yaml`, `.env.example`, `.gitignore`.
- [ ] `README.md` con las 18 secciones pedidas (objetivo, tabla de integrantes, prerrequisitos/versiones,
      instalación desde cero, variables de entorno, diagrama Mermaid, contratos, ejecución por terminal,
      reglas de reintento/DLQ, idempotencia, matriz con esperado/obtenido, limitaciones, evidencia del canal,
      limpieza, video con marcas por integrante, declaración de IA/ayuda externa).
- [ ] Por cada prueba: ID, fecha, comando/pytest, datos anonimizados, esperado, observado, evidencia y responsable.
- [ ] Adjuntar: 1 log nominal JSON, 1 log de fallo, 1 `.gz` legible, resultado final verificado con Boto3.
- [ ] Prueba de reproducibilidad: otro integrante clona en carpeta nueva, arma su `.env` y sigue **solo** el README.
- [ ] Limpieza del laboratorio: terminar instancia, vaciar/borrar bucket, eliminar SG (solo IDs inventariados);
      destruir colas recién después de documentar la DLQ.
- [ ] Video grupal (30–40 min, 5–7 min por persona) + defensa oral individual con modificación en vivo.
- [ ] Declarar qué se usó de IA, qué se modificó y cómo se verificó.

---

## 10. Reutilización desde `D:\yamil\vulcano_engine` (TP-2)

| Pieza de Vulcano | Uso en Fénix | Ajuste necesario |
|---|---|---|
| `webhook_crypto.py` (HMAC sobre raw bytes + ventana) | base de `fenix/utils/crypto.py` | ventana **±300 s** y formato `t=...,v1=...` del enunciado |
| `webhook_receiver.py` (FastAPI, 202, `request.body()`) | base de `services/receiver.py` | añadir 413/422/503, límite 16 KiB, `send_message` confirmado antes del 202 |
| `core_sqs.py` (clientes → :4566, producer/consumer, DLQ) | base de `services/sqs.py` | `maxReceiveCount=3`, visibilidad 45, long polling 10, backoff `min(30, 2**n)` |
| `logging_engine.py` (QueueHandler/Listener, JSON, gzip) | base de `utils/logging_json.py` | campos del formateador (`action`, `status`, …) y archivo de log por proceso |
| `sanitizer.py` (argparse fail-fast) | apoyo para `cli.py` | comandos: `init-queues`, `setup-demo`, `worker`, `send`, `fault`, `inject_fault` |
| `exceptions.py` + `except*` / `ExceptionGroup` | patrón para `utils/errors.py` | sin cambios (heredan de `Exception`) |
| `scripts/setup_queues.py` + `docker-compose.yml` | arranque de LocalStack y colas | añadir S3/EC2 mock y tags de grupo |
| `tests/test_chaos.py` (20) | patrones para F04–F07 | adaptar contrato Fénix |
| `tests/test_forensic_log.py` (16) | base de F16 (rotación gzip forense) | sin cambios grandes |
| `scripts/mock_destination.py` / `send_test_event.py` | base del stub de `notify` (429/5xx) y del `cli send` | firmar con el HMAC de Fénix |

**Diferencias conceptuales a tener en cuenta:** Fénix agrega S3 + EC2 mock, inventario/manifiesto,
ledger `data/events.json` con etapas, notificador Telegram/Slack, aliases conocidos, 422/503, y
reparación **convergente** e idempotente (Vulcano solo re-emitía el webhook).
