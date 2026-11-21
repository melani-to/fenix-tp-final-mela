"""Laboratorio acotado de recursos. Integrante 2 prepara, integrante 6 verifica."""
import time  # Acota las consultas de estado sin esperas indefinidas.
from botocore.exceptions import ClientError
from fenix.config.settings import client
from fenix.utils.state import load, save
from fenix.utils.validation import identifier
from fenix.utils.errors import ValidationError, PermanentError
from fenix.services.cloud_support import (aws_tags, create_group, create_instance,
                                          mock_inputs)

OWNERSHIP = ("ManagedBy", "Project", "GroupId", "Owner")  # Tags de pertenencia.
TAGS_TEMPLATE = {"ManagedBy": "ppa2", "Project": "fenix"}  # Constantes del TP.


def _tags_for(group: str) -> dict[str, str]:
    """Arma la política de etiquetas propia de este grupo.

    Args:
        group: Identificador del grupo ya validado.
    Returns:
        Mapa con ManagedBy, Project, GroupId y Owner del grupo.
    """
    return {**TAGS_TEMPLATE, "GroupId": group, "Owner": group}


def _read_tags(s3, bucket: str) -> dict[str, str] | None:
    """Lee tags del bucket; None si no existe, {} si está sin etiquetar.

    Args:
        s3: Cliente S3 del emulador.
        bucket: Nombre del bucket a consultar.
    Returns:
        Diccionario clave->valor, {} sin etiquetas o None si no existe.
    Raises:
        ClientError: Si el fallo no es de existencia ni de etiquetas.
    """
    try:
        response = s3.get_bucket_tagging(Bucket=bucket)
    except ClientError as error:  # Se clasifican sólo los códigos conocidos.
        code = error.response.get("Error", {}).get("Code", "")
        if code == "NoSuchBucket":
            return None
        if code == "NoSuchTagSet":
            return {}  # Bucket creado sin su segunda llamada de etiquetado.
        raise
    return {row["Key"]: row["Value"] for row in response["TagSet"]}


def _ownership_ok(actual: dict[str, str], expected: dict[str, str]) -> bool:
    """Comprueba que un recurso existente pertenezca a este grupo.

    Args:
        actual: Etiquetas leídas por SDK.
        expected: Etiquetas que el laboratorio exige.
    Returns:
        True si las cuatro etiquetas de pertenencia coinciden.
    """
    return all(actual.get(key) == expected[key] for key in OWNERSHIP)


def _live_instances(ec2, instance_id: str) -> list[dict]:
    """Devuelve la instancia inventariada si existe y no está terminada.

    Args:
        ec2: Cliente EC2 del emulador.
        instance_id: InstanceId guardado en el progreso del laboratorio.
    Returns:
        Lista con la instancia viva, o lista vacía si no existe o terminó.
    Raises:
        ClientError: Si el fallo no es de identificador desconocido.
    """
    try:
        reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code.startswith("InvalidInstanceID"):  # ID obsoleto: se recrea, no revienta.
            return []
        raise
    instances = reservations[0]["Instances"] if reservations else []
    if instances and instances[0]["State"]["Name"] in ("terminated", "shutting-down"):
        return []  # Una instancia terminada no se reutiliza.
    return instances


def setup_demo(group: str) -> dict:
    """Prepara una instancia mock y un bucket a partir de identificadores estables.

    Args:
        group: ID validado del grupo; determina prefijos de los recursos.
    Returns:
        Inventario con aliases node-01 y bucket-01 y sus IDs reales del emulador.
    Raises:
        ValidationError: Si el identificador del grupo no cumple el patrón.
        ClientError: Si el emulador no soporta una operación o falla su ejecución.
        PermanentError: Si un recurso existente contradice la propiedad esperada.
    Side Effects:
        Crea recursos de laboratorio etiquetados y publica data/inventory.json.
    Notes:
        Idempotente: consulta por tags y nombre antes de crear; si encuentra,
        conserva los IDs y sólo re-verifica. Nunca adopta un recurso cuya
        identidad contradice el inventario: en ese caso explica y se detiene.
        El avance se guarda en data/setup-{group}.json tras cada creación, de
        modo que una interrupción se retoma sin duplicar recursos.
        ClientToken estable para no duplicar la instancia en un reintento.
    """
    group = identifier(group)  # Nada se construye con un grupo inválido.
    tags = _tags_for(group)
    progress_path = f"data/setup-{group}.json"
    progress = load(progress_path)

    if not progress.get("vpc_id") or not progress.get("image_id"):
        progress["vpc_id"], progress["image_id"] = mock_inputs()  # VPC + AMI mock.
        save(progress_path, progress)  # Se conserva aunque falle después.

    ec2 = client("ec2")
    sg_name = f"fenix-{group}-sg"
    found = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [progress["vpc_id"]]},
        {"Name": "group-name", "Values": [sg_name]}])["SecurityGroups"]
    if len(found) > 1:
        raise PermanentError("Hay varios grupos de seguridad con el nombre del laboratorio")
    if found:
        group_id = found[0]["GroupId"]  # Se reutiliza el existente.
    else:
        group_id = create_group(sg_name, progress["vpc_id"], tags)  # Sin puertos.
    # Verificación por SDK, no por memoria: el SG debe ser del grupo y del mismo ID.
    current_sg = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
    sg_tags = {tag["Key"]: tag["Value"] for tag in current_sg.get("Tags", [])}
    if not _ownership_ok(sg_tags, tags):
        raise PermanentError("El grupo de seguridad no pertenece a este laboratorio")
    saved = progress.get("security_group_id")
    if saved and saved != group_id:
        raise PermanentError("El grupo de seguridad registrado cambió de identidad")
    progress["security_group_id"] = group_id
    save(progress_path, progress)

    node_name = f"fenix-{group}-node-01"
    node_tags = {**tags, "Name": node_name}
    instance_id = progress.get("instance_id")
    found_instances = []
    if instance_id:  # Se reutiliza el ID guardado si sigue vigente.
        found_instances = _live_instances(ec2, instance_id)
        if not found_instances:  # Desaparecida o terminada: se vuelve a buscar.
            instance_id = None

    if not found_instances:  # Busca por tags antes de crear: sin duplicados.
        candidates = [
            row for reservation in ec2.describe_instances(Filters=[
                {"Name": "tag:Project", "Values": ["fenix"]},
                {"Name": "tag:GroupId", "Values": [group]},
                {"Name": "tag:Name", "Values": [node_name]},
                {"Name": "instance-state-name",
                 "Values": ["pending", "running", "stopping", "stopped"]}])
            ["Reservations"] for row in reservation["Instances"]]
        if len(candidates) > 1:
            raise PermanentError("Hay varias instancias con el nombre del laboratorio")
        if candidates:
            instance_id = candidates[0]["InstanceId"]  # Recuperada, no creada.
        else:
            # Token estable y ASCII: un reintento no duplica la instancia (R10).
            instance_id = create_instance(progress["image_id"], group_id,
                                          f"fenix-{group}-node-01", node_tags)
        progress["instance_id"] = instance_id
        save(progress_path, progress)  # Persistido inmediatamente tras crear.
        found_instances = ec2.describe_instances(
            InstanceIds=[instance_id])["Reservations"][0]["Instances"]

    instance = found_instances[0]
    instance_tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    attached = {row["GroupId"] for row in instance["SecurityGroups"]}
    if (any(instance_tags.get(key) != node_tags[key] for key in OWNERSHIP + ("Name",))
            or group_id not in attached):
        raise PermanentError("La instancia existente no pertenece a este laboratorio")
    state = instance["State"]["Name"]
    if state == "pending":  # Espera acotada con el waiter del SDK.
        ec2.get_waiter("instance_running").wait(
            InstanceIds=[instance_id], WaiterConfig={"Delay": 2, "MaxAttempts": 10})
    elif state not in {"running", "stopping", "stopped"}:
        raise PermanentError(f"La instancia no se puede reutilizar en estado {state}")

    s3 = client("s3")
    bucket = f"fenix-{group}-demo"
    actual = _read_tags(s3, bucket)
    if actual is None:  # No existe: se crea y luego se etiqueta en otra llamada.
        s3.create_bucket(Bucket=bucket)  # us-east-1 no admite LocationConstraint.
        progress["untagged_bucket"] = bucket  # Marca del fallo parcial posible.
        save(progress_path, progress)
        actual = {}
    created_here = progress.get("untagged_bucket") == bucket and not actual
    if not created_here and not _ownership_ok(actual, tags):
        # Un bucket homónimo ajeno jamás se adopta ni se reetiqueta.
        raise PermanentError("El bucket existente no pertenece a este laboratorio")
    merged = {**actual, **tags}  # Se completan sólo las obligatorias.
    if any(actual.get(key) != merged[key] for key in OWNERSHIP):
        s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": aws_tags(merged)})
    verified = _read_tags(s3, bucket) or {}
    if not _ownership_ok(verified, tags):
        raise PermanentError("No se pudieron verificar las etiquetas del bucket")
    if progress.pop("untagged_bucket", None) is not None:
        save(progress_path, progress)  # El fallo parcial quedó subsanado.

    inventory = {
        "node-01": {"type": "ec2", "id": instance_id,
                    "image_id": instance["ImageId"], "vpc_id": progress["vpc_id"],
                    "security_group_id": group_id, "expected_tags": node_tags},
        "bucket-01": {"type": "s3", "id": bucket, "expected_tags": tags},
    }
    save("data/inventory.json", inventory)  # Se publica sólo con propiedad verificada.
    return inventory


def inject_fault(alias: str) -> None:
    """Detiene el nodo o elimina una etiqueta del bucket de laboratorio.

    Args:
        alias: node-01 o bucket-01 presente en inventory.json.
    Returns:
        None cuando el fallo simulado se verifica mediante el SDK.
    Raises:
        ValidationError: Si el alias no pertenece al inventario.
        PermanentError: Si el recurso no está en un estado reparable.
        ClientError: Si el emulador rechaza la operación.
    Side Effects:
        Modifica únicamente el recurso de prueba de ese grupo; no envía eventos.
    Notes:
        El alias se resuelve siempre contra data/inventory.json: jamás se acepta
        un InstanceId o nombre de bucket libre desde la línea de comandos.
        Las consultas de estado están limitadas a un número fijo de intentos.
    """
    inventory = load("data/inventory.json")
    resource = inventory.get(alias)
    if not isinstance(resource, dict):  # Alias desconocido: nada se toca.
        raise ValidationError("El alias no pertenece al inventario del grupo")
    if resource["type"] == "ec2":
        ec2 = client("ec2")
        instance_id = resource["id"]
        state = _instance_state(ec2, instance_id)
        if state == "stopped":  # Reejecutar es inocuo: no duplica el problema.
            print(f"{alias} ya estaba detenido")
            return
        if state not in ("running", "stopping"):  # terminated no es inyectable.
            raise PermanentError(f"No se puede detener un nodo en estado {state}")
        if state == "running":
            ec2.stop_instances(InstanceIds=[instance_id])
        # Consultas limitadas: sin esperas largas ni bucles indefinidos.
        for _ in range(20):
            if _instance_state(ec2, instance_id) == "stopped":
                print(f"{alias} verificado en estado stopped")
                return
            time.sleep(1)
        raise PermanentError("El nodo no llegó a stopped dentro del límite de consultas")
    if resource["type"] == "s3":
        s3 = client("s3")
        current = _read_tags(s3, resource["id"])
        if current is None:
            raise PermanentError("El bucket del inventario no existe")
        if "Owner" not in current:
            print(f"{alias} ya no tiene la etiqueta Owner")  # Idempotente.
            return
        remaining = {key: value for key, value in current.items() if key != "Owner"}
        # Sólo se quita Owner: el resto de las etiquetas se conserva intacto.
        s3.put_bucket_tagging(Bucket=resource["id"],
                              Tagging={"TagSet": aws_tags(remaining)})
        after = _read_tags(s3, resource["id"]) or {}
        if "Owner" in after:
            raise PermanentError("La etiqueta Owner sigue presente tras la inyección")
        print(f"{alias} quedó sin Owner y con el resto de las etiquetas")
        return
    raise ValidationError("El recurso del inventario no es de un tipo conocido")


def _instance_state(ec2, instance_id: str) -> str:
    """Lee el estado actual de la instancia inventariada.

    Args:
        ec2: Cliente EC2 del emulador.
        instance_id: InstanceId leído del inventario.
    Returns:
        Nombre del estado: pending, running, stopping o stopped.
    Raises:
        PermanentError: Si la instancia del inventario no existe.
    """
    try:
        reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code.startswith("InvalidInstanceID"):
            raise PermanentError("La instancia del inventario no existe") from error
        raise
    if not reservations or not reservations[0]["Instances"]:
        raise PermanentError("La instancia del inventario no existe")
    return reservations[0]["Instances"][0]["State"]["Name"]
