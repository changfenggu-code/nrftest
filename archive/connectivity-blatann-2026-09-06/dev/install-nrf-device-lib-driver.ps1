# Download and install the official Nordic nRF device-lib driver on Windows.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Uri,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$Sha256
)

$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'The nRF device-lib driver installer is supported on Windows only.'
}

$downloadPath = Join-Path (
    [System.IO.Path]::GetTempPath()
) (
    'nrftest-nrf-device-lib-driver-' + [System.Guid]::NewGuid().ToString('N') + '.exe'
)

try {
    Write-Host 'Downloading the official nRF device-lib driver installer...'
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $downloadPath

    $hashAlgorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($downloadPath)
        try {
            $actualHash = [System.BitConverter]::ToString(
                $hashAlgorithm.ComputeHash($stream)
            ).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $stream.Dispose()
        }
    }
    finally {
        $hashAlgorithm.Dispose()
    }

    $expectedHash = $Sha256.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "Driver installer SHA-256 mismatch. Expected $expectedHash, got $actualHash."
    }

    Write-Host 'Starting the driver installer. Windows may show a UAC prompt and installer UI.'
    $process = Start-Process -FilePath $downloadPath -Verb RunAs -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Driver installer failed with exit code $($process.ExitCode)."
    }
}
finally {
    if (Test-Path -LiteralPath $downloadPath) {
        Remove-Item -Force -LiteralPath $downloadPath
    }
}
