param(
    [string]$QuestionDir = "tests/question/redesign",
    [string]$OutDir = "tests/question/eval_results/lazy_jd_full_20260812_4p_staggered",
    [int]$StartIntervalSeconds = 60,
    [ValidateSet("live", "record", "replay")]
    [string]$EvidenceMode = "live",
    [string]$FixtureDir = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$questionRoot = (Resolve-Path (Join-Path $repoRoot $QuestionDir)).Path
$outputRoot = Join-Path $repoRoot $OutDir
$fixtureRoot = $null
if ($FixtureDir) {
    $fixtureRoot = Join-Path $repoRoot $FixtureDir
}
if ($EvidenceMode -ne "live" -and -not $fixtureRoot) {
    throw "-FixtureDir is required when -EvidenceMode is $EvidenceMode"
}

function Protect-ProcessArgument([string]$Value) {
    return '"' + $Value.Replace('"', '\\"') + '"'
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python runtime not found: $python"
}
if (Test-Path -LiteralPath $outputRoot) {
    throw "Output directory already exists; choose a new -OutDir to avoid overwriting: $outputRoot"
}

$ids = @(
    Get-ChildItem -LiteralPath $questionRoot -Filter "*.json" -File |
        Where-Object { $_.BaseName -match "^[QRC][0-9]+$" } |
        Sort-Object BaseName |
        ForEach-Object { $_.BaseName }
)
if ($ids.Count -ne 83) {
    throw "Expected 83 question documents, found $($ids.Count) in $questionRoot"
}

# Round-robin assignment keeps the four process workloads approximately even.
$buckets = @(0..3 | ForEach-Object { ,@() })
for ($index = 0; $index -lt $ids.Count; $index++) {
    $bucket = $index % 4
    $buckets[$bucket] += $ids[$index]
}

Write-Host "Question count: $($ids.Count)"
Write-Host "Concurrency: 4 processes"
Write-Host "Start interval: $StartIntervalSeconds seconds"
Write-Host "Evidence mode: $EvidenceMode"
if ($fixtureRoot) { Write-Host "Fixture directory: $fixtureRoot" }
Write-Host "Output: $outputRoot"

if ($DryRun) {
    for ($worker = 0; $worker -lt 4; $worker++) {
        $workerDir = Join-Path $outputRoot ("worker_{0:D2}" -f ($worker + 1))
        $arguments = @(
            "-m", "tests.question.eval_runner",
            "--ids"
        ) + $buckets[$worker] + @(
            "--question-dir", (Protect-ProcessArgument $questionRoot),
            "--out-dir", (Protect-ProcessArgument $workerDir),
            "--evidence-mode", $EvidenceMode
        )
        if ($fixtureRoot) { $arguments += @("--fixture-dir", (Protect-ProcessArgument $fixtureRoot)) }
        Write-Host ("DRY-RUN worker_{0:D2}: {1} ids; start after {2}s" -f ($worker + 1), $buckets[$worker].Count, ($worker * $StartIntervalSeconds))
        Write-Host ("  {0} {1}" -f $python, ($arguments -join " "))
    }
    exit 0
}

New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$processes = @()
for ($worker = 0; $worker -lt 4; $worker++) {
    if ($worker -gt 0) {
        Write-Host "Waiting $StartIntervalSeconds seconds before starting worker $($worker + 1)..."
        Start-Sleep -Seconds $StartIntervalSeconds
    }

    $workerName = "worker_{0:D2}" -f ($worker + 1)
    $workerDir = Join-Path $outputRoot $workerName
    New-Item -ItemType Directory -Path $workerDir -Force | Out-Null
    $stdout = Join-Path $workerDir "stdout.log"
    $stderr = Join-Path $workerDir "stderr.log"
    $arguments = @(
        "-m", "tests.question.eval_runner",
        "--ids"
    ) + $buckets[$worker] + @(
        "--question-dir", (Protect-ProcessArgument $questionRoot),
        "--out-dir", (Protect-ProcessArgument $workerDir),
        "--evidence-mode", $EvidenceMode
    )
    if ($fixtureRoot) { $arguments += @("--fixture-dir", (Protect-ProcessArgument $fixtureRoot)) }
    Write-Host "Starting $workerName with $($buckets[$worker].Count) questions"
    $processes += Start-Process `
        -FilePath $python `
        -ArgumentList $arguments `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru
}

Write-Host "All 4 workers started. Waiting for completion..."
$processes | Wait-Process

$failed = @($processes | Where-Object { $_.ExitCode -ne 0 })
if ($failed.Count -gt 0) {
    $codes = ($failed | ForEach-Object { "$($_.Id):$($_.ExitCode)" }) -join ", "
    throw "Evaluation worker failure(s): $codes"
}
Write-Host "All 4 workers completed successfully. Results: $outputRoot"
