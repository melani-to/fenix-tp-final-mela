"""Adaptador HTTP guiado. Elegir Telegram O Slack, no ambos obligatoriamente."""
import os  # Recupera credenciales fuera del repositorio.
import requests  # Aplica timeout explícito a cada llamada HTTP.
from fenix.utils.errors import PermanentError, RetryableError

MAX_WAIT = 30  # Tope de espera del ejercicio (§5.3.2.b); más se documenta.


def _post(url: str, payload: dict) -> requests.Response:
    """Hace el POST con timeout y sin dejar rastros de la URL en los errores.

    Args:
        url: Dirección del proveedor; puede contener el token, por eso no se registra.
        payload: Cuerpo JSON del mensaje.
    Returns:
        La respuesta HTTP, sin interpretar todavía.
    Raises:
        RetryableError: Timeout o falla de conexión (transitorio).
        PermanentError: Otro fallo de la librería (no se reintenta a ciegas).
    Notes:
        timeout=(conexión 2 s, lectura 5 s): una llamada colgada nunca bloquea
        al worker. Se usa "from None" porque el texto original de requests
        puede incluir la URL completa con el token dentro.
    """
    try:
        return requests.post(url, json=payload, timeout=(2, 5))
    except (requests.Timeout, requests.ConnectionError):
        raise RetryableError("El proveedor no respondió a tiempo; se reintentará") from None
    except requests.RequestException:
        raise PermanentError("No se pudo enviar la solicitud al proveedor") from None


def _retry_after(response: requests.Response) -> int:
    """Lee la espera pedida por el proveedor y la acota al rango del ejercicio.

    Args:
        response: Respuesta 429 del proveedor.
    Returns:
        Segundos a esperar entre 1 y 30; 5 si no hay valor utilizable.
    Notes:
        Slack informa Retry-After en cabecera; Telegram en
        parameters.retry_after del JSON. Si el proveedor pide más de 30 s se
        limita el valor y el mensaje queda pendiente para la siguiente
        recepción SQS: no se amplía la espera de este ejercicio.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        try:  # Telegram devuelve el dato dentro del cuerpo JSON.
            value = response.json().get("parameters", {}).get("retry_after")
        except (ValueError, AttributeError):
            value = None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return 5  # Valor por defecto acotado.
    return max(1, min(seconds, MAX_WAIT))  # Nunca fuera del rango 1..30.


def _check_status(response: requests.Response) -> None:
    """Traduce el código HTTP a un error reintentable o permanente.

    Args:
        response: Respuesta del proveedor elegido.
    Raises:
        RetryableError: 429 o 5xx, siempre con espera acotada.
        PermanentError: Cualquier otro código distinto de 200.
    Notes:
        400/401/403 son de configuración (token inválido o chat inexistente):
        se informa el diagnóstico y no se repite indefinidamente en un bucle
        interno; los intentos los limita la recepción SQS y la DLQ.
    """
    status = response.status_code
    if status == 429:
        wait = _retry_after(response)
        raise RetryableError(f"Límite de tasa (429); esperar {wait} s")
    if status >= 500:
        raise RetryableError(f"El proveedor respondió {status}; se reintentará")
    if status != 200:
        raise PermanentError(f"Configuración o solicitud rechazada ({status})")


def notify(text: str) -> None:
    """Envía un mensaje breve y valida la respuesta del proveedor seleccionado.

    Args:
        text: Resumen público de hasta 1000 caracteres, sin secretos ni raw body.
    Returns:
        None únicamente después de confirmar el éxito del proveedor.
    Raises:
        RetryableError: Timeout, conexión, 429 o respuesta 5xx.
        PermanentError: Configuración inválida, 4xx definitivo o rechazo funcional.
    Side Effects:
        Publica en el chat o canal del grupo. Un timeout puede duplicar el aviso.
    Notes:
        El modo console sirve para desarrollo y no reemplaza la evidencia final
        real: la entrega exige al menos un envío verdadero al canal elegido.
        Nunca se registra la excepción original de requests: puede contener el
        token dentro de la URL.
    """
    mode = os.getenv("NOTIFY_PROVIDER", "console")
    text = text[:1000]  # Resumen corto; nunca el evento completo.
    if mode == "console":  # Ensayo local sin credenciales de terceros.
        print(text)
        return
    if mode == "telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat:  # Sin credenciales no hay nada que reintentar.
            raise PermanentError("Configuración de Telegram incompleta")
        response = _post(f"https://api.telegram.org/bot{token}/sendMessage",
                         {"chat_id": chat, "text": text})
        _check_status(response)
        try:  # Un HTTP 200 no basta: la API de Telegram devuelve ok=true.
            confirmed = response.json().get("ok") is True
        except (ValueError, AttributeError):
            confirmed = False
        if not confirmed:
            raise PermanentError("Telegram no confirmó el envío")
        return
    if mode == "slack":
        url = os.getenv("SLACK_WEBHOOK_URL", "")
        if not url.startswith("https://"):  # El webhook es un secreto y va por HTTPS.
            raise PermanentError("Configuración de Slack incompleta")
        response = _post(url, {"text": text})
        _check_status(response)
        if response.text.strip() != "ok":  # Slack responde texto plano "ok".
            raise PermanentError("Slack no confirmó el envío")
        return
    raise PermanentError("NOTIFY_PROVIDER no es válido")  # Fallo de configuración.
