# GLM setup for Bili2Text (ASCII-only on purpose: safe for PowerShell 5.1)
# - asks for the Zhipu API key with hidden input
# - saves MINIMAX_APIKEY / MINIMAX_API_BASE / MINIMAX_MODEL as USER variables
# - runs one tiny real request to verify key, model and endpoint
# The key is never printed and never written to disk.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$refiner = Join-Path $PSScriptRoot 'glm_refine.py'
$baseUrl = 'https://open.bigmodel.cn/api/paas/v4'
$modelName = 'glm-4.7-flash'

Write-Host ''
Write-Host '=== Bili2Text / Zhipu GLM setup ===' -ForegroundColor Cyan
Write-Host "Project : $root"
Write-Host "Model   : $modelName (free)"
Write-Host "Base URL: $baseUrl"
Write-Host ''

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "[ERROR] venv python not found: $python" -ForegroundColor Red
    exit 1
}

Write-Host 'Paste your Zhipu API key below. Input is hidden (no echo, not in history).'
Write-Host 'Get one at: https://bigmodel.cn/usercenter/proj-mgmt/apikeys'
$secure = Read-Host 'API Key' -AsSecureString

$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    if ([string]::IsNullOrWhiteSpace($plain)) {
        Write-Host '[ERROR] Empty key, nothing saved.' -ForegroundColor Red
        exit 2
    }
    if ($plain.Length -lt 20) {
        Write-Host '[ERROR] Key looks too short, nothing saved.' -ForegroundColor Red
        exit 2
    }

    [Environment]::SetEnvironmentVariable('MINIMAX_APIKEY', $plain.Trim(), 'User')
    [Environment]::SetEnvironmentVariable('MINIMAX_API_BASE', $baseUrl, 'User')
    [Environment]::SetEnvironmentVariable('MINIMAX_MODEL', $modelName, 'User')
    $env:MINIMAX_APIKEY = $plain.Trim()
}
finally {
    if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    $plain = $null
    $secure = $null
    Remove-Variable secure -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host '[OK] Saved to current-user environment variables (no admin needed).'
Write-Host 'Now running a tiny real request to verify key / model / endpoint...'
Write-Host ''

$env:MINIMAX_API_BASE = $baseUrl
$env:MINIMAX_MODEL = $modelName
$env:PYTHONIOENCODING = 'utf-8'

& $python $refiner --test
$code = $LASTEXITCODE

Write-Host ''
if ($code -eq 0) {
    Write-Host '[SUCCESS] Zhipu GLM is reachable and the key works.' -ForegroundColor Green
    Write-Host 'Next: start Bili2Text again, or double-click GLM-Refine.cmd to polish existing Markdown.'
} else {
    Write-Host "[FAILED] Test exit code: $code" -ForegroundColor Red
    Write-Host 'Check the message above: invalid key, model permission, or network/proxy.'
}
exit $code
