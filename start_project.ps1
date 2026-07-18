param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173,
    [switch]$SkipDocker,
    [switch]$SkipMigrations,
    [switch]$SkipFixtures
)

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    # Older Windows PowerShell versions may not allow replacing the console encoding.
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Import-DotEnv {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }
        if ($line -match "^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$") {
            $name = $matches[1]
            $value = $matches[2].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function Import-UserEnv {
    param(
        [string]$Name,
        [switch]$Optional
    )

    $current = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ($current) {
        return
    }

    $userValue = [Environment]::GetEnvironmentVariable($Name, "User")
    if ($userValue) {
        [Environment]::SetEnvironmentVariable($Name, $userValue, "Process")
        return
    }

    if (-not $Optional) {
        throw "缺少环境变量 $Name。请在 .env 或用户环境变量中配置后再启动。"
    }
}

function Require-Command {
    param([string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "未找到命令 $Name。请先安装并确认它已加入 PATH。"
    }
}

function Require-File {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        throw "未找到 $Path。请先完成项目依赖安装。"
    }
}

function Set-DefaultEnv {
    param(
        [string]$Name,
        [string]$Value
    )

    if (-not [Environment]::GetEnvironmentVariable($Name, "Process")) {
        [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
    }
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath 执行失败，退出码 $LASTEXITCODE。"
    }
}

function Wait-ContainerHealthy {
    param(
        [string]$Name,
        [int]$TimeoutSeconds = 120
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $status = $null
        try {
            $status = docker inspect -f "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $Name 2>$null
        } catch {
            $status = $null
        }

        if ($status -eq "healthy" -or $status -eq "running") {
            Write-Host "${Name}: $status"
            return
        }

        Write-Host "${Name}: $status，等待中..."
        Start-Sleep -Seconds 3
    }

    throw "$Name 在 $TimeoutSeconds 秒内未就绪。"
}

function Get-PowerShellExe {
    $pwsh = Get-Command "pwsh.exe" -ErrorAction SilentlyContinue
    if ($pwsh) {
        return $pwsh.Source
    }

    $powershell = Get-Command "powershell.exe" -ErrorAction SilentlyContinue
    if ($powershell) {
        return $powershell.Source
    }

    throw "未找到 pwsh.exe 或 powershell.exe。"
}

function Start-DevWindow {
    param(
        [string]$Title,
        [string]$Command
    )

    $shell = Get-PowerShellExe
    $fullCommand = "`$Host.UI.RawUI.WindowTitle = '$Title'; Set-Location '$root'; $Command"
    Start-Process -FilePath $shell -ArgumentList @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $fullCommand) -WindowStyle Normal
}

Write-Step "读取本地环境变量"
Import-DotEnv ".env"

Set-DefaultEnv "MYSQL_HOST_PORT" "3307"
Set-DefaultEnv "REDIS_HOST_PORT" "6380"
Set-DefaultEnv "MINIO_HOST_PORT" "19000"
Set-DefaultEnv "MINIO_CONSOLE_HOST_PORT" "19001"
Set-DefaultEnv "DB_HOST" "127.0.0.1"
Set-DefaultEnv "DB_PORT" $env:MYSQL_HOST_PORT
Set-DefaultEnv "DB_NAME" "career_assistant"
Set-DefaultEnv "CHECKPOINT_BACKEND" "redis"
Set-DefaultEnv "OBJECT_STORE_REGION" "us-east-1"
Set-DefaultEnv "OBJECT_STORE_BUCKET" "career-assistant"
Set-DefaultEnv "OBJECT_STORE_ENDPOINT" "http://127.0.0.1:$($env:MINIO_HOST_PORT)"

Import-UserEnv "DB_PASSWORD"
Import-UserEnv "REDIS_PASSWORD"
Import-UserEnv "MINIO_ROOT_USER"
Import-UserEnv "MINIO_ROOT_PASSWORD"
Import-UserEnv "APP_AUTH_SECRET"
Import-UserEnv "OBJECT_ENCRYPTION_KEY"
Import-UserEnv "DEEPSEEK_API_KEY" -Optional
Import-UserEnv "TENCENT_DOCS_TOKEN" -Optional

if (-not $env:OBJECT_STORE_ACCESS_KEY) {
    $env:OBJECT_STORE_ACCESS_KEY = $env:MINIO_ROOT_USER
}
if (-not $env:OBJECT_STORE_SECRET_KEY) {
    $env:OBJECT_STORE_SECRET_KEY = $env:MINIO_ROOT_PASSWORD
}

$encodedDbPassword = [System.Uri]::EscapeDataString($env:DB_PASSWORD)
$encodedRedisPassword = [System.Uri]::EscapeDataString($env:REDIS_PASSWORD)
$env:DATABASE_URL = "mysql+pymysql://root:$encodedDbPassword@127.0.0.1:$($env:MYSQL_HOST_PORT)/$($env:DB_NAME)?charset=utf8mb4"
$env:REDIS_URL = "redis://:$encodedRedisPassword@127.0.0.1:$($env:REDIS_HOST_PORT)/0"

Write-Step "检查本机依赖"
Require-Command "npm.cmd"
Require-File ".\.venv\Scripts\python.exe"

if (-not $SkipDocker) {
    Require-Command "docker"

    Write-Step "启动 Docker 基础服务 MySQL / Redis / MinIO"
    Invoke-Checked -FilePath "docker" -Arguments @("compose", "-p", "platform-foundation", "up", "-d", "mysql", "redis", "minio")

    Write-Step "等待基础服务就绪"
    Wait-ContainerHealthy "platform-foundation-mysql-1"
    Wait-ContainerHealthy "platform-foundation-redis-1"
    Wait-ContainerHealthy "platform-foundation-minio-1"
} else {
    Write-Step "已跳过 Docker 基础服务启动"
}

if (-not $SkipMigrations) {
    Write-Step "执行数据库迁移"
    Invoke-Checked -FilePath ".\.venv\Scripts\python.exe" -Arguments @("-m", "alembic", "upgrade", "head")
} else {
    Write-Step "已跳过数据库迁移"
}

if (-not $SkipFixtures) {
    Write-Step "写入开发演示数据"
    Invoke-Checked -FilePath ".\.venv\Scripts\python.exe" -Arguments @("scripts\create_wave2_fixtures.py")
} else {
    Write-Step "已跳过演示数据写入"
}

Write-Step "打开后端和前端开发服务窗口"
Start-DevWindow -Title "Career Assistant Backend" -Command "& '.\scripts\start_dev_backend.ps1' -Port $BackendPort"
Start-DevWindow -Title "Career Assistant Frontend" -Command "`$env:VITE_API_PROXY_TARGET = 'http://127.0.0.1:$BackendPort'; npm.cmd --prefix frontend run dev -- --host 127.0.0.1 --port $FrontendPort"

$frontendUrl = "http://127.0.0.1:$FrontendPort"
$backendUrl = "http://127.0.0.1:$BackendPort/api/health/live"
$minioUrl = "http://127.0.0.1:$($env:MINIO_CONSOLE_HOST_PORT)"

Write-Host ""
Write-Host "启动命令已发出：" -ForegroundColor Green
Write-Host "前端: $frontendUrl"
Write-Host "后端健康检查: $backendUrl"
Write-Host "MinIO 控制台: $minioUrl"
Write-Host ""
Write-Host "后端和前端日志分别在新打开的两个 PowerShell 窗口中。"
Write-Host "如需停止服务，请关闭这两个窗口；Docker 基础服务可用 docker compose -p platform-foundation down 停止。"

Start-Sleep -Seconds 4
Start-Process $frontendUrl

Write-Host ""
Read-Host "按 Enter 关闭这个启动窗口"
