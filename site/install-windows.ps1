# Install Shoal (one hub per device) on this Windows PC.
#
# Your first hub: run it from your clone of the repository. That folder becomes the hub (its code
# is what `git pull` updates) and this PC gets the key that signs updates for your mesh:
#   powershell -ExecutionPolicy Bypass -File site\install-windows.ps1
# Every other PC: any online hub serves this script with its own address already inserted
# (PowerShell, no admin), and the new hub downloads the code from it and joins the mesh:
#   irm __HUB__/install.ps1 | iex
# Remove it again (keeps its data unless -Purge):
#   & ([scriptblock]::Create((irm __HUB__/install.ps1))) -Uninstall [-Purge]
# It then updates itself from the mesh with the latest signed release.
# Optional: $env:HUB_PRIORITY = 100 before installing if this PC is always on.
param([switch]$Uninstall, [switch]$Purge)
$ErrorActionPreference = 'Stop'
$Master = '__HUB__'
$Task = 'Shoal'
$OldTask = 'TailscaleHub'          # the name used before the rename; replaced on reinstall
$OldDir = Join-Path $env:LOCALAPPDATA 'TailnetHub'
$Dir = if (Test-Path $OldDir) { $OldDir } else { Join-Path $env:LOCALAPPDATA 'Shoal' }  # keep an existing install's data
$First = $Master -like '__HUB*'   # not served by a hub: the first hub, run from a clone
$App = $Dir
if ($First) {
  $App = if ($PSScriptRoot) { Split-Path $PSScriptRoot } else { '' }
  if (-not $App -or -not (Test-Path (Join-Path $App 'core\__main__.py'))) {
    throw 'Run this from your clone: powershell -ExecutionPolicy Bypass -File site\install-windows.ps1'
  }
}
function Step($n, $t) { Write-Host "[shoal] $n/7 $t" -ForegroundColor Green }

function Remove-Startup {
  foreach ($t in @($Task, $OldTask)) {
    $found = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    # the pre-rename task is only ours if it runs from this install's folder (a hub run from a
    # source checkout, for example, keeps its own task)
    if ($found -and ($t -eq $Task -or $found.Actions.WorkingDirectory -eq $App)) {
      Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
      Unregister-ScheduledTask -TaskName $t -Confirm:$false
    }
  }
  # only this install's processes: matched by its own data folder
  Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like "*--home*$Dir*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
  Write-Host '[shoal] removing Shoal from this PC' -ForegroundColor Green
  Remove-Startup
  if (Get-Command tailscale -ErrorAction SilentlyContinue) { tailscale serve --https=443 --set-path=/hub off 2>$null | Out-Null }
  if ($Purge) {
    Remove-Item -Recurse -Force $Dir -ErrorAction SilentlyContinue
    Write-Host '[shoal] done: code and data removed' -ForegroundColor Green
  } else {
    Get-ChildItem $Dir -Exclude 'data' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
    Write-Host "[shoal] done: code removed; its identity and settings stay in $Dir\data (add -Purge to delete them)" -ForegroundColor Green
  }
  Write-Host '[shoal] other hubs will show it offline; remove it from the mesh in Control > Network' -ForegroundColor Green
  return
}

Step 1 'Tailscale'
$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) { throw 'Install Tailscale and sign in before continuing: https://tailscale.com/download' }
$st = tailscale status --json | Out-String | ConvertFrom-Json
if ($st.BackendState -ne 'Running') { throw 'Tailscale is not connected. Open it and sign in.' }

Step 2 'Python'
$py = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $py) {
  winget install --id Python.Python.3.12 -e --scope user --accept-source-agreements --accept-package-agreements --silent | Out-Null
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
  $py = Get-Command pythonw.exe -ErrorAction Stop
}
$pythonw = $py.Source
$python = Join-Path (Split-Path $pythonw) 'python.exe'

Step 3 'Optional dependencies (pywebpush for push notifications)'
# cryptography also gives native Ed25519 (signatures in well under 1 ms instead of 0.2-0.4 s)
& $python -m pip install --user --quiet --disable-pip-version-check pywebpush cryptography

New-Item -ItemType Directory -Force $Dir | Out-Null
if ($First) {
  Step 4 "Your first hub runs from $App"
} else {
  Step 4 "Downloading the hub from $Master into $Dir"
  $zip = Join-Path $env:TEMP 'shoal.zip'
  # the hub only serves the package to your Tailscale identity (tailscale serve adds it)
  Invoke-WebRequest -UseBasicParsing "$Master/dist/hub.zip" -OutFile $zip
  Expand-Archive -Force $zip $Dir
  Remove-Item $zip
}
if (-not (Test-Path "$App\services.json")) {
  '{"routes": {"443/hub": {"name": "This index", "about": "Hub on this PC.", "icon": "ph-compass"}}}' |
    Set-Content -Encoding utf8 "$App\services.json"
}

Step 5 'Publishing /hub on the tailnet and joining the mesh'
# serve first, so enrollment records the https://<device>/hub/ URL
tailscale serve --bg --https=443 --set-path=/hub http://127.0.0.1:8790 | Out-Null
Push-Location $App
try {
  if ($First) {
    $pub = & $python -m core --home "$Dir\data" --signing-key
    Write-Host "[shoal]   public key $pub (back up $Dir\data\release_key.json: without it you cannot publish updates)" -ForegroundColor Yellow
  } else {
    & $python -m core --home "$Dir\data" --bind 127.0.0.1 --enroll $Master
  }
} finally { Pop-Location }

Step 6 "Automatic startup ($Task task)"
Remove-Startup  # one supervisor per hub: replaces an older install, including the pre-rename task
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m core --home `"$Dir\data`" --port 8790 --bind 127.0.0.1 --auto-deps" -WorkingDirectory $App
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $Task

Step 7 'Done'
$dns = $st.Self.DNSName.TrimEnd('.')
if ($First) {
  Write-Host "[shoal] ready: https://$dns/hub/  Add your other devices from Control > Network. To update: git pull in $App" -ForegroundColor Green
} else {
  Write-Host "[shoal] ready: https://$dns/hub/  (it appears in the Mesh menu of every hub and updates itself)" -ForegroundColor Green
}
