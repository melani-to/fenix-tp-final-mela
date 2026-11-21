"""Observabilidad (I4): F16 de rotación gzip y demostración de los cinco niveles."""
import gzip  # Descomprime el histórico rotado.
import json  # Cada línea debe ser JSON parseable.
from pathlib import Path

import pytest

from fenix.utils.logging_json import setup as setup_logs

SECRETO = "sentinela-de-secreto-que-no-debe-aparecer"


def _detener(logger, listener) -> str:
    """Cierra el pipeline y devuelve la ruta del archivo de log vigente."""
    listener.stop()  # Drena lo pendiente antes de leer el archivo.
    return str(next(Path("logs").glob("*.jsonl")))


@pytest.mark.pendiente
def test_f16_rotacion_gzip_lineas_json_y_utc(tmp_path, monkeypatch):
    """F16: fuerza rotación con tamaño reducido, abre el .gz y parsea cada línea.

    Returns:
        None si todas las líneas son JSON válido, con UTC y contexto correlacionado.
    Raises:
        AssertionError: Si una línea no parsea, falta UTC o aparece un secreto.
    Side Effects:
        Genera y descomprime archivos de log dentro de una carpeta temporal.
    """
    monkeypatch.chdir(tmp_path)  # Los logs de la prueba no tocan los reales.
    logger, listener = setup_logs("f16", max_bytes=512)  # Rotación forzada, ~2 líneas.
    try:
        for index in range(40):  # Suficiente volumen para varias rotaciones.
            logger.info("evento_de_prueba",
                        extra={"event_id": f"evt-{index}", "message_id": "m-1",
                               "resource_id": "node-01", "attempt": index,
                               "action": "demo", "status": "ok"})
        logger.error("fallo_simulado", extra={"event_id": "evt-err",
                                              "status": "TimeoutError"})
    finally:
        listener.stop()

    files = list(Path("logs").iterdir())
    compressed = [path for path in files if path.suffix == ".gz"]
    assert compressed, "La rotación no produjo ningún histórico comprimido"

    rows = []
    for path in compressed:  # Se abre cada histórico y se parsea línea por línea.
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                rows.append(json.loads(line))  # Toda línea es JSON válido.
    assert rows, "El histórico comprimido quedó vacío"  # La rotación conservó datos.
    actuales = [json.loads(line) for line in
                next(Path("logs").glob("*.jsonl")).read_text(
                    encoding="utf-8").splitlines()]
    for row in rows + actuales:  # Contrato de campos de la consigna §4.2.6.
        assert row["timestamp"].endswith("+00:00")  # UTC explícito.
        assert row["level"] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        assert set(row) >= {"event_id", "message_id", "resource_id", "attempt",
                            "action", "status"}
        assert SECRETO not in json.dumps(row)  # Ningún secreto en el histórico.
    assert any(row["level"] == "ERROR" for row in actuales)  # Fallo registrado.
    assert all(json.loads(line) for line in  # Todo el archivo vigente parsea.
               next(Path("logs").glob("*.jsonl")).read_text(
                   encoding="utf-8").splitlines())


@pytest.mark.pendiente
def test_cinco_niveles_y_campos_nulos(tmp_path, monkeypatch):
    """Demuestra DEBUG, INFO, WARNING, ERROR y CRITICAL con casos propios.

    Returns:
        None si los cinco niveles quedan registrados con JSON válido.
    Raises:
        AssertionError: Si falta un nivel o los campos no aplicables no son null.
    Notes:
        DEBUG: paso interno del worker; INFO: confirmación; WARNING: fallo de
        procesamiento; ERROR: fallo de transporte; CRITICAL: configuración de
        arranque inválida (§4.2.6). Ningún fallo normal se eleva a CRITICAL.
    """
    monkeypatch.chdir(tmp_path)
    logger, listener = setup_logs("niveles")
    try:
        logger.debug("mensaje_parseado", extra={"action": "parse"})
        logger.info("mensaje_confirmado",
                    extra={"event_id": "evt-001", "action": "ack",
                           "status": "acknowledged", "attempt": 1})
        logger.warning("fallo_procesando", extra={"status": "ValidationError"})
        logger.error("fallo_receive", extra={"status": "EndpointConnectionError"})
        logger.critical("configuracion_invalida", extra={"status": "KeyError"})
    finally:
        listener.stop()

    rows = [json.loads(line)
            for line in next(Path("logs").glob("*.jsonl")).read_text(
                encoding="utf-8").splitlines()]
    assert [row["level"] for row in rows] == ["DEBUG", "INFO", "WARNING",
                                              "ERROR", "CRITICAL"]
    assert rows[0]["event_id"] is None  # Campos no aplicables representados null.
    assert rows[1]["status"] == "acknowledged"  # ACK registrado tras el SDK.
    assert rows[4]["message"] == "configuracion_invalida"  # Cierre controlado.


@pytest.mark.pendiente
def test_log_nunca_registra_el_secreto(tmp_path, monkeypatch):
    """El valor del secreto del entorno jamás llega al archivo de log.

    Returns:
        None si el secreto no aparece en ninguna línea registrada.
    Raises:
        AssertionError: Si el log contiene la sentinela del secreto.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRETO)
    logger, listener = setup_logs("seguridad")
    try:
        logger.error("fallo_controlado",
                     extra={"status": "ValidationError",
                            "event_id": "evt-secreto"})  # Contexto público sólo.
    finally:
        listener.stop()
    contenido = next(Path("logs").glob("*.jsonl")).read_text(encoding="utf-8")
    assert SECRETO not in contenido  # Sin secretos ni firmas completas.
    assert "evt-secreto" in contenido  # El contexto público sí se conserva.
