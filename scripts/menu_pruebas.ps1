<#
.SYNOPSIS
    Menu interactivo de pruebas del Proyecto Fenix (terminal T3).
.DESCRIPTION
    Genera eventos con event_id nuevos, envia, fabrica fallas, muestra ledger,
    cola/DLQ, estado real por API y logs. El worker (T2) hace el trabajo en
    paralelo; aqui se prepara y se observa.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\menu_pruebas.ps1
#>
$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz
$py = Join-Path $raiz '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { Write-Host "Falta .venv: corre scripts\instalar.ps1" -ForegroundColor Red; exit 1 }

# Datos del laboratorio desde .env (sin mostrar secretos).
$envLimpio = Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=' }
function Leer-Env($nombre) {
    $linea = $envLimpio | Where-Object { $_ -match "^$nombre=" } | Select-Object -First 1
    if ($linea) { return ($linea -replace "^$nombre=", '') }
    return ''
}
$grupo  = Leer-Env 'GROUP_ID'; if (-not $grupo) { $grupo = 'grupo01' }
$prefijo = Leer-Env 'QUEUE_PREFIX'; if (-not $prefijo) { $prefijo = 'fenix-grupo01' }
$colaBase = "http://localhost:4566/000000000000"
$nombreCola = "$prefijo-events"
$nombreDlq  = "$prefijo-dlq"

function Titulo($t) { Write-Host "`n================ $t ================" -ForegroundColor Cyan }
function Info($t)   { Write-Host "  $t" -ForegroundColor Gray }
function Bien($t)   { Write-Host "  $t" -ForegroundColor Green }
function Mal($t)    { Write-Host "  $t" -ForegroundColor Red }
function Consejo($t){ Write-Host "  >> $t" -ForegroundColor Yellow }

function Ejecutar {
    param([scriptblock]$Comando)
    $previo = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Comando | Out-Null; $codigo = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previo }
    return $codigo
}

function Ver {
    param([scriptblock]$Comando)
    $previo = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Comando }
    finally { $ErrorActionPreference = $previo }
}

function Nuevo-Evento {
    param([string]$Tipo = 'instance.stopped', [string]$Recurso = 'node-01',
          [int]$Sim = 0, [string]$Archivo = 'examples\demo.json')
    $codigo = "import json,time,pathlib;" +
              "e={'schema_version':1,'event_id':'evt-'+time.strftime('%Y%m%d%H%M%S')," +
              "'event_type':'$Tipo','resource_id':'$Recurso'," +
              "'occurred_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())," +
              "'simulate_failures':$Sim};" +
              "pathlib.Path('$Archivo').write_text(json.dumps(e),encoding='utf-8');" +
              "print('  event_id:', e['event_id'], '| simulate_failures:', e['simulate_failures'])"
    Ver { & $py -c $codigo }
}

function Enviar {
    param([string]$Archivo = 'examples\demo.json')
    Ver { & $py -m fenix.cli send $Archivo }
    if ($LASTEXITCODE -ne 0) { Mal "send fallo (codigo $LASTEXITCODE)" } else { Bien "mensaje encolado; el worker (T2) lo procesa" }
}

# ---------------------------------------------------------------- opciones
function Opcion-Nominal {
    Titulo 'F02 - Flujo nominal: falla -> evento -> reparacion'
    Consejo 'Esperado en T2:  Fénix: evt-... (instance.stopped) sobre node-01: repaired'
    Consejo 'Esperado en Telegram: el aviso (si NOTIFY_PROVIDER=telegram)'
    Info '1/2 inyectando la falla en node-01...'
    Ver { & $py -m fenix.cli fault node-01 }
    if ($LASTEXITCODE -ne 0) { Mal "fault fallo (codigo $LASTEXITCODE)"; return }
    Nuevo-Evento
    Enviar
    Consejo 'Verificar luego con la opcion 10 (estado real debe quedar running) y 8 (ledger).'
}

function Opcion-Bucket {
    Titulo 'F03 - Falla de etiquetas del bucket'
    Info '1/2 inyectando: se quita Owner conservando el resto...'
    Ver { & $py -m fenix.cli fault bucket-01 }
    if ($LASTEXITCODE -ne 0) { Mal "fault fallo (codigo $LASTEXITCODE)"; return }
    Nuevo-Evento -Tipo 'bucket.tags_missing' -Recurso 'bucket-01'
    Enviar
    Consejo 'Verificar con la opcion 10: Owner debe volver y las etiquetas ajenas quedar intactas.'
}

function Opcion-Control {
    Titulo 'Control - evento valido sin falla fabricada'
    Nuevo-Evento
    Enviar
    Consejo 'Si el recurso ya cumple, el ledger dira repair_result=already_ok.'
}

function Opcion-Duplicado {
    Titulo 'F11 - Reenviar el MISMO archivo (duplicado)'
    if (-not (Test-Path 'examples\demo.json')) { Mal 'No hay examples\demo.json: usa antes la opcion 1'; return }
    Info 'Se vuelve a enviar el mismo body...'
    Enviar
    Consejo 'Esperado: 202 con message_id NUEVO, pero el ledger no cambia (opcion 8) y no hay segundo aviso.'
}

function Opcion-FalloTemporal {
    Titulo 'F09 - simulate_failures=2 (backoff 2 s y 4 s)'
    Consejo 'En T2 veras: fallo_procesando -> reintento_programado_en_2s -> ..._en_4s -> mensaje_confirmado'
    Nuevo-Evento -Sim 2
    Enviar
    Consejo 'El worker debe recibir 3 veces: falla, falla y recien a la tercera ACKea.'
}

function Opcion-DLQ {
    Titulo 'F10 - simulate_failures=5 -> mensaje a la DLQ'
    Consejo 'Despues de 3 recepciones SQS deriva solo a la DLQ: sin ACK y sin reparar.'
    Nuevo-Evento -Sim 5
    Enviar
    Info 'Dale unos 30 segundos (o tres procesamientos en T2) y vuelve a elegir 6 para leer la DLQ.'
    $leer = Read-Host 'Leer la DLQ ahora? (s/n)'
    if ($leer -match '^[sS]') { Opcion-Leer-DLQ }
}

function Opcion-Leer-DLQ {
    Titulo 'DLQ - lectura real sin eliminar el mensaje'
    # receive-message NO borra: el mensaje queda en la DLQ (se hace invisible un rato).
    Ver { docker compose exec -T localstack awslocal sqs receive-message `
        --queue-url "$colaBase/$nombreDlq" --max-number-of-messages 1 `
        --attribute-names ApproximateReceiveCount --output json }
    Consejo 'Conservalo antes de limpiar: esa es la evidencia (ver evidencias/).'
}

function Opcion-Negativos {
    do {
        Titulo 'F04-F07 - Rechazos del webhook (nada se encola)'
        Write-Host "  1) sin firma        -> 401     5) JSON roto      -> 400"
        Write-Host "  2) firma alterada   -> 401     6) lista JSON     -> 422"
        Write-Host "  3) firma de hace 1h -> 401     7) alias ajeno    -> 422"
        Write-Host "  4) claves extra     -> 422     8) cuerpo 17 KiB  -> 413"
        Write-Host "  9) caso valido      -> 202     0) volver"
        $n = Read-Host '  Caso'
        $mapa = @{ '1'='sin-firma'; '2'='firma-mala'; '3'='firma-vieja'; '4'='claves-extra'
                   '5'='json-roto'; '6'='lista'; '7'='alias-ajeno'; '8'='grande'; '9'='ok' }
        if ($mapa.ContainsKey($n)) {
            Ver { & $py "$raiz\scripts\enviar_pruebas.py" $mapa[$n] }
        }
    } while ($n -ne '0')
}

function Opcion-Ledger {
    Titulo 'Ledger - data/events.json'
    if (Test-Path 'data\events.json') { Get-Content 'data\events.json' -Raw } else { Mal 'No existe todavia' }
    if (Test-Path 'data\conflicts.json') { Info '--- conflictos registrados:'; Get-Content 'data\conflicts.json' -Raw }
}

function Opcion-Cola {
    Titulo "Cola principal y DLQ ($nombreCola)"
    Ver { docker compose exec -T localstack awslocal sqs get-queue-attributes `
        --queue-url "$colaBase/$nombreCola" `
        --attribute-names ApproximateNumberOfMessages ApproximateReceiveCount --output text }
    Info '--- DLQ:'
    Ver { docker compose exec -T localstack awslocal sqs get-queue-attributes `
        --queue-url "$colaBase/$nombreDlq" `
        --attribute-names ApproximateNumberOfMessages --output text }
}

function Opcion-Estado {
    Titulo 'Estado real verificado por API (awslocal)'
    Info "--- EC2 node-01 (etiqueta Name=fenix-$grupo-node-01):"
    Ver { docker compose exec -T localstack awslocal ec2 describe-instances `
        --filters "Name=tag:Name,Values=fenix-$grupo-node-01" `
        --query 'Reservations[0].Instances[0].{State:State.Name,InstanceId:InstanceId}' --output json }
    Info "--- S3 bucket-01 (fenix-$grupo-demo):"
    Ver { docker compose exec -T localstack awslocal s3api get-bucket-tagging `
        --bucket "fenix-$grupo-demo" --output json }
}

function Opcion-Logs {
    Titulo 'Ultimas lineas del log del worker'
    $ultimo = Get-ChildItem 'logs\fenix-worker-*.jsonl' -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime | Select-Object -Last 1
    if (-not $ultimo) { Mal 'Sin logs del worker todavia'; return }
    Info $ultimo.FullName
    Get-Content $ultimo.FullName -Tail 8
}

function Opcion-Idempotencia {
    Titulo 'F01 - init-queues y setup-demo una segunda vez'
    Consejo 'Esperado: mismas URLs e IDs (Instance/bucket); nada se duplica.'
    Ver { & $py -m fenix.cli init-queues }
    Ver { & $py -m fenix.cli setup-demo }
}

function Opcion-Pruebas {
    Titulo 'Pruebas unitarias (pytest -m "not integracion")'
    Ver { & $py -m pytest -q -m "not integracion" }
}

function Opcion-WorkerOnce {
    Titulo 'worker --once (procesa UN mensaje y sale)'
    Consejo 'Util si T2 no esta corriendo.'
    Ver { & $py -m fenix.cli worker --once }
}

# ---------------------------------------------------------------- bucle
Write-Host @"

   PROYECTO FENIX - menu de pruebas        (worker: T2 | receptor: T1)
   Carpeta: $raiz
"@ -ForegroundColor Cyan

$op = ''
do {
    Write-Host ''
    Write-Host '  1) Flujo nominal: fault node-01 + evento + send      (F02)' -ForegroundColor White
    Write-Host '  2) Falla de etiquetas del bucket + send              (F03)' -ForegroundColor White
    Write-Host '  3) Enviar evento valido sin falla fabricada          (control)' -ForegroundColor White
    Write-Host '  4) Reenviar el MISMO archivo (duplicado)             (F11)' -ForegroundColor White
    Write-Host '  5) Fallo temporal simulate_failures=2               (F09)' -ForegroundColor White
    Write-Host '  6) Fallo persistente -> DLQ                          (F10)' -ForegroundColor White
    Write-Host '  7) Rechazos del webhook: 401/400/422/413             (F04-F07)' -ForegroundColor White
    Write-Host '  8) Ver ledger (data/events.json)' -ForegroundColor White
    Write-Host '  9) Ver cola principal y DLQ' -ForegroundColor White
    Write-Host ' 10) Ver estado real EC2/S3 por API' -ForegroundColor White
    Write-Host ' 11) Ver ultimas lineas de log del worker' -ForegroundColor White
    Write-Host ' 12) Repetir init-queues + setup-demo (sin duplicar)   (F01)' -ForegroundColor White
    Write-Host '  13) Correr pruebas unitarias' -ForegroundColor White
    Write-Host '  14) Procesar 1 mensaje ahora (worker --once)' -ForegroundColor White
    Write-Host '  0) Salir' -ForegroundColor DarkGray
    $op = Read-Host '  Opcion'
    try {
        switch ($op) {
            '1'  { Opcion-Nominal }
            '2'  { Opcion-Bucket }
            '3'  { Opcion-Control }
            '4'  { Opcion-Duplicado }
            '5'  { Opcion-FalloTemporal }
            '6'  { Opcion-DLQ }
            '7'  { Opcion-Negativos }
            '8'  { Opcion-Ledger }
            '9'  { Opcion-Cola }
            '10' { Opcion-Estado }
            '11' { Opcion-Logs }
            '12' { Opcion-Idempotencia }
            '13' { Opcion-Pruebas }
            '14' { Opcion-WorkerOnce }
            '0'  { Write-Host '  Hasta luego.' -ForegroundColor Cyan }
            default { if ($op) { Write-Host '  Opcion no valida' -ForegroundColor Yellow } }
        }
    }
    catch {
        Mal "Error: $($_.Exception.Message)"
        Info  'El menu sigue vivo; el worker (T2) no se ve afectado.'
    }
} while ($op -ne '0')
