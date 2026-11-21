"""Firma de apoyo y verificación HMAC-SHA256; integrante 3."""
import hashlib  # Selecciona SHA-256 para autenticar el contenido.
import hmac  # Usa una comparación segura para las firmas.
import re  # Exige que v1 sea exactamente 64 caracteres hexadecimales.
import time  # Comprueba la ventana temporal.
from fenix.utils.errors import ValidationError  # Rechazo de autenticación público.


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """Firma exactamente timestamp, punto y cuerpo binario original.

    Args:
        secret: Secreto compartido de al menos 16 caracteres, leído del entorno.
        timestamp: Unix epoch en segundos, igual al enviado en la cabecera.
        body: Bytes exactos enviados por HTTP; no volver a serializar el JSON.
    Returns:
        HMAC-SHA256 hexadecimal de 64 caracteres.
    Raises:
        ValidationError: Si el secreto es demasiado corto.
    """
    if len(secret) < 16:  # Se comprueba también al enviar la prueba.
        raise ValidationError("Configurar WEBHOOK_SECRET de 16 caracteres o más")
    signed = str(timestamp).encode("ascii") + b"." + body  # Prefijo autenticado.
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def verify(secret: str, header: str, body: bytes, now: int | None = None) -> None:
    """Rechaza cabeceras ambiguas y firmas o fechas inválidas.

    Args:
        secret: Secreto compartido del entorno, nunca del payload.
        header: Cabecera exacta t=<epoch>,v1=<hex> sin claves duplicadas.
        body: Bytes del cuerpo antes de llamar a json.loads.
        now: Reloj inyectable para pruebas; None usa time.time.
    Returns:
        None si las comprobaciones son satisfactorias.
    Raises:
        ValidationError: Formato inválido, firma incorrecta o diferencia mayor a 300 s.
    Notes:
        El orden importa: primero formato, después ventana y recién al final la
        comparación de la firma. La ventana de 300 s mitiga replay antiguo; no
        impide repetir un evento válido dentro de los 5 minutos, y para eso
        están el event_id y el ledger de idempotencia.
        Ningún mensaje incluye la cabecera, la firma calculada ni el secreto.
    """
    if not isinstance(header, str) or not isinstance(body, bytes):
        raise ValidationError("Cabecera de firma inválida")
    parts = header.split(",")  # Debe haber exactamente dos pares: t y v1.
    if len(parts) != 2:
        raise ValidationError("Cabecera de firma inválida")
    fields = {}
    for part in parts:  # Se arma el mapa rechazando pares rotos o repetidos.
        key, separator, value = part.partition("=")
        if not separator or key in fields:  # Sin "=" o clave duplicada: ambiguo.
            raise ValidationError("Cabecera de firma inválida")
        fields[key] = value
    if set(fields) != {"t", "v1"}:  # Ni faltan claves ni sobran desconocidas.
        raise ValidationError("Cabecera de firma inválida")
    raw_timestamp, received = fields["t"], fields["v1"]
    # isascii descarta dígitos de otros alfabetos; isdigit signos, puntos y espacios.
    if not (raw_timestamp.isascii() and raw_timestamp.isdigit()):
        raise ValidationError("Cabecera de firma inválida")
    try:  # Conversión explícita: cualquier fallo va al error público del dominio.
        timestamp = int(raw_timestamp)
    except ValueError as error:
        raise ValidationError("Cabecera de firma inválida") from error
    if re.fullmatch(r"[0-9a-f]{64}", received) is None:  # SHA-256 en hex minúscula.
        raise ValidationError("Cabecera de firma inválida")
    current = int(time.time()) if now is None else now  # Reloj inyectable en pruebas.
    if abs(current - timestamp) > 300:  # Ventana de 5 minutos, pasado y futuro.
        raise ValidationError("Fecha fuera de la ventana permitida")
    expected = signature(secret, timestamp, body)  # Calcula sobre los bytes crudos.
    if not hmac.compare_digest(expected, received):  # Tiempo constante: nunca "==".
        raise ValidationError("Firma inválida")
