param(
    [int]$WorkerCount = 4,
    [int]$StaggerSeconds = 90,
    [string]$RunName = "full83_4proc_stagger90_20260814"
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repo ".venv\Scripts\python.exe"
$manifestPath = Join-Path $repo "tests\question\redesign\manifest.json"
$resultRoot = Join-Path $repo "tests\question\eval_results\$RunName"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python executable not found: $python"
}
if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "Question manifest not found: $manifestPath"
}

$manifest = @(Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json)
$ids = @($manifest | ForEach-Object { $_.id })
if ($ids.Count -ne 83) {
    throw "Expected 83 manifest ids, got $($ids.Count)"
}

New-Item -ItemType Directory -Force -Path $resultRoot | Out-Null
$launchManifestPath = Join-Path $resultRoot "launch_manifest.json"
$launches = @()

for ($worker = 1; $worker -le $WorkerCount; $worker++) {
    $workerDirName = "worker$worker"
    $workerDir = Join-Path $resultRoot $workerDirName
    New-Item -ItemType Directory -Force -Path $workerDir | Out-Null
    $stdoutPath = Join-Path $workerDir "process.stdout.log"
    $stderrPath = Join-Path $workerDir "process.stderr.log"
    $relativeOutDir = "tests/question/eval_results/$RunName/$workerDirName"
    $argumentList = @(
        "-m", "tests.question.eval_runner",
        "--ids"
    ) + $ids + @(
        "--question-dir", "tests/question/redesign",
        "--out-dir", $relativeOutDir
    )

    $startedAt = Get-Date
    $process = Start-Process `
        -FilePath $python `
        -ArgumentList $argumentList `
        -WorkingDirectory $repo `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -WindowStyle Hidden `
        -PassThru

    $launches += [pscustomobject]@{
        worker = $worker
        pid = $process.Id
        started_at = $startedAt.ToString("o")
        expected_question_count = $ids.Count
        output_dir = $workerDir
        stdout = $stdoutPath
        stderr = $stderrPath
    }
    $launches | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $launchManifestPath -Encoding UTF8

    if ($worker -lt $WorkerCount) {
        Start-Sleep -Seconds $StaggerSeconds
    }
}

$launches | ConvertTo-Json -Depth 4
