# Project command entrypoint for Windows. Mirrors ./dev; invoked as
# pwsh -File dev.ps1 <setup|test|lint|build|node>.
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("setup", "test", "lint", "build", "node")]
    [string]$Command,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# 本仓库的工具链输出中文（检查脚本的提示、pytest 的用例名、错误信息），而 Windows 上
# Python 的 stdout 在管道下用遗留代码页（实测 cp1252），打印中文直接 UnicodeEncodeError。
#
# 这不是显示问题，是崩溃：RAY-258 首次在 windows-latest 上运行 dev.ps1 时，分层红线检查
# 炸在它自己的**成功**消息上 —— 也就是说仓库干净时也会失败。该检查自 RAY-192 合并起在
# Windows 上就完全不可用，只是没有 Windows CI 所以无人知道。
#
# 在入口设 PYTHONUTF8 而不是把消息改成英文：根因是平台默认编码，不是消息的语言；改语言
# 只挡得住我们自己的脚本，挡不住 pytest 与 ruff 的输出。
$env:PYTHONUTF8 = "1"

function Invoke-Step {
    param([string]$Exe, [string[]]$StepArgs)
    & $Exe @StepArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# 与 ./dev 的判定逻辑一一对应，理由见那里：uv 自己解析 .python-version，而裸
# pnpm 用 PATH 上第一个 node，fnm exec 是 Node 侧的等价物。只有当 fnm 解析出的
# 版本确实等于 .node-version 时才走它；否则回退 PATH，由 preflight 守版本。
$script:NodeViaFnm = $false
if (Get-Command fnm -ErrorAction SilentlyContinue) {
    $expected = "v" + (Get-Content -Path ".node-version" -Raw).Trim()
    $actual = & fnm exec -- node --version 2>$null
    if ($LASTEXITCODE -eq 0 -and $actual -eq $expected) {
        $script:NodeViaFnm = $true
    }
}

function Invoke-Node {
    param([string]$Exe, [string[]]$StepArgs)
    if ($script:NodeViaFnm) {
        Invoke-Step "fnm" (@("exec", "--", $Exe) + $StepArgs)
    }
    else {
        Invoke-Step $Exe $StepArgs
    }
}

switch ($Command) {
    "setup" {
        Invoke-Step "uv" @("sync", "--locked")
        Invoke-Node "pnpm" @("install", "--frozen-lockfile")
    }
    "test" {
        Invoke-Step "uv" (@("run", "--locked", "python", "-m", "pytest") + $Rest)
        Invoke-Node "pnpm" @("run", "test")
    }
    "lint" {
        Invoke-Step "uv" (@("run", "--locked", "python", "-m", "ruff", "check", ".") + $Rest)
        # 分层红线，与 ./dev 对应。
        Invoke-Step "uv" @("run", "--locked", "python", "tools/check_layering.py")
        Invoke-Step "uv" @("run", "--locked", "python", "tools/check_quality_single_source.py")
        Invoke-Step "uv" @("run", "--locked", "python", "tools/check_calibration_channel.py")
        Invoke-Node "pnpm" @("run", "lint")
    }
    "build" {
        Invoke-Step "uv" (@("build") + $Rest)
        Invoke-Node "pnpm" @("run", "build")
    }
    "node" {
        # 逃生舱，与 ./dev node 对应：在本入口解析出的 Node 环境里跑任意命令。
        if (-not $Rest -or $Rest.Count -eq 0) {
            Write-Error "usage: pwsh -File dev.ps1 node <cmd> [args...]"
            exit 2
        }
        # 不能写成 $Rest[1..($Rest.Count - 1)]：只有一个元素时它等于 $Rest[1..0]，
        # 而 PowerShell 的 1..0 是反向区间 @(1,0)，取出的是越界项加第 0 项。
        $nodeArgs = @()
        if ($Rest.Count -gt 1) { $nodeArgs = $Rest[1..($Rest.Count - 1)] }
        Invoke-Node $Rest[0] $nodeArgs
    }
}
