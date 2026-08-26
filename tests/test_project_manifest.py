"""治理清单与两个入口脚本之间的耦合。RAY-275。

## 这个文件守的是一条承诺，不是一段代码

`.ai-project/project.yaml` 里的 `node_resolution: "entrypoint"` 是一句**对外的
声明**：它告诉治理 preflight「本项目的入口自己解析 Node，别去查 PATH」。
preflight 于是不再查 PATH，改为执行 `<入口> node node --version` 来问入口。

所以这条声明只有在**两个入口都真的有 `node` 子命令**时才成立。哪天有人把那个
逃生舱删了，闸门不会变成「回答错误」而是「无法回答」—— `./dev` 打印 usage 退出
2，preflight 停住，而错误信息指向 Node 版本，读起来像项目坏了。RAY-275 的成因
正是这一类：一个没人验的前提，被一个巧合掩盖了三周。

## 为什么用正则读 YAML 而不是解析它

本项目的运行时依赖只有 numpy 与 wt901，没有 YAML 解析器，为一个断言引入一个
依赖不划算。preflight 自己读这个键用的也是正则（`^node_resolution:\\s*(.+)$`），
这里刻意与它同形：断言的是「preflight 会读到什么」，不是「YAML 语义上等于
什么」。若哪天 preflight 改了解析方式，这里要跟着改 —— 这一点写在这里，免得
将来有人以为它在做通用的 YAML 校验。
"""

import re
from pathlib import Path

import pytest

#: 与 `tests/test_quality.py` 同一种推导 —— 一个目录里只该有一种
#: 定位仓库根的做法。不经 `gait.__file__`：那要求包是可编辑安装的，而这里
#: 读的是仓库文件，与包安装成什么样无关。
REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / ".ai-project" / "project.yaml"
DEV_SH = REPO_ROOT / "dev"
DEV_PS1 = REPO_ROOT / "dev.ps1"

#: 两个入口都必须提供的子命令。前四个是治理清单里登记的动作，`node` 是
#: preflight 在 entrypoint 模式下用的探针入口。
REQUIRED_SUBCOMMANDS = ("setup", "test", "lint", "build", "node")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_manifest_declares_entrypoint_node_resolution():
    """没有这行，preflight 会去校验一条 ./dev 永远不会用的 PATH（RAY-236）。"""
    match = re.search(r"^node_resolution:\s*(.+)$", read(MANIFEST), re.MULTILINE)
    assert match is not None, (
        "`.ai-project/project.yaml` 缺 node_resolution。缺失时 preflight 默认走 "
        '"path"，会校验 PATH 上的 node —— 而 ./dev 用 fnm 按 .node-version 解析，'
        "根本不看 PATH。本机装了别的 Node 就会让闸门红，而项目本身是好的。"
    )
    assert match.group(1).strip().strip('"') == "entrypoint"


@pytest.mark.parametrize("subcommand", REQUIRED_SUBCOMMANDS)
def test_the_posix_entrypoint_implements_every_required_subcommand(subcommand):
    assert re.search(rf"^\s+{subcommand}\)", read(DEV_SH), re.MULTILINE), (
        f"./dev 缺 `{subcommand}` 分支"
    )


@pytest.mark.parametrize("subcommand", REQUIRED_SUBCOMMANDS)
def test_the_windows_entrypoint_implements_every_required_subcommand(subcommand):
    """dev.ps1 有两处要写到：ValidateSet 与 switch。少写 ValidateSet 的那处，
    参数会在进入 switch 之前就被 PowerShell 拒绝。
    """
    source = read(DEV_PS1)
    validate_set = re.search(r"\[ValidateSet\(([^)]*)\)\]", source)
    assert validate_set is not None, "dev.ps1 的 $Command 缺 ValidateSet"
    assert f'"{subcommand}"' in validate_set.group(1), (
        f"dev.ps1 的 ValidateSet 缺 `{subcommand}`"
    )
    assert re.search(rf'^\s+"{subcommand}"\s*\{{', source, re.MULTILINE), (
        f"dev.ps1 的 switch 缺 `{subcommand}` 分支"
    )


def test_the_node_escape_hatch_is_what_the_declaration_promises():
    """探针执行的是 `<入口> node node --version`：`node` 子命令必须把**剩余参数
    原样**交给入口解析出的 Node 环境，而不是自己拼一条固定命令。

    断言两个入口都把剩余参数转发出去 —— 写死成 `node --version` 也能让探针通过，
    但 `<入口> node pnpm --version` 那一路会静默地跑错东西。
    """
    assert re.search(r'run_node\s+"\$@"', read(DEV_SH)), (
        './dev 的 node 分支必须转发 "$@"'
    )
    assert re.search(r"Invoke-Node\s+\$Rest\[0\]", read(DEV_PS1)), (
        "dev.ps1 的 node 分支必须把 $Rest[0] 当命令、其余当参数"
    )


def test_the_declared_commands_use_the_two_entrypoints():
    """清单里登记的 argv 必须就是这两个入口 —— preflight 的探针前缀取自它们
    （`entrypoint_probe_prefix`），登记别的东西会让探针指向一个没有 node 子命令
    的程序。
    """
    manifest = read(MANIFEST)
    # 用正则而不是精确字符串：后者是在断言 YAML 的排版，重排一次空格就会红，
    # 而那时配置其实仍然是对的 —— 一个因为无关改动而红的守卫，下一个人的第一
    # 反应是关掉它。
    assert re.search(r'\[\s*"\./dev"\s*,\s*"setup"\s*\]', manifest), (
        "清单的 default.setup 必须是 ./dev"
    )
    assert re.search(
        r'\[\s*"pwsh"\s*,\s*"-File"\s*,\s*"dev\.ps1"\s*,\s*"setup"\s*\]', manifest
    ), "清单的 windows.setup 必须是 pwsh -File dev.ps1"
