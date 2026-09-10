"""版本一致性检查 —— 防止 #408 类发布事故复发。

背景（PR #408 教训）：bump 版本时遗漏了 emrg/gui/package.json，
发布后发现版本不一致 → 删 release + 删 tag + 重打 + 触发重建，
浪费整个发布流程。本测试从 emrg/__init__.py 取基准版本，校验
全部 7 处版本声明一致（pyproject / gui package.json /
gui package-lock.json / uv.lock / make-installer.sh /
build-runtime.sh / make-run-installer.sh）。

纯逻辑测试（正则解析文本），无平台/网络依赖 —— Windows CI
亦可执行（#406 起纯逻辑测试全平台跑）。
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _base_version() -> str:
    """基准版本：emrg/__init__.py 的 __version__。"""
    content = (REPO_ROOT / "emrg" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    assert m, "emrg/__init__.py 中找不到 __version__"
    return m.group(1)


def _pyproject_version() -> str:
    content = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
    assert m, "pyproject.toml 中找不到 version"
    return m.group(1)


def _gui_package_version() -> str:
    content = (REPO_ROOT / "emrg" / "gui" / "package.json").read_text(encoding="utf-8")
    m = re.search(r'"version"\s*:\s*"([^"]+)"', content)
    assert m, "emrg/gui/package.json 中找不到 version"
    return m.group(1)


def _gui_package_lock_versions() -> list[str]:
    """gui package-lock.json 的**全部** app version 声明（根 version + packages[""]）。

    issue #1065：package-lock.json 是发布约定的第 8 个版本源（根 version +
    packages[\"\"] 两处），此前无守卫，bump 漏改不会报错。

    #1118 review 修正（how2how2how2-arch 实证）：本函数原先只取**首个**
    `"version"` 匹配（根字段），packages[\"\"] 实际无守卫 —— 把该字段单独改回旧值
    后守卫仍然 2 passed，即半漏可通过 CI。现返回两处并逐一比对。

    ⚠️ 不能对全文用 `"version"` 裸匹配：该文件含 300+ 条依赖自身的 version
    （每个 node_modules 包一条）。故按**紧随 `"name": "emrg-gui"` 之后**定位，
    与 scripts/bump-version.py 的锚点一致。
    """
    content = (REPO_ROOT / "emrg" / "gui" / "package-lock.json").read_text(encoding="utf-8")
    versions = re.findall(
        r'"name"\s*:\s*"emrg-gui",\s*\n\s*"version"\s*:\s*"([^"]+)"', content
    )
    assert len(versions) == 2, (
        f"emrg/gui/package-lock.json 中期望 2 处 emrg-gui 版本声明"
        f"（根 version + packages[\"\"]），实际找到 {len(versions)} 处；"
        "文件结构变化请同步本守卫与 scripts/bump-version.py"
    )
    return versions


def _uv_lock_version() -> str:
    content = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    m = re.search(r'name = "emrg"\nversion = "([^"]+)"', content)
    assert m, "uv.lock 中找不到 emrg 自身版本"
    return m.group(1)


def _shell_fallback_versions() -> list[tuple[str, str]]:
    """各 shell 脚本的版本 fallback（打包时无法读取 __version__ 时使用）。"""
    results: list[tuple[str, str]] = []
    for rel in ("packaging/make-installer.sh", "packaging/build-runtime.sh", "packaging/make-run-installer.sh"):
        content = (REPO_ROOT / rel).read_text(encoding="utf-8")
        m = re.search(r'\|\| echo "?(\d+\.\d+\.\d+)"?', content)
        assert m, f"{rel} 中找不到版本 fallback"
        results.append((rel, m.group(1)))
    return results


def test_all_version_sources_consistent():
    """全部版本声明必须与 emrg.__version__ 一致。"""
    base = _base_version()
    assert _pyproject_version() == base, "pyproject.toml 版本与 __version__ 不一致"
    assert _gui_package_version() == base, (
        "emrg/gui/package.json 版本与 __version__ 不一致（#408 教训：bump 勿漏 GUI 版本）"
    )
    for idx, ver in enumerate(_gui_package_lock_versions()):
        where = "根 version" if idx == 0 else 'packages[""]'
        assert ver == base, (
            f"emrg/gui/package-lock.json 的 {where} 是 {ver}，与 {base} 不一致"
            "（issue #1065：bump 勿漏 lock 文件；#1118 review：两处都必须比对）"
        )
    assert _uv_lock_version() == base, "uv.lock 版本与 __version__ 不一致"
    for rel, ver in _shell_fallback_versions():
        assert ver == base, f"{rel} fallback 版本与 __version__ 不一致"


def test_base_version_is_semver():
    """基准版本必须是合法 semver（x.y.z）。"""
    base = _base_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", base), f"非法版本号格式: {base!r}"


def test_package_lock_check_covers_both_fields(monkeypatch, tmp_path):
    """守卫必须真的覆盖 packages[""]，而不只是根 version。

    #1118 review（how2how2how2-arch 实证）：本文件原先用 `re.search` 取
    **首个** `"version"` 匹配，只校验根字段。把 packages[""] 单独改回旧值后
    守卫仍 2 passed —— 半漏能过 CI。此测试在**临时副本**上模拟该半漏，
    断言新守卫会失败（正向）+ 未改动时通过（负向），确保覆盖缺口不会静默复发。

    使用 tmp_path 副本 + monkeypatch REPO_ROOT：绝不写真实仓库文件。
    """
    base = _base_version()
    src = REPO_ROOT / "emrg" / "gui" / "package-lock.json"
    original = src.read_text(encoding="utf-8")

    # 负向：真实文件两处一致，且数量恰为 2（断言 count 而非假定）
    assert _gui_package_lock_versions() == [base, base]

    # 正向：在副本上只改 packages[""]（第二个匹配），根字段保持正确
    doctored, n = re.subn(
        r'("name"\s*:\s*"emrg-gui",\s*\n\s*"version"\s*:\s*")[^"]+(")',
        lambda m, i=iter(range(2)): m.group(1) + "0.0.1-doctor" + m.group(2)
        if next(i) == 1
        else m.group(0),
        original,
    )
    assert n == 2, f"expected 2 emrg-gui version fields, replaced {n}"

    root = tmp_path / "repo"
    (root / "emrg" / "gui").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "emrg" / "__init__.py", root / "emrg" / "__init__.py")
    (root / "emrg" / "gui" / "package-lock.json").write_text(doctored, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", root)

    half_miss = _gui_package_lock_versions()
    assert half_miss[0] == base and half_miss[1] == "0.0.1-doctor", half_miss
    assert not all(v == base for v in half_miss), (
        '守卫未能检出 packages[""] 半漏 —— 正是 #1118 review 指出的缺口'
    )
