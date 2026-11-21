<#
.SYNOPSIS
    Instala y deja funcionando el Proyecto Fénix desde cero en cualquier Windows.

.DESCRIPTION
    Detecta o instala Python y Docker Desktop (via winget), crea el entorno virtual,
    prepara .env, levanta LocalStack, crea colas y laboratorio, y corre las pruebas.
    Se puede ejecutar cuantas veces se quiera: todo lo existente se conserva.

.PARAMETER Reinstalar
    Recrea .venv aunque ya exista.

.PARAMETER LimpiaEstado
    Borra data/ y logs/ (laboratorio nuevo, p. ej. al llegar desde un pendrive).

.PARAMETER SinPruebas
    No ejecuta pytest (unitarias ni de integración).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\instalar.ps1 -LimpiaEstado
#>
[CmdletBinding()]
param(
    [switch]$Reinstalar,
    [switch]$LimpiaEstado,
    [switch]$SinPruebas
)

$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $PSScriptRoot     # Carpeta del proyecto, sea donde sea.
Set-Location $raiz

function Paso  { param($t) Write-Host "`n==> $t" -ForegroundColor Cyan }
function Ok    { param($t) Write-Host "    OK    $t" -ForegroundColor Green }
function Aviso { param($t) Write-Host "    AVISO $t" -ForegroundColor Yellow }
function Fallo { param($t) Write-Host "    ERROR $t" -ForegroundColor Red }

# Ejecuta un comando nativo (python/docker/winget) en silencio y devuelve SOLO
# su codigo de salida; el stderr no interrumpe el script.
function Ejecutar {
    param([scriptblock]$Comando)
    $previo = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Comando | Out-Null
        $codigo = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previo
    }
    return $codigo
}

# Igual que Ejecutar pero muestra la salida; despues leer $LASTEXITCODE.
function Ver {
    param([scriptblock]$Comando)
    $previo = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Comando }
    finally { $ErrorActionPreference = $previo }
}

# Igual que Ejecutar pero devuelve la salida estandar como texto (o $null si fallo).
function Texto {
    param([scriptblock]$Comando)
    $previo = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $salida = (& $Comando | Out-String).Trim()
        $codigo = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previo
    }
    if ($codigo -ne 0) { return $null }
    return $salida
}

try {
    Write-Host "=== Proyecto Fenix - instalacion desde cero ===" -ForegroundColor White
    Write-Host "    Carpeta: $raiz"

    # ---------------------------------------------------------------- 1. Python
    Paso "Python 3.11 o superior"
    $version = $null
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $version = Texto { python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" }
        if ($version -notmatch '^\d+\.\d+$') { $version = $null }
    }
    if (-not $version) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Aviso "No se detecto Python: instalando con winget (Python 3.13)"
            Ejecutar { winget install --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements --silent } | Out-Null
            $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                        [Environment]::GetEnvironmentVariable('Path', 'User')
            $version = Texto { python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" }
            if ($version -notmatch '^\d+\.\d+$') { $version = $null }
        }
    }
    if (-not $version) { throw "No hay Python instalado. Instala Python 3.11+ desde python.org y vuelve a correr este script." }
    $partes = $version.Split('.')
    if ([int]$partes[0] -lt 3 -or ([int]$partes[0] -eq 3 -and [int]$partes[1] -lt 11)) {
        throw "Python $version no sirve: hace falta 3.11 o superior."
    }
    Ok "Python $version"

    # ---------------------------------------------------------------- 2. Docker
    Paso "Docker Desktop"
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw "No hay docker ni winget. Instala Docker Desktop desde docker.com y vuelve a correr el script."
        }
        Aviso "Instalando Docker Desktop con winget (puede pedir permisos de administrador)"
        Ejecutar { winget install --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements --silent } | Out-Null
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
        Aviso "Si te pidio reiniciar, reinicia y vuelve a correr scripts\instalar.ps1"
    }
    $daemon = $false
    foreach ($intento in 1..45) {
        $codigoDocker = Ejecutar { docker info }
        if ($codigoDocker -eq 0) { $daemon = $true; break }
        if ($intento -eq 15) {   # A los 30 s se intenta abrir Docker Desktop.
            $exe = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
            if (Test-Path $exe) { Aviso "Abriendo Docker Desktop..."; Start-Process $exe }
        }
        Start-Sleep -Seconds 2
    }
    if (-not $daemon) { throw "Docker no responde. Abre Docker Desktop, espera que quede 'running' y vuelve a correr el script." }
    Ok "Docker operativo"

    # ---------------------------------------------------------------- 3. .env
    Paso "Variables de entorno (.env)"
    if (-not (Test-Path .env)) {
        Copy-Item .env.example .env
        Aviso ".env creado a partir de .env.example"
    }
    $envText = Get-Content .env -Raw
    if ($envText -notmatch '(?m)^WEBHOOK_SECRET=.+$') {
        $secret = Texto { python -c "import secrets; print(secrets.token_urlsafe(32))" }
        if (-not $secret) { throw "No se pudo generar WEBHOOK_SECRET" }
        $envText = $envText -replace '(?m)^WEBHOOK_SECRET=.*$', "WEBHOOK_SECRET=$secret"
        Ok "WEBHOOK_SECRET generado"
    }
    if ($envText -notmatch '(?m)^LOCALSTACK_IMAGE=.+$') {
        $envText += "`r`nLOCALSTACK_IMAGE=localstack/localstack:4.14.0`r`n"
    }
    # Sin token de licencia -> imagen community (con la de 2026 LocalStack sale con exit 55).
    if (($envText -match '(?m)^LOCALSTACK_AUTH_TOKEN=\s*$') -and
        ($envText -match '(?m)^LOCALSTACK_IMAGE=localstack/localstack:2026')) {
        $envText = $envText -replace '(?m)^LOCALSTACK_IMAGE=.*$', 'LOCALSTACK_IMAGE=localstack/localstack:4.14.0'
        Aviso "Sin LOCALSTACK_AUTH_TOKEN: se usa la imagen community localstack:4.14.0"
    }
    Set-Content .env -Value $envText -Encoding utf8 -NoNewline
    Ok ".env listo (fuera de Git)"

    # ------------------------------------------------------- 4. Entorno virtual
    Paso "Entorno virtual y dependencias"
    $venvPy = Join-Path $raiz '.venv\Scripts\python.exe'
    if ($Reinstalar -or -not (Test-Path $venvPy)) {
        Ejecutar { python -m venv .venv } | Out-Null
        if (-not (Test-Path $venvPy)) { throw "No se pudo crear .venv" }
    }
    $req = if (Test-Path 'requirements-lock.txt') { 'requirements-lock.txt' } else { 'requirements.txt' }
    # Un pip freeze puede traer el propio proyecto como editable o ruta local:
    # eso no se reinstala (el paquete entra con "pip install -e ." de mas abajo).
    $lineas = Get-Content $req | Where-Object {
        $_ -notmatch '^\s*-e\s' -and $_ -notmatch 'file://' -and $_ -notmatch 'Editable install'
    }
    $depsTemp = Join-Path ([System.IO.Path]::GetTempPath()) "fenix-deps-$PID.txt"
    Set-Content -Path $depsTemp -Value $lineas -Encoding utf8
    Ejecutar { & $venvPy -m pip install --quiet --upgrade pip } | Out-Null
    $codigo = Ejecutar { & $venvPy -m pip install --quiet -r $depsTemp }
    Remove-Item $depsTemp -ErrorAction SilentlyContinue
    if ($codigo -ne 0) { throw "Fallo la instalacion de dependencias ($req)" }
    $codigo = Ejecutar { & $venvPy -m pip install --quiet -e . }
    if ($codigo -ne 0) { throw "Fallo pip install -e ." }
    Ver { & $venvPy -c "import fenix, boto3, fastapi, requests, dotenv; print('    paquetes instalados, boto3', boto3.__version__)" }
    Ok "venv + paquetes instalados"

    # ------------------------------------------------------------- 5. Estado
    if ($LimpiaEstado) {
        Paso "Limpiando estado del laboratorio"
        Remove-Item data -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item logs -Recurse -Force -ErrorAction SilentlyContinue
        Ok "data/ y logs/ eliminados"
    }

    # --------------------------------------------------------- 6. LocalStack
    Paso "Levantando LocalStack (docker compose up -d)"
    Ver { docker compose up -d }
    $codigo = $LASTEXITCODE
    if ($codigo -ne 0) { throw "docker compose up fallo (codigo $codigo). Revisa: docker compose logs" }
    $salud = $false
    foreach ($intento in 1..45) {
        try { Invoke-RestMethod 'http://127.0.0.1:4566/_localstack/health' -TimeoutSec 3 | Out-Null; $salud = $true; break }
        catch { Write-Host -NoNewline '.'; Start-Sleep -Seconds 2 }
    }
    Write-Host ''
    if (-not $salud) {
        Write-Host "--- ultimas lineas del contenedor ---" -ForegroundColor Yellow
        Ver { docker compose logs --tail 20 }
        throw "LocalStack no respondio en 90 s (revisa LOCALSTACK_AUTH_TOKEN / docker compose logs)."
    }
    Ok "LocalStack responde en localhost:4566"

    # --------------------------------------------------- 7. Colas y laboratorio
    Paso "Colas (init-queues) y laboratorio (setup-demo)"
    Ver { & $venvPy -m fenix.cli init-queues }
    $codigo = $LASTEXITCODE
    if ($codigo -ne 0) { throw "init-queues fallo (codigo $codigo)" }
    Ver { & $venvPy -m fenix.cli setup-demo }
    $codigo = $LASTEXITCODE
    if ($codigo -ne 0) { throw "setup-demo fallo (codigo $codigo)" }
    Ok "Colas, instancia mock y bucket creados/verificados"

    # -------------------------------------------------------------- 8. Pruebas
    if (-not $SinPruebas) {
        Paso "Pruebas unitarias (sin necesitar Docker)"
        Ver { & $venvPy -m pytest -q -m "not integracion" }
        $codigo = $LASTEXITCODE
        if ($codigo -ne 0) { throw "Fallaron pruebas unitarias (codigo $codigo). Corrige antes de continuar." }
        Ok "Unitarias en verde"

        Paso "Pruebas de integracion sobre LocalStack (unos 4 minutos)"
        Ver { & $venvPy -m pytest -q -m integracion }
        $codigo = $LASTEXITCODE
        if ($codigo -ne 0) { throw "Fallaron pruebas de integracion (codigo $codigo)." }
        Ok "Integracion en verde"
    }

    # ------------------------------------------------------------- 9. Resumen
    Write-Host "`n=== INSTALACION COMPLETA ===" -ForegroundColor Green
    Write-Host @"
  Proyecto   : $raiz
  Python     : $version   (venv en .venv)
  LocalStack : http://localhost:4566   (imagen: LOCALSTACK_IMAGE en .env)
  Receptor   : python -m uvicorn fenix.services.receiver:app --host 127.0.0.1 --port 8000 --workers 1 --env-file .env
  Worker     : python -m fenix.cli worker
  Avisos     : NOTIFY_PROVIDER en .env (console | telegram | slack)

  Siguiente paso: abrir las 3 terminales con el menu de pruebas
      powershell -ExecutionPolicy Bypass -File scripts\abrir_terminales.ps1
"@
    exit 0
}
catch {
    Fallo $_.Exception.Message
    Write-Host "`nRevisa lo indicado y vuelve a ejecutar:  scripts\instalar.ps1" -ForegroundColor Yellow
    exit 1
}
