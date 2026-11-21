"""Pruebas del integrante 6: duplicados, orden del ACK y redrive real."""
import json  # Construye cuerpos de mensaje exactos.
import os  # Prefijo de log exclusivo del proceso de prueba.
import time  # Deadline total acotado para el ensayo con LocalStack.
import uuid  # Colas de prueba únicas que no tocan las del laboratorio.
from pathlib import Path

import pytest  # Mantiene visibles los casos y sus marcas.
from fenix.services import remediation, worker
from fenix.utils.errors import PermanentError, RetryableError
from fenix.utils.logging_json import setup as setup_logs
from fenix.utils.state import load, save

EVENT = {"schema_version": 1, "event_id": "evt-node-001",
         "event_type": "instance.stopped", "resource_id": "node-01",
         "occurred_at": "2026-10-22T18:00:00Z", "simulate_failures": 0}

INVENTORY = {"node-01": {"type": "ec2", "id": "i-000test", "security_group_id": "sg-0"},
             "bucket-01": {"type": "s3", "id": "fenix-demo", "expected_tags": {}}}


class Recorder:
    """Stub mínimo de logger: conserva el worker real y sólo anota los niveles."""

    def __init__(self):
        self.rows = []

    def _add(self, level, message, extra):
        self.rows.append((level, message, extra or {}))

    def debug(self, message, extra=None):
        self._add("DEBUG", message, extra)

    def info(self, message, extra=None):
        self._add("INFO", message, extra)

    def warning(self, message, extra=None):
        self._add("WARNING", message, extra)

    def error(self, message, extra=None):
        self._add("ERROR", message, extra)

    def critical(self, message, extra=None):
        self._add("CRITICAL", message, extra)


@pytest.mark.pendiente
def test_duplicate_is_idempotent(tmp_path, monkeypatch):
    """Comprueba que repetir un evento no repite recursos ni acciones completadas.

    Returns:
        None cuando dos ejecuciones conservan el estado final esperado.
    Raises:
        AssertionError: Si una segunda entrega vuelve a reparar o a notificar.
    Notes:
        El estado se guarda y se relee desde disco entre ambas ejecuciones, de
        modo que el ensayo representa de verdad un reinicio del proceso.
    """
    monkeypatch.chdir(tmp_path)  # data/ aislado en la carpeta temporal.
    save("data/inventory.json", INVENTORY)
    calls = []

    def fake_repair(resource, event_type):
        calls.append(("repair", resource["id"], event_type))
        return "repaired"

    def fake_notify(text):
        calls.append(("notify", text))

    monkeypatch.setattr(remediation, "repair", fake_repair)
    monkeypatch.setattr(remediation, "notify", fake_notify)

    remediation.handle(EVENT)  # Primera entrega completa.
    first = load("data/events.json")  # Estado durable en disco.
    remediation.handle(EVENT)  # "Reinicio": todo se vuelve a leer del disco.
    second = load("data/events.json")

    assert [row[0] for row in calls] == ["repair", "notify"]  # Una sola de cada una.
    assert first == second  # El ledger no cambió con el duplicado.
    entry = second["evt-node-001"]
    assert entry["repair_done"] and entry["notified"]  # Etapas completas.
    assert entry["attempts"] == 1  # El duplicado no altera el registro original.


@pytest.mark.pendiente
def test_ack_is_after_success(monkeypatch):
    """Verifica que un handler fallido jamás confirma la entrega de SQS.

    Returns:
        None si fallo=retry sin ACK y éxito=ACK exactamente al finalizar.
    Raises:
        AssertionError: Si hay un ACK antes del éxito o falta el ACK final.
    Notes:
        Se observa worker.run real: sólo se reemplazan la cola y las funciones
        de confirmación, que son las piezas externas de las que depende el ACK.
    """
    message = {"Body": json.dumps(EVENT), "ReceiptHandle": "r-1",
               "MessageId": "m-1",
               "Attributes": {"ApproximateReceiveCount": "1"}}
    calls = []
    monkeypatch.setattr(worker, "receive", lambda url: [message])

    def acknowledge(url, receipt):
        calls.append(("ack", receipt))

    def retry_later(url, current):
        calls.append(("retry", current["ReceiptHandle"]))
        return 2

    monkeypatch.setattr(worker, "acknowledge", acknowledge)
    monkeypatch.setattr(worker, "retry_later", retry_later)
    logger = Recorder()

    def failing(event):  # Handler que falla antes de cualquier etapa.
        calls.append(("handler", event["event_id"]))
        raise PermanentError("fallo de negocio")

    worker.run("cola-una", failing, logger, once=True)
    assert calls == [("handler", "evt-node-001"), ("retry", "r-1")]  # Cero ACK.

    calls.clear()

    def healthy(event):
        calls.append(("handler", event["event_id"]))

    worker.run("cola-una", healthy, logger, once=True)
    assert calls == [("handler", "evt-node-001"), ("ack", "r-1")]  # Orden exacto.
    ack_logs = [row for row in logger.rows if row[1] == "mensaje_confirmado"]
    assert ack_logs and ack_logs[-1][2]["status"] == "acknowledged"  # §5.2.5.


@pytest.mark.integracion
def test_redrive_with_localstack():
    """Comprueba DLQ efectiva usando el broker, no una lista simulada en Python.

    Returns:
        None si un mensaje venenoso termina en la DLQ y el worker sigue vivo.
    Raises:
        AssertionError: Si el mensaje no llega a la DLQ dentro del deadline.
    Side Effects:
        Crea colas de prueba con prefijo único, guarda la evidencia en
        evidencias/ y recién entonces limpia mensaje y colas.
    Notes:
        Usa un prefijo nuevo por ejecución: jamás purga ni destruye colas del
        laboratorio del grupo. La evidencia se escribe antes de borrar nada.
    """
    import requests  # Sólo para comprobar la salud del emulador.
    from fenix.config.settings import client
    from fenix.services import sqs as fenix_sqs

    try:  # Sin LocalStack el ensayo no se cuela como aprobado.
        requests.get("http://localhost:4566/_localstack/health", timeout=2)
    except requests.RequestException:
        pytest.skip("LocalStack no disponible: levantar docker compose up -d")

    prefix = f"prueba-{uuid.uuid4().hex[:8]}"  # Prefijo exclusivo de esta corrida.
    work_url, dlq_url = fenix_sqs.ensure_queues(prefix)
    sqs = client("sqs")
    body = '{"event_id": cuerpo-corrompido'  # JSON roto inyectado directo.
    logger, listener = setup_logs(f"test-redrive-{os.getpid()}")
    evidence = None
    try:
        sqs.send_message(QueueUrl=work_url, MessageBody=body)  # F08: directo a SQS.
        deadline = time.time() + 120  # Espera total acotada; sin loops infinitos.
        rows = []
        while time.time() < deadline and not rows:
            worker.run(work_url, lambda event: None, logger, once=True)  # Consume.
            rows = sqs.receive_message(  # Consulta la DLQ sin borrar nada.
                QueueUrl=dlq_url, MaxNumberOfMessages=1, WaitTimeSeconds=0,
                MessageSystemAttributeNames=["ApproximateReceiveCount"]
            ).get("Messages", [])
        assert rows, "El mensaje no derivó a la DLQ dentro de los 120 segundos"
        receipt = rows[0]["ReceiptHandle"]
        count = rows[0]["Attributes"]["ApproximateReceiveCount"]
        assert rows[0]["Body"] == body, "La DLQ no conserva el cuerpo original"
        assert int(count) >= 3, "La redrive policy no contó las recepciones"
        # Evidencia ANTES de limpiar: cuerpo original, intentos y fecha UTC.
        Path("evidencias").mkdir(exist_ok=True)
        evidence = {"caso": "F08", "cola": prefix, "recibir_hasta_dlq": int(count),
                    "cuerpo": body,
                    "registrado_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime())}
        Path("evidencias/dlq-f08.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        # Documentado el DLQ, recién ahí se limpia lo que creó esta prueba.
        sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=receipt)
    finally:
        listener.stop()
        import contextlib
        from botocore.exceptions import BotoCoreError, ClientError
        for url in (work_url, dlq_url):  # Destruir sólo las colas de esta corrida.
            with contextlib.suppress(BotoCoreError, ClientError):  # Limpieza final.
                sqs.delete_queue(QueueUrl=url)
    assert evidence is not None  # Se alcanzó la evidencia antes de limpiar.
