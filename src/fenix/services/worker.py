"""Plantilla central de integración: un worker, un mensaje por vez."""
import json  # Convierte Body solo dentro del control por mensaje.
import time  # Evita un bucle intensivo si el emulador no responde.
from botocore.exceptions import BotoCoreError, ClientError
from fenix.services.sqs import receive, acknowledge, retry_later
from fenix.utils.errors import ProjectError


def _safe(value) -> str | None:
    """Devuelve un texto público y corto, o None si no es seguro registrarlo.

    Args:
        value: Dato no confiable proveniente del mensaje o del entorno.
    Returns:
        El mismo texto si es str de hasta 64 caracteres; None en otro caso.
    Notes:
        Evita convertir en log un JSON entero o un dato demasiado largo.
    """
    return value if isinstance(value, str) and len(value) <= 64 else None


def _process(url: str, message: dict, handler, logger) -> bool:
    """Procesa un mensaje: handler primero, confirmación recién al final.

    Args:
        url: URL de la cola de trabajo.
        message: Mensaje crudo de SQS con Body, ReceiptHandle y Attributes.
        handler: Callable handler(event) que ejecuta el caso de negocio.
        logger: Logger JSON del proceso; sólo recibe contexto público.
    Returns:
        True si falló retry_later y conviene esperar; False en otro caso.
    Raises:
        NotImplementedError: Se propaga a propósito: revela trabajo sin completar.
    Side Effects:
        Confirma el mensaje si terminó bien; si no, cambia su visibilidad.
    Notes:
        El ACK se emite sólo después de que handler completó y persistió todas
        sus etapas, y el registro status=acknowledged recién después de que el
        SDK confirmó el borrado. Nunca hay ACK en finally.
    """
    attempt = message.get("Attributes", {}).get("ApproximateReceiveCount")
    context = {"message_id": _safe(message.get("MessageId")),  # Antes de parsear.
               "attempt": int(attempt) if str(attempt).isdigit() else None}
    try:  # Todo el control es por mensaje: uno corrupto no detiene a los demás.
        event = json.loads(message["Body"])
        if not isinstance(event, dict):  # JSON válido pero no es un objeto.
            raise ValueError("El cuerpo no es un objeto JSON")
        context.update(event_id=_safe(event.get("event_id")),
                       resource_id=_safe(event.get("resource_id")))
        logger.debug("mensaje_parseado", extra=context)  # Nivel DEBUG con caso real.
        handler(event)  # Revalida, repara, avisa y deja el registro durable.
        acknowledge(url, message["ReceiptHandle"])  # Sólo si todo lo anterior salió.
        # Consigna §5.2.5: registrar recién después de que el SDK confirmó.
        logger.info("mensaje_confirmado",
                    extra={**context, "action": "ack", "status": "acknowledged"})
    except NotImplementedError:  # Falta completar una plantilla: no se oculta.
        raise
    except (ProjectError, ValueError, KeyError, TypeError, OSError,
            BotoCoreError, ClientError) as error:
        # Sólo el nombre del tipo: el texto del error podría contener datos.
        logger.warning("fallo_procesando",
                       extra={**context, "status": type(error).__name__})
        try:  # Sin ACK: el mensaje vuelve y la RedrivePolicy cuenta recepciones.
            seconds = retry_later(url, message)
            logger.info(f"reintento_programado_en_{seconds}s",
                        extra={**context, "action": "retry", "status": "retry"})
        except (BotoCoreError, ClientError, KeyError, ValueError) as retry_error:
            logger.error("fallo_retry_later",
                         extra={**context, "status": type(retry_error).__name__})
            return True  # Avisa al bucle que debe esperar antes de seguir.
    return False


def run(url: str, handler, logger, once: bool = False) -> None:
    """Consume mensajes y confirma únicamente resultados durables.

    Args:
        url: URL de la cola de trabajo, obtenida con get_queue_url.
        handler: Callable handler(event) que revalida y ejecuta el negocio.
        logger: Logger JSON del proceso worker.
        once: Si es True, realiza una sola consulta y termina; útil en pruebas.
    Returns:
        None al terminar la consulta única o al detenerse mediante Ctrl+C.
    Raises:
        NotImplementedError: Si una plantilla sigue pendiente; se propaga.
    Side Effects:
        Recibe mensajes, ejecuta el handler y cambia o confirma su recepción.
    Notes:
        Algoritmo de la sección 5.2: long polling de 10 s y una entrega por
        consulta; "sin Messages" no es un error; el fallo de transporte se
        registra y se espera 2 s; la validación HTTP no protege los mensajes
        inyectados directos, por eso handler revalida el contrato siempre.
        Ctrl+C sale por KeyboardInterrupt y quien llama cierra el listener.
    """
    while True:
        try:  # Un fallo de transporte no termina el worker.
            messages = receive(url)
        except (BotoCoreError, ClientError) as error:
            logger.error("fallo_receive", extra={"status": type(error).__name__})
            if once:  # --once hizo su única consulta, aunque haya fallado.
                return
            time.sleep(2)  # Espera acotada antes de volver a consultar.
            continue
        logger.debug("consulta_realizada", extra={"action": "receive"})
        wait = False
        for message in messages:  # Un mensaje malo no esconde a los siguientes.
            wait = _process(url, message, handler, logger) or wait
        if once:  # Una sola consulta y termina.
            return
        if wait:  # Falló retry_later: conviene esperar antes de reintentar.
            time.sleep(2)
