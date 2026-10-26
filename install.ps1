# Smart AI CLI - instalação no Windows (PowerShell 5.1+)
#   powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Smart AI CLI - Instalação" -ForegroundColor Cyan

# 1. Python 3.11+
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Host "  x Python 3.11+ não encontrado. Instale em https://www.python.org/downloads/ (marque 'Add to PATH')." -ForegroundColor Red
    exit 1
}
$pyArgs = @(); if ($py.Name -like 'py*') { $pyArgs = @('-3') }
& $py.Source @pyArgs -c "import sys; sys.exit(sys.version_info < (3, 11))"
if ($LASTEXITCODE -ne 0) { Write-Host "  x Python 3.11+ é necessário." -ForegroundColor Red; exit 1 }
Write-Host "  ok Python: $(& $py.Source @pyArgs --version)" -ForegroundColor Green

# 2. Claude Code (instalador oficial)
if (-not (Get-Command claude -ErrorAction SilentlyContinue) -and $env:SMART_AI_SKIP_CLAUDE_INSTALL -ne '1') {
    Write-Host "  -> Instalando Claude Code (instalador oficial)..." -ForegroundColor Cyan
    Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (Get-Command claude -ErrorAction SilentlyContinue) {
    Write-Host "  ok Claude Code: $(claude --version)" -ForegroundColor Green
} else {
    Write-Host "  x Claude Code não pôde ser instalado." -ForegroundColor Red; exit 1
}
foreach ($t in @('codex', 'agy')) {
    if (Get-Command $t -ErrorAction SilentlyContinue) { Write-Host "  ok $t" -ForegroundColor Green }
    else { Write-Host "  i $t é opcional (não encontrado)" -ForegroundColor Yellow }
}

# 3. Comando ai em %USERPROFILE%\.local\bin
$Bin = Join-Path $env:USERPROFILE '.local\bin'
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE '.cache\smart-ai') | Out-Null
@"
@echo off
call "$Root\ai.cmd" %*
"@ | Set-Content -Encoding ASCII (Join-Path $Bin 'ai.cmd')
Write-Host "  ok Comando criado: $Bin\ai.cmd" -ForegroundColor Green

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $Bin) {
    [Environment]::SetEnvironmentVariable('Path', "$Bin;$userPath", 'User')
    Write-Host "  ok $Bin adicionado ao PATH (abra um novo terminal)" -ForegroundColor Green
}

# 4. Login da conta principal
claude auth status *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ! Conta principal sem login. Abrindo login..." -ForegroundColor Yellow
    claude auth login
}
& $py.Source @pyArgs (Join-Path $Root 'config_tool.py') account list

Write-Host ""
Write-Host "Instalação concluída!" -ForegroundColor Green
Write-Host "  ai            Abrir o chat (/help dentro dele)"
Write-Host "  ai --status   Ver status"
