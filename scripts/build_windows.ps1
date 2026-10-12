param(
    [string]$Uv = 'uv',
    [string]$ArtifactId = ''
)
$ErrorActionPreference = 'Stop'
$Uv = (Get-Command $Uv -ErrorAction Stop).Source
$repositoryRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repositoryRoot
try {
    if (-not $ArtifactId) {
        $ArtifactId = git rev-parse --short=7 HEAD
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read commit SHA' }
    }
    & $Uv sync --locked --group build
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed' }
    & $Uv run --locked --no-sync --group build python scripts/release_environment.py
    if ($LASTEXITCODE -ne 0) { throw 'Unsupported Python environment' }
    & $Uv run --locked --no-sync --group build python scripts/export_requirements.py --check --uv $Uv
    if ($LASTEXITCODE -ne 0) { throw 'Requirements exports are out of date' }
    $releasePython = & $Uv run --locked --no-sync --group build python -c "import sys; print(sys.executable)"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate the release interpreter' }
    & $Uv pip check --python $releasePython
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed' }
    $previousQtPlatform = $env:QT_QPA_PLATFORM
    $env:QT_QPA_PLATFORM = 'offscreen'
    & $Uv run --locked --no-sync --group build python -m unittest discover -s tests -t . -p 'test_*.py' -v
    if ($LASTEXITCODE -ne 0) { throw 'Offline tests failed' }
    $buildDirectory = Join-Path 'build' ('release-build-' + [Guid]::NewGuid().ToString('N'))
    & $Uv run --locked --no-sync --group build python -m PyInstaller --noconfirm --onefile --windowed --noconsole --name Scribe2SRT --collect-data opencc --additional-hooks-dir scripts/hooks --specpath $buildDirectory --distpath "$buildDirectory/dist" --workpath "$buildDirectory/work" app.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    & $Uv run --locked --no-sync --group build python scripts/package_windows.py --executable "$buildDirectory/dist/Scribe2SRT.exe" --artifact-id $ArtifactId
    if ($LASTEXITCODE -ne 0) { throw 'ZIP packaging or extracted executable validation failed' }
} finally {
    if (Test-Path variable:previousQtPlatform) { $env:QT_QPA_PLATFORM = $previousQtPlatform }
    Pop-Location
}
