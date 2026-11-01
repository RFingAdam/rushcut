# Builds VegasDirector.dll (Application Extension = auto-start host) and installs it plus the Script Menu fallback.
# Run: powershell -ExecutionPolicy Bypass -File host\build.ps1 [-Version 22|2027|all]   (close VEGAS first if updating an installed DLL)
#
# ScriptPortal.Vegas.dll is strong-named per release (22.0.0.250 vs 2027.0.0.31), so each installed VEGAS
# gets its own build, compiled against its own ScriptPortal.Vegas.dll and installed to that version's
# per-user scan folder: %LOCALAPPDATA%\VEGAS Pro\<ver>\Application Extensions (DomainManager.GetModuleScanPath).
# The shared <MyDocuments>\Vegas Application Extensions folder is scanned by EVERY version, so it is no longer used.
param(
  [ValidateSet('all', '22', '2027')]
  [string]$Version = 'all'
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'

# Known installs. Boris FX VEGAS Pro 2027 moved to C:\Program Files\BorisFX; 22 is still MAGIX/VEGAS Creative layout.
$installs = @(
  @{ Key = '22';   AppVer = '22.0';   Dir = 'C:\Program Files\VEGAS\VEGAS Pro 22.0';      Exe = 'vegas220.exe' },
  @{ Key = '2027'; AppVer = '2027.0'; Dir = 'C:\Program Files\BorisFX\Vegas Pro 2027';    Exe = 'vegas2027.exe' }
)
if ($env:VEGAS_DIR) {
  # Override: VEGAS_DIR + VEGAS_APPVER (e.g. 2027.0) for a non-default install location.
  $installs = @(@{ Key = 'custom'; AppVer = $env:VEGAS_APPVER; Dir = $env:VEGAS_DIR; Exe = '' })
}
$targets = $installs | Where-Object { ($Version -eq 'all' -or $_.Key -eq $Version -or $_.Key -eq 'custom') -and (Test-Path (Join-Path $_.Dir 'ScriptPortal.Vegas.dll')) }
if (-not $targets) { throw "No VEGAS install with ScriptPortal.Vegas.dll found for -Version $Version" }

# Known Folder MyDocuments — follows OneDrive / folder redirection.
# $env:USERPROFILE\Documents is the wrong path when Documents lives under OneDrive.
$docs = [Environment]::GetFolderPath('MyDocuments')
if ([string]::IsNullOrWhiteSpace($docs)) {
  $docs = Join-Path $env:USERPROFILE 'Documents'
}
$sharedExt = Join-Path $docs 'Vegas Application Extensions'
$sharedDll = Join-Path $sharedExt 'VegasDirector.dll'
if (Test-Path $sharedDll) {
  # A copy here would be loaded by every VEGAS version (and fail to bind in the wrong one).
  $legacy = "$sharedDll.shared-legacy.disabled"
  Move-Item $sharedDll $legacy -Force
  "disabled shared legacy DLL: $sharedDll -> $legacy"
}

foreach ($t in $targets) {
  $sp = Join-Path $t.Dir 'ScriptPortal.Vegas.dll'
  $spVer = [Reflection.AssemblyName]::GetAssemblyName($sp).Version
  $binDir = Join-Path $here ("bin\" + $t.AppVer)
  $out = Join-Path $binDir 'VegasDirector.dll'
  New-Item -ItemType Directory -Force $binDir | Out-Null
  & $csc /nologo /target:library /optimize+ /out:$out `
    /r:"$sp" /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Core.dll /r:System.Xml.dll /r:System.Data.dll `
    (Join-Path $here 'VegasDirectorHost.cs') (Join-Path $here 'VegasDirectorExtension.cs')
  if ($LASTEXITCODE -ne 0) { throw "csc failed for VEGAS $($t.AppVer) ($LASTEXITCODE)" }
  $ext = Join-Path $env:LOCALAPPDATA ("VEGAS Pro\" + $t.AppVer + "\Application Extensions")
  New-Item -ItemType Directory -Force $ext | Out-Null
  Copy-Item $out (Join-Path $ext 'VegasDirector.dll') -Force
  "built $out against ScriptPortal.Vegas $spVer and installed to $ext"
}

$scriptMenu = Join-Path $docs 'Vegas Script Menu'
New-Item -ItemType Directory -Force $scriptMenu | Out-Null
# Script Menu copy stays as a manual fallback (it detects the running extension and exits politely).
# VEGAS compiles the .cs itself, so one copy works for every version.
Copy-Item (Join-Path $here 'VegasDirectorHost.cs') (Join-Path $scriptMenu 'VegasDirectorHost.cs') -Force
"installed Script Menu fallback to $scriptMenu (Documents=$docs)"
