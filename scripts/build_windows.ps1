param(
    [string]$Python = 'python',
    [string]$ArtifactId = ''
)
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repositoryRoot
try {
    if (-not $ArtifactId) {
        $ArtifactId = git rev-parse --short=7 HEAD
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read commit SHA' }
    }
    & $Python -c "import struct,sys; assert sys.platform == 'win32' and sys.version_info[:2] == (3,11) and struct.calcsize('P') == 8, 'Use Windows x64 Python 3.11'"
    if ($LASTEXITCODE -ne 0) { throw 'Unsupported Python environment' }
    & $Python -m pip install --disable-pip-version-check --no-input --progress-bar off -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    & $Python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed' }
    $previousQtPlatform = $env:QT_QPA_PLATFORM
    $env:QT_QPA_PLATFORM = 'offscreen'
    & $Python -m unittest discover -s tests -t . -p 'test_*.py' -v
    if ($LASTEXITCODE -ne 0) { throw 'Offline tests failed' }
    $buildDirectory = Join-Path 'build' ('release-build-' + [Guid]::NewGuid().ToString('N'))
    & $Python -m PyInstaller --noconfirm --onefile --windowed --noconsole --name Scribe2SRT --collect-data opencc --additional-hooks-dir scripts/hooks --specpath $buildDirectory --distpath "$buildDirectory/dist" --workpath "$buildDirectory/work" app.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    & $Python scripts/package_windows.py --executable "$buildDirectory/dist/Scribe2SRT.exe" --artifact-id $ArtifactId
    if ($LASTEXITCODE -ne 0) { throw 'ZIP packaging or extracted executable validation failed' }
} finally {
    if (Test-Path variable:previousQtPlatform) { $env:QT_QPA_PLATFORM = $previousQtPlatform }
    Pop-Location
}
