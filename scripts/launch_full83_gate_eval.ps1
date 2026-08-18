param(
    [string]$RunName = "gate83_single_20260817",
    [string]$QuestionDir = "tests/question/redesign"
)

# Single-process full-83 eval launcher (mirrors the v3 baseline runs:
# full83_autorecovery_v3_20260816 / refactor_83_v3_20260816_single).
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repo ".venv\Scripts\python.exe"
$manifestPath = Join-Path $repo "tests\question\redesign\manifest.json"
$resultRoot = Join-Path $repo "tests\question\eval_results\$RunName"

if (-not (Test-Path -LiteralPath $python)) { throw "Python executable not found: $python" }
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "Question manifest not found: $manifestPath" }
if (Test-Path -LiteralPath $resultRoot) { throw "Output directory already exists: $resultRoot" }

$manifest = @(Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json)
$ids = @($manifest | ForEach-Object { $_.id })
if ($ids.Count -ne 83) { throw "Expected 83 manifest ids, got $($ids.Count)" }

New-Item -ItemType Directory -Force -Path $resultRoot | Out-Null
$stdoutPath = Join-Path $resultRoot "process.stdout.log"
$stderrPath = Join-Path $resultRoot "process.stderr.log"
$relativeOutDir = "tests/question/eval_results/$RunName"

$argumentList = @(
    "-m", "tests.question.eval_runner",
    "--ids"
) + $ids + @(
    "--question-dir", $QuestionDir,
    "--out-dir", $relativeOutDir
)

$startedAt = Get-Date
$process = Start-Process -FilePath $python -ArgumentList $argumentList -WorkingDirectory $repo -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru

$launch = [ordered]@{
    run_name = $RunName
    worker_mode = "single"
    count = 1
    ids = ($ids -join " ")
    started_at = $startedAt.ToString("o")
    pid = $process.Id
}
$launch | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $resultRoot "launch_manifest.json") -Encoding UTF8

Write-Host "Launched $RunName with $($ids.Count) questions, PID $($process.Id)"
Write-Host "Output: $resultRoot"
