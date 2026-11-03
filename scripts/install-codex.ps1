<#
.SYNOPSIS
    Mirror the canonical wb-exec skill into the places that load it.

.DESCRIPTION
    This repo is the single source of truth for the wb-exec skill. Three copies
    of it exist on a working machine -- this repo, the DSH skills directory and
    the Codex skills directory -- and they have drifted before: a permission fix
    and an --add-dir fix both reached one copy and not the others, which is how a
    "fixed" bug keeps happening. This script copies the repo version outward and
    then compares hashes, so drift is either prevented or reported, never assumed
    away.

    Never copies __pycache__ or *.pyc. That is not tidiness: running the test
    suite inside an install directory regenerates them, and they once grew an npm
    package from 66 KB to 456 KB.

.PARAMETER Check
    Report differences and exit non-zero if there are any. Changes nothing.

.PARAMETER Target
    Skill directory to write. Repeatable. Defaults to the DSH and Codex skill
    directories under the user profile.

.PARAMETER NoPrune
    Keep files in the target that no longer exist in the source. Off by default,
    because a file deleted upstream is exactly the kind of drift this guards.

.EXAMPLE
    pwsh scripts/install-codex.ps1 -Check

.EXAMPLE
    pwsh scripts/install-codex.ps1
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [string[]]$Target,
    [switch]$NoPrune
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repoRoot 'skills/wb-exec'

if (-not (Test-Path $source)) {
    throw "canonical skill not found: $source"
}

if (-not $Target -or $Target.Count -eq 0) {
    $Target = @(
        (Join-Path $env:USERPROFILE '.agents/skills/wb-exec'),
        (Join-Path $env:USERPROFILE '.codex/skills/wb-exec')
    )
}

# Not part of a runtime skill: npm packaging metadata.
$excludedNames = @('.npmignore')

# Tool caches. Never synced: they are regenerated locally, they differ per
# machine, and __pycache__ once grew the npm package from 66 KB to 456 KB.
$excludedDirs = @('__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.cache')

function Get-SkillFiles {
    param([string]$Root)

    $map = [ordered]@{}
    if (-not (Test-Path $Root)) { return $map }
    Get-ChildItem $Root -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($Root.Length).TrimStart('\', '/')
        if ($excludedNames -contains $_.Name) { return }
        if ($_.Extension -eq '.pyc') { return }
        $segments = $rel -split '[\\/]'
        foreach ($seg in $segments) {
            if ($excludedDirs -contains $seg) { return }
        }
        $map[$rel.Replace('\', '/')] = $_.FullName
    }
    return $map
}

function Get-FileHashOf {
    param([string]$Path)
    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash
}

$srcFiles = Get-SkillFiles -Root $source
Write-Host "source: $source ($($srcFiles.Count) files)"

$drift = 0
$copied = 0
$pruned = 0

foreach ($t in $Target) {
    $target = [System.IO.Path]::GetFullPath($t)
    Write-Host ""
    Write-Host "target: $target"

    if (-not (Test-Path $target)) {
        if ($Check) {
            Write-Host "  MISSING (would be created)" -ForegroundColor Yellow
            $drift++
            continue
        }
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        Write-Host "  created"
    }

    $dstFiles = Get-SkillFiles -Root $target

    # Copy or compare, file by file.
    foreach ($rel in $srcFiles.Keys) {
        $from = $srcFiles[$rel]
        $to = Join-Path $target ($rel -replace '/', '\')

        if ($dstFiles.Contains($rel)) {
            if ((Get-FileHashOf $from) -eq (Get-FileHashOf $to)) {
                $dstFiles.Remove($rel)   # identical; nothing to prune
                continue
            }
            if ($Check) {
                Write-Host "  DIFFERS  $rel" -ForegroundColor Yellow
                $drift++
                $dstFiles.Remove($rel)
                continue
            }
        } elseif ($Check) {
            Write-Host "  MISSING  $rel" -ForegroundColor Yellow
            $drift++
            continue
        }

        $parent = Split-Path -Parent $to
        if (-not (Test-Path $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Copy-Item -LiteralPath $from -Destination $to -Force
        $copied++
        if ($dstFiles.Contains($rel)) { $dstFiles.Remove($rel) }
    }

    # Anything left in the target does not exist upstream.
    foreach ($rel in $dstFiles.Keys) {
        if ($NoPrune) { continue }
        $path = $dstFiles[$rel]
        # Only ever delete inside the target we are already writing to.
        if (-not $path.StartsWith($target + '\')) { continue }
        if ($Check) {
            Write-Host "  EXTRA    $rel" -ForegroundColor Yellow
            $drift++
            continue
        }
        Remove-Item -LiteralPath $path -Force
        Write-Host "  pruned   $rel" -ForegroundColor DarkGray
        $pruned++
    }
}

Write-Host ""
if ($Check) {
    if ($drift -gt 0) {
        Write-Host "drift: $drift difference(s). Run without -Check to sync." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "in sync: every target matches the canonical source." -ForegroundColor Green
    exit 0
}

Write-Host "copied $copied file(s), pruned $pruned." -ForegroundColor Green

# Prove the result rather than trust the copy loop.
$bad = 0
foreach ($t in $Target) {
    $target = [System.IO.Path]::GetFullPath($t)
    $dst = Get-SkillFiles -Root $target
    if ($dst.Count -ne $srcFiles.Count) {
        Write-Host "verify FAILED: $target has $($dst.Count) files, source has $($srcFiles.Count)" -ForegroundColor Red
        $bad++
        continue
    }
    foreach ($rel in $srcFiles.Keys) {
        if (-not $dst.Contains($rel) -or
            (Get-FileHashOf $srcFiles[$rel]) -ne (Get-FileHashOf $dst[$rel])) {
            Write-Host "verify FAILED: $target :: $rel" -ForegroundColor Red
            $bad++
        }
    }
}
if ($bad -gt 0) { exit 1 }
Write-Host "verified: all targets match, file for file." -ForegroundColor Green
exit 0
