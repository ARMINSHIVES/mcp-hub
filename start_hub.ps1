# Start the MCP hub on http://127.0.0.1:8750 (hidden window, log in state\hub.log).
#   powershell -ExecutionPolicy Bypass -File start_hub.ps1
# Creates the venv on first run. Stops a hub already running on the port first.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    python -m venv "$root\.venv"
    & $py -m pip install -q -r "$root\requirements.txt"
}
if (-not (Test-Path "$root\state\tokens.json")) { & $py -m hub tokens init }

$old = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match '-m hub serve' }
$old | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

New-Item -ItemType Directory -Force "$root\state" | Out-Null
Start-Process -FilePath $py -ArgumentList '-u', '-m', 'hub', 'serve' -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput "$root\state\hub.log" -RedirectStandardError "$root\state\hub.err.log"
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    try { Invoke-RestMethod http://127.0.0.1:8750/health -TimeoutSec 1 | Out-Null; Write-Host 'hub is up'; exit 0 } catch {}
}
Write-Host "hub didn't answer on :8750; see $root\state\hub.err.log"
exit 1
