# Anexo de código por integrante (reconstruido)

> **Nota de procedencia:** la página de Notion **no contiene la sección "Anexo de código por
> integrante"**. Recorrí el árbol completo de bloques vía API pública (`loadPageChunk`, 373 bloques,
> 4 chunks): el documento termina en *5.9. Referencias*, sin secciones colapsadas (no hay toggles)
> y sin páginas hijas con ese nombre. Tampoco está en `doc/fenix_template.zip`. Lo único que existe
> hoy son las **etiquetas `TODO I#-X` repartidas en el código**, que equivalen al anexo. Esta tabla
> es esa información reconstruida y verificada contra los archivos reales.

## 1. Plantillas por rol (archivos → TODO → dependencias)

| Rol | Archivo(s) | TODO | Qué hay que completar |
|---|---|---|---|
| **I1** Entradas y Excepciones | `src/fenix/utils/validation.py` | `I1-A` | `identifier()`: `isinstance(str)` + `re.fullmatch(r"[a-z0-9-]{3,32}")` |
| | | `I1-B` | `ttl_seconds()`: `int(...)` capturando `ValueError` con `raise ... from` |
| | | `I1-C` | `utc_text()`: `fromisoformat` reemplazando `Z` por `+00:00` |
| | `src/fenix/services/events.py` | `I1-D` | `validate()`: exigir `dict`, campos exactos, `schema_version` entero == 1, alias del inventario |
| | `src/fenix/utils/errors.py` | (revisar) | Excepciones del dominio: cuándo usar cada tipo (apoyo entregado) |
| **I2** Mensajería e Inventario | `src/fenix/services/sqs.py` | `I2-A` | `ensure_queues()`: `{prefix}-events` con `VisibilityTimeout="45"`, retención `345600`, `RedrivePolicy` → DLQ (`maxReceiveCount=3`) |
| | | `I2-B` | `retry_later()`: leer `ApproximateReceiveCount` → `min(30, 2**n)` vía `change_message_visibility` |
| | `src/fenix/services/demo.py` | `I2-C` | `setup_demo()`: validar `group`, `describe_vpcs` (VPC+AMI), `create_instance`/`create_group`/`create_bucket` de `cloud_support`, tags de grupo, guardar `data/inventory.json` |
| | `src/fenix/config/settings.py` | (apoyo) | Cliente Boto3 en loopback:4566 + `WEBHOOK_SECRET` — revisión de I2 |
| **I3** Webhooks y Reparación | `src/fenix/utils/crypto.py` | `I3-A` | `verify()`: partir por comas, exigir exactamente `t` y `v1` una sola vez, `compare_digest`, ventana ±300 s |
| | `src/fenix/services/receiver.py` | `I3-B` | `webhook()`: verificar HMAC sobre raw bytes → 401/413/400/422 → `publish` → 202 (vía `run_in_threadpool`) |
| | `src/fenix/services/remediation.py` | `I3-C` | `repair()`: EC2 `describe_instances` (estado real) y S3 `get_bucket_tagging` (solo `NoSuchTagSet` = vacío) + mezcla/escritura/verificación |
| | `src/fenix/services/cloud_support.py` | (apoyo) | Las 5 recetas Boto3 — ya implementadas, solo se usan |
| **I4** Observabilidad y Comunicación | `src/fenix/services/notifications.py` | `I4-A` | `notify()`: Telegram `sendMessage` (exigir `ok=true`) **o** Slack `{"text"}` (HTTP 200 + cuerpo), timeouts 2 s/5 s |
| | `src/fenix/utils/logging_json.py` | (revisar) | Logger con `QueueHandler`/`QueueListener`, formato con `event_id, message_id, resource_id, attempt, action, status`, rotación gzip |
| **I5** Integración y Worker | `src/fenix/services/worker.py` | `I5-A` | `run()`: algoritmo completo (long polling 10 s, `try` por mensaje, revalidar, etapas, ACK final, backoff, `--once`) |
| | `src/fenix/services/remediation.py` | `I5-B` | `handle()`: cargar inventario + ledger `data/events.json`, orden de etapas `repair_done → notified`, persistir antes de continuar |
| | `src/fenix/cli.py` | (revisar) | Comandos: `init-queues`, `setup-demo`, `fault`, `worker`, `send` |
| **I6** Pruebas y Diagnóstico | `src/fenix/services/demo.py` | `I6-A` | `inject_fault()`: resolver alias desde inventario, jamás `InstanceId` libre, `simulate_failures` |
| | `tests/test_scenarios.py` | `I6-B` | `test_duplicate_is_idempotent` (F11): inventario en `tmp_path` + evento fijo |
| | | `I6-C` | `test_ack_is_after_success` (F12): monkeypatch de `receive` con `ReceiptHandle` fijo |
| | | `I6-D` | `test_redrive_with_localstack` (F08/F10): JSON corrupto real → DLQ (marcado `integracion`) |
| | `tests/test_contracts.py` | (completar) | `identifier` / `ttl` / `utc` (marcados `pendiente`) — pruebas de I1 |
| | `tests/test_fenix.py` | (completar) | `test_signature_authenticates_raw_bytes` (F05) — pruebas de I3 |

## 2. Dependencias principales por rol

```
I1  → utils/errors.py (exceptiones) · services/events.py → utils/validation.py · inventario (I2)
I2  → services/sqs.py → config/settings.client · services/demo.py → services/cloud_support.py
I3  → utils/crypto.py · services/receiver.py → utils/crypto.py + services/sqs.publish
      services/remediation.repair → services/cloud_support.py + inventario (I2)
I4  → services/notifications.py · utils/logging_json.py (consumido por I3 e I5)
I5  → services/worker.py → services/sqs.receive/acknowledge/retry_later + remediation.handle
      remediation.handle → utils/state.py (ledger) · services/notifications.notify
I6  → services/demo.inject_fault · tests/* · matrix F01–F16
```

Todos dependen de `config/settings.py` (cliente loopback y secreto) y `utils/state.py`
(lectura/escritura atómica del ledger, apoyo entregado).

## 3. Marcas de pytest (para saber qué corre sin LocalStack)

| Marca | Significado | Archivos |
|---|---|---|
| `base` | apoyo ya implementado (debe pasar siempre) | `tests/test_base.py` |
| `pendiente` | contratos que completa el alumnado (falla hasta resolver TODOs) | `test_contracts.py`, `test_fenix.py`, `test_scenarios.py` |
| `integracion` | requiere LocalStack corriendo | `test_scenarios.py::test_redrive_with_localstack` |

```powershell
python -m pytest -m base -q        # apoyo
python -m pytest -m pendiente -q   # debe fallar al inicio
python -m pytest -m integracion -q # con LocalStack levantado
```

## 4. Avisos sobre el material

- `README.md` de la plantilla dice *"Leer Guia.md"* pero **`Guia.md` no existe** en el zip: la guía
  es el enunciado (`issues/tpf.pdf`). Reemplazá ese README por el definitivo del equipo.
- `doc/fenix` quedó vacío (la extracción fue directa a la raíz de `tpf/`); se puede borrar.
- Si la cátedra publica luego el anexo real en Notion, compararlo con esta tabla: los IDs
  `I1-A … I6-D` son los que coinciden con la numeración de la consigna.
