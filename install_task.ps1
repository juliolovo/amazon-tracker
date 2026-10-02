# Registra el tracker en el Programador de tareas de Windows.
# Uso:  powershell -ExecutionPolicy Bypass -File .\install_task.ps1 [-At "11:00"]
#   Corre una vez al día a la hora indicada. Si la PC estaba apagada a esa hora, corre al encenderla.
param(
    [string]$At = "11:00",
    [string]$TaskName = "AmazonPriceTracker"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "tracker.py"

# pythonw.exe = Python sin ventana de consola
$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

# Registra un AppUserModelID para que las notificaciones aparezcan como "Amazon Price Tracker"
$aumid = "HKCU:\Software\Classes\AppUserModelId\AmazonPriceTracker"
New-Item -Path $aumid -Force | Out-Null
New-ItemProperty -Path $aumid -Name DisplayName -Value "Amazon Price Tracker" -PropertyType String -Force | Out-Null

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`" track" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, "HH:mm", $null))
# Priority 4 = normal. El valor por defecto (7) es tan bajo que Python/Edge tardan minutos en arrancar.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew `
    -Priority 4
# Interactive = solo cuando el usuario tiene sesión iniciada (necesario para mostrar notificaciones)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Revisa precios de Amazon todos los días a las $At" -Force | Out-Null

Write-Host "Tarea '$TaskName' registrada: todos los días a las $At."
Write-Host "Ejecutable: $pythonw $script"
Write-Host "Ejecutar ahora:  Start-ScheduledTask -TaskName $TaskName"
