# Elimina la tarea del Programador de tareas.
param([string]$TaskName = "AmazonPriceTracker")

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item "HKCU:\Software\Classes\AppUserModelId\AmazonPriceTracker" -Force -ErrorAction SilentlyContinue
Write-Host "Tarea '$TaskName' eliminada."
