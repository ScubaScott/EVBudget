# Creates an EVBudget shortcut with the app icon on the Desktop. Run through create_desktop_shortcut.bat.
param([Parameter(Mandatory = $true)][string]$Root)

$root = (Resolve-Path $Root).Path
$desktop = [Environment]::GetFolderPath('Desktop')
$link = Join-Path $desktop 'EVBudget.lnk'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = Join-Path $root 'launch_dashboard.bat'
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = (Join-Path $root 'assets\evbudget.ico') + ',0'
$shortcut.Description = 'Open the EVBudget dashboard'
$shortcut.Save()

Write-Host "Shortcut created: $link"
