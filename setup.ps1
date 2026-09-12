# Thin wrapper for Windows. All installer logic lives in install.py.
# Usage: powershell -ExecutionPolicy Bypass -File .\setup.ps1 [install.py arguments]
#   e.g. .\setup.ps1 --target ..\app --plan plus

$ErrorActionPreference = 'Stop'
$installer = Join-Path $PSScriptRoot 'install.py'

# The py launcher first; "python" alone may be the Microsoft Store stub.
foreach ($candidate in @(@('py', '-3'), @('python3'), @('python'))) {
    $command = Get-Command $candidate[0] -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) { continue }

    $prefix = @($candidate | Select-Object -Skip 1)
    & $command.Source @prefix -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>$null
    if ($LASTEXITCODE -ne 0) { continue }

    & $command.Source @prefix $installer @args
    exit $LASTEXITCODE
}

[Console]::Error.WriteLine('Error: Python 3.8+ is required. Install it from https://www.python.org/downloads/')
exit 1
