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


def _gui_package_lock_version() -> str:
    """gui package-lock.json 的根 version（npm 由 package.json 生成，须同步）。

    issue #1065：package-lock.json 是发布约定的第 8 个版本源（根 version +
    packages[\"\"] 两处），此前无守卫，bump 漏改不会报错。文件头部字段序为
    name → version → lockfileVersion，首个 \"version\" 匹配即根版本。
    """
    content = (REPO_ROOT / "emrg" / "gui" / "package-lock.json").read_text(encoding="utf-8")
    m = re.search(r'"version"\s*:\s*"([^"]+)"', content)
    assert m, "emrg/gui/package-lock.json 中找不到根 version"
    return m.group(1)


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
    """全部 7 处版本声明必须与 emrg.__version__ 一致。"""
    base = _base_version()
    assert _pyproject_version() == base, "pyproject.toml 版本与 __version__ 不一致"
    assert _gui_package_version() == base, (
        "emrg/gui/package.json 版本与 __version__ 不一致（#408 教训：bump 勿漏 GUI 版本）"
    )
    assert _gui_package_lock_version() == base, (
        "emrg/gui/package-lock.json 根版本与 __version__ 不一致（issue #1065：bump 勿漏 lock 文件）"
    )
    assert _uv_lock_version() == base, "uv.lock 版本与 __version__ 不一致"
    for rel, ver in _shell_fallback_versions():
        assert ver == base, f"{rel} fallback 版本与 __version__ 不一致"


def test_base_version_is_semver():
    """基准版本必须是合法 semver（x.y.z）。"""
    base = _base_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", base), f"非法版本号格式: {base!r}"
