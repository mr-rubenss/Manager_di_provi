param(
    [Parameter(Mandatory)]
    [ValidateSet('Status','Restart','Update','Rollback','ConfigureAdmins','Profile')]
    [string]$Action,
    [string]$Backup,
    [string]$AdminConfigFile
)

$ErrorActionPreference = 'Stop'
$root = Join-Path $env:ProgramData 'Manager_di_provi'
$task = 'Manager_di_provi'
$log = Join-Path $root 'logs\bot.log'
$marker = Join-Path $root 'cutover.enabled'
$config = Join-Path $root 'config.json'
$backupRoot = Join-Path $root 'backups'
$source = Split-Path -Parent $PSScriptRoot
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open Windows PowerShell as Administrator.'
}

function Get-BotState {
    (Get-ScheduledTask -TaskName $task).State
}

function Confirm-SingleHost {
    $api = 'https://api.github.com/repos/mr-rubenss/Manager_di_provi/actions'
    foreach ($file in @('poll.yml','daily.yml')) {
        $workflow = Invoke-RestMethod -Uri "$api/workflows/$file" -TimeoutSec 12
        if ($workflow.state -notlike 'disabled*') { throw "GitHub workflow is active: $file" }
        $runs = Invoke-RestMethod -Uri "$api/workflows/$file/runs?per_page=10" -TimeoutSec 12
        if ($runs.workflow_runs | Where-Object status -NE 'completed') {
            throw "The GitHub workflow still has a run: $file"
        }
    }
}

function Wait-Stopped {
    Remove-Item -LiteralPath $marker -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(80)
    while ((Get-BotState) -in @('Running','Queued')) {
        if ((Get-Date) -ge $deadline) {
            New-Item -ItemType File -Path $marker -Force | Out-Null
            throw 'The task did not complete its current processing. Files were left in place.'
        }
        Start-Sleep -Seconds 2
    }
    Disable-ScheduledTask -TaskName $task | Out-Null
}

function Start-Verified {
    $from = Get-Date
    New-Item -ItemType File -Path $marker -Force | Out-Null
    Enable-ScheduledTask -TaskName $task | Out-Null
    Start-ScheduledTask -TaskName $task
    $deadline = $from.AddSeconds(70)
    do {
        Start-Sleep -Seconds 2
        $recent = @(Get-Content -LiteralPath $log -Encoding UTF8 -Tail 60 | Where-Object {
            $_ -match '^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d' -and
            [datetime]::ParseExact($_.Substring(0,19),'yyyy-MM-dd HH:mm:ss',[Globalization.CultureInfo]::InvariantCulture) -ge $from.AddSeconds(-1)
        })
        if ($recent -match ' ERROR ') { throw 'The bot reported a startup error. Inspect bot.log.' }
        if ((Get-BotState) -eq 'Running' -and ($recent -match 'POLLING_STARTED') -and
            ($recent -match 'google_sheet duration_ms=')) {
            'WINDOWS_RUNNING'
            return
        }
    } while ((Get-Date) -lt $deadline)
    throw 'Did not verify poll and sheet access before the startup deadline.'
}

function Save-Deployment([string]$label) {
    $folder = Join-Path $backupRoot ($label + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    New-Item -ItemType Directory -Path $folder | Out-Null
    foreach ($item in @('app','server.py','data')) {
        Copy-Item -LiteralPath (Join-Path $root $item) -Destination $folder -Recurse
    }
    foreach ($name in @('state.json','subscribers.json','schedule_snapshot.json')) {
        $live = Join-Path (Join-Path $root 'data') $name
        $copy = Join-Path (Join-Path $folder 'data') $name
        if ((Get-FileHash -LiteralPath $live).Hash -ne (Get-FileHash -LiteralPath $copy).Hash) {
            throw "Backup validation failed: $name"
        }
    }
    $folder
}

function Restore-Code([string]$folder) {
    Copy-Item -LiteralPath (Join-Path $folder 'server.py') -Destination (Join-Path $root 'server.py') -Force
    foreach ($item in @('bot.py','broadcast_message.txt')) {
        Copy-Item -LiteralPath (Join-Path $folder "app\$item") -Destination (Join-Path $root "app\$item") -Force
    }
}

function Read-AdminList {
    if ($AdminConfigFile) {
        $encrypted = Get-Content -LiteralPath $AdminConfigFile -Raw
        $secret = ConvertTo-SecureString $encrypted.Trim()
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
        try {
            $ids = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Trim()
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
            $secret.Dispose()
        }
    } else {
        $ids = (Read-Host 'ADMIN_TELEGRAM_IDS (positive IDs separated by commas; blank disables)').Trim()
    }
    if ($ids -and $ids -notmatch '^[1-9]\d*(\s*,\s*[1-9]\d*)*$') { throw 'Invalid Telegram ID list.' }
    $ids
}

function Config-WithAdmins([string]$old, [string]$ids) {
    $settings = $old | ConvertFrom-Json
    if ($settings.PSObject.Properties['ADMIN_TELEGRAM_IDS']) { $settings.ADMIN_TELEGRAM_IDS = $ids }
    else { $settings | Add-Member NoteProperty ADMIN_TELEGRAM_IDS $ids }
    $settings | ConvertTo-Json
}

function Save-JsonConfig([string]$content) {
    $temporary = Join-Path $root ('config-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary,$content,[Text.UTF8Encoding]::new($false))
        [IO.File]::Replace($temporary,$config,[System.Management.Automation.Language.NullString]::Value)
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}

switch ($Action) {
    'Status' {
        Get-ScheduledTask -TaskName $task | Select-Object TaskName,State
        Get-ScheduledTaskInfo -TaskName $task | Select-Object LastRunTime,LastTaskResult
        Get-Content -LiteralPath $log -Encoding UTF8 -Tail 15
    }
    'Restart' {
        Confirm-SingleHost
        Wait-Stopped
        Start-Verified
    }
    'Update' {
        Confirm-SingleHost
        if ((Get-BotState) -ne 'Running') { throw 'Expected the previous version to be running.' }
        foreach ($file in @('bot.py','broadcast_message.txt','windows\server.py')) {
            if (-not (Test-Path -LiteralPath (Join-Path $source $file))) { throw "Source file missing: $file" }
        }
        $original = $null
        if ($AdminConfigFile) {
            $original = [IO.File]::ReadAllText($config)
            $configured = Config-WithAdmins $original (Read-AdminList)
        }
        Wait-Stopped
        $saved = $null
        $configWritten = $false
        try {
            $saved = Save-Deployment 'before-update'
            "BACKUP $saved"
            foreach ($item in @('bot.py','broadcast_message.txt')) {
                Copy-Item -LiteralPath (Join-Path $source $item) -Destination (Join-Path $root "app\$item") -Force
            }
            Copy-Item -LiteralPath (Join-Path $source 'windows\server.py') -Destination (Join-Path $root 'server.py') -Force
            if ($original) {
                Save-JsonConfig $configured
                $configWritten = $true
            }
            Start-Verified
        } catch {
            $initialFailure = $_
            if ($saved) {
                Wait-Stopped
                $failed = Save-Deployment 'failed-update'
                "FAILED_VERSION_BACKUP $failed"
                Restore-Code $saved
                if ($configWritten) { Save-JsonConfig $original }
            }
            Start-Verified
            throw $initialFailure
        }
    }
    'Rollback' {
        Confirm-SingleHost
        if (-not $Backup) {
            $selected = Get-ChildItem -LiteralPath $backupRoot -Directory -Filter 'before-update-*' |
                Sort-Object Name -Descending | Select-Object -First 1
            if (-not $selected) { throw 'No previous version was found.' }
            $Backup = $selected.FullName
        }
        if (-not (Test-Path -LiteralPath (Join-Path $Backup 'app\bot.py')) -or
            -not (Test-Path -LiteralPath (Join-Path $Backup 'server.py'))) { throw 'Backup has no application code.' }
        Wait-Stopped
        $current = Save-Deployment 'before-rollback'
        "CURRENT_VERSION_BACKUP $current"
        try {
            Restore-Code $Backup
            Start-Verified
        } catch {
            Wait-Stopped
            Restore-Code $current
            Start-Verified
            throw
        }
    }
    'ConfigureAdmins' {
        $ids = Read-AdminList
        Confirm-SingleHost
        $old = [IO.File]::ReadAllText($config)
        $configured = Config-WithAdmins $old $ids
        Wait-Stopped
        $configWritten = $false
        try {
            Save-JsonConfig $configured
            $configWritten = $true
            Start-Verified
        } catch {
            Wait-Stopped
            if ($configWritten) { Save-JsonConfig $old }
            Start-Verified
            throw
        }
    }
    'Profile' {
        & (Join-Path $root 'python\python.exe') -I -B (Join-Path $root 'server.py') profile
        if ($LASTEXITCODE) { exit $LASTEXITCODE }
    }
}
