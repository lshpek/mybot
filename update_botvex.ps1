[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetPath,
    [switch]$StartBot
)

$ErrorActionPreference = 'Stop'
$SourcePath = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$TargetPath = [System.IO.Path]::GetFullPath($TargetPath)
$BackupRoot = Join-Path (Split-Path -Parent $TargetPath) 'BOTVEX_backups'
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$BackupPath = Join-Path $BackupRoot "BOTVEX_order_bot_$Stamp"
$BackupProject = Join-Path $BackupPath 'project'
$dbResolved = $null
$dbBackupDir = $null
$CopyStarted = $false
$BackupComplete = $false

function Assert-ExternalExitCode([string]$Action) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Action завершилось с кодом $LASTEXITCODE. Резервная копия: $BackupPath"
    }
}

function Copy-DirectoryContents([string]$FromPath, [string]$ToPath) {
    Get-ChildItem -LiteralPath $FromPath -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $ToPath -Recurse -Force
    }
}

function Restore-Backup {
    if (!(Test-Path -LiteralPath $BackupProject -PathType Container)) { return }

    Write-Host 'Восстанавливаю исходные файлы из резервной копии...' -ForegroundColor Yellow
    $preserved = @('.venv', 'venv', '.git')
    Get-ChildItem -LiteralPath $TargetPath -Force | Where-Object { $_.Name -notin $preserved } | ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }
    Copy-DirectoryContents -FromPath $BackupProject -ToPath $TargetPath

    if ($dbResolved -and $dbBackupDir -and (Test-Path -LiteralPath $dbBackupDir -PathType Container)) {
        $dbBackupFile = Get-ChildItem -LiteralPath $dbBackupDir -File | Where-Object {
            $_.Name -notlike '*-wal' -and $_.Name -notlike '*-shm'
        } | Select-Object -First 1
        if ($dbBackupFile) {
            Copy-Item -LiteralPath $dbBackupFile.FullName -Destination $dbResolved -Force
            foreach ($suffix in @('-wal', '-shm')) {
                $sidecarBackup = Join-Path $dbBackupDir ($dbBackupFile.Name + $suffix)
                $liveSidecar = "$dbResolved$suffix"
                if (Test-Path -LiteralPath $sidecarBackup) {
                    Copy-Item -LiteralPath $sidecarBackup -Destination $liveSidecar -Force
                } elseif (Test-Path -LiteralPath $liveSidecar) {
                    Remove-Item -LiteralPath $liveSidecar -Force
                }
            }
        }
    }
    Write-Host 'Исходные файлы восстановлены. Проверь виртуальное окружение, если ошибка была при установке зависимостей.' -ForegroundColor Yellow
}

try {
    if ($SourcePath.TrimEnd('\') -eq $TargetPath.TrimEnd('\')) {
        throw 'Источник архива и целевая папка совпадают. Распакуй BOTVEX_order_bot_v4 в отдельную папку перед обновлением.'
    }
    if (!(Test-Path -LiteralPath $TargetPath -PathType Container)) {
        throw "Целевая папка не существует: $TargetPath"
    }
    if (!(Test-Path (Join-Path $SourcePath 'bot.py')) -or !(Test-Path (Join-Path $SourcePath '.env.example'))) {
        throw 'В папке распакованного архива не найдены bot.py и .env.example.'
    }
    if (!(Test-Path (Join-Path $TargetPath 'bot.py'))) {
        throw "В целевой папке не найден bot.py: $TargetPath"
    }

    Write-Host 'BOTVEX Order Bot — безопасное обновление' -ForegroundColor Cyan
    Write-Host "Источник: $SourcePath"
    Write-Host "Целевая папка: $TargetPath"
    Write-Host ''
    Write-Host 'Перед продолжением останови работающего бота в его терминале: Ctrl+C.' -ForegroundColor Yellow
    $confirmation = Read-Host 'Когда бот остановлен, введи STOP для продолжения'
    if ($confirmation -cne 'STOP') {
        throw 'Пользователь отменил обновление. Файлы не изменялись.'
    }

    New-Item -ItemType Directory -Path $BackupProject -Force | Out-Null
    $excludedBackupItems = @('.venv', 'venv', '.git', '__pycache__', '.pytest_cache', '.ruff_cache')
    Get-ChildItem -LiteralPath $TargetPath -Force | Where-Object { $_.Name -notin $excludedBackupItems } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $BackupProject -Recurse -Force
    }

    # Preserve and separately back up the configured SQLite DB, including WAL sidecars.
    $envFile = Join-Path $TargetPath '.env'
    if (Test-Path -LiteralPath $envFile) {
        $dbLine = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^\s*DB_PATH\s*=' } | Select-Object -First 1
        if ($dbLine) {
            $dbRaw = ($dbLine -replace '^\s*DB_PATH\s*=\s*', '').Trim().Trim('"').Trim("'")
            if ($dbRaw) {
                if ([System.IO.Path]::IsPathRooted($dbRaw)) {
                    $dbResolved = [System.IO.Path]::GetFullPath($dbRaw)
                } else {
                    $dbResolved = [System.IO.Path]::GetFullPath((Join-Path $TargetPath $dbRaw))
                }
                if (Test-Path -LiteralPath $dbResolved -PathType Leaf) {
                    $dbBackupDir = Join-Path $BackupPath 'external_database'
                    New-Item -ItemType Directory -Path $dbBackupDir -Force | Out-Null
                    Copy-Item -LiteralPath $dbResolved -Destination (Join-Path $dbBackupDir ([System.IO.Path]::GetFileName($dbResolved))) -Force
                    foreach ($suffix in @('-wal', '-shm')) {
                        $sidecar = "$dbResolved$suffix"
                        if (Test-Path -LiteralPath $sidecar -PathType Leaf) {
                            Copy-Item -LiteralPath $sidecar -Destination (Join-Path $dbBackupDir ([System.IO.Path]::GetFileName($sidecar))) -Force
                        }
                    }
                }
            }
        }
    }
    $BackupComplete = $true
    Write-Host "Резервная копия создана: $BackupPath" -ForegroundColor Green

    # Keep local secrets, local DB, virtualenv, Git metadata and a customised service catalog.
    $excludedSourceItems = @('.env', '.venv', 'venv', '.git', 'data', '__pycache__', '.pytest_cache', '.ruff_cache')
    $CopyStarted = $true
    Get-ChildItem -LiteralPath $SourcePath -Force | Where-Object { $_.Name -notin $excludedSourceItems } | ForEach-Object {
        if ($_.Name -eq 'config' -and $_.PSIsContainer) {
            $targetConfig = Join-Path $TargetPath 'config'
            New-Item -ItemType Directory -Path $targetConfig -Force | Out-Null
            Get-ChildItem -LiteralPath $_.FullName -Force | ForEach-Object {
                $destination = Join-Path $targetConfig $_.Name
                if ($_.Name -eq 'services.json' -and (Test-Path -LiteralPath $destination)) {
                    Write-Host 'Сохраняю текущий config/services.json.' -ForegroundColor Yellow
                } else {
                    Copy-Item -LiteralPath $_.FullName -Destination $targetConfig -Recurse -Force
                }
            }
        } else {
            Copy-Item -LiteralPath $_.FullName -Destination $TargetPath -Recurse -Force
        }
    }

    $pythonExe = Join-Path $TargetPath '.venv\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $pythonExe)) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3.12 -m venv (Join-Path $TargetPath '.venv')
            Assert-ExternalExitCode 'Создание виртуального окружения'
        } else {
            & python -m venv (Join-Path $TargetPath '.venv')
            Assert-ExternalExitCode 'Создание виртуального окружения'
        }
    }
    if (!(Test-Path -LiteralPath $pythonExe)) {
        throw 'Python в .venv не найден после создания окружения.'
    }

    Push-Location $TargetPath
    try {
        Write-Host "`nУстановка/обновление зависимостей..." -ForegroundColor Cyan
        & $pythonExe -m pip install -r (Join-Path $TargetPath 'requirements.txt')
        Assert-ExternalExitCode 'Установка зависимостей'

        Write-Host "`nПроверка синтаксиса..." -ForegroundColor Cyan
        & $pythonExe -m compileall -q bot.py app tests
        Assert-ExternalExitCode 'Проверка синтаксиса'

        Write-Host "`nАвтоматические тесты..." -ForegroundColor Cyan
        & $pythonExe -m unittest discover -s tests -v
        Assert-ExternalExitCode 'Автоматические тесты'

        Write-Host "`nПроверка и безопасная миграция SQLite..." -ForegroundColor Cyan
        @'
import os
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))
from app.storage import OrderStorage
storage = OrderStorage(os.getenv("DB_PATH", "data/botvex_orders.sqlite3"))
storage.initialize()
print("SQLite schema/migration check: OK")
'@ | & $pythonExe -
        Assert-ExternalExitCode 'Миграция SQLite'
    } finally {
        Pop-Location
    }

    Write-Host "`nОбновление завершено. .env и текущая база данных сохранены." -ForegroundColor Green
    if (!(Test-Path -LiteralPath $envFile)) {
        Write-Host 'Внимание: .env отсутствовал. Скопируй .env.example в .env и заполни BOT_TOKEN и ADMIN_IDS.' -ForegroundColor Yellow
    }
    Write-Host "Резервная копия: $BackupPath"
    Write-Host "Для запуска вручную: Set-Location '$TargetPath'; .\.venv\Scripts\python.exe bot.py"

    if ($StartBot) {
        Push-Location $TargetPath
        try {
            & $pythonExe bot.py
        } finally {
            Pop-Location
        }
    }
} catch {
    Write-Host "`nОШИБКА: $($_.Exception.Message)" -ForegroundColor Red
    if ($BackupComplete -and $CopyStarted -and (Test-Path -LiteralPath $BackupProject)) {
        try {
            Restore-Backup
        } catch {
            Write-Host "Автоматическое восстановление тоже завершилось ошибкой: $($_.Exception.Message)" -ForegroundColor Red
            Write-Host 'Не удаляй резервную копию — она содержит предыдущую версию.' -ForegroundColor Yellow
        }
    }
    if (Test-Path -LiteralPath $BackupPath) {
        Write-Host "Резервная копия сохранена: $BackupPath" -ForegroundColor Yellow
    }
    exit 1
}
