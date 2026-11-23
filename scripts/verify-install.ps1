<#
.SYNOPSIS
  Post-install self-check for reelens (DSH plugin).

.DESCRIPTION
  "The plugin row is enabled" and "the routes are actually registered" look the same from
  outside: an unknown path under the API namespace falls through to the gateway's auth
  fallback and returns 401, exactly like a route that exists but rejects the caller.
  So the check compares two of **our own** paths:

      /api/video-breakdown/capabilities      → 200 means the plugin is live
      /api/video-breakdown/__definitely_404__ → the gateway's own answer for a non-route

  If both return the same status, nothing was registered (usual cause: the host was not
  restarted after enabling the plugin).

.EXAMPLE
  pwsh -File scripts/verify-install.ps1
#>
[CmdletBinding()]
param(
  [string]$BaseUrl = 'http://127.0.0.1:3080',
  [string]$ProfileJson = "$env:USERPROFILE\.dsh\profiles\web\package.json"
)

$ErrorActionPreference = 'Stop'
$fail = 0

function Write-Check([string]$name, [bool]$ok, [string]$detail) {
  if ($ok) { Write-Host ("  ok   {0} — {1}" -f $name, $detail) -ForegroundColor Green }
  else { Write-Host ("  FAIL {0} — {1}" -f $name, $detail) -ForegroundColor Red; $script:fail++ }
}

function Get-Status([string]$path) {
  try {
    $r = Invoke-WebRequest -Uri "$BaseUrl$path" -TimeoutSec 8 -ErrorAction Stop
    return [int]$r.StatusCode
  } catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code) { return [int]$code }
    return -1
  }
}

Write-Host 'reelens · install self-check' -ForegroundColor Cyan

# 1) Did the host restart after the plugin row was added?
$hostPid = (Get-NetTCPConnection -LocalPort 3080 -State Listen -ErrorAction SilentlyContinue |
  Select-Object -First 1).OwningProcess
if ($hostPid) {
  $started = (Get-Process -Id $hostPid).StartTime
  $manifest = (Get-Item $ProfileJson -ErrorAction SilentlyContinue).LastWriteTime
  if ($manifest) {
    Write-Check 'host restarted after install' ($started -gt $manifest) `
      ("host started {0} / profile manifest {1}" -f $started, $manifest)
  } else {
    Write-Host "  skip host restart check — no profile manifest at $ProfileJson"
  }
} else {
  Write-Check 'host is running' $false 'nothing is listening on port 3080'
}

# 2) Are the routes registered? (compare against our own non-route)
$target = Get-Status '/api/video-breakdown/capabilities'
$control = Get-Status '/api/video-breakdown/__definitely_404__'
Write-Check 'routes registered' ($target -eq 200) `
  "capabilities=$target (a non-route under the same prefix answers $control)"

# 3) Route contents
if ($target -eq 200) {
  try {
    $doc = Invoke-RestMethod -Uri "$BaseUrl/api/video-breakdown/capabilities" -TimeoutSec 10
    $toolNames = @($doc.tools | ForEach-Object { if ($_ -is [string]) { $_ } else { $_.name } })
    Write-Check 'six tools exposed' ($toolNames.Count -eq 6) ("tools=" + ($toolNames -join ','))
    Write-Check 'seven routes exposed' (@($doc.routes).Count -eq 7) ("routes=" + (@($doc.routes) -join ' '))
    Write-Check 'python interpreter resolved' ([bool]$doc.python) ("python=" + $doc.python)
    Write-Host ("  vision: {0}" -f (@($doc.vision_chain) -join ' / '))
  } catch {
    Write-Check 'capabilities readable' $false $_.Exception.Message
  }
}

Write-Host ''
if ($fail -eq 0) {
  Write-Host 'All good. Open the "Breakdown" entry in the sidebar (reload the page if it is missing).' -ForegroundColor Green
} else {
  Write-Host @'
Something is off. Most common causes:
  1) the host was not restarted after enabling the plugin → stop it and start it again
  2) the plugin row failed to activate → check the host log for a line mentioning reelens
  3) the panel is missing but the routes work → reload the page: the client bundle is
     loaded when the page boots
'@ -ForegroundColor Yellow
}
exit $fail
