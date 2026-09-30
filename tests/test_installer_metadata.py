"""Windows 安装器元数据（rant 2026-09-30T11:43:44 → issue 本轮）。

`emrg.iss` 的 `[Setup]` 段此前**零元数据**：全仓 grep `VersionInfo` 零命中，
于是 exe 的「属性 → 详细信息」里公司/产品/说明全空，产物看起来是个匿名自解压包。
实测后果有两面：它是 Defender 判 `Trojan:Win32/Sabsik.FL.A!ml` 并**直接删除**下载
文件的诱因之一（WDSI 申诉 f9954caf-739b-48b5-adaa-c5490df9bb92，2026-09-30 读到
答复「The detection has been removed」），也是 SignPath Foundation 对签名产物的
硬性要求（其条款列明签名产物须有强制元数据）。

本文件钉两件事，且**两件不同**：

1. 模板里有那四行，且版本值来自构建期的 `$VERSION`（`{#MyAppVersion}`），不是手写死值
   —— 手写死值会在下一次 bump 后静默说谎。
2. 存在一条**读产物**的检查：`.github/workflows/test.yml` 的 Inno 冒烟步骤在 `iscc`
   之后读刚编译出的 exe 的版本资源并断言四个字段非空。rant 明确要求不得只断言
   `.iss` 文本（「那只能证明写了指令，不能证明编译进产物」），所以这里同时断言
   **检查存在**、**它断言的是产物字段**、且它排在 `iscc` **之后**。

另钉 README（中英各一份）覆盖「包被 Defender 删除」的处置，与现有 SmartScreen
提示区分开 —— 那是「提示可点保留」，这是「文件已被删，没有可点的按钮」。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKE_INSTALLER = REPO_ROOT / "packaging" / "make-installer.sh"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"

#: 四个字段 → Inno 指令名。`VersionInfoVersion` 是 Inno 唯一要求 x.y.z.w 数字形式的
#: 一个，所以它必须由 `$VERSION` 补一位得到，而不能是裸 `$VERSION`。
FIELDS = {
    "CompanyName": "VersionInfoCompany",
    "ProductName": "VersionInfoProductName",
    "FileDescription": "VersionInfoDescription",
    "FileVersion": "VersionInfoVersion",
}


def _iss_template() -> str:
    """The Inno script as `make-installer.sh` writes it (macro text, not rendered)."""
    return MAKE_INSTALLER.read_text(encoding="utf-8")


def _workflow() -> str:
    return TEST_WORKFLOW.read_text(encoding="utf-8")


def _inno_smoke_step() -> str:
    """The `run:` body of the step that renders and compiles `emrg.iss`.

    Parsed rather than sliced between two markers: that step is the **last** one in its
    job, so a "next `- name:`" boundary does not exist and a slicing helper fails with
    `ValueError` — a guard that breaks when its subject moves to the end of a list.
    """
    import yaml

    doc = yaml.safe_load(_workflow())
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == "Inno Setup script compile smoke test":
                return step["run"]
    raise AssertionError("test.yml 里找不到 Inno 冒烟步骤 —— 该检查被删了？")


# ── 1. 模板：四行都在，版本取自构建期 ────────────────────────────────────────


def test_iss_setup_block_carries_every_publisher_field():
    """每一个字段都必须单独钉住：少一行就是一个空字段，而空字段正是这个 bug 的形状
    （匿名自解压包）。断言整块 `[Setup]` 而不是逐行 `in` 整个文件，避免注释里提到
    指令名也算通过。"""
    iss = _iss_template()
    setup = re.search(r"^\[Setup\]\n(.*?)^\[", iss, re.S | re.M)
    assert setup, "make-installer.sh 里找不到 [Setup] 段"
    block = setup.group(1)
    lines = {
        m.group(1): m.group(2).strip()
        for m in re.finditer(r"^(VersionInfo\w+)=(.*)$", block, re.M)
    }
    for field in FIELDS.values():
        assert field in lines, f"[Setup] 缺 {field}（exe 版本资源会留空）"
        assert lines[field], f"{field} 是空值 —— 空字段就是当初的匿名自解压包形态"


def test_version_fields_derive_from_the_build_not_a_hand_written_number():
    """版本值必须是构建期宏，不得手写死值：写死会在下一次 bump 后与 `$VERSION` 分叉，
    而两处版本号不一致是 `tests/test_version_sync.py` 花大力气消灭的那类缺陷。"""
    iss = _iss_template()
    assert re.search(r"^VersionInfoVersion=\{#MyAppVersion\}", iss, re.M), (
        "VersionInfoVersion 必须由 {#MyAppVersion} 派生（且补足 x.y.z.w 四位）"
    )
    assert re.search(r"^VersionInfoVersion=\{#[^}]+\}\.0$", iss, re.M), (
        "VersionInfoVersion 必须是四位数字形式（Inno 要求 x.y.z.w），当前不是"
    )
    # 不得出现字面版本号：两处/三处版本号正是 test_version_sync 要消灭的分叉
    assert not re.search(r"^VersionInfoVersion=\d", iss, re.M), (
        "VersionInfoVersion 写死了版本号 —— bump-version 不会改它，它会静默说谎"
    )
    assert "VersionInfoCompany=argszero" in iss, (
        "VersionInfoCompany 必须是对外主体（本仓 GitHub 账号 argszero），不是占位符"
    )


# ── 2. 产物级检查存在，且它不是读文本 ───────────────────────────────────────


def _product_check(step: str) -> str:
    """The PowerShell body that reads the compiled exe's version resource.

    Scoped to that block on purpose, and this is not cosmetic: the step also contains
    `test -f "$EXE" || { …; exit 1; }` **after** `iscc`, so an assertion as loose as
    "there is an `exit 1` after iscc" is satisfied by a neighbour and reports a failure
    path that does not exist. Measured with a mutation arm that deleted this block's
    `exit 1`: the unscoped assertion stayed green (A4), which is exactly the class of
    defect these arms exist to find.
    """
    marker = 'powershell -NoProfile -Command "'
    start = step.index(marker) + len(marker)
    rest = step[start:]
    lines = rest.splitlines()
    body = []
    for line in lines:
        if line.strip() == '"':
            break
        body.append(line)
    return "\n".join(body)


def test_ci_asserts_the_metadata_reached_the_compiled_exe():
    """rant 的原话：不要只断言 `.iss` 文本里有那几行。所以守卫必须落在产出上，
    并且必须排在 `iscc` 之后 —— 排在前面的断言根本没机会看到产物。"""
    step = _inno_smoke_step()
    assert "iscc " in step, "这一步不再编译 emrg.iss？检查的对象变了，本测试需重写"
    after = step.split("iscc ", 1)[1]
    assert "powershell" in after, (
        "iscc 之后没有读产物的检查 —— 只断言 .iss 文本正是 rant 禁止的那一种"
    )
    block = _product_check(step)
    assert "VersionInfo" in block, "产物级检查没有读 exe 的版本资源"
    for attr in ("CompanyName", "ProductName", "FileDescription", "FileVersion"):
        assert f"$v.{attr}" in block, (
            f"产物级检查没有断言 {attr} —— 少断言一个字段，空字段就能溜过去"
        )


def test_ci_assertion_can_fail():
    """一个不能失败的检查不是检查：`Write-Host` 之后必须有一条只在该块内成立的失败
    路径，否则缺字段也照样绿。断言范围限定在 PowerShell 块内（见 `_product_check`），
    否则同一个 step 里 `test -f … exit 1` 会让这条守卫恒真。"""
    block = _product_check(_inno_smoke_step())
    assert re.search(r"^\s*exit 1\s*$", block, re.M), (
        "产物级检查没有失败路径 —— 缺字段时它仍然会 exit 0"
    )
    assert re.search(r"missing", block), (
        "失败路径没有说明缺了什么，日志无法定位"
    )


# ── 3. heredoc：`$` 紧贴多字节字符会吃掉一个字节 ─────────────────────────────


def test_no_bare_dollar_touches_a_multibyte_char():
    """写这一条时真的踩到了，所以它留在这里。

    `emrg.iss` 由**不带引号**的 heredoc 渲染，因此 `$NAME` 会展开；而当 `$NAME` 紧跟一个
    多字节字符时，bash 会把那个字符的**首字节**并进变量名。隔离复现（bash，macOS）：

        X=abc; cat <<EOF
        值取自 $X，不手写
        EOF

    产出为 `\\xbc\\x8c`（`，` = EF BC 8C，少了 EF）—— 两处后果同时发生：`$X` 展开成了
    **空串**（变量名成了 `X\\xef`，未定义），且产出的 `.iss` 是**非法 UTF-8**，而 iscc 要
    读这个文件。本次改动的第一版注释正是这个形状（`构建期的 $VERSION，不手写死值`），
    本地渲染 `.iss` 时才暴露；只读文本、只跑 pytest 都看不见它。

    所以模式钉的是**变量名之后**的那个字符（第一版把 `$` 后面的字符当作判据，于是
    `$VERSION，` 从它眼皮底下溜过 —— 变异臂 A6 当场证明了这一点）。`${NAME}，` 是安全的：
    花括号界定了名字，故不在此列。
    """
    src = _iss_template()
    offenders = []
    for lineno, line in enumerate(src.splitlines(), 1):
        for m in re.finditer(r"(?<!\\)\$[A-Za-z_][A-Za-z0-9_]*", line):
            nxt = line[m.end():m.end() + 1]
            if nxt and ord(nxt) > 127:
                offenders.append(f"{lineno}: {line.strip()[:70]}")
    assert not offenders, (
        "`$NAME` 紧跟多字节字符 —— bash 会吞掉它的首字节，值静默变空且 .iss 成为非法 UTF-8：\n"
        + "\n".join(offenders)
    )


# ── 4. README：被删除的处置，与 SmartScreen 提示区分 ────────────────────────
def test_readmes_document_the_deleted_installer_case_in_both_languages():
    """「没有任何效果」对当事人无用：被删除时**没有按钮可点**，而两份 README 此前
    只讲了可点「保留」的 SmartScreen。所以这条要的正是那三条恢复命令与病毒库页。"""
    for rel, needs in (
        ("README.md", "false positive that deletes"),
        ("README.cn.md", "误报把文件删掉"),
    ):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "MpCmdRun.exe -removedefinitions -dynamicsignatures" in text, (
            f"{rel} 没有给出刷新病毒库的第一步"
        )
        assert "MpCmdRun.exe -SignatureUpdate" in text, (
            f"{rel} 没有给出刷新病毒库的第二步"
        )
        assert "defenderupdates" in text, f"{rel} 没有给出最新病毒库链接"
        assert needs in text, f"{rel} 没有点名「文件被删除」这一档"
        # 与 SmartScreen 一段必须并存，不是替换 —— 两档处置不同
        assert "SmartScreen" in text, f"{rel} 丢了原有的 SmartScreen 说明"
