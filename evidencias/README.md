# Evidencias del Proyecto Fénix

Cada archivo indica el caso de la matriz, el comando que lo produjo y la fecha UTC.
Los secretos (tokens, firmas, URLs de Slack/Telegram) nunca aparecen en estas piezas.
Todas las lecturas se hicieron **antes** de limpiar; ningún archivo se generó con
logs fabricados: son la salida real de los mismos módulos que se defienden.

| Archivo | Caso | Cómo se obtuvo |
|---|---|---|
| `resultado-final-awslocal.txt` | F02/F03 — estado final por API | `awslocal ec2 describe-instances`, `s3api get-bucket-tagging`, `sqs get-queue-attributes` tras el flujo nominal completo |
| `log-nominal.json` | F02 — flujo correcto (37 líneas) | Flujo manual §8 del README: `init-queues → setup-demo → fault → send → worker` |
| `log-fallo.json` | F09 — entregas fallidas con backoff 2 y 4 s (28 líneas) | `send` con `simulate_failures=2` y tres ejecuciones de `python -m fenix.cli worker --once` |
| `historico-rotacion.json.gz` | F16 — rotación y compresión gzip | Pipeline real `utils/logging_json.setup(max_bytes=512)`; cada línea es JSON UTC parseable |
| `log-rotacion-actual.jsonl` | F16 — archivo vigente posterior a la rotación | Mismo pipeline, después de detener el listener |
| `dlq-grupo-f10.json` | F10 — DLQ real del laboratorio | `awslocal sqs receive-message` sobre `fenix-grupo01-dlq` **sin eliminarlo** |
| `dlq-f08.json` | F08 — JSON corrupto directo a SQS | `pytest -m integracion::test_redrive_with_localstack` (colas `prueba-*` propias) |
| `dlq-f10.json` | F10 — `simulate_failures=5` automatizado | `pytest -m integracion::test_f10_falla_persistente_llega_a_la_dlq` |
| `aviso-real-telegram.txt` | §5.8 — envío **real** a Telegram (HTTP 200, `ok=true`) | Flujo completo `fault → send → worker` con `NOTIFY_PROVIDER=telegram`; credenciales ocultas |
| `log-aviso-telegram.json` | F14 — fallo transitorio del aviso y recuperación sin repetir reparación | Mismo evento `evt-node-aviso`: 1.ª entrega falló (backoff 2 s), 2.ª sólo notificó |
