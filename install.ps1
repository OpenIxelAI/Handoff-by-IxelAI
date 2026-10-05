# Installs Handoff into its own virtualenv and puts a `handoff` command on PATH.
#   From a checkout:  powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = 'Stop'

$RepoUrl = if ($env:HANDOFF_REPO_URL) { $env:HANDOFF_REPO_URL } else { 'https://github.com/OpenIxelAI/Handoff-by-IxelAI.git' }
$Branch = if ($env:HANDOFF_BRANCH) { $env:HANDOFF_BRANCH } else { 'main' }
$InstallRoot = if ($env:HANDOFF_INSTALL_ROOT) { $env:HANDOFF_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA 'Handoff' }
$BinDir = if ($env:HANDOFF_BIN_DIR) { $env:HANDOFF_BIN_DIR } else { Join-Path $HOME '.local\bin' }
$RepoDir = Join-Path $InstallRoot 'repo'
$VenvDir = Join-Path $InstallRoot '.venv'
$HandoffExe = Join-Path $VenvDir 'Scripts\handoff.exe'
# The `handoff` command is a real .exe where it can be: Claude Code and Codex start the plugin's MCP server
# without a shell, and without one Windows only finds programs ending in .exe (the plugin runs `handoff` by
# name). With Smart App Control on, Windows blocks that unsigned .exe, so the command is handoff.cmd, which
# runs the environment's signed python.exe; `handoff setup --write` connects the apps the same way.
$BinExe = Join-Path $BinDir 'handoff.exe'
$BinCmd = Join-Path $BinDir 'handoff.cmd'

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name. $Hint"
    }
}

# PowerShell doesn't stop on a failing native command, so check every exit code. Exit
# codes are the signal: a warning pip or git writes to stderr isn't a failure, though
# Windows PowerShell 5.1 can turn one into a terminating error under 'Stop'.
function Invoke-Checked([string]$What, [scriptblock]$Command) {
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = -1  # stays -1 if the program can't be started at all (Continue only prints that)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

# A command is an array: the program, then its arguments (e.g. 'py', '-3.13').
# Always splat an array: splatting a lone string passes nothing sensible.
function Split-Command([string[]]$Command) {
    return $Command[0], @($Command | Select-Object -Skip 1)
}

function Test-Python([string[]]$Command) {
    $exe, $rest = Split-Command $Command
    try {
        # 3.10+ and a final release: libraries Handoff needs can break on alphas and release candidates.
        # No double quotes in arguments: Windows PowerShell 5.1 drops them on the way to the program.
        & $exe @rest -c 'import sys; v = sys.version_info; sys.exit(0 if v >= (3, 10) and v.releaselevel == ''final'' else 1)' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Get-PythonVersion([string[]]$Command) {
    $exe, $rest = Split-Command $Command
    try {
        $version = & $exe @rest -c 'import sys; print(sys.version.split()[0])' 2>$null
        if ($LASTEXITCODE -eq 0 -and $version) { return "$version".Trim() }
    } catch { }
    return $null
}

function Get-PythonId([string[]]$Command) {
    # Which Python this is: its version and where it's installed (for a venv's python, its base's)
    $exe, $rest = Split-Command $Command
    try {
        $id = & $exe @rest -c 'import os, sys; print(sys.version, os.path.realpath(sys.base_prefix))' 2>$null
        if ($LASTEXITCODE -eq 0 -and $id) { return "$id".Trim() }
    } catch { }
    return ''
}

function Get-PythonCandidates {
    # The py launcher's default first, then each version it knows (newest first), then
    # whatever is on PATH, then python.org's install folders: "Add to PATH" is off by
    # default in its installer, so a working Python is often on disk but not on PATH.
    $candidates = @(@('py', '-3'), @('py', '-3.14'), @('py', '-3.13'), @('py', '-3.12'), @('py', '-3.11'),
                    @('py', '-3.10'), @('python'), @('python3'))
    foreach ($dir in @("$env:LOCALAPPDATA\Programs\Python", $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $dir -or -not (Test-Path $dir)) { continue }
        $installs = Get-ChildItem -Path $dir -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
            Sort-Object { [int]('0' + ($_.Name -replace '\D', '')) } -Descending
        foreach ($install in $installs) {
            $exe = Join-Path $install.FullName 'python.exe'
            if (Test-Path $exe) { $candidates += ,@($exe) }
        }
    }
    return $candidates
}

# What each command turned out to be, for the message when none will do.
$script:PythonNotes = @()

function Find-Python {
    $script:PythonNotes = @()
    foreach ($candidate in Get-PythonCandidates) {
        $found = Get-Command $candidate[0] -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        if (Test-Python $candidate) { return ,$candidate }
        if ($candidate[0] -eq 'py' -and $candidate[1] -ne '-3') { continue }  # one line for the launcher
        $name = $candidate -join ' '
        $version = Get-PythonVersion $candidate
        if ($version) {
            $script:PythonNotes += "$name is Python $version (too old, or a pre-release)"
        } elseif ("$($found.Source)" -like '*\WindowsApps\*') {
            $script:PythonNotes += "$name is only the Microsoft Store shortcut, not an installed Python"
        } else {
            $script:PythonNotes += "$name didn't run"
        }
    }
    return $null
}

function Update-SessionPath {
    # Pick up the PATH an installer just changed, without opening a new window
    if ($env:OS -ne 'Windows_NT') { return }
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user, $env:Path) | Where-Object { $_ }) -join ';'
}

function Get-PythonCommand {
    if ($env:HANDOFF_PYTHON) {
        if (Test-Python @($env:HANDOFF_PYTHON)) { return ,@($env:HANDOFF_PYTHON) }
        throw ("HANDOFF_PYTHON=$($env:HANDOFF_PYTHON) isn't usable (too old, a pre-release, or it doesn't run). " +
               "Handoff needs a final release of Python 3.10 or newer.")
    }
    $python = Find-Python
    if ($python) { return ,$python }

    Write-Host "Handoff needs Python 3.10 or newer, and this computer doesn't have one yet."
    foreach ($note in $script:PythonNotes) { Write-Host "  - $note" }
    $canAsk = [Environment]::UserInteractive -and -not $env:CI
    if ($canAsk -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        $answer = Read-Host "Install Python 3.13 now with winget (Microsoft's installer)? [Y/n]"
        if ($answer -notmatch '^\s*[Nn]') {
            # Its exit code isn't a verdict ("already installed" is non-zero), so just look again.
            # Out-Host: anything a program prints inside a function becomes its return value.
            & winget install --id Python.Python.3.13 --exact --source winget --accept-package-agreements --accept-source-agreements |
                Out-Host
            Update-SessionPath
            $python = Find-Python
            if ($python) { return ,$python }
            Write-Host "Python was installed, but this window can't see it yet."
        }
    }
    throw ("Install Python with:  winget install Python.Python.3.13   (or from https://www.python.org/downloads/)," +
           " then open a new PowerShell window and run this again.")
}

function Ensure-UserPath([string]$Dir) {
    if ($env:HANDOFF_SKIP_PATH_UPDATE -eq '1') { return }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($userPath) { $parts = $userPath.Split(';') | Where-Object { $_ } }
    if ($parts -contains $Dir) { return }
    $newPath = if ($userPath) { "$userPath;$Dir" } else { $Dir }
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
}

function Test-SmartAppControl {
    # 1 = on (it blocks), 2 = evaluation (it doesn't block yet), 0 = off
    try {
        $state = Get-ItemPropertyValue -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' `
            -Name 'VerifiedAndReputablePolicyState' -ErrorAction Stop
        return $state -eq 1
    } catch {
        return $false
    }
}

function Get-Commit([string]$Dir) {
    # The commit a checkout is at; '' if it isn't one, or git isn't installed
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return '' }
    $ErrorActionPreference = 'Continue'  # as in Invoke-Checked: the exit code is the signal
    try {
        $head = & git -C $Dir rev-parse HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and $head) { return "$head".Trim() }
    } catch { }
    return ''
}

$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
# An environment from the last install that works is kept, with its own Python (below), so updating it needs
# no other Python here. One is looked for (or installed) only when there's no such environment, or you chose one.
if (-not $env:HANDOFF_PYTHON -and (Test-Path $VenvPython) -and (Test-Python @($VenvPython))) {
    $Python = @($VenvPython)
} else {
    $Python = Get-PythonCommand
}
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

# Install the checkout this script lives in, if it is one; otherwise clone.
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { '' }
if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir 'pyproject.toml')) -and (Test-Path (Join-Path $ScriptDir 'handoff'))) {
    $SourceDir = $ScriptDir
} else {
    Require-Command git 'Install it with:  winget install Git.Git'
    if (Test-Path (Join-Path $RepoDir '.git')) {
        Invoke-Checked 'git fetch' { git -C $RepoDir fetch origin }
        Invoke-Checked 'git checkout' { git -C $RepoDir checkout $Branch }
        Invoke-Checked 'git pull' { git -C $RepoDir pull --ff-only origin $Branch }
    } else {
        if (Test-Path $RepoDir) { Remove-Item -Recurse -Force $RepoDir }
        Invoke-Checked 'git clone' { git clone --branch $Branch $RepoUrl $RepoDir }
    }
    $SourceDir = $RepoDir
}

$PyExe, $PyArgs = Split-Command $Python
# Keep an environment that works: Claude Code or Codex may be running Handoff from it right now, and
# Windows won't let a running python.exe be replaced, so making it again would fail
$VenvWorks = (Test-Path $VenvPython) -and (Test-Python @($VenvPython))
# Unless you chose another Python with HANDOFF_PYTHON: then it's made again with that one. It's moved aside
# first, which Windows refuses (changing nothing) while an app runs Handoff from it, and it's deleted only once
# the new one works: if making it fails (no network, say), the old one is put back.
$OldVenv = ''
if ($VenvWorks -and $env:HANDOFF_PYTHON -and (Get-PythonId @($VenvPython)) -ne (Get-PythonId $Python)) {
    Write-Host "Making a new environment with $($env:HANDOFF_PYTHON) (the last install used another Python)"
    $Aside = $VenvDir + '.old-' + [DateTime]::Now.Ticks
    try {
        Move-Item -Path $VenvDir -Destination $Aside -ErrorAction Stop
    } catch {
        throw ("Couldn't replace the environment at $VenvDir, probably because an app is running Handoff from " +
               "it. Close Claude Desktop, Claude Code and Codex, then run this again.")
    }
    $OldVenv = $Aside
    $VenvWorks = $false
}
if ($VenvWorks) {
    Write-Host "Using $(& $VenvPython --version 2>&1), in the environment from the last install"
} else {
    Write-Host "Using $(& $PyExe @PyArgs --version 2>&1)"
}
$Works = $false
try {
    if ($env:HANDOFF_USE_UV -ne '0' -and (Get-Command uv -ErrorAction SilentlyContinue)) {
        if (-not $VenvWorks) {
            $BasePython = & $PyExe @PyArgs -c 'import sys; print(sys.executable)'
            Invoke-Checked 'uv venv' { uv venv --quiet --allow-existing --python $BasePython $VenvDir }
        }
        Invoke-Checked 'uv pip install' { uv pip install --quiet --python $VenvPython --upgrade $SourceDir }
    } else {
        if (-not $VenvWorks) {
            Invoke-Checked 'Creating the virtual environment' { & $PyExe @PyArgs -m venv $VenvDir }
        }
        Invoke-Checked 'pip upgrade' { & $VenvPython -m pip install --quiet --upgrade pip }
        Invoke-Checked 'pip install' { & $VenvPython -m pip install --quiet --upgrade $SourceDir }
    }
    # Import everything the apps use, not just the entry point: a broken dependency
    # should fail the install, not the first handoff. Its own last lines say why.
    $ErrorActionPreference = 'Continue'  # as in Invoke-Checked: the exit code is the signal
    $ImportError = & $VenvPython -c 'import handoff.cli, handoff.board, handoff.mcp_server' 2>&1
    $Loaded = $LASTEXITCODE -eq 0
    $ErrorActionPreference = 'Stop'
    if (-not $Loaded) {
        $Why = @($ImportError | ForEach-Object { "$_" } | Select-Object -Last 3) -join "`n  "
        throw ("Handoff didn't finish installing: its libraries don't load on $(& $PyExe @PyArgs --version 2>&1).`n" +
               "  $Why`n  Try another Python:  `$env:HANDOFF_PYTHON = 'C:\path\to\python.exe'; " +
               "powershell -ExecutionPolicy Bypass -File '$(Join-Path $SourceDir 'install.ps1')'")
    }
    $Works = $true
} finally {
    # Runs on an error and on Ctrl+C too
    if ($OldVenv -and $Works) {
        Remove-Item -Recurse -Force $OldVenv -ErrorAction SilentlyContinue
    } elseif ($OldVenv) {
        Remove-Item -Recurse -Force $VenvDir -ErrorAction SilentlyContinue
        Move-Item -Path $OldVenv -Destination $VenvDir -ErrorAction SilentlyContinue
        Write-Host "Put the last install's environment back, so Handoff works as it did before."
    }
}

# A copy an app is running (as its MCP server) can't be overwritten or deleted, but it can be renamed away.
if (Test-Path $BinExe) {
    $Stale = Join-Path $BinDir ("handoff.exe.old-" + [DateTime]::Now.Ticks)
    Move-Item -Force $BinExe $Stale
}
Get-ChildItem -Path $BinDir -Filter 'handoff.exe.old-*' -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item -Force $_.FullName -ErrorAction SilentlyContinue }
# HANDOFF_LAUNCHER=cmd or exe chooses; otherwise it's handoff.cmd only where Smart App Control is on
$UseCmd = if ($env:HANDOFF_LAUNCHER) { $env:HANDOFF_LAUNCHER -eq 'cmd' } else { Test-SmartAppControl }
if ($UseCmd) {
    # -I: nothing is imported from the folder you run handoff in. ASCII (cmd.exe reads it in the console's
    # code page): under %LOCALAPPDATA%, as by default, the path names that variable and cmd fills it in, so
    # an accented letter in a user folder's name survives.
    $WrapperPython = $VenvPython
    if ($env:LOCALAPPDATA -and $VenvPython.StartsWith($env:LOCALAPPDATA + '\', [StringComparison]::OrdinalIgnoreCase)) {
        $WrapperPython = '%LOCALAPPDATA%' + $VenvPython.Substring($env:LOCALAPPDATA.Length)
    }
    Set-Content -Path $BinCmd -Encoding ASCII -Value "@echo off`r`n`"$WrapperPython`" -I -m handoff %*"
    $BinCommand = $BinCmd
} else {
    # pip's launcher holds the full path to the venv's Python, so a copy of it works from anywhere
    Copy-Item -Force $HandoffExe $BinExe
    if (Test-Path $BinCmd) { Remove-Item -Force $BinCmd }
    $BinCommand = $BinExe
}

Ensure-UserPath $BinDir

# Remember where this install came from, for `handoff update`, and the commit it installed. Last, so an
# install that fails keeps the last one's record, and `handoff update` knows to run this again. The Python
# you chose with HANDOFF_PYTHON is kept too, so `handoff update` uses it again. No double quotes: Windows
# PowerShell 5.1 drops them on the way to the program. It drops an empty argument too, so the last two are
# commit=... and python=..., never empty.
$Commit = Get-Commit $SourceDir
$ChosenPython = ''
if ($env:HANDOFF_PYTHON) { $ChosenPython = "$(& $PyExe @PyArgs -c 'import sys; print(sys.executable)')".Trim() }
Invoke-Checked 'Recording the install' {
    & $VenvPython -c 'import json, sys; extra = dict(a.split(chr(61), 1) for a in sys.argv[5:]); json.dump(dict(source=sys.argv[2], install_root=sys.argv[3], bin_dir=sys.argv[4], installer=''install.ps1'', commit=extra.get(''commit'', ''''), **(dict(python=extra[''python'']) if extra.get(''python'') else dict())), open(sys.argv[1], ''w'', encoding=''utf-8''))' (Join-Path $InstallRoot 'install.json') $SourceDir $InstallRoot $BinDir ('commit=' + $Commit) ('python=' + $ChosenPython)
}

Write-Host ""
Write-Host "Handoff installed from $SourceDir"
Write-Host "Command: $BinCommand"
if ($UseCmd) {
    Write-Host "Smart App Control is on, so apps can't start the plugin's handoff.exe. Connect them with:"
    Write-Host "         handoff setup --write   (it points them at this install's signed python.exe)"
}
Write-Host "Next:    cd into a project, then run: handoff setup --write"
Write-Host "Update:  handoff update"
Write-Host "Remove:  handoff setup --remove, then: Remove-Item -Recurse -Force '$InstallRoot', '$BinCommand'"
Write-Host "If the command is not found in this shell yet, restart PowerShell or run:"
Write-Host "  `$env:Path = '$BinDir;' + `$env:Path"
