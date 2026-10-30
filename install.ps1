<#
.SYNOPSIS
  Installs cad-eye-mcp's private runtime into .\runtime (portable Python, Python packages,
  CADCraft and CJK fonts). Nothing is written outside this folder: no PATH, registry,
  system Python or system font changes.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1
#>
param(
    [switch]$Force   # re-download and re-extract everything
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = $PSScriptRoot
$Runtime = Join-Path $Root 'runtime'
$Downloads = Join-Path $Runtime 'downloads'
$lock = Get-Content (Join-Path $Root 'runtime.lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json

switch ($env:PROCESSOR_ARCHITECTURE) {
    'AMD64' { $Platform = 'windows-x64' }
    'ARM64' { $Platform = 'windows-arm64' }
    default { throw "Unsupported Windows architecture: $($env:PROCESSOR_ARCHITECTURE)" }
}

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Get-Pinned($url, $sha256, $fileName) {
    New-Item -ItemType Directory -Force $Downloads | Out-Null
    $dest = Join-Path $Downloads $fileName
    if ((Test-Path $dest) -and -not $Force) {
        if ((Get-FileHash $dest -Algorithm SHA256).Hash -eq $sha256.ToUpper()) { return $dest }
    }
    Write-Host "    downloading $fileName"
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    $actual = (Get-FileHash $dest -Algorithm SHA256).Hash
    if ($actual -ne $sha256.ToUpper()) {
        Remove-Item -LiteralPath $dest
        throw "SHA-256 mismatch for $fileName`n  expected $sha256`n  actual   $actual"
    }
    return $dest
}

# 1. Portable Python (python-build-standalone). The archive's top-level folder is "python/";
#    extracting over an existing copy overwrites it. tar.exe ships with Windows 10 1803+.
$PyExe = Join-Path $Runtime 'python\python.exe'
$PyMarker = Join-Path $Runtime "python\.version-$($lock.python.version)"
if ($Force -or -not (Test-Path $PyMarker)) {
    Step "Python $($lock.python.version) ($Platform)"
    $a = $lock.python.assets.$Platform
    $archive = Get-Pinned $a.url $a.sha256 "python-$($lock.python.version)-$Platform.tar.gz"
    & tar.exe -xzf $archive -C $Runtime
    if ($LASTEXITCODE -ne 0) { throw 'tar failed to extract Python' }
    New-Item -ItemType File -Force $PyMarker | Out-Null
}

# 2. Python packages, installed into the private interpreter only
Step 'Python packages'
& $PyExe -I -m pip install --isolated --disable-pip-version-check --no-warn-script-location `
    --quiet -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
# Make the package importable from source without installing a copy of it. UTF-8 (Python reads
# .pth files as UTF-8, BOM accepted), so a folder with Chinese or Japanese in its path works.
$site = Join-Path $Runtime 'python\Lib\site-packages'   # python-build-standalone's layout on Windows
Set-Content -Path (Join-Path $site 'cad_eye_mcp_src.pth') -Value (Join-Path $Root 'src') -Encoding UTF8

# 3. CADCraft (headless CLI used as the view engine), one folder per version
$CcDir = Join-Path $Runtime "cadcraft\$($lock.cadcraft.version)"
$CcMarker = Join-Path $CcDir '.complete'
if ($Force -or -not (Test-Path $CcMarker)) {
    Step "CADCraft $($lock.cadcraft.version) ($Platform)"
    $a = $lock.cadcraft.assets.$Platform
    $archive = Get-Pinned $a.url $a.sha256 ([IO.Path]::GetFileName($a.url))
    Expand-Archive -Path $archive -DestinationPath $CcDir -Force
    New-Item -ItemType File -Force $CcMarker | Out-Null
}

# 4. CJK fonts (Noto Sans TC/HK/SC/JP). CADCraft only looks fonts up by name in the system and
#    per-user font folders, so the server points LOCALAPPDATA at runtime\fonthome for the
#    CADCraft process only.
Step 'Fonts'
$FontDir = Join-Path $Runtime 'fonthome\Microsoft\Windows\Fonts'
New-Item -ItemType Directory -Force $FontDir | Out-Null
foreach ($f in $lock.fonts.files) {
    $target = Join-Path $FontDir $f.name
    if ($Force -or -not (Test-Path $target)) {
        $src = Get-Pinned $f.url $f.sha256 $f.name
        Copy-Item $src $target -Force
    }
}

# 5. Smoke test
Step 'Checking the installation'
& $PyExe -I -m cad_eye_mcp --check
if ($LASTEXITCODE -ne 0) { throw 'self-check failed' }

$cmd = Join-Path $Root 'cad-eye-mcp.cmd'
Write-Host ''
Write-Host 'Installed. Register the server with your MCP client, for example:' -ForegroundColor Green
Write-Host "  claude mcp add cad-eye -- `"$cmd`""
Write-Host 'or add to your client''s JSON config:'
$json = @{ mcpServers = @{ 'cad-eye' = @{ command = $cmd; args = @() } } } | ConvertTo-Json -Depth 5
Write-Host $json
