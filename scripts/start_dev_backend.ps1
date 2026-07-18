param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$ProbeOnly
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$") {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
        }
    }
}

if (-not $env:OBJECT_STORE_ENDPOINT) {
    $hostPort = if ($env:MINIO_HOST_PORT) { $env:MINIO_HOST_PORT } else { "19000" }
    $env:OBJECT_STORE_ENDPOINT = "http://127.0.0.1:$hostPort"
}
$redisContainerPassword = $null
try {
    $redisContainerEnv = docker inspect -f "{{range .Config.Env}}{{println .}}{{end}}" platform-foundation-redis-1 2>$null
    $redisPasswordLine = $redisContainerEnv | Where-Object { $_ -like "REDIS_PASSWORD=*" } | Select-Object -First 1
    if ($redisPasswordLine) {
        $redisContainerPassword = $redisPasswordLine.Substring("REDIS_PASSWORD=".Length)
    }
} catch {
    $redisContainerPassword = $null
}
if ($redisContainerPassword) {
    $env:REDIS_PASSWORD = $redisContainerPassword
}
$redisUrlNeedsHostMapping = (
    (-not $env:REDIS_URL) `
    -or ($env:REDIS_URL -match "redis://redis:") `
    -or ($env:REDIS_URL -match "redis://localhost:6379") `
    -or ($env:REDIS_URL -match "redis://127\.0\.0\.1:6379") `
    -or [bool]$redisContainerPassword
)
if ($redisUrlNeedsHostMapping) {
    $redisPort = if ($env:REDIS_HOST_PORT) { $env:REDIS_HOST_PORT } else { "6380" }
    if ($env:REDIS_PASSWORD) {
        $env:REDIS_URL = "redis://:$($env:REDIS_PASSWORD)@127.0.0.1:$redisPort/0"
    } else {
        $env:REDIS_URL = "redis://127.0.0.1:$redisPort/0"
    }
}
if (-not $env:OBJECT_STORE_ACCESS_KEY -and $env:MINIO_ROOT_USER) {
    $env:OBJECT_STORE_ACCESS_KEY = $env:MINIO_ROOT_USER
}
if (-not $env:OBJECT_STORE_SECRET_KEY -and $env:MINIO_ROOT_PASSWORD) {
    $env:OBJECT_STORE_SECRET_KEY = $env:MINIO_ROOT_PASSWORD
}

if ($ProbeOnly) {
    & ".\.venv\Scripts\python.exe" -c "import redis, os; client = redis.Redis.from_url(os.environ['REDIS_URL'], socket_connect_timeout=2, socket_timeout=2); print({'redis_url': os.environ['REDIS_URL'].split('@')[-1], 'redis_ping': client.ping()})"
    exit $LASTEXITCODE
}

& ".\.venv\Scripts\python.exe" -m uvicorn backend.app.main:app --host $HostAddress --port $Port
