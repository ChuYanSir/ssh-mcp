# Update an existing ssh-mcp *source* install (the `pip install -e .` layout)
# from this repository's HEAD, keeping its own servers.yaml / .venv intact.
#
#   .\scripts\update-local.ps1 -Target E:\AI\tools\ssh-mcp
#   .\scripts\update-local.ps1 -Target E:\AI\tools\ssh-mcp -Check      # only report
#   .\scripts\update-local.ps1 -Target E:\AI\tools\ssh-mcp -NoBackup -SkipTests
#
# What it does:
#   1. read the version of both sides and tell you whether an update is needed
#   2. back the target up (everything except .venv / .pytest_cache)
#   3. extract `git archive HEAD` into the target, overwriting tracked files
#   4. delete files that no longer exist in HEAD (old modules, removed tests...)
#   5. re-run `pip install -e . --no-deps` in the target venv to refresh metadata
#   6. run the test suite there (unless -SkipTests) and print the summary
#
# ASCII only on purpose: Windows PowerShell 5.1 misreads UTF-8 .ps1 without a BOM.

param(
    [Parameter(Mandatory = $true)][string]$Target,
    [string]$Source = '',
    [switch]$NoBackup,
    [switch]$SkipTests,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'

if (-not $Source) {
    # $PSScriptRoot is not always available while binding parameters
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $Source = Split-Path -Parent $scriptDir
}

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Info($msg) { Write-Host $msg }
function Step($msg) { Write-Host "== $msg" -ForegroundColor Cyan }

$Source = (Resolve-Path $Source).Path
$Target = (Resolve-Path $Target -ErrorAction SilentlyContinue).Path
if (-not $Target) { Fail "target not found (pass -Target <dir>)" }
if ($Source -eq $Target) { Fail "source and target are the same directory" }

Step "source"
if (-not (Test-Path (Join-Path $Source '.git'))) { Fail "source is not a git repository: $Source" }
Push-Location $Source
$headSha = (git rev-parse --short HEAD).Trim()
$headSub = (git log -1 --format=%s).Trim()
$headVersion = (Select-String -Path (Join-Path $Source 'src\ssh_mcp\__init__.py') -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Pop-Location
Info "  HEAD    $headSha  $headSub"
Info "  version $headVersion"

Step "target"
$venvPy = Join-Path $Target '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { Fail "no venv in target: $venvPy (create it with: python -m venv .venv)" }
$targetVersion = (& $venvPy -c "import ssh_mcp; print(ssh_mcp.__version__)" 2>$null)
Info "  $Target"
Info "  version $targetVersion"

if ($targetVersion -eq $headVersion) {
    Info ""
    Info "Already at $headVersion. Re-running is safe (idempotent) but usually unnecessary."
    if ($Check) { exit 0 }
}

if ($Check) {
    Info ""
    Info "Update available: $targetVersion -> $headVersion"
    exit 0
}

# ---------------------------------------------------------------- 1. backup
if (-not $NoBackup) {
    Step "backup"
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$Target.bak-$stamp"
    robocopy $Target $backup /E /XD "$Target\.venv" "$Target\.pytest_cache" /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "backup failed (robocopy exit $LASTEXITCODE)" }
    $count = (Get-ChildItem $backup -Recurse -File | Measure-Object).Count
    Info "  $backup ($count files)"
}
else {
    Step "backup skipped (-NoBackup)"
}

# ------------------------------------------------- 2. protected files, for the report
$protected = @('servers.yaml', 'servers.yaml.bak')
$before = @{}
foreach ($name in $protected) {
    $p = Join-Path $Target $name
    if (Test-Path $p) { $before[$name] = (Get-FileHash $p -Algorithm SHA256).Hash }
}

# ---------------------------------------------------------------- 3. extract
Step "extract $headSha into target"
$tar = Join-Path $env:TEMP ("ssh-mcp-update-{0}.tar" -f [guid]::NewGuid().ToString('N').Substring(0, 8))
Push-Location $Source
git archive --format=tar -o $tar HEAD
Pop-Location
if (-not (Test-Path $tar)) { Fail "git archive produced nothing" }
tar -xf $tar -C $Target
Remove-Item $tar -Force -ErrorAction SilentlyContinue
Info "  done"

# ------------------------------------------------- 4. prune files not in HEAD
Step "prune stale files"
Push-Location $Source
$tracked = @(git ls-files | ForEach-Object { $_.Replace('/', '\') })
Pop-Location
$skipDirs = @('.venv', '.pytest_cache', '.git', '__pycache__')
$existing = Get-ChildItem $Target -Recurse -File |
    Where-Object {
        $rel = $_.FullName.Substring($Target.Length + 1)
        $parts = $rel.Split('\')
        -not ($parts | Where-Object { $skipDirs -contains $_ }) -and
        ($protected -notcontains $rel)
    } |
    ForEach-Object { $_.FullName.Substring($Target.Length + 1) }
$stale = @($existing | Where-Object { $tracked -notcontains $_ })
# __pycache__ / *.egg-info 属于构建产物，也顺手清掉
$junk = Get-ChildItem $Target -Recurse -Directory -Include '__pycache__', '*.egg-info' -ErrorAction SilentlyContinue
foreach ($d in $junk) { Remove-Item $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }
if ($stale.Count -gt 0) {
    foreach ($rel in $stale) {
        Remove-Item (Join-Path $Target $rel) -Force -ErrorAction SilentlyContinue
        Info "  removed $rel"
    }
}
else {
    Info "  nothing to remove"
}

# ------------------------------------------------------ 5. refresh editable install
Step "refresh editable install"
& $venvPy -m pip install -q -e $Target --no-deps
if ($LASTEXITCODE -ne 0) { Fail "pip install -e failed" }
$newVersion = (& $venvPy -c "import ssh_mcp; print(ssh_mcp.__version__)")
Info "  version $targetVersion -> $newVersion"

# ------------------------------------------------------------- 6. verify
Step "verify"
foreach ($name in $protected) {
    $p = Join-Path $Target $name
    if (-not (Test-Path $p)) { continue }
    $now = (Get-FileHash $p -Algorithm SHA256).Hash
    if ($before.ContainsKey($name) -and $before[$name] -ne $now) {
        Info "  WARNING: $name changed on disk (should not happen)"
    }
    else {
        Info "  $name untouched"
    }
}
$toolCount = (& $venvPy -c "import asyncio, ssh_mcp.server as s; print(len(asyncio.run(s.mcp.list_tools())))")
Info "  tools: $toolCount"

if (-not $SkipTests) {
    Push-Location $Target
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    & $venvPy -m pytest tests -q
    $testExit = $LASTEXITCODE
    Pop-Location
    if ($testExit -ne 0) { Fail "tests failed in $Target" }
}

Info ""
Write-Host "Updated $Target to $newVersion ($headSha)." -ForegroundColor Green
Write-Host "Restart any client that keeps a long-lived stdio server (the old process still runs the old code)."
