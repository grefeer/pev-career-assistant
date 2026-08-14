param(
    [int]$WorkerCount = 4,
    [string]$SourceResultRoot = "tests/question/eval_results/full83_4proc_stagger90_20260814",
    [string]$RunName = "full83_remaining78_4proc_20260814"
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repo ".venv\Scripts\python.exe"
$manifestPath = Join-Path $repo "tests\question\redesign\manifest.json"
$sourceRootPath = Join-Path $repo $SourceResultRoot
$resultRoot = Join-Path $repo "tests\question\eval_results\$RunName"

if (-not (Test-Path -LiteralPath $python)) { throw "Python executable not found: $python" }
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "Question manifest not found: $manifestPath" }
if (-not (Test-Path -LiteralPath $sourceRootPath)) { throw "Source result root not found: $sourceRootPath" }

$manifest = @(Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json)
$allIds = @($manifest | ForEach-Object { [string]$_.id })
$completed = [System.Collections.Generic.HashSet[string]]::new()

Get-ChildItem -LiteralPath $sourceRootPath -Recurse -Filter '*.json' -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notin @('launch_manifest.json', 'partition_manifest.json') } |
    ForEach-Object {
        try {
            $record = Get-Content -Raw -LiteralPath $_.FullName | ConvertFrom-Json
            if ($record.id) { [void]$completed.Add([string]$record.id) }
        } catch {
            # Ignore non-result or incomplete JSON files; they are not completed results.
        }
    }

$pending = @($allIds | Where-Object { -not $completed.Contains($_) })
if ($pending.Count -eq 0) { throw "No pending questions remain." }

New-Item -ItemType Directory -Force -Path $resultRoot | Out-Null
$partitions = @{}
for ($i = 0; $i -lt $WorkerCount; $i++) { $partitions[$i + 1] = @() }
for ($i = 0; $i -lt $pending.Count; $i++) {
    $worker = ($i % $WorkerCount) + 1
    $partitions[$worker] += $pending[$i]
}

$partitionManifest = @()
$processManifest = @()
for ($worker = 1; $worker -le $WorkerCount; $worker++) {
    $workerDirName = "worker$worker"
    $workerDir = Join-Path $resultRoot $workerDirName
    New-Item -ItemType Directory -Force -Path $workerDir | Out-Null
    $workerIds = @($partitions[$worker])
    $relativeOutDir = "tests/question/eval_results/$RunName/$workerDirName"
    $argumentList = @("-m", "tests.question.eval_runner", "--ids") + $workerIds + @(
        "--question-dir", "tests/question/redesign",
        "--out-dir", $relativeOutDir
    )
    $stdoutPath = Join-Path $workerDir "process.stdout.log"
    $stderrPath = Join-Path $workerDir "process.stderr.log"
    $process = Start-Process `
        -FilePath $python `
        -ArgumentList $argumentList `
        -WorkingDirectory $repo `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -WindowStyle Hidden `
        -PassThru

    $partitionManifest += [pscustomobject]@{
        worker = $worker
        count = $workerIds.Count
        ids = $workerIds
        output_dir = $workerDir
    }
    $processManifest += [pscustomobject]@{
        worker = $worker
        pid = $process.Id
        count = $workerIds.Count
        output_dir = $workerDir
    }
}

[pscustomobject]@{
    source_result_root = $sourceRootPath
    excluded_completed_ids = @($completed | Sort-Object)
    excluded_count = $completed.Count
    pending_count = $pending.Count
    worker_count = $WorkerCount
    partitions = $partitionManifest
    processes = $processManifest
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $resultRoot "partition_manifest.json") -Encoding UTF8

Get-Content -Raw -LiteralPath (Join-Path $resultRoot "partition_manifest.json")
