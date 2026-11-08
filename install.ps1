# Install lariat (Windows): the release binary, checked against the release's
# SHA256SUMS, into %LOCALAPPDATA%\Programs\lariat (added to your PATH); then
# `lariat setup`.
#
#   irm https://raw.githubusercontent.com/marshallsfolly/lariat/main/install.ps1 | iex
#
# Environment: LARIAT_VERSION (a tag like v0.1.0; default: the latest release),
# LARIAT_BIN_DIR, LARIAT_NO_SETUP=1 (install only).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is much faster without the progress bar

$repo = if ($env:LARIAT_REPO) { $env:LARIAT_REPO } else { 'marshallsfolly/lariat' }
$dir = if ($env:LARIAT_BIN_DIR) { $env:LARIAT_BIN_DIR } else { Join-Path $env:LOCALAPPDATA 'Programs\lariat' }
$base = if ($env:LARIAT_VERSION) { "https://github.com/$repo/releases/download/$($env:LARIAT_VERSION)" } else { "https://github.com/$repo/releases/latest/download" }
# An x64 PowerShell under emulation reports AMD64 and gets the x86_64 binary, which runs there too.
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x86_64' }
$asset = "lariat-windows-$arch.exe"

New-Item -ItemType Directory -Force $dir | Out-Null
$download = Join-Path $dir 'lariat.download.exe'
Write-Host "Downloading $asset..."
Invoke-WebRequest "$base/$asset" -OutFile $download -UseBasicParsing
$sums = (Invoke-WebRequest "$base/SHA256SUMS" -UseBasicParsing).Content
if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
$want = ($sums -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -match "\s\*?$([regex]::Escape($asset))$" } | ForEach-Object { ($_ -split '\s+')[0] } | Select-Object -First 1)
$got = (Get-FileHash $download -Algorithm SHA256).Hash
if (-not $want -or $want.ToLower() -ne $got.ToLower()) {
  Remove-Item $download -ErrorAction SilentlyContinue
  throw "lariat: the download does not match the release's SHA256SUMS; not installed"
}

$exe = Join-Path $dir 'lariat.exe'
if (Test-Path $exe) {
  # A running lariat.exe cannot be replaced, but it can be renamed (it is deleted on its next start).
  $aside = Join-Path $dir 'lariat.old.exe'
  Remove-Item $aside -ErrorAction SilentlyContinue
  Move-Item $exe $aside
}
Move-Item $download $exe
Write-Host "Installed $(& $exe --version) to $exe"

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not (($userPath -split ';') -contains $dir)) {
  [Environment]::SetEnvironmentVariable('Path', ($(if ($userPath) { "$userPath;" } else { '' }) + $dir), 'User')
  Write-Host "Added $dir to your PATH (new terminals see it)."
}
$env:Path = "$env:Path;$dir"

if ($env:LARIAT_NO_SETUP) {
  Write-Host 'Next: lariat setup'
} else {
  Write-Host ''
  & $exe setup
}
