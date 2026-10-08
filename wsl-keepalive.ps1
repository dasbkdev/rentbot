# Keep ASCII source for Windows PowerShell 5.1 compatibility.
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName 'RentbotDockerEngine' -ErrorAction SilentlyContinue
if (-not $task) {
    throw 'The RentbotDockerEngine Windows task is missing. This WSL installation needs its startup task.'
}
if ($task.State -ne 'Running') {
    Start-ScheduledTask -TaskName 'RentbotDockerEngine'
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Seconds 1
        try {
            $null = & "$env:SystemRoot\System32\wsl.exe" --distribution Ubuntu-24.04 --exec docker info 2>$null
            $ready = ($LASTEXITCODE -eq 0)
        }
        catch { $ready = $false }
    } while (-not $ready -and [DateTime]::UtcNow -lt $deadline)
    if (-not $ready) { throw 'The WSL Docker Engine did not become ready within 45 seconds.' }
}
