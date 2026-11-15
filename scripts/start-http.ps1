# Start ssh-mcp in resident HTTP mode: one credential set + one connection pool
# shared by every client session.
#
#   .\scripts\start-http.ps1                     # foreground (Ctrl+C to stop)
#   .\scripts\start-http.ps1 -Background         # background, logs go to logs\
#   .\scripts\start-http.ps1 -Port 8765 -AllowAnyHost
#
# Kept ASCII-only on purpose: Windows PowerShell 5.1 may misread UTF-8 .ps1 files
# without a BOM, which breaks parsing on Chinese code pages.

param(
    [string]$Config = (Join-Path (Split-Path $PSScriptRoot -Parent) 'servers.yaml'),
    [string]$Listen = '127.0.0.1',
    [int]$Port = 8765,
    [string]$Token = $env:SSH_MCP_TOKEN,
    [switch]$AllowAnyHost,
    [switch]$NoAuth,
    [switch]$Background
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "venv interpreter not found: $python (create .venv and run: pip install -e .)"
}
if (-not (Test-Path $Config)) {
    throw "config not found: $Config (copy servers.example.yaml to servers.yaml first)"
}

if ($NoAuth) {
    $Token = ''
    Remove-Item Env:SSH_MCP_TOKEN -ErrorAction SilentlyContinue
}
elseif (-not $Token) {
    $tokenFile = Join-Path $HOME '.ssh-mcp\token'
    if (Test-Path $tokenFile) {
        $Token = (Get-Content $tokenFile -Raw).Trim()
    }
    else {
        $chars = (48..57) + (65..90) + (97..122) | ForEach-Object { [char]$_ }
        $Token = -join (1..56 | ForEach-Object { $chars | Get-Random })
        New-Item -ItemType Directory -Force -Path (Split-Path $tokenFile) | Out-Null
        Set-Content -Path $tokenFile -Value $Token -NoNewline
        Write-Host "generated token, saved to $tokenFile"
    }
}

$env:SSH_MCP_TOKEN = $Token

$cliArgs = @(
    '-m', 'ssh_mcp',
    '--transport', 'http',
    '--host', $Listen,
    '--port', "$Port",
    '--config', $Config
)
if ($Token) { $cliArgs += @('--token', $Token) }
if ($AllowAnyHost) { $cliArgs += '--allow-any-host' }

Write-Host "config = $Config"
Write-Host "mcp    = http://${Listen}:${Port}/mcp"
if ($NoAuth) {
    Write-Host "auth   = OFF (only use this on 127.0.0.1)"
}
else {
    Write-Host "token  = $Token"
    Write-Host "header = Authorization: Bearer $Token"
}

if ($Background) {
    $logDir = Join-Path $root 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $out = Join-Path $logDir 'ssh-mcp.out.log'
    $err = Join-Path $logDir 'ssh-mcp.err.log'
    $proc = Start-Process -FilePath $python -ArgumentList $cliArgs -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $out -RedirectStandardError $err
    Write-Host "started in background, pid=$($proc.Id), logs: $out / $err"
}
else {
    & $python @cliArgs
}
