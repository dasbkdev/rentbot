# Run inside the Windows scheduled task, independently of terminal sessions.
$hostLog = Join-Path $PSScriptRoot 'wsl-host.log'
Add-Content -LiteralPath $hostLog -Value ((Get-Date -Format o) + ' Starting WSL host.')
& "$env:SystemRoot\System32\wsl.exe" --distribution Ubuntu-24.04 --exec /usr/local/bin/rentbot-wsl-keepalive >> $hostLog 2>&1
Add-Content -LiteralPath $hostLog -Value ((Get-Date -Format o) + ' WSL host exit: ' + $LASTEXITCODE)
exit $LASTEXITCODE
