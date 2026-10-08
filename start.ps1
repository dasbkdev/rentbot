param(
    [ValidateSet('up', 'check', 'ids', 'logs', 'status', 'stop')]
    [string]$Mode = 'up'
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$script:DockerExe = $null

function Invoke-Docker {
    param([string[]]$DockerArgs)
    & $script:DockerExe @DockerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed (exit $LASTEXITCODE). See the output above."
    }
}

function Test-DockerReady {
    try {
        $null = & $script:DockerExe info --format '{{.OSType}}' 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    catch { return $false }
}

Push-Location -LiteralPath $PSScriptRoot
try {
    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerCommand) { $script:DockerExe = $dockerCommand.Source }
    if (-not $script:DockerExe -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'docker.cmd'))) {
        $script:DockerExe = Join-Path $PSScriptRoot 'docker.cmd'
    }
    if (-not $script:DockerExe) {
        throw 'Docker CLI was not found in PATH. Configure your Docker Engine first. This script does not install software.'
    }
    if (-not (Test-DockerReady)) {
        throw 'Docker Engine is not available. Start your engine or select the correct Docker context, then try again.'
    }
    Invoke-Docker -DockerArgs @('compose', 'version')

    if (-not (Test-Path -LiteralPath '.env')) {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
        throw "Created $PSScriptRoot\.env. Fill it in, then run this command again."
    }
    # Quiet validation never prints interpolated secrets.
    Invoke-Docker -DockerArgs @('compose', 'config', '--quiet')
    switch ($Mode) {
        'status' { Invoke-Docker -DockerArgs @('compose', 'ps', '--all') }
        'logs' { Invoke-Docker -DockerArgs @('compose', 'logs', '--follow', '--tail', '100', 'bot') }
        'stop' { Invoke-Docker -DockerArgs @('compose', 'stop', 'bot') }
        'ids' {
            # The helper polls Telegram too; stop the main service to avoid conflicts.
            Invoke-Docker -DockerArgs @('compose', 'stop', 'bot')
            Invoke-Docker -DockerArgs @('compose', 'build', 'bot')
            Invoke-Docker -DockerArgs @('compose', 'run', '--rm', '--no-deps', 'bot', 'python', '-m', 'rentbot', 'ids')
        }
        default {
            Invoke-Docker -DockerArgs @('compose', 'build', 'bot')
            Invoke-Docker -DockerArgs @('compose', 'run', '--rm', '--no-deps', '-T', 'bot', 'python', '-m', 'rentbot', 'check')
            if ($Mode -eq 'up') {
                Invoke-Docker -DockerArgs @('compose', 'up', '--detach', '--no-build', 'bot')
                Invoke-Docker -DockerArgs @('compose', 'ps', '--all')
                Write-Host "Container started. Check Telegram startup and logs: & '$PSScriptRoot\start.cmd' -Mode logs"
            }
            else { Write-Host 'Configuration format is valid. Telegram access has not been checked.' }
        }
    }
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
finally { Pop-Location }
