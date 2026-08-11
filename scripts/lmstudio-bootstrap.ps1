<#
.SYNOPSIS
    Bring up a local LM Studio model and wire JARVIS to it.

.DESCRIPTION
    Runs on the Windows machine where LM Studio and the model files live.
    It verifies the models directory, starts the LM Studio server, confirms a
    model is loaded, probes the OpenAI-compatible endpoint, and writes the
    matching LOCAL_LLM_* entries into JARVIS's .env.

    The models directory itself is set inside the LM Studio app (My Models tab)
    — there is no CLI flag for it. This script checks the path and tells you
    what to change if it doesn't match.

.PARAMETER ModelsDir
    Where the .gguf files live. Default D:\Tony6-Home\.lmstudio\models

.PARAMETER Model
    Model id to load. If omitted, the script lists what's available and uses
    the already-loaded model when there is exactly one.

.PARAMETER Port
    LM Studio server port. Default 1234.

.PARAMETER EnvFile
    Path to the JARVIS .env to update. Default: .env beside the repo root.

.EXAMPLE
    .\scripts\lmstudio-bootstrap.ps1
    .\scripts\lmstudio-bootstrap.ps1 -Model "kimi-k2-instruct" -Port 1234
#>

[CmdletBinding()]
param(
    [string]$ModelsDir = 'D:\Tony6-Home\.lmstudio\models',
    [string]$Model = '',
    [int]$Port = 1234,
    [string]$EnvFile = ''
)

$ErrorActionPreference = 'Stop'

function Write-Step { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Warn { param($m) Write-Host "[warn] $m" -ForegroundColor Yellow }
function Write-Fail { param($m) Write-Host "[fail] $m" -ForegroundColor Red }

$BaseUrl = "http://localhost:$Port/v1"

if (-not $EnvFile) {
    $EnvFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env'
}

# --- 1. models directory ----------------------------------------------------
Write-Step "Checking models directory: $ModelsDir"
if (-not (Test-Path $ModelsDir)) {
    Write-Fail "Not found: $ModelsDir"
    Write-Host @"
    Either the path is wrong or LM Studio stores models elsewhere.
    Open LM Studio -> My Models -> change the models directory, or re-run with:
        .\scripts\lmstudio-bootstrap.ps1 -ModelsDir '<your path>'
"@
    exit 1
}

$ggufCount = (Get-ChildItem -Path $ModelsDir -Recurse -Filter '*.gguf' -ErrorAction SilentlyContinue |
              Measure-Object).Count
Write-Host "    found $ggufCount .gguf file(s)"
if ($ggufCount -eq 0) {
    Write-Warn "No .gguf files under $ModelsDir. LM Studio may be pointed at a different folder."
    Write-Warn "Check LM Studio -> My Models to confirm the directory it actually uses."
}

# --- 2. lms CLI -------------------------------------------------------------
Write-Step 'Checking the lms CLI'
$lms = Get-Command lms -ErrorAction SilentlyContinue
if (-not $lms) {
    Write-Warn 'lms not on PATH.'
    Write-Host @"
    The LM Studio CLI ships with the app. Install it once with:
        cmd /c %USERPROFILE%\.lmstudio\bin\lms.exe bootstrap
    then open a new terminal. You can also skip the CLI entirely and start the
    server from the app's Developer tab, then re-run this script.
"@
} else {
    Write-Host "    $($lms.Source)"

    Write-Step 'Starting the LM Studio server'
    # --port is not documented on every version; fall back to a bare start.
    & lms server start --port $Port 2>$null
    if ($LASTEXITCODE -ne 0) {
        & lms server start 2>$null
        if ($LASTEXITCODE -ne 0) { Write-Warn 'lms server start reported a failure; probing anyway.' }
    }

    Write-Step 'Models visible to LM Studio'
    & lms ls
}

# --- 3. probe the endpoint --------------------------------------------------
Write-Step "Probing $BaseUrl/models"
$loaded = @()
try {
    $resp = Invoke-RestMethod -Uri "$BaseUrl/models" -Method Get -TimeoutSec 20
    $loaded = @($resp.data | ForEach-Object { $_.id })
} catch {
    Write-Fail "Cannot reach $BaseUrl — $($_.Exception.Message)"
    Write-Host @"
    Open LM Studio -> Developer -> toggle 'Start server' (or run 'lms server start'),
    confirm the port matches $Port, then re-run.
"@
    exit 1
}

if ($loaded.Count -eq 0) {
    Write-Fail 'Server is up but no model is loaded.'
    Write-Host "    Load one:  lms load <model>  (or use the app's chat tab), then re-run."
    exit 1
}

Write-Host "    loaded: $($loaded -join ', ')"

# --- 4. pick the model ------------------------------------------------------
if (-not $Model) {
    if ($loaded.Count -eq 1) {
        $Model = $loaded[0]
        Write-Step "Using the only loaded model: $Model"
    } else {
        Write-Fail 'Several models are loaded — pick one explicitly.'
        Write-Host "    .\scripts\lmstudio-bootstrap.ps1 -Model '$($loaded[0])'"
        exit 1
    }
} elseif ($loaded -notcontains $Model) {
    Write-Fail "'$Model' is not loaded. Loaded: $($loaded -join ', ')"
    Write-Host "    Use an id from that list verbatim (it usually includes a quantization suffix)."
    exit 1
}

# --- 5. real completion -----------------------------------------------------
Write-Step 'Sending a test completion'
$body = @{
    model      = $Model
    messages   = @(
        @{ role = 'system'; content = 'You are terse. Reply with exactly: OK' },
        @{ role = 'user';   content = 'Say OK.' }
    )
    max_tokens = 32
    stream     = $false
} | ConvertTo-Json -Depth 6

try {
    $completion = Invoke-RestMethod -Uri "$BaseUrl/chat/completions" -Method Post `
        -ContentType 'application/json' -Body $body -TimeoutSec 120
    $reply = $completion.choices[0].message.content
    Write-Host "    reply: $($reply.Trim())"
} catch {
    Write-Fail "Completion failed — $($_.Exception.Message)"
    exit 1
}

# --- 6. write .env ----------------------------------------------------------
Write-Step "Updating $EnvFile"

$settings = [ordered]@{
    'LLM_PROVIDER'       = 'local'
    'LOCAL_LLM_BASE_URL' = $BaseUrl
    'LOCAL_LLM_MODEL'    = $Model
}

$lines = if (Test-Path $EnvFile) { @(Get-Content $EnvFile) } else { @() }

foreach ($key in $settings.Keys) {
    $value   = $settings[$key]
    $pattern = "^\s*#?\s*$([regex]::Escape($key))\s*="
    $idx     = ($lines | Select-String -Pattern $pattern | Select-Object -First 1).LineNumber

    if ($idx) {
        $lines[$idx - 1] = "$key=$value"
    } else {
        $lines += "$key=$value"
    }
}

Set-Content -Path $EnvFile -Value $lines -Encoding UTF8
foreach ($key in $settings.Keys) { Write-Host "    $key=$($settings[$key])" }

Write-Host ''
Write-Host 'Done. Verify from the JARVIS repo with:' -ForegroundColor Green
Write-Host '    python llm_provider.py --check'
Write-Host '    python server.py'
