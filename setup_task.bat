@echo off
REM ─────────────────────────────────────────────────────────
REM setup_task.bat  v3
REM Crée deux tâches planifiées Windows via PowerShell :
REM   1. WodOpenImport     — daily_import.py  (scoring.fit)  à 06:00
REM   2. WodOpenImportCC   — cc_import.py     (CompCorner)   à 06:30
REM
REM - WorkingDirectory explicitement fixé (correction v3)
REM - StartWhenAvailable : lance dès le démarrage si l'heure est passée
REM - Lancer en tant qu'Administrateur
REM ─────────────────────────────────────────────────────────

powershell -NoProfile -ExecutionPolicy Bypass -Command "& {

  $PYTHON  = 'C:\Users\thery.persyn\AppData\Local\Programs\Python\Python313\python.exe'
  $WORKDIR = 'C:\projects\wod-open-import'

  # ── Action commune ────────────────────────────────────────
  function Make-Action($script) {
    $a = New-ScheduledTaskAction `
      -Execute $PYTHON `
      -Argument \"`\"`$WORKDIR\$script`\"\" `
      -WorkingDirectory $WORKDIR
    return $a
  }

  # ── Trigger quotidien ─────────────────────────────────────
  function Make-Trigger($hour, $min) {
    return New-ScheduledTaskTrigger -Daily -At \"${hour}:${min}\"
  }

  # ── Principal (compte courant) ────────────────────────────
  $principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType S4U `
    -RunLevel Limited

  # ── Settings ─────────────────────────────────────────────
  $settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

  # ══ Tâche 1 : WodOpenImport ═══════════════════════════════
  Unregister-ScheduledTask -TaskName 'WodOpenImport' -Confirm:`$false -ErrorAction SilentlyContinue
  Register-ScheduledTask `
    -TaskName 'WodOpenImport' `
    -Action   (Make-Action 'daily_import.py') `
    -Trigger  (Make-Trigger 6 00) `
    -Principal `$principal `
    -Settings `$settings `
    -Description 'Import scoring.fit → wod-open.com (quotidien 06h00)' | Out-Null
  Write-Host '[OK] WodOpenImport (daily_import.py) cree a 06:00'

  # ══ Tâche 2 : WodOpenImportCC ═════════════════════════════
  Unregister-ScheduledTask -TaskName 'WodOpenImportCC' -Confirm:`$false -ErrorAction SilentlyContinue
  Register-ScheduledTask `
    -TaskName 'WodOpenImportCC' `
    -Action   (Make-Action 'cc_import.py') `
    -Trigger  (Make-Trigger 6 30) `
    -Principal `$principal `
    -Settings `$settings `
    -Description 'Import CompetitionCorner → wod-open.com (quotidien 06h30)' | Out-Null
  Write-Host '[OK] WodOpenImportCC (cc_import.py) cree a 06:30'

  # ── Vérification ─────────────────────────────────────────
  Write-Host ''
  Write-Host '============================================================'
  Write-Host ' Verification'
  Write-Host '============================================================'
  foreach ($tn in @('WodOpenImport','WodOpenImportCC')) {
    $t = Get-ScheduledTask -TaskName `$tn -ErrorAction SilentlyContinue
    if (`$t) {
      `$a = `$t.Actions[0]
      Write-Host \"  [`$tn]\"
      Write-Host \"    Execute : `$(`$a.Execute)\"
      Write-Host \"    Args    : `$(`$a.Arguments)\"
      Write-Host \"    WorkDir : `$(`$a.WorkingDirectory)\"
      `$info = Get-ScheduledTaskInfo -TaskName `$tn
      Write-Host \"    NextRun : `$(`$info.NextRunTime)\"
    }
  }
  Write-Host ''
  Write-Host ' Pour tester maintenant (PowerShell admin) :'
  Write-Host '   Start-ScheduledTask -TaskName WodOpenImport'
  Write-Host '   Start-ScheduledTask -TaskName WodOpenImportCC'
  Write-Host '============================================================'
}"

pause
