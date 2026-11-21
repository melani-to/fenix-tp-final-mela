<#
.SYNOPSIS
    Abre las 3 terminales del Proyecto Fénix listas para usar, sin importar
    dónde esté copiada la carpeta del proyecto.

.DESCRIPTION
    Chequea .venv, .env y LocalStack; si falta algo lo prepara (docker compose up,
    init-queues y setup-demo) y después abre:
        T1 - receptor (uvicorn en :8000)
        T2 - worker (consumidor de SQS)
        T3 - menú interactivo de pruebas (F01-F16)
    Si el puerto 8000 ya está ocupado (por un receptor anterior) no abre T1.

.PARAMETER DryRun
    No abre ventanas: muestra los comandos que ejecutaría.

.PARAMETER SinWorker
    No abre T2.

.PARAMETER SinMenu
    No abre T3.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\abrir_terminales.ps1
    powershell -ExecutionPolicy Bypass -File scripts\abrir_terminales.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SinReceptor,
    [switch]$SinWorker,
    [switch]$SinMenu
)

$ErrorActionPreference = 'Stop'
$raiz   = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $raiz '.venv\Scripts\python.exe'

function Aviso { param($t) Write-Host "    AVISO $t" -ForegroundColor Yellow }
function Ok    { param($t) Write-Host "    OK    $t" -ForegroundColor Green }

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

try {
    Write-Host "=== Proyecto Fenix - terminales ===" -ForegroundColor White
    Write-Host "    Carpeta: $raiz"

    # ---------------------------------------------------------- Preflight
    if (-not (Test-Path $venvPy)) {
        throw "No existe .venv. Corre primero:  powershell -ExecutionPolicy Bypass -File scripts\instalar.ps1"
    }
    if (-not (Test-Path (Join-Path $raiz '.env'))) {
        throw "No existe .env. Corre:  scripts\instalar.ps1"
    }

    # LocalStack: si no responde, se levanta y se espera.
    $salud = $false
    try { Invoke-RestMethod 'http://127.0.0.1:4566/_localstack/health' -TimeoutSec 3 | Out-Null; $salud = $true } catch { $salud = $false }
    if (-not $salud) {
        Aviso "LocalStack no responde: docker compose up -d"
        Push-Location $raiz
        try { Ver { docker compose up -d } } finally { Pop-Location }
        foreach ($i in 1..45) {
            try { Invoke-RestMethod 'http://127.0.0.1:4566/_localstack/health' -TimeoutSec 3 | Out-Null; $salud = $true; break }
            catch { Write-Host -NoNewline '.'; Start-Sleep -Seconds 2 }
        }
        Write-Host ''
        if (-not $salud) { throw "LocalStack no arranco en 90 s. Revisa: docker compose logs" }
    }
    Ok "LocalStack responde"

    # Inventario: si no existe, se preparan colas y laboratorio.
    if (-not (Test-Path (Join-Path $raiz 'data\inventory.json'))) {
        Aviso "Sin data\inventory.json: corriendo init-queues y setup-demo"
        Ver { & $venvPy -m fenix.cli init-queues }
        $codigo = $LASTEXITCODE
        if ($codigo -ne 0) { throw "init-queues fallo (codigo $codigo)" }
        Ver { & $venvPy -m fenix.cli setup-demo }
        $codigo = $LASTEXITCODE
        if ($codigo -ne 0) { throw "setup-demo fallo (codigo $codigo)" }
        Ok "Laboratorio preparado"
    }

    # ---------------------------------------------------------- Ventanas
    $puerto8000 = $false
    if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { $puerto8000 = $true }

    $cmdReceptor = "Set-Location '$raiz'; `$Host.UI.RawUI.WindowTitle='Fenix T1 - receptor :8000'; " +
                   "& '$venvPy' -m uvicorn fenix.services.receiver:app --host 127.0.0.1 --port 8000 --workers 1 --env-file .env"
    $cmdWorker   = "Set-Location '$raiz'; `$Host.UI.RawUI.WindowTitle='Fenix T2 - worker'; " +
                   "& '$venvPy' -m fenix.cli worker"
    $cmdMenu     = "Set-Location '$raiz'; `$Host.UI.RawUI.WindowTitle='Fenix T3 - menu de pruebas'; " +
                   "& '$raiz\scripts\menu_pruebas.ps1'"

    function Ventana {
        param($titulo, $comando, $omitir)
        if ($omitir) { return }
        if ($DryRun) {
            Write-Host "`n[$titulo]" -ForegroundColor Cyan
            Write-Host "  $comando"
            return
        }
        $argumentos = "-NoLogo -NoExit -Command `"$comando`""
        Start-Process powershell.exe -ArgumentList $argumentos
        Write-Host "    Abierta: $titulo" -ForegroundColor Green
    }

    if ($puerto8000) { Aviso "El puerto 8000 ya esta en uso: se omite T1 (ya hay un receptor corriendo)" }
    Ventana 'T1 receptor'  $cmdReceptor ($SinReceptor -or $puerto8000)
    Ventana 'T2 worker'    $cmdWorker   $SinWorker
    Ventana 'T3 pruebas'   $cmdMenu     $SinMenu

    if (-not $DryRun) {
        Write-Host @"

  Listo. En T3 elegi una opcion y segui el menu:
      1  flujo nominal (fault node-01 + evento nuevo + send) -> ver T2
      5  fallo temporal (backoff 2 y 4 s)
      6  fallo persistente -> DLQ
      7  rechazos del webhook (401/400/422/413)
      4  duplicado sin efectos
      8-11  ledger, cola/DLQ, estado real y logs
"@ -ForegroundColor Cyan
    }
    exit 0
}
catch {
    Write-Host "    ERROR $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
