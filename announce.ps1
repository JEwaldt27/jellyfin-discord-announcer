<#
.SYNOPSIS
    Post a Discord announcement to the Jellyfin announcer, from Windows.

.DESCRIPTION
    Runs announce.sh on the homelab box over SSH. The announce endpoint is
    bound to 127.0.0.1 there, so nothing is exposed to the network - SSH
    already provides the encryption and authentication, and the token never
    leaves the box.

    Run with no arguments (or double-click a shortcut to it) for a menu.

.EXAMPLE
    .\announce.ps1 "Jellyfin down ~20 min for system updates"

.EXAMPLE
    .\announce.ps1 -Maintenance 20m -Message "System updates"

.EXAMPLE
    .\announce.ps1 -Done -Message "Back up, libraries rescanned."
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Message,

    [string]$Title,

    # Duration for a maintenance notice, e.g. 20m, 2h, 1d.
    [string]$Maintenance,

    # Post the all-clear for an open maintenance window.
    [switch]$Done,

    # Ask the endpoint whether it is up. Needs no token.
    [switch]$Health,

    [string]$BotHost = "jewaldt@192.168.12.234",

    [string]$ScriptPath = "./jellyfin-discord-bot/announce.sh",

    # Print the command that would run, without running it.
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Wrap a value in bash single quotes. Everything is literal inside them except
# a single quote itself, which has to be closed, escaped, and reopened. This is
# what keeps an apostrophe or an & in a message from being reinterpreted by the
# remote shell.
function ConvertTo-BashArg([string]$Value) {
    "'" + ($Value -replace "'", "'\''") + "'"
}

function Invoke-Announce([string[]]$Arguments) {
    $remote = ($ScriptPath + " " + ($Arguments -join " ")).Trim()

    # Windows PowerShell 5.1 mangles embedded double quotes when it builds the
    # command line for a native exe: a message of  Say "hello" now  reaches the
    # far end as  Say hello now. Rather than fight that escaping, ship the
    # command base64-encoded so the argument ssh receives is quote-free, and
    # let the remote shell decode it. The pipeline's exit status is bash's, so
    # announce.sh's exit code still propagates.
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remote))
    $wrapped = "echo $encoded | base64 -d | bash"

    if ($DryRun) {
        Write-Host "ssh $BotHost " -NoNewline -ForegroundColor DarkGray
        Write-Host $remote -ForegroundColor Cyan
        return 0
    }

    Write-Host "-> $BotHost" -ForegroundColor DarkGray
    # Out-Host, not a bare call: anything a native command writes to the
    # pipeline would otherwise be returned by this function alongside the exit
    # code, and the caller would compare an array against 0.
    & ssh $BotHost $wrapped | Out-Host
    return $LASTEXITCODE
}

function Read-Required([string]$Prompt) {
    while ($true) {
        $value = Read-Host $Prompt
        if ($value -and $value.Trim()) { return $value.Trim() }
        Write-Host "  (required)" -ForegroundColor Yellow
    }
}

function Show-Menu {
    Write-Host ""
    Write-Host "  Jellyfin announcer" -ForegroundColor Cyan
    Write-Host "  ------------------"
    Write-Host "  1  Announcement"
    Write-Host "  2  Start maintenance"
    Write-Host "  3  All clear (maintenance done)"
    Write-Host "  4  Health check"
    Write-Host "  Q  Quit"
    Write-Host ""

    switch ((Read-Host "  Choose").Trim().ToLower()) {
        "1" {
            $text = Read-Required "  Message"
            $heading = Read-Host "  Title (optional, Enter to skip)"
            $parts = @()
            if ($heading -and $heading.Trim()) {
                $parts += "--title"
                $parts += (ConvertTo-BashArg $heading.Trim())
            }
            $parts += (ConvertTo-BashArg $text)
            return Invoke-Announce $parts
        }
        "2" {
            $reason = Read-Required "  Reason (e.g. system updates)"
            # Required on purpose. Falling back to a plain announcement would
            # post something that looks like a maintenance notice without
            # recording the window, and the later all-clear would then fail
            # with "no maintenance in progress". Use /maintenance start in
            # Discord if you genuinely want one with no estimate.
            $duration = Read-Required "  Expected downtime (20m, 2h)"
            return Invoke-Announce @("--maintenance",
                                     (ConvertTo-BashArg $duration),
                                     (ConvertTo-BashArg $reason))
        }
        "3" {
            $note = Read-Host "  Note (optional, Enter to skip)"
            if ($note -and $note.Trim()) {
                return Invoke-Announce @("--done", (ConvertTo-BashArg $note.Trim()))
            }
            return Invoke-Announce @("--done")
        }
        "4" { return Invoke-Announce @("--health") }
        "q" { return 0 }
        default {
            Write-Host "  Not an option." -ForegroundColor Yellow
            return Show-Menu
        }
    }
}

$interactive = -not ($Message -or $Done -or $Health -or $Maintenance)

if ($interactive) {
    $code = Show-Menu
} elseif ($Health) {
    $code = Invoke-Announce @("--health")
} elseif ($Done) {
    # Not $args - that is an automatic variable in PowerShell.
    $parts = @("--done")
    if ($Message) { $parts += (ConvertTo-BashArg $Message) }
    $code = Invoke-Announce $parts
} elseif ($Maintenance) {
    if (-not $Message) { throw "-Maintenance needs -Message giving the reason." }
    $code = Invoke-Announce @("--maintenance",
                              (ConvertTo-BashArg $Maintenance),
                              (ConvertTo-BashArg $Message))
} else {
    $parts = @()
    if ($Title) { $parts += "--title"; $parts += (ConvertTo-BashArg $Title) }
    $parts += (ConvertTo-BashArg $Message)
    $code = Invoke-Announce $parts
}

if ($code -ne 0) {
    Write-Host ""
    Write-Host "  Failed (exit $code)." -ForegroundColor Red
}

# A shortcut opens its own window; without this it would vanish before the
# result could be read.
if ($interactive -or $code -ne 0) {
    Write-Host ""
    Read-Host "  Press Enter to close" | Out-Null
}

exit $code
