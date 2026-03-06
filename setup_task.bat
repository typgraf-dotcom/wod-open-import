@echo off
REM ─────────────────────────────────────────────────────────
REM setup_task.bat
REM Crée deux tâches planifiées Windows :
REM   1. WodOpenImport     — daily_import.py  (scoring.fit)  à 03:00
REM   2. WodOpenImportCC   — cc_import.py     (CompCorner)   à 03:30
REM
REM Corrections v2 :
REM   - Chemin Python complet (évite erreur PATH en tâche planifiée)
REM   - StartWhenAvailable : lance dès le démarrage si 3h était manqué
REM   - Répertoire de travail explicite (/sd fixé)
REM
REM Lancer en tant qu'Administrateur :
REM   clic droit → "Exécuter en tant qu'administrateur"
REM ─────────────────────────────────────────────────────────

REM Chemin Python complet (pas via PATH, pour fiabilité en tâche planifiée)
SET PYTHON=C:\Users\thery.persyn\AppData\Local\Programs\Python\Python313\python.exe

REM Répertoire du projet
SET WORKDIR=C:\projects\wod-open-import

ECHO Python : %PYTHON%
ECHO Workdir: %WORKDIR%
ECHO.

REM ─── Tâche 1 : scoring.fit — daily_import.py à 03:00 ─────
schtasks /delete /tn "WodOpenImport" /f >nul 2>&1

schtasks /create ^
  /tn "WodOpenImport" ^
  /tr "\"%PYTHON%\" \"%WORKDIR%\daily_import.py\"" ^
  /sc DAILY ^
  /st 03:00 ^
  /ru "%USERNAME%" ^
  /f

IF %ERRORLEVEL% NEQ 0 (
    ECHO [ERREUR] Tache 1 echouee. Lancez en Administrateur.
    goto :end
)
ECHO [OK] Tache 1 creee : WodOpenImport (daily_import.py a 03:00)

REM Activer "lancer dès que possible si l'heure est passée" via PowerShell
powershell -NoProfile -Command ^
  "$t = Get-ScheduledTask -TaskName 'WodOpenImport';" ^
  "$t.Settings.StartWhenAvailable = $true;" ^
  "$t | Set-ScheduledTask" >nul 2>&1

IF %ERRORLEVEL% EQU 0 (
    ECHO [OK] StartWhenAvailable active pour WodOpenImport
) ELSE (
    ECHO [WARN] StartWhenAvailable non configure (droit admin requis)
)

REM ─── Tâche 2 : CompetitionCorner — cc_import.py à 03:30 ──
schtasks /delete /tn "WodOpenImportCC" /f >nul 2>&1

schtasks /create ^
  /tn "WodOpenImportCC" ^
  /tr "\"%PYTHON%\" \"%WORKDIR%\cc_import.py\"" ^
  /sc DAILY ^
  /st 03:30 ^
  /ru "%USERNAME%" ^
  /f

IF %ERRORLEVEL% NEQ 0 (
    ECHO [ERREUR] Tache 2 echouee.
    goto :end
)
ECHO [OK] Tache 2 creee : WodOpenImportCC (cc_import.py a 03:30)

powershell -NoProfile -Command ^
  "$t = Get-ScheduledTask -TaskName 'WodOpenImportCC';" ^
  "$t.Settings.StartWhenAvailable = $true;" ^
  "$t | Set-ScheduledTask" >nul 2>&1

IF %ERRORLEVEL% EQU 0 (
    ECHO [OK] StartWhenAvailable active pour WodOpenImportCC
) ELSE (
    ECHO [WARN] StartWhenAvailable non configure (droit admin requis)
)

ECHO.
ECHO ============================================================
ECHO  Recapitulatif
ECHO ============================================================
ECHO  WodOpenImport    : daily_import.py  a 03:00
ECHO  WodOpenImportCC  : cc_import.py     a 03:30
ECHO  Python           : %PYTHON%
ECHO  Workdir          : %WORKDIR%
ECHO  Logs             : %WORKDIR%\logs\
ECHO.
ECHO  Si le PC est eteint a 3h, les taches tourneront
ECHO  automatiquement au demarrage suivant (StartWhenAvailable).
ECHO.
ECHO  Pour verifier :
ECHO    schtasks /query /tn "WodOpenImport"   /fo LIST
ECHO    schtasks /query /tn "WodOpenImportCC" /fo LIST
ECHO.
ECHO  Pour tester maintenant :
ECHO    schtasks /run /tn "WodOpenImport"
ECHO    schtasks /run /tn "WodOpenImportCC"
ECHO ============================================================

:end
pause
