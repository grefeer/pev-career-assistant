param(
    [Parameter(Mandatory = $true)]
    [string]$OutDir,
    [int]$PollSeconds = 180,
    [int]$SuccessTarget = 65,
    [int]$NonSuccessLimit = 30,
    [int]$ExpectedCount = 0,
    [string]$EvalPids = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$outputRoot = (Resolve-Path (Join-Path $repoRoot $OutDir)).Path

function Get-EvalSnapshot {
    param([Parameter(Mandatory = $true)][string]$Root)
    $jsonFiles = @(Get-ChildItem -LiteralPath $Root -Recurse -Filter "*.json" -File)
    $rawRecords = @()
    foreach ($jsonFile in $jsonFiles) {
        try {
            $rawRecords += (Get-Content -LiteralPath $jsonFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json)
        } catch {
            # A result is written atomically only after the question finishes.
        }
    }
    $recordsById = @{}
    foreach ($record in $rawRecords) {
        if ($null -eq $record.id -or $null -eq $record.result -or $null -eq $record.result.status) {
            continue
        }
        # A chain is one top-level case; links are diagnostic children only.
        # A chain wrapper is one manifest-level case; only its diagnostic
        # links are excluded from the top-level count.
        if ([string]$record.id -match "-L\d+$") {
            continue
        }
        $caseId = [string]$record.id
        if (-not $recordsById.ContainsKey($caseId)) {
            $recordsById[$caseId] = $record
        }
    }
    $records = @(
        $recordsById.GetEnumerator() | ForEach-Object { $_.Value }
    )
    $statuses = @($records | ForEach-Object {
        if ($_.result.status -in @("succeeded", "failed", "waiting_user")) {
            $_.result.status
        } else {
            "unknown"
        }
    })
    $nonSuccess = @($statuses | Where-Object {
        $_ -in @("failed", "waiting_user", "unknown")
    }).Count
    [pscustomobject]@{
        Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Completed = $records.Count
        Succeeded = @($statuses | Where-Object { $_ -eq "succeeded" }).Count
        WaitingUser = @($statuses | Where-Object { $_ -eq "waiting_user" }).Count
        Failed = @($statuses | Where-Object { $_ -eq "failed" }).Count
        Unknown = @($statuses | Where-Object { $_ -eq "unknown" }).Count
        NonSuccess = $nonSuccess
    }
}

function Get-EvalProcesses {
    if (-not [string]::IsNullOrWhiteSpace($EvalPids)) {
        $pidList = @($EvalPids -split "," | ForEach-Object { [int]$_.Trim() })
        return @(Get-Process -Id $pidList -ErrorAction SilentlyContinue)
    }
    $processes = @()
    try {
        $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        # Windows PowerShell installations may not have CimCmdlets available.
        # The WMI provider exposes the same ProcessId/CommandLine fields here.
        $processes = @(Get-WmiObject Win32_Process -ErrorAction Stop)
    }
    @($processes | Where-Object {
        $_.CommandLine -match "tests[.]question[.]eval_runner" -and
        $_.CommandLine -like "*$outputRoot*"
    })
}

function Stop-EvalProcesses {
    $processes = Get-EvalProcesses
    foreach ($process in $processes) {
        $processId = if ($null -ne $process.Id) { $process.Id } else { $process.ProcessId }
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    return $processes.Count
}

while ($true) {
    $snapshot = Get-EvalSnapshot -Root $outputRoot
    $snapshot | Format-List
    $shouldStop =
        $snapshot.Succeeded -ge $SuccessTarget -or
        $snapshot.NonSuccess -gt $NonSuccessLimit -or
        ($ExpectedCount -gt 0 -and $snapshot.Completed -ge $ExpectedCount)
    $processes = Get-EvalProcesses
    if ($shouldStop) {
        $stopped = Stop-EvalProcesses
        Write-Host "STOP condition reached; stopped $stopped evaluation process(es)."
        break
    }
    if ($processes.Count -eq 0) {
        Write-Host "Evaluation processes are complete."
        break
    }
    Start-Sleep -Seconds $PollSeconds
}
