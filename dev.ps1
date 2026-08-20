# Project command entrypoint for Windows. Mirrors ./dev; invoked as
# pwsh -File dev.ps1 <setup|test|lint|build>.
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("setup", "test", "lint", "build")]
    [string]$Command,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Invoke-Step {
    param([string]$Exe, [string[]]$StepArgs)
    & $Exe @StepArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

switch ($Command) {
    "setup" {
        Invoke-Step "uv" @("sync", "--locked")
        Invoke-Step "pnpm" @("install", "--frozen-lockfile")
    }
    "test" {
        Invoke-Step "uv" (@("run", "--locked", "python", "-m", "pytest") + $Rest)
        Invoke-Step "pnpm" @("run", "test")
    }
    "lint" {
        Invoke-Step "uv" (@("run", "--locked", "python", "-m", "ruff", "check", ".") + $Rest)
        Invoke-Step "pnpm" @("run", "lint")
    }
    "build" {
        Invoke-Step "uv" (@("build") + $Rest)
        Invoke-Step "pnpm" @("run", "build")
    }
}
