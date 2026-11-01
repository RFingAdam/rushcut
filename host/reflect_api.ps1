$dll = Get-ChildItem "C:\Program Files\VEGAS\VEGAS Pro 22.0" -Recurse -Filter ScriptPortal.Vegas.dll | Select-Object -First 1
"DLL: " + $dll.FullName
$asm = [Reflection.Assembly]::LoadFrom($dll.FullName)
function Show($typeName, $pattern) {
  $t = $asm.GetType("ScriptPortal.Vegas.$typeName")
  if (-not $t) { "-- no type $typeName"; return }
  "== $typeName"
  $t.GetMethods() | ? { $_.Name -match $pattern -and $_.DeclaringType -eq $t } | % { "  M " + $_.ToString() }
  $t.GetProperties() | ? { $_.Name -match $pattern } | % { "  P " + $_.PropertyType.Name + " " + $_.Name + " set=" + $_.CanWrite }
}
Show "Vegas" "Project|Render|Open|Close|Refresh|UpdateUI"
Show "Media" "Replace|Stream|Offline"
Show "VideoStream" "Alpha|Field|Pixel"
Show "Envelope" "Points|Clear|Remove"
Show "EnvelopePoints" "Clear|Remove"
Show "BaseList``1" "Clear|Remove"
Show "AudioTrack" "Volume|Pan"
Show "Track" "Name|Mute|Solo|Envelopes"
Show "AudioEvent" "Normalize|Gain"
Show "TrackEvent" "Normalize|Gain|FadeIn|Mute"
Show "Renderer" "Templates|Name|FileType"
Show "RenderTemplate" "Name|Index|Description|Video|Audio|Frame"
