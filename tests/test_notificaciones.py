"""Pruebas del adaptador de avisos: tiempos, códigos y ausencia de secretos."""
import pytest

from fenix.services import notifications
from fenix.utils.errors import PermanentError, RetryableError

TOKEN = "111222333:token-de-prueba-que-no-debe-filtrarse"


class FakeResponse:
    """Respuesta HTTP mínima con la superficie que usa el adaptador."""

    def __init__(self, status_code, headers=None, payload=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture
def telegram(monkeypatch):
    """Entorno de Telegram con credenciales de prueba en el proceso."""
    monkeypatch.setenv("NOTIFY_PROVIDER", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123456")
    return monkeypatch


def _responder(monkeypatch, respuesta):
    """Reemplaza requests.post del adaptador por una respuesta fija o un fallo."""
    if isinstance(respuesta, Exception):
        def lanzar(*args, **kwargs):
            raise respuesta
        monkeypatch.setattr(notifications.requests, "post", lanzar)
    else:
        monkeypatch.setattr(notifications.requests, "post",
                            lambda *a, **k: respuesta)


@pytest.mark.pendiente
def test_telegram_exige_ok_verdadero(telegram, monkeypatch):
    """Un 200 sin ok=true no es éxito: Telegram debe confirmar el envío."""
    _responder(monkeypatch, FakeResponse(200, payload={"ok": True}))
    assert notifications.notify("aviso correcto") is None  # Éxito confirmado.
    _responder(monkeypatch, FakeResponse(200, payload={"ok": False,
                                                       "description": "fallo"}))
    with pytest.raises(PermanentError):
        notifications.notify("aviso sin confirmar")


@pytest.mark.pendiente
@pytest.mark.parametrize("status", [400, 401, 403])
def test_errores_de_configuracion_son_permanentes(telegram, monkeypatch, status):
    """400/401/403 producen diagnóstico de configuración, no reintentos ciegos."""
    _responder(monkeypatch, FakeResponse(status, payload={"ok": False}))
    with pytest.raises(PermanentError):
        notifications.notify("aviso rechazado")


@pytest.mark.pendiente
@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_es_transitorio(telegram, monkeypatch, status):
    """Un fallo del proveedor es transitorio y conserva la tarea pendiente."""
    _responder(monkeypatch, FakeResponse(status, payload={}))
    with pytest.raises(RetryableError):
        notifications.notify("aviso con proveedor caído")


@pytest.mark.pendiente
@pytest.mark.parametrize("espera,esperado", [("3", "3 s"), ("9999", "30 s"),
                                             ("mañana", "5 s")])
def test_429_con_espera_acotada(telegram, monkeypatch, espera, esperado):
    """429 lee Retry-After y acota la demora al máximo del ejercicio (30 s)."""
    _responder(monkeypatch, FakeResponse(
        429, headers={"Retry-After": espera}, payload={}))
    with pytest.raises(RetryableError) as falla:
        notifications.notify("aviso limitado")
    assert esperado in str(falla.value)  # Se informa la espera real acotada.


@pytest.mark.pendiente
def test_timeout_sin_filtrar_url_ni_token(telegram, monkeypatch):
    """Timeout y conexión son transitorios y no copian la URL con el token."""
    import requests  # Sólo para construir la excepción de la librería.
    _responder(monkeypatch, requests.ConnectionError(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage"))
    with pytest.raises(RetryableError) as falla:
        notifications.notify("aviso con red caída")
    mensaje = str(falla.value)
    assert TOKEN not in mensaje and "telegram.org" not in mensaje  # Sin secretos.


@pytest.mark.pendiente
def test_slack_confirma_con_cuerpo_ok(monkeypatch):
    """Slack exige HTTP 200 y cuerpo textual ok; no parsea JSON a la fuerza."""
    monkeypatch.setenv("NOTIFY_PROVIDER", "slack")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc123")
    _responder(monkeypatch, FakeResponse(200, text="ok"))
    assert notifications.notify("aviso a slack") is None
    _responder(monkeypatch, FakeResponse(200, text="invalid_payload"))
    with pytest.raises(PermanentError):
        notifications.notify("aviso no confirmado")


@pytest.mark.pendiente
def test_modo_console_no_toca_la_red(monkeypatch):
    """El modo console permite desarrollar sin cuentas externas."""
    monkeypatch.setenv("NOTIFY_PROVIDER", "console")

    def prohibido(*args, **kwargs):  # Ninguna llamada saliente en consola.
        raise AssertionError("no debe haber llamadas HTTP en modo console")

    monkeypatch.setattr(notifications.requests, "post", prohibido)
    assert notifications.notify("aviso de desarrollo") is None


@pytest.mark.pendiente
def test_configuracion_incompleta_es_permanente(monkeypatch):
    """Sin credenciales completas no hay nada que reintentar."""
    monkeypatch.setenv("NOTIFY_PROVIDER", "telegram")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    with pytest.raises(PermanentError):
        notifications.notify("sin token")
