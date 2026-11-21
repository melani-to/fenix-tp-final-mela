"""Matriz automatizada del TP: F04-F07, F09, F11-F15 en unitario controlado."""
import json  # Construye cuerpos exactos para el receptor.
import time  # Timestamps firmados de la prueba.
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from fenix.services import receiver as receiver_module
from fenix.services import remediation, worker
from fenix.services import sqs as fenix_sqs
from fenix.services import notifications
from fenix.utils.crypto import signature
from fenix.utils.errors import PermanentError, RetryableError, ValidationError
from fenix.utils.state import load, save

SECRET = "clave-ficticia-solo-test"  # Secreto de prueba: sin uso fuera de los tests.
EVENT = {"schema_version": 1, "event_id": "evt-node-001",
         "event_type": "instance.stopped", "resource_id": "node-01",
         "occurred_at": "2026-10-22T18:00:00Z", "simulate_failures": 0}
INVENTORY = {"node-01": {"type": "ec2", "id": "i-000test", "security_group_id": "sg-0"},
             "bucket-01": {"type": "s3", "id": "fenix-demo", "expected_tags": {}}}


class FakeSQS:
    """Cliente SQS falso que sólo registra los cambios de visibilidad."""

    def __init__(self):
        self.visibility = []

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):
        self.visibility.append(VisibilityTimeout)


@pytest.fixture
def receptor(tmp_path, monkeypatch):
    """Receptor real con inventario temporal y publicación espiada."""
    monkeypatch.chdir(tmp_path)  # data/ de la prueba, nunca el del laboratorio.
    save("data/inventory.json", INVENTORY)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("QUEUE_PREFIX", "fenix-prueba")
    published = []

    def fake_publish(url, event, delay=0):  # Sustituye a send_message.
        published.append(event)
        return "msg-001"

    monkeypatch.setattr(receiver_module, "publish", fake_publish)
    monkeypatch.setattr(receiver_module, "queue_url", lambda prefix: "url-de-cola")
    client = TestClient(receiver_module.app)
    return client, published


def firmar(body: bytes, secret: str = SECRET, offset: int = 0) -> dict:
    """Devuelve las cabeceras con una firma válida desplazada offset segundos."""
    stamp = int(time.time()) + offset
    return {"X-Webhook-Signature": f"t={stamp},v1={signature(secret, stamp, body)}"}


def cuerpo(**cambios) -> bytes:
    """Cuerpo JSON del evento con los cambios indicados."""
    event = {**EVENT, **cambios}
    return json.dumps(event, separators=(",", ":")).encode("utf-8")


@pytest.mark.pendiente
@pytest.mark.parametrize("caso", ["sin-cabecera", "cuerpo-alterado",
                                  "timestamp-viejo", "secreto-equivocado"])
def test_f04_firma_invalida_responde_401_sin_publicar(receptor, caso):
    """F04: firma ausente, alterada o fuera de ventana -> 401 y ningún mensaje."""
    client, published = receptor
    body = cuerpo()
    if caso == "sin-cabecera":
        headers = {}
    elif caso == "cuerpo-alterado":  # Se firma un cuerpo y llega otro distinto.
        headers = firmar(body)
        body = body + b" "
    elif caso == "timestamp-viejo":  # Hora de hace una hora: fuera de los 300 s.
        headers = firmar(body, offset=-3600)
    else:  # Firma calculada con otro secreto.
        headers = firmar(body, secret="otro-secreto-completamente-distinto")
    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 401
    assert published == []  # Sin publicación SQS.


@pytest.mark.pendiente
def test_f05_mismos_datos_con_bytes_distintos(receptor):
    """F05: misma información JSON con otros bytes tras firmar -> 401 por HMAC."""
    client, published = receptor
    body = cuerpo()
    headers = firmar(body)
    alterado = body + b" "  # json.loads da lo mismo; los bytes no.
    response = client.post("/webhook", content=alterado, headers=headers)
    assert response.status_code == 401
    assert published == []


@pytest.mark.pendiente
@pytest.mark.parametrize("body,esperado", [(b'{"event_id": roto', 400),
                                           (b"[1, 2]", 422)])
def test_f06_json_invalido_sin_publicacion(receptor, body, esperado):
    """F06: JSON roto con firma valida -> 400; lista JSON firmada -> 422."""
    client, published = receptor
    response = client.post("/webhook", content=body, headers=firmar(body))
    assert response.status_code == esperado
    assert published == []  # Ninguno de los dos se encola.


@pytest.mark.pendiente
@pytest.mark.parametrize("body", [
    cuerpo(resource_id="ajeno-99"),  # Alias fuera del inventario autorizado.
    cuerpo(extra="sobrante"),  # Clave extra: esquema cerrado.
    cuerpo(simulate_failures=True),  # bool no es entero.
    cuerpo(event_type="instance.deleted"),  # Tipo no permitido.
])
def test_f07_entradas_invalidas_sin_efectos_cloud(receptor, body):
    """F07: alias ajeno o claves extra -> 422 y ningún cambio en la nube."""
    client, published = receptor
    before = Path("data/inventory.json").read_text(encoding="utf-8")
    response = client.post("/webhook", content=body, headers=firmar(body))
    assert response.status_code == 422
    assert published == []
    # El inventario no fue escrito ni modificado por el rechazo.
    assert Path("data/inventory.json").read_text(encoding="utf-8") == before


@pytest.mark.pendiente
def test_f13_sqs_no_disponible_responde_503(tmp_path, monkeypatch):
    """F13: si la cola no responde no se promete aceptación: 503 sin accepted."""
    monkeypatch.chdir(tmp_path)
    save("data/inventory.json", INVENTORY)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("QUEUE_PREFIX", "fenix-inexistente")

    def caido(prefix):  # Cola inexistente o servicio inalcanzable.
        raise ClientError({"Error": {"Code": "QueueDoesNotExist",
                                     "Message": "x"},
                           "ResponseMetadata": {"HTTPStatusCode": 400}},
                          "GetQueueUrl")

    monkeypatch.setattr(receiver_module, "queue_url", caido)
    body = cuerpo()
    response = TestClient(receiver_module.app).post(
        "/webhook", content=body, headers=firmar(body))
    assert response.status_code == 503
    assert "accepted" not in response.text


def _preparar_negocio(tmp_path, monkeypatch, notify):
    """Aísla data/ y reemplaza repair/notify por espías controlables."""
    monkeypatch.chdir(tmp_path)
    save("data/inventory.json", INVENTORY)
    repairs = []
    notices = []

    def fake_repair(resource, event_type):
        repairs.append((resource["id"], event_type))
        return "repaired"

    monkeypatch.setattr(remediation, "repair", fake_repair)
    monkeypatch.setattr(remediation, "notify", notify(notices))
    return repairs, notices


def _entrega(count: int, handle: str | None = None) -> dict:
    """Mensaje crudo de SQS con la recepción indicada."""
    return {"Body": json.dumps(EVENT), "MessageId": f"m-{count}",
            "ReceiptHandle": handle or f"r-{count}",
            "Attributes": {"ApproximateReceiveCount": str(count)}}


@pytest.mark.pendiente
def test_f09_simulate_failures_backoff_y_ack(tmp_path, monkeypatch):
    """F09: con simulate_failures=2 los backoff valen 2 y 4; la tercera ACKea."""
    ok_notify = lambda notices: (lambda text: notices.append(text))
    repairs, notices = _preparar_negocio(tmp_path, monkeypatch, ok_notify)
    event = {**EVENT, "simulate_failures": 2}
    entregas = iter([{**_entrega(n), "Body": json.dumps(event)}
                     for n in (1, 2, 3)])
    monkeypatch.setattr(worker, "receive", lambda url: [next(entregas)])
    fake = FakeSQS()
    monkeypatch.setattr(fenix_sqs, "client", lambda service: fake)
    acks = []
    monkeypatch.setattr(worker, "acknowledge",
                        lambda url, receipt: acks.append(receipt))
    logger = _Logger()

    for _ in range(3):  # Tres entregas de la cola.
        worker.run("cola-x", remediation.handle, logger, once=True)

    assert fake.visibility == [2, 4]  # Primeras demoras de la consigna.
    assert acks == ["r-3"]  # Ningún ACK en las dos fallidas.
    assert len(repairs) == 1 and len(notices) == 1  # Repara recién en la 3.ª.
    assert load("data/events.json")["evt-node-001"]["attempts"] == 3


@pytest.mark.pendiente
def test_f11_repetir_evento_completo_no_repite_efectos(tmp_path, monkeypatch):
    """F11: repetir un evento ya completo, con el worker reiniciado, no repite."""
    ok_notify = lambda notices: (lambda text: notices.append(text))
    repairs, notices = _preparar_negocio(tmp_path, monkeypatch, ok_notify)
    remediation.handle(EVENT)  # Primera pasada completa.
    ledger_original = load("data/events.json")

    monkeypatch.setattr(worker, "receive", lambda url: [_entrega(1)])  # Reinicio.
    acks = []
    monkeypatch.setattr(worker, "acknowledge",
                        lambda url, receipt: acks.append(receipt))
    worker.run("cola-x", remediation.handle, _Logger(), once=True)

    assert len(repairs) == 1 and len(notices) == 1  # Cero acciones nuevas.
    assert acks == ["r-1"]  # El duplicado se confirma como ya procesado.
    assert load("data/events.json") == ledger_original  # Ledger intacto.


@pytest.mark.pendiente
def test_f12_corte_entre_reparacion_y_aviso(tmp_path, monkeypatch):
    """F12: corte después de repair_done y antes de notify: al reanudar sólo avisa."""
    fallas = {"primera": True}

    def notify_con_falla(notices):
        def body(text):
            if fallas["primera"]:
                fallas["primera"] = False
                raise RetryableError("proveedor caído")
            notices.append(text)
        return body

    repairs, notices = _preparar_negocio(tmp_path, monkeypatch, notify_con_falla)
    entregas = iter([_entrega(1), _entrega(2)])
    monkeypatch.setattr(worker, "receive", lambda url: [next(entregas)])
    acks = []
    monkeypatch.setattr(worker, "acknowledge",
                        lambda url, receipt: acks.append(receipt))
    monkeypatch.setattr(worker, "retry_later", lambda url, message: 2)
    logger = _Logger()

    worker.run("cola-x", remediation.handle, logger, once=True)  # 1.ª entrega.
    entry = load("data/events.json")["evt-node-001"]
    assert entry["repair_done"] and not entry["notified"]  # Corte simulado.
    assert acks == []  # Sin ACK con el aviso pendiente.

    worker.run("cola-x", remediation.handle, logger, once=True)  # Reinicio.
    entry = load("data/events.json")["evt-node-001"]
    assert entry["repair_done"] and entry["notified"]  # Sólo faltaba el aviso.
    assert len(repairs) == 1  # No se volvió a reparar ni a tocar etiquetas.
    assert len(notices) == 1
    assert acks == ["r-2"]


@pytest.mark.pendiente
def test_f15_conflicto_de_event_id_no_altera_el_ledger(tmp_path, monkeypatch):
    """F15: cambiar el payload conservando event_id registra conflicto."""
    ok_notify = lambda notices: (lambda text: notices.append(text))
    repairs, _ = _preparar_negocio(tmp_path, monkeypatch, ok_notify)
    remediation.handle(EVENT)
    before = load("data/events.json")

    with pytest.raises(PermanentError):  # Se conserva para la DLQ, sin ACK.
        remediation.handle({**EVENT, "simulate_failures": 3})

    assert load("data/events.json") == before  # Primer registro sin cambios.
    conflicts = load("data/conflicts.json")  # Conflicto registrado aparte.
    assert conflicts["evt-node-001"][0]["conflict"] is True
    assert len(repairs) == 1  # El evento ajeno no ejecutó reparaciones.


@pytest.mark.pendiente
def test_f14_aviso_429_luego_exito_sin_repetir_reparacion(tmp_path, monkeypatch):
    """F14: 429 del proveedor deja el aviso pendiente sin repetir la reparación."""
    monkeypatch.chdir(tmp_path)
    save("data/inventory.json", INVENTORY)
    repairs = []
    monkeypatch.setattr(remediation, "repair",
                        lambda resource, kind: repairs.append(kind) or "repaired")

    respuestas = iter([type("R", (), {"status_code": 429,
                                      "headers": {"Retry-After": "3"},
                                      "json": lambda self: {}})(),
                       type("R", (), {"status_code": 200,
                                      "headers": {},
                                      "json": lambda self: {"ok": True},
                                      "text": "ok"})()])
    monkeypatch.setattr(notifications.requests, "post",
                        lambda *a, **k: next(respuestas))
    monkeypatch.setenv("NOTIFY_PROVIDER", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:token-de-prueba")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123456")

    with pytest.raises(RetryableError) as falla:  # 429: transitorio, espera 3 s.
        remediation.handle(EVENT)
    assert "3 s" in str(falla.value)
    entry = load("data/events.json")["evt-node-001"]
    assert entry["repair_done"] and not entry["notified"]  # Aviso pendiente.

    remediation.handle(EVENT)  # Siguiente recepción: reintenta sólo el aviso.
    entry = load("data/events.json")["evt-node-001"]
    assert entry["notified"] and len(repairs) == 1  # Reparación no repetida.


class _Logger:
    """Registro mínimo para observar el worker sin escribir archivos de log."""

    def __init__(self):
        self.rows = []

    def debug(self, message, extra=None):
        self.rows.append(("DEBUG", message, extra or {}))

    def info(self, message, extra=None):
        self.rows.append(("INFO", message, extra or {}))

    def warning(self, message, extra=None):
        self.rows.append(("WARNING", message, extra or {}))

    def error(self, message, extra=None):
        self.rows.append(("ERROR", message, extra or {}))
