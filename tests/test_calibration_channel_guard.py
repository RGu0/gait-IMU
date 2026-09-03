"""wt901 标定通道红线的测试。

这里最重要的不是「真实的 `src/` 目前干净」—— 那是现状，随时会变，而且一个永远返回
空列表的检查也能让它通过。重要的是**有违规时会被抓到**，且各种触碰写法一条都不漏。
所以每条都在临时目录里造出真实的违规文件，让检查去扫。

上游 RAY-294 是一条自某次改动起就在到达断言**之前**报错的测试 —— 看起来还在、实际
早已失效。本文件因此额外钉住三件容易悄悄坏掉的事：

* 扫描根真的存在且非空（扫一个空目录也会「通过」）；
* `tests/` 确实在扫描范围内（本文件自己就在里面）；
* 往**真实的生产文件**里加一处调用，检查必须变红（变异验证）。

本文件不需要豁免，尽管它满篇都是违规写法：那些写法是**字符串字面量**，是喂给
`ast.parse` 的数据，而检查读的是 AST。字符串里的 `device.calibration` 不是属性访问
节点，只是一个 `str`。第一版真的加过一条豁免，验证时才发现它什么都没豁免掉 ——
`test_this_file_needs_no_exemption` 把这个结论钉住，免得有人再加回来。
"""

from pathlib import Path

import check_calibration_channel
import pytest

REPO_ROOT = Path(check_calibration_channel.__file__).resolve().parent.parent


def write(root: Path, source: str, name: str = "probe.py") -> Path:
    """把一份源码落成 `root/pkg/<name>`，返回扫描用的根。"""
    package = root / "pkg"
    package.mkdir(parents=True, exist_ok=True)
    (package / name).write_text(source, encoding="utf-8")
    return package


VIOLATIONS = [
    pytest.param("import wt901.calibration\n", id="import wt901.calibration"),
    pytest.param(
        "import wt901.calibration as cal\n", id="import ... as 别名（import 本身就是信号）"
    ),
    pytest.param(
        "from wt901.calibration import Calibration\n", id="from wt901.calibration import X"
    ),
    pytest.param("from wt901 import calibration\n", id="from wt901 import calibration"),
    pytest.param(
        "from wt901.protocol.registers import CalibrationMode\n", id="CalibrationMode"
    ),
    pytest.param(
        "async def f(device):\n    await device.calibration.calibrate_acceleration()\n",
        id="device.calibration.calibrate_acceleration()",
    ),
    pytest.param(
        "async def f(device):\n    async with device.calibration.field_calibration():\n        pass\n",
        id="磁场校准（同样写 CALSW）",
    ),
    pytest.param(
        "def f(registers, mode):\n    registers.write(Register.CALSW, mode)\n",
        id="直接写 Register.CALSW",
    ),
    pytest.param(
        "def f(device):\n    channel = device.calibration\n    return channel\n",
        id="先取出通道再用（把调用拆成两步）",
    ),
    pytest.param(
        "def f(device):\n    return getattr(device, 'calibration')\n",
        id="getattr 取通道",
        marks=pytest.mark.xfail(
            reason="字符串形式的属性访问 ast 看不出来；这是本检查已知的边界",
            strict=True,
        ),
    ),
]


@pytest.mark.parametrize("source", VIOLATIONS)
def test_every_spelling_of_a_violation_is_caught(tmp_path, source):
    root = write(tmp_path, source)
    reported = check_calibration_channel.scan(root)
    assert reported, f"未能抓到违规写法：{source!r}"
    assert "标定通道" in reported[0]


CLEAN = [
    pytest.param("from wt901 import scan\n", id="wt901 的其它 API 不受影响"),
    pytest.param(
        "from wt901.protocol.units import accel_to_m_s2\n", id="单位换算（本项目正当用法）"
    ),
    pytest.param(
        "from gait.calib.still import CalibrationError, CalibrationVerdict\n",
        id="本仓库自己的 CalibrationError / CalibrationVerdict",
    ),
    pytest.param(
        "from gait.calib.accel import AccelCalibration\n", id="本仓库自己的 AccelCalibration"
    ),
    pytest.param(
        "from gait.calib.walk import MountingCalibration\n",
        id="本仓库自己的 MountingCalibration",
    ),
    pytest.param(
        "def f(binding):\n    return binding.calibration_id\n",
        id="calibration_id（另一个属性名，精确匹配不命中）",
    ),
    pytest.param(
        "def f(meta):\n    return meta.calib_snapshot\n", id="calib_snapshot（本项目命名）"
    ),
    pytest.param(
        "from gait.calib import calibrate_still\n", id="calibrate_still（会话标定，本项目的）"
    ),
    pytest.param(
        "from wt901.protocol.registers import Register\n",
        id="Register 本身正当 —— 禁的是 .CALSW 那一个，不是整张寄存器表",
    ),
]


@pytest.mark.parametrize("source", CLEAN)
def test_legitimate_code_is_not_flagged(tmp_path, source):
    root = write(tmp_path, source)
    assert check_calibration_channel.scan(root) == []


def test_the_repository_is_currently_clean():
    """现状检查。它单独一条是不够的 —— 上面那组才是这个检查的理由。"""
    for name in check_calibration_channel.SCANNED_ROOTS:
        root = REPO_ROOT / name
        assert check_calibration_channel.scan(root) == [], f"{name}/ 触碰了 wt901 标定通道"


def test_scanned_roots_exist_and_contain_python():
    """扫一个空目录也会「通过」。这条钉住扫描根真的有东西可扫。"""
    for name in check_calibration_channel.SCANNED_ROOTS:
        root = REPO_ROOT / name
        assert root.is_dir(), f"扫描根 {name}/ 不存在"
        assert any(root.rglob("*.py")), f"扫描根 {name}/ 里没有 .py，检查会空转"


def test_this_file_needs_no_exemption():
    """本文件满篇违规写法，却干净 —— 因为它们是字符串，不是代码。

    这条钉住「不需要豁免名单」这个结论。没有它，下一个人看到一个装满
    `device.calibration` 的测试文件躺在被扫描的 `tests/` 里，第一反应会是加一条豁免，
    而那条豁免会是空操作，并给后来者一个往里加第二条的口子。
    """
    assert not hasattr(check_calibration_channel, "EXEMPT_FILES"), (
        "豁免名单又回来了；先确认它真的豁免掉了什么，再决定要不要留"
    )
    reported = check_calibration_channel.scan(REPO_ROOT / "tests")
    assert not any(Path(__file__).name in line for line in reported)


def test_mutation_a_real_call_in_a_real_production_file_turns_it_red(tmp_path):
    """变异验证：往**真实的生产文件**里加一处真实调用，检查必须变红。

    用真实文件而不是新造一个空文件，是为了同时验证「这个文件本来就在扫描范围内」——
    一个扫不到该文件的检查，加多少调用都不会红。
    """
    target = REPO_ROOT / "src" / "gait" / "device" / "adapter.py"
    original = target.read_text(encoding="utf-8")

    package = tmp_path / "src" / "gait" / "device"
    package.mkdir(parents=True)
    clean = package / "adapter.py"
    clean.write_text(original, encoding="utf-8")
    assert check_calibration_channel.scan(tmp_path / "src") == [], (
        "未变异的真实文件就已经被判违规，变异验证无从谈起"
    )

    clean.write_text(
        original + "\n\nasync def _mutation(device):\n"
        "    await device.calibration.calibrate_acceleration()\n",
        encoding="utf-8",
    )
    reported = check_calibration_channel.scan(tmp_path / "src")
    assert reported, "加了一处真实调用，红线却没有变红 —— 这道闸什么都没守"
    assert "adapter.py" in reported[0]
