$ErrorActionPreference = 'Stop'

$ProjectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $ProjectDirectory
try {
    py -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Unit tests failed with exit code $LASTEXITCODE."
    }

    py app.py --self-test
    if ($LASTEXITCODE -ne 0) {
        throw "Source self-test failed with exit code $LASTEXITCODE."
    }

    py -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name CubeSat_GCS `
        --add-data "assets;assets" `
        --add-data "..\CTE Images;assets\cte_reference" `
        app.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE. Close any running CubeSat GCS instance and retry."
    }

    $Executable = Join-Path $ProjectDirectory 'dist\CubeSat_GCS.exe'
    if (-not (Test-Path -LiteralPath $Executable)) {
        throw "Build completed without the expected executable: $Executable"
    }

    $Process = Start-Process -FilePath $Executable -ArgumentList '--self-test' -WindowStyle Hidden -Wait -PassThru
    if ($Process.ExitCode -ne 0) {
        throw "Packaged application self-test failed with exit code $($Process.ExitCode)."
    }

    Write-Host "Build verified: $Executable"
}
finally {
    Pop-Location
}
