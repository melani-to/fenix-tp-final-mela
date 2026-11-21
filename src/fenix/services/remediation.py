"""Acciones convergentes: integrante 3 las implementa, integrante 5 las orquesta."""
from datetime import datetime, timezone  # Marca los conflictos en UTC explícito.
from botocore.exceptions import BotoCoreError, ClientError  # Errores del SDK.
from fenix.config.settings import client
from fenix.services.cloud_support import aws_tags
from fenix.utils.state import load, save
from fenix.services.events import validate
from fenix.services.notifications import notify
from fenix.utils.errors import PermanentError, RetryableError

# Códigos AWS que indican un fallo pasajero: conviene otra recepción.
TRANSIENT_CODES = {"Throttling", "ThrottlingException", "RequestLimitExceeded",
                   "ServiceUnavailable", "InternalError", "InternalFailure",
                   "RequestTimeout", "Unavailable", "SlowDown"}

INVENTORY_PATH = "data/inventory.json"  # Lo publica setup-demo; aquí sólo se lee.
LEDGER_PATH = "data/events.json"  # Estado durable por event_id.
CONFLICT_PATH = "data/conflicts.json"  # Reutilizaciones registradas aparte.


def classify(error: Exception) -> Exception:
    """Traduce un error de Boto3 a un error del proyecto, sin copiar datos sensibles.

    Args:
        error: ClientError o BotoCoreError capturado al llamar a una API.
    Returns:
        RetryableError si el fallo es transitorio; PermanentError en otro caso.
    Notes:
        No todo ClientError es transitorio: un 404 o un AccessDenied no se
        reintenta, mientras que throttling y 5xx sí lo hacen.
    """
    if isinstance(error, BotoCoreError):  # Red, DNS o conexión: pasajero.
        return RetryableError("Fallo de conexión con el servicio; se reintentará")
    code = error.response.get("Error", {}).get("Code", "")
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    if code in TRANSIENT_CODES or status >= 500:
        return RetryableError(f"Fallo transitorio del servicio ({code})")
    return PermanentError(f"Operación rechazada por el servicio ({code})")


def instance_state(ec2, instance_id: str) -> str:
    """Lee el estado actual de una instancia del inventario.

    Args:
        ec2: Cliente EC2 del emulador.
        instance_id: InstanceId leído del inventario, nunca de la petición.
    Returns:
        Nombre del estado: pending, running, stopping, stopped, etc.
    Raises:
        PermanentError: Si la instancia del inventario no existe.
    """
    try:
        reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code.startswith("InvalidInstanceID"):
            raise PermanentError("La instancia del inventario no existe") from error
        raise  # Otro error de API se clasifica más arriba con classify.
    if not reservations or not reservations[0]["Instances"]:
        raise PermanentError("La instancia del inventario no existe")
    return reservations[0]["Instances"][0]["State"]["Name"]


def bucket_tags(s3, bucket: str) -> dict:
    """Lee las etiquetas de un bucket; "sin etiquetas" equivale a vacío.

    Args:
        s3: Cliente S3 del emulador.
        bucket: Nombre del bucket del inventario.
    Returns:
        Diccionario clave -> valor con las etiquetas actuales.
    Raises:
        PermanentError: Si el bucket no existe.
    """
    try:
        tag_set = s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code == "NoSuchTagSet":  # Sólo este código significa "sin etiquetas".
            return {}
        if code == "NoSuchBucket":
            raise PermanentError("El bucket del inventario no existe") from error
        raise
    return {row["Key"]: row["Value"] for row in tag_set}


def repair_instance(resource: dict) -> str:
    """Arranca una instancia detenida y exige observarla en running.

    Args:
        resource: Registro de inventario con el id de la instancia.
    Returns:
        "already_ok" si ya estaba running; "repaired" si se arrancó.
    Raises:
        RetryableError: Estado pasajero (pending, stopping) o aún no observado.
        PermanentError: Estado no reparable (terminated, shutting-down).
    Notes:
        Se consulta antes y después: el resultado no se supone, se verifica.
        Nunca reboot_instances: reiniciar no converge hacia "encendida".
    """
    ec2 = client("ec2")
    instance_id = resource["id"]
    state = instance_state(ec2, instance_id)
    if state == "running":  # Ya cumple la política: no se toca nada.
        return "already_ok"
    if state in ("pending", "stopping"):  # Transición en curso: otra recepción.
        raise RetryableError(f"La instancia está en transición ({state})")
    if state != "stopped":  # terminated o shutting-down no se pueden reparar.
        raise PermanentError(f"La instancia está en un estado no reparable ({state})")
    ec2.start_instances(InstanceIds=[instance_id])
    if instance_state(ec2, instance_id) != "running":  # Verificado, no supuesto.
        raise RetryableError("La instancia aún no se observa en running")
    return "repaired"


def repair_bucket(resource: dict) -> str:
    """Restaura las etiquetas obligatorias de un bucket sin borrar las ajenas.

    Args:
        resource: Registro de inventario con el nombre y las expected_tags.
    Returns:
        "already_ok" si ya cumplen; "repaired" si se restauraron.
    Raises:
        RetryableError: Si tras escribir todavía no se observan.
        PermanentError: Bucket inexistente o inventario sin etiquetas esperadas.
    Notes:
        Lectura, mezcla, escritura y verificación: cuatro pasos separados para
        que un fallo parcial quede a la vista y se reintente con seguridad.
    """
    expected = resource.get("expected_tags")
    if not isinstance(expected, dict) or not expected:
        raise PermanentError("El inventario no define expected_tags para el bucket")
    s3 = client("s3")
    current = bucket_tags(s3, resource["id"])
    if all(current.get(key) == value for key, value in expected.items()):
        return "already_ok"  # Ya cumple: no se escribe nada.
    merged = {**current, **expected}  # Obligatorias ganan; ajenas se conservan.
    s3.put_bucket_tagging(Bucket=resource["id"], Tagging={"TagSet": aws_tags(merged)})
    after = bucket_tags(s3, resource["id"])  # Se vuelve a leer para verificar.
    if any(after.get(key) != value for key, value in expected.items()):
        raise RetryableError("Las etiquetas aún no se observan")
    return "repaired"


def repair(resource: dict, event_type: str) -> str:
    """Acerca un recurso autorizado a su estado deseado y verifica el resultado.

    Args:
        resource: Registro de inventario con type/id y etiquetas esperadas.
        event_type: instance.stopped o bucket.tags_missing ya validado.
    Returns:
        "repaired" si cambió el estado; "already_ok" si ya cumplía la política.
    Raises:
        RetryableError: La API falla temporalmente o falta verificar el estado.
        PermanentError: Recurso inexistente o tipo de evento no autorizado.
    Side Effects:
        Arranca una instancia EC2 mock detenida o restaura tags de un bucket S3.
    Notes:
        El recurso sale siempre del inventario: un ID ajeno dentro de un evento
        válido jamás llega hasta acá, porque validate() lo rechaza antes.
    """
    kinds = {"instance.stopped": "ec2", "bucket.tags_missing": "s3"}
    if kinds.get(event_type) is None or resource.get("type") != kinds[event_type]:
        raise PermanentError("El evento no corresponde al tipo de recurso")
    if not isinstance(resource.get("id"), str) or not resource["id"]:
        raise PermanentError("El inventario no define el identificador del recurso")
    try:
        if event_type == "instance.stopped":
            return repair_instance(resource)
        return repair_bucket(resource)
    except (ClientError, BotoCoreError) as error:
        raise classify(error) from error  # Causa preservada, mensaje público.


def _current_tags(resource: dict) -> dict:
    """Devuelve las etiquetas verificadas del recurso recién reparado.

    Args:
        resource: Registro de inventario del recurso.
    Returns:
        Para S3, las etiquetas releídas por SDK (prueba de la restauración).
        Para EC2, las expected_tags del inventario: start_instances no altera
        las etiquetas y setup-demo ya verificó su pertenencia al crearlas.
    Raises:
        ClientError: Si la lectura S3 posterior falla; se clasifica en el worker.
    """
    if resource["type"] == "s3":
        return bucket_tags(client("s3"), resource["id"])
    return dict(resource.get("expected_tags") or {})


def _store(event_id: str, entry: dict) -> None:
    """Guarda un registro sin pisar los de los demás eventos.

    Args:
        event_id: Identificador del evento al que pertenece el registro.
        entry: Estado durable de ese evento.
    Side Effects:
        Relee el ledger, reemplaza sólo esta clave y publica con reemplazo atómico.
    """
    ledger = load(LEDGER_PATH)  # Se relee para conservar el resto del ledger.
    ledger[event_id] = entry
    save(LEDGER_PATH, ledger)


def _register_conflict(event_id: str, received: dict) -> None:
    """Anota la reutilización de un event_id sin tocar el primer registro.

    Args:
        event_id: Identificador que ya tenía contenido guardado.
        received: Contenido nuevo que contradice al registrado.
    Side Effects:
        Append en data/conflicts.json; el ledger original queda intacto.
    Notes:
        Es un registro aparte a propósito: la consigna pide conservar el
        primer registro sin cambios y además retener el mensaje para la DLQ.
    """
    conflicts = load(CONFLICT_PATH)
    rows = conflicts.get(event_id, [])
    rows.append({"conflict": True, "event_id": event_id,
                 "registered_at": datetime.now(timezone.utc)
                                     .isoformat().replace("+00:00", "Z"),
                 "resource_id": received.get("resource_id"),
                 "event_type": received.get("event_type")})
    conflicts[event_id] = rows
    save(CONFLICT_PATH, conflicts)


def handle(event: dict) -> None:
    """Completa reparación y notificación usando un registro durable por evento.

    Args:
        event: Objeto recibido de SQS, que se vuelve a validar aquí.
    Returns:
        None solo cuando reparación y aviso quedaron registrados como exitosos.
    Raises:
        ValidationError: Si el evento fue inyectado directo a SQS y no es válido.
        RetryableError: Si la simulación o una dependencia temporal falla.
        PermanentError: Conflicto de event_id, recurso no autorizado o falla final.
    Side Effects:
        Actualiza recursos, data/events.json, data/conflicts.json y el canal.
    Notes:
        Etapas en este orden: revalidar, vincular event_id al contenido,
        incrementar y persistir intentos, simular fallas, reparar, notificar.
        Cada etapa verificada se guarda ANTES de continuar con la siguiente, de
        modo que un corte entre "repair_done" y "notified" se retoma sin volver
        a reparar ni a reescribir etiquetas correctas.
        Un solo proceso modifica el ledger. Un timeout al notificar puede
        repetir el aviso: no hay transacción común entre JSON, AWS y Telegram.
    """
    inventory = load(INVENTORY_PATH)
    valid = validate(event, inventory)  # Revalida: el HTTP no protege a SQS.
    event_id = valid["event_id"]
    entry = load(LEDGER_PATH).get(event_id)
    if entry is None:
        entry = {"event_id": event_id, "event": valid, "attempts": 0,
                 "repair_done": False, "repair_result": None,
                 "notified": False, "tags": None}
    elif entry.get("event") != valid:  # Mismo ID, otro contenido: conflicto.
        _register_conflict(event_id, valid)  # Se registra aparte...
        raise PermanentError(  # ...y el mensaje se conserva sin ACK hasta la DLQ.
            "El event_id ya se usó con otro contenido")
    if entry["repair_done"] and entry["notified"]:  # Duplicado de algo terminado.
        return  # Sin efectos repetidos: ni reparación ni aviso.
    entry["attempts"] += 1  # Se guarda ANTES de simular fallas: sobrevive reinicios.
    _store(event_id, entry)
    if entry["attempts"] <= valid["simulate_failures"]:
        raise RetryableError("Falla simulada de laboratorio")  # Toca cero recursos.
    if not entry["repair_done"]:
        resource = inventory[valid["resource_id"]]  # Alias ya resuelto y validado.
        entry["repair_result"] = repair(resource, valid["event_type"])
        entry["tags"] = _current_tags(resource)  # Etiquetas verificadas por SDK.
        entry["repair_done"] = True  # Se persiste apenas termina la reparación.
        _store(event_id, entry)
    if not entry["notified"]:
        notify(f"Fénix: {event_id} ({valid['event_type']}) sobre "
               f"{valid['resource_id']}: {entry['repair_result']}")
        entry["notified"] = True  # Recién después de que el proveedor confirmó.
        _store(event_id, entry)
