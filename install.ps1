# Smart AI CLI - instalação no Windows (PowerShell 5.1+)
#   powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Smart AI CLI - Instalação" -ForegroundColor Cyan

# 1. Python 3.11+: the system one, or a private 3.12 via uv (no admin rights)
$Python = $null
foreach ($c in @(@('py', '-3'), @('python'), @('python3'))) {
    $cmd = Get-Command $c[0] -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $pre = @($c | Select-Object -Skip 1)
    $exe = (& $cmd.Source @pre -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null)
    if ($LASTEXITCODE -eq 0 -and $exe) { $Python = $exe.Trim(); break }
}
if (-not $Python) {
    Write-Host "  ! Python 3.11+ não encontrado. Instalando Python 3.12 só para o Smart AI..." -ForegroundColor Yellow
    $Data = Join-Path $env:LOCALAPPDATA 'smart-ai'
    $env:UV_INSTALL_DIR = Join-Path $Data 'uv'; $env:UV_NO_MODIFY_PATH = '1'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $Data 'python'
    $uv = Join-Path $env:UV_INSTALL_DIR 'uv.exe'
    if (-not (Test-Path $uv)) { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
    & $uv python install --no-bin 3.12
    if ($LASTEXITCODE -ne 0) { & $uv python install 3.12 }
    $Python = (& $uv python find 3.12).Trim()
}
if (-not $Python) { Write-Host "  x Não consegui um Python 3.11+." -ForegroundColor Red; exit 1 }
Write-Host "  ok Python: $(& $Python --version) ($Python)" -ForegroundColor Green
$Cfg = Join-Path $env:USERPROFILE '.config\smart-ai'
New-Item -ItemType Directory -Force -Path $Cfg | Out-Null
Set-Content -NoNewline -Encoding UTF8 -Path (Join-Path $Cfg 'python-path') -Value $Python

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
"$Python" "$Root\smart_ai.py" %*
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
& $Python (Join-Path $Root 'config_tool.py') account list

Write-Host ""
Write-Host "Instalação concluída!" -ForegroundColor Green
Write-Host "  ai            Abrir o chat (/help dentro dele)"
Write-Host "  ai --status   Ver status"
