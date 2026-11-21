"""Cliente de pruebas negativas del webhook Fénix.

Reproduce en vivo los rechazos de la matriz (F04-F07) contra el receptor corriendo
en el puerto 8000. Nunca publica en SQS si el receptor funciona correctamente.

Uso:
    python scripts/enviar_pruebas.py ok
    python scripts/enviar_pruebas.py sin-firma
    python scripts/enviar_pruebas.py firma-vieja
    python scripts/enviar_pruebas.py json-roto
    python scripts/enviar_pruebas.py lista
    python scripts/enviar_pruebas.py alias-ajeno
    python scripts/enviar_pruebas.py claves-extra
    python scripts/enviar_pruebas.py firma-mala
    python scripts/enviar_pruebas.py grande
"""
import argparse  # Casos reproducibles por nombre.
import os  # Secreto y configuración desde .env.
import time  # Timestamp y event_id frescos.

import requests  # Cliente HTTP con timeout acotado.
from dotenv import load_dotenv

from fenix.utils.crypto import signature  # Misma firma que usa la CLI.

load_dotenv()  # WEBHOOK_SECRET y configuración del laboratorio.
URL = os.environ.get("FENIX_WEBHOOK_URL", "http://127.0.0.1:8000/webhook")


def evento(nuevo: bool = True) -> dict:
    """Arma un evento válido; con nuevo=True genera un event_id distinto."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    event = {"schema_version": 1,
             "event_id": "evt-node-001" if not nuevo else "evt-" + time.strftime("%H%M%S"),
             "event_type": "instance.stopped", "resource_id": "node-01",
             "occurred_at": stamp, "simulate_failures": 0}
    return event


def cabecera(body: bytes, offset: int = 0, alterar: bool = False) -> dict:
    """Cabecera X-Webhook-Signature válida, vieja o alterada según se pida."""
    secret = os.environ["WEBHOOK_SECRET"]
    stamp = int(time.time()) + offset
    digest = signature(secret, stamp, body)
    if alterar:  # Se cambia un solo carácter: HMAC inválida con formato correcto.
        digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    return {"X-Webhook-Signature": f"t={stamp},v1={digest}"}


def main() -> int:
    """Ejecuta el caso elegido e informa código y cuerpo de la respuesta."""
    parser = argparse.ArgumentParser(description="Pruebas negativas del webhook")
    parser.add_argument("caso", choices=["ok", "sin-firma", "firma-mala",
                                         "firma-vieja", "json-roto", "lista",
                                         "alias-ajeno", "claves-extra", "grande"])
    parser.add_argument("--esperado", default="", help="Código HTTP esperado")
    args = parser.parse_args()

    headers, body = {}, b""
    if args.caso == "ok":
        body = str.encode(__import__("json").dumps(evento()))
        headers = cabecera(body)
    elif args.caso == "sin-firma":
        body = str.encode(__import__("json").dumps(evento()))  # Sin cabecera.
    elif args.caso == "firma-mala":
        body = str.encode(__import__("json").dumps(evento()))
        headers = cabecera(body, alterar=True)
    elif args.caso == "firma-vieja":
        body = str.encode(__import__("json").dumps(evento()))
        headers = cabecera(body, offset=-3600)  # Fuera de la ventana de 300 s.
    elif args.caso == "json-roto":
        body = b'{"event_id": roto'  # Sintaxis inválida con firma correcta.
        headers = cabecera(body)
    elif args.caso == "lista":
        body = b"[1, 2]"  # JSON válido pero no es un objeto: contrato 422.
        headers = cabecera(body)
    elif args.caso == "alias-ajeno":
        body = str.encode(__import__("json").dumps(
            {**evento(), "resource_id": "ajeno-99"}))
        headers = cabecera(body)
    elif args.caso == "claves-extra":
        body = str.encode(__import__("json").dumps({**evento(), "extra": 1}))
        headers = cabecera(body)
    elif args.caso == "grande":
        body = b"x" * 17000  # 16 KiB: debe rechazarse antes de cualquier otra cosa.
        headers = cabecera(body)

    # requests usa data= para bytes crudos; content= es de httpx.
    response = requests.post(URL, data=body, headers=headers, timeout=(2, 15))
    print(f"HTTP {response.status_code}  (esperado: {args.esperado or 'n/d'})")
    print(response.text[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
