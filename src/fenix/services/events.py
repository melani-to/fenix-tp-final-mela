"""Contrato de eventos de autorreparación; integrante 1."""
from fenix.utils.errors import ValidationError
from fenix.utils.validation import identifier, utc_text

# Campos exactos del evento: ni uno menos ni uno más.
FIELDS = {"schema_version", "event_id", "event_type", "resource_id",
          "occurred_at", "simulate_failures"}

# Cada tipo de evento solo apunta a un tipo de recurso del inventario.
# setup-demo publica "type": "ec2" para node-01 y "type": "s3" para bucket-01.
EVENT_TYPES = {"instance.stopped": "ec2", "bucket.tags_missing": "s3"}


def validate(event: dict, inventory: dict) -> dict:
    """Valida esquema cerrado y resuelve exclusivamente aliases autorizados.

    Args:
        event: JSON objeto con schema_version, event_id, event_type, resource_id,
            occurred_at y simulate_failures.
        inventory: Mapa alias -> recurso, preparado por setup-demo y revisado por
            el grupo; solo se consulta, nunca se modifica.
    Returns:
        Copia validada del evento; no modifica inventory ni el evento original.
    Raises:
        ValidationError: Tipo, campo, timestamp o recurso no permitido.
    Notes:
        event_type solo admite instance.stopped y bucket.tags_missing.
        simulate_failures es de laboratorio: entero de 0 a 5, excluyendo bool.
        type(...) is int descarta True/False, que en Python son subclases de int.
        El vínculo event_id -> contenido no se guarda aquí: validate es pura y no
        persiste nada. El conflicto de reutilización lo resuelve handle (I5-B)
        contra data/events.json, que es el único lugar donde hay historia.
    """
    if not isinstance(event, dict):  # El cuerpo JSON podría ser lista, número o texto.
        raise ValidationError("El evento debe ser un objeto JSON")
    if set(event) != FIELDS:  # Rechaza claves ausentes y claves extra a la vez.
        raise ValidationError("El evento debe tener exactamente los campos del contrato")
    if type(event["schema_version"]) is not int or event["schema_version"] != 1:
        raise ValidationError("schema_version debe ser el entero 1")
    identifier(event["event_id"])  # Patrón [a-z0-9-]{3,32} aplicado al identificador.
    identifier(event["resource_id"])  # Un alias ajeno ni siquiera llega al inventario.
    utc_text(event["occurred_at"])  # UTC explícito; no confiar en la hora local.
    failures = event["simulate_failures"]
    if type(failures) is not int or not 0 <= failures <= 5:
        raise ValidationError("simulate_failures debe ser un entero entre 0 y 5")
    event_type = event["event_type"]
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise ValidationError("event_type no permitido")
    if not isinstance(inventory, dict):  # Sin inventario no hay alias autorizados.
        raise ValidationError("Inventario no disponible")
    record = inventory.get(event["resource_id"])  # Solo aliases conocidos.
    if not isinstance(record, dict):
        raise ValidationError("resource_id no pertenece al inventario autorizado")
    if record.get("type") != EVENT_TYPES[event_type]:
        # instance.stopped sobre un bucket sería un evento ajeno a su recurso.
        raise ValidationError("El tipo de evento no corresponde al tipo de recurso")
    return dict(event)  # Copia: quien recibe no puede alterar la entrada original.
