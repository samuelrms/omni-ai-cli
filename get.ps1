# Smart AI CLI - one-command install / update for Windows (PowerShell):
#   irm https://raw.githubusercontent.com/samuelrms/smart-ai-cli/main/get.ps1 | iex
$ErrorActionPreference = 'Stop'
$Repo = if ($env:SMART_AI_REPO) { $env:SMART_AI_REPO } else { 'https://github.com/samuelrms/smart-ai-cli' }
$Branch = if ($env:SMART_AI_BRANCH) { $env:SMART_AI_BRANCH } else { 'main' }
$Dest = if ($env:SMART_AI_DIR) { $env:SMART_AI_DIR } else { Join-Path $env:LOCALAPPDATA 'smart-ai-cli' }

Write-Host "Smart AI CLI -> $Dest" -ForegroundColor Cyan
if (Get-Command git -ErrorAction SilentlyContinue) {
    if (Test-Path (Join-Path $Dest '.git')) {
        Write-Host 'Atualizando...'
        git -C $Dest fetch --depth 1 origin $Branch -q
        git -C $Dest reset --hard "origin/$Branch" -q
    } else {
        if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
        git clone --depth 1 --branch $Branch -q $Repo $Dest
    }
} else {
    $zip = Join-Path $env:TEMP 'smart-ai-cli.zip'
    $tmp = Join-Path $env:TEMP 'smart-ai-cli-unzip'
    Invoke-WebRequest "$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip -UseBasicParsing
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive $zip -DestinationPath $tmp
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
    Move-Item (Join-Path $tmp "smart-ai-cli-$Branch") $Dest
    Remove-Item -Recurse -Force $tmp, $zip
}

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dest 'install.ps1')
