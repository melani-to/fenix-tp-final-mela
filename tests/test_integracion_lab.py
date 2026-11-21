"""Integración real sobre LocalStack: F01, F02, F03, F10 y F13.

Todas usan prefijos y grupos únicos por corrida: jamás tocan ni destruyen los
recursos del laboratorio propio del grupo. El estado de cada ensayo vive en una
carpeta temporal, así que data/inventory.json del laboratorio queda intacto.
"""
import json
import time
import uuid
from pathlib import Path

import pytest
import requests as http_requests
from fastapi.testclient import TestClient

from fenix.config.settings import client
from fenix.services import demo, receiver as receiver_module, remediation, sqs, worker
from fenix.utils.crypto import signature
from fenix.utils.logging_json import setup as setup_logs
from fenix.utils.state import load, save

pytestmark = pytest.mark.integracion  # Todo este módulo requiere LocalStack vivo.

SECRETO = "clave-de-integracion-solo-test"
EVENTO = {"schema_version": 1, "event_id": "evt-lab-001",
          "event_type": "instance.stopped", "resource_id": "node-01",
          "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "simulate_failures": 0}


def _disponible() -> bool:
    """True si LocalStack responde en el puerto del laboratorio."""
    try:
        http_requests.get("http://localhost:4566/_localstack/health", timeout=2)
    except http_requests.RequestException:
        return False
    return True


@pytest.fixture
def laboratorio(tmp_path, monkeypatch):
    """Carpeta temporal, credenciales locales y limpieza de lo que esta prueba cree."""
    if not _disponible():
        pytest.skip("LocalStack no disponible: docker compose up -d")
    from dotenv import load_dotenv
    load_dotenv()  # Carga .env del proyecto ANTES de cambiar de directorio.
    monkeypatch.chdir(tmp_path)  # data/ y logs/ de esta corrida, aislados.
    group = f"lab-{uuid.uuid4().hex[:6]}"  # Grupo único: nunca el del equipo.
    prefix = f"lab-{uuid.uuid4().hex[:8]}"  # Colas únicas de esta corrida.
    monkeypatch.setenv("WEBHOOK_SECRET", SECRETO)
    monkeypatch.setenv("QUEUE_PREFIX", prefix)
    monkeypatch.setenv("GROUP_ID", group)
    monkeypatch.setenv("NOTIFY_PROVIDER", "console")
    estado = {"group": group, "prefix": prefix, "inventario": None}
    yield estado
    _limpiar(estado)  # Sólo los IDs que inventarió esta prueba.


def _limpiar(estado) -> None:
    """Termina, borra y elimina únicamente lo que creó este ensayo."""
    import contextlib
    from botocore.exceptions import BotoCoreError, ClientError
    inventario = estado.get("inventario")
    if inventario:
        ec2, s3 = client("ec2"), client("s3")
        with contextlib.suppress(BotoCoreError, ClientError):
            ec2.terminate_instances(
                InstanceIds=[inventario["node-01"]["id"]])
        with contextlib.suppress(BotoCoreError, ClientError):
            s3.delete_bucket(Bucket=inventario["bucket-01"]["id"])
        with contextlib.suppress(BotoCoreError, ClientError):
            ec2.delete_security_group(
                GroupId=inventario["node-01"]["security_group_id"])
    if estado.get("cola"):
        sqs_client = client("sqs")
        for url in estado["cola"]:
            with contextlib.suppress(BotoCoreError, ClientError):
                sqs_client.delete_queue(QueueUrl=url)


def _firmar(body: bytes) -> dict:
    """Cabecera válida para el receptor real."""
    stamp = int(time.time())
    return {"X-Webhook-Signature": f"t={stamp},v1={signature(SECRETO, stamp, body)}"}


def _esperar_estado(instance_id: str, deseado: str, limite: int = 20) -> str:
    """Consulta acotada del estado real de la instancia."""
    ec2 = client("ec2")
    estado = ""
    for _ in range(limite):
        estado = ec2.describe_instances(InstanceIds=[instance_id])[
            "Reservations"][0]["Instances"][0]["State"]["Name"]
        if estado == deseado:
            return estado
        time.sleep(1)
    return estado


def test_f01_init_y_setup_demo_se_repite_sin_duplicados(laboratorio):
    """F01: dos init-queues y dos setup-demo -> mismas colas y recursos."""
    estado = laboratorio
    first = sqs.ensure_queues(estado["prefix"])
    second = sqs.ensure_queues(estado["prefix"])
    assert first == second  # Mismas URLs devueltas por el SDK.
    estado["cola"] = list(first)

    inv1 = demo.setup_demo(estado["group"])
    inv2 = demo.setup_demo(estado["group"])
    assert inv1 == inv2  # Misma instancia y mismo bucket: cero duplicados.
    estado["inventario"] = inv2

    ec2 = client("ec2")  # Comprobación por API, no por la memoria del test.
    instancias = [
        row for reservation in ec2.describe_instances(Filters=[
            {"Name": "tag:GroupId", "Values": [estado["group"]]}])
        ["Reservations"] for row in reservation["Instances"]]
    assert len(instancias) == 1  # Una sola instancia para el grupo.
    grupos = client("ec2").describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [f"fenix-{estado['group']}-sg"]}])
    assert len(grupos["SecurityGroups"]) == 1  # Un solo SG.
    assert Path("data/inventory.json").exists()  # Inventario publicado.


def test_f02_nodo_detenido_se_repara_y_se_confirma(laboratorio):
    """F02: nodo detenido + evento firmado -> running, ledger, aviso y ACK."""
    estado = laboratorio
    inventario = demo.setup_demo(estado["group"])
    estado["inventario"] = inventario
    work_url, dlq_url = sqs.ensure_queues(estado["prefix"])
    estado["cola"] = [work_url, dlq_url]

    demo.inject_fault("node-01")  # Fabrica el problema (I6-A).
    instance_id = inventario["node-01"]["id"]
    assert _esperar_estado(instance_id, "stopped") == "stopped"  # Estado real previo.

    body = json.dumps(EVENTO, separators=(",", ":")).encode("utf-8")
    response = TestClient(receiver_module.app).post(
        "/webhook", content=body, headers=_firmar(body))  # Receptor real.
    assert response.status_code == 202  # Recién tras confirmar send_message.
    assert response.json()["message_id"]

    logger, listener = setup_logs(f"f02-{uuid.uuid4().hex[:6]}")
    try:
        worker.run(work_url, remediation.handle, logger, once=True)  # Worker real.
    finally:
        listener.stop()

    assert _esperar_estado(instance_id, "running") == "running"  # Verificado por API.
    entry = load("data/events.json")["evt-lab-001"]
    assert entry["repair_done"] and entry["notified"] and entry["attempts"] == 1
    assert entry["repair_result"] == "repaired"  # Etapas del ledger §3.2.5.
    restantes = sqs.receive(work_url)  # El mensaje fue confirmado.
    assert restantes == []


def test_f03_bucket_recupera_owner_y_preserva_ajenas(laboratorio):
    """F03: bucket sin Owner -> etiquetas restauradas y ajenas preservadas."""
    estado = laboratorio
    inventario = demo.setup_demo(estado["group"])
    estado["inventario"] = inventario
    work_url, dlq_url = sqs.ensure_queues(estado["prefix"])
    estado["cola"] = [work_url, dlq_url]

    s3 = client("s3")
    bucket = inventario["bucket-01"]["id"]
    s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [
        {"Key": "ManagedBy", "Value": "ppa2"}, {"Key": "Project", "Value": "fenix"},
        {"Key": "GroupId", "Value": estado["group"]},
        {"Key": "Departamento", "Value": "Sistemas"}]})  # Owner borrado + ajena.
    demo.inject_fault("bucket-01")  # I6-A también puede reforzar la falla.

    evento = {**EVENTO, "event_id": "evt-lab-bucket",
              "event_type": "bucket.tags_missing", "resource_id": "bucket-01"}
    body = json.dumps(evento, separators=(",", ":")).encode("utf-8")
    response = TestClient(receiver_module.app).post(
        "/webhook", content=body, headers=_firmar(body))
    assert response.status_code == 202

    logger, listener = setup_logs(f"f03-{uuid.uuid4().hex[:6]}")
    try:
        worker.run(work_url, remediation.handle, logger, once=True)
    finally:
        listener.stop()

    tags = {row["Key"]: row["Value"]
            for row in s3.get_bucket_tagging(Bucket=bucket)["TagSet"]}
    assert tags.get("Owner") == estado["group"]  # Obligatoria restaurada.
    assert tags.get("Departamento") == "Sistemas"  # Ajena preservada.
    entry = load("data/events.json")["evt-lab-bucket"]
    assert entry["repair_done"] and entry["notified"]
    assert entry["tags"]["Departamento"] == "Sistemas"  # Tags verificados en ledger.


def test_f10_falla_persistente_llega_a_la_dlq(laboratorio):
    """F10: simulate_failures=5 -> sin ACK y derivación automática a la DLQ."""
    estado = laboratorio
    inventario = demo.setup_demo(estado["group"])
    estado["inventario"] = inventario
    work_url, dlq_url = sqs.ensure_queues(estado["prefix"])
    estado["cola"] = [work_url, dlq_url]

    evento = {**EVENTO, "event_id": "evt-lab-dlq", "simulate_failures": 5}
    sqs.publish(work_url, evento)  # Publicación directa: sólo importa la cola.

    logger, listener = setup_logs(f"f10-{uuid.uuid4().hex[:6]}")
    filas = []
    try:
        deadline = time.time() + 120  # Espera total acotada.
        while time.time() < deadline and not filas:
            worker.run(work_url, remediation.handle, logger, once=True)
            filas = client("sqs").receive_message(
                QueueUrl=dlq_url, MaxNumberOfMessages=1, WaitTimeSeconds=0,
                MessageSystemAttributeNames=["ApproximateReceiveCount"]
            ).get("Messages", [])
    finally:
        listener.stop()

    assert filas, "El mensaje fallido no derivó a la DLQ dentro de los 120 s"
    assert json.loads(filas[0]["Body"])["event_id"] == "evt-lab-dlq"
    assert int(filas[0]["Attributes"]["ApproximateReceiveCount"]) >= 3
    entry = load("data/events.json")["evt-lab-dlq"]
    assert not entry["repair_done"]  # Nunca hubo reparación ni aviso.
    # Evidencia ANTES de limpiar la DLQ.
    Path("evidencias").mkdir(exist_ok=True)
    Path("evidencias/dlq-f10.json").write_text(json.dumps(
        {"caso": "F10", "cola": estado["prefix"], "intentos": int(
            filas[0]["Attributes"]["ApproximateReceiveCount"]),
         "cuerpo": filas[0]["Body"],
         "registrado_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        ensure_ascii=False, indent=2), encoding="utf-8")
    client("sqs").delete_message(QueueUrl=dlq_url,
                                 ReceiptHandle=filas[0]["ReceiptHandle"])


def test_f13_cola_inexistente_responde_503_en_el_broker_real(laboratorio, monkeypatch):
    """F13: con SQS real sin cola disponible -> 503 y ninguna aceptación falsa."""
    estado = laboratorio  # QUEUE_PREFIX apunta a una cola que nunca se creó.
    # Sin inventario el 422 precedería al 503: primero se valida correctamente.
    save("data/inventory.json", {
        "node-01": {"type": "ec2", "id": "i-00000000", "security_group_id": "sg-0"},
        "bucket-01": {"type": "s3", "id": "fenix-demo", "expected_tags": {}}})
    monkeypatch.setenv("QUEUE_PREFIX", f"no-existe-{uuid.uuid4().hex[:6]}")
    body = json.dumps(EVENTO, separators=(",", ":")).encode("utf-8")
    response = TestClient(receiver_module.app).post(
        "/webhook", content=body, headers=_firmar(body))
    assert response.status_code == 503
    assert "accepted" not in response.text  # No se prometió aceptación.
    monkeypatch.setenv("QUEUE_PREFIX", estado["prefix"])  # Restaura el entorno.
