"""Guard against the recurring README/Agent.md test-count drift.

Pattern history: #426 -> #430 -> #510 -> #511. Every time tests are added or
removed, the documented counts drift and require a follow-up doc PR. This
module asserts the documented Python count matches the real collection, and
that the documented GUI breakdown sums to its headline number.

#584: README.cn.md was the only test-count doc NOT guarded — it drifted to
91 (22 renderer smoke) while README.md/Agent.md said 96 (27 renderer smoke)
after #580 added 3 GUI tests. Both checks now cover all three docs
(README.md, README.cn.md, Agent.md); CJK full-width parens and the
"项：" separator are normalized before matching.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _collected_pytest_count() -> int:
    """Run pytest in collect-only mode and parse the total."""
    out = subprocess.check_output(
        [__import__("sys").executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(REPO_ROOT),
        text=True,
        stderr=subprocess.STDOUT,
    )
    m = re.search(r"(\d+) tests? collected", out)
    assert m, f"could not parse collected count from pytest output:\n{out[-2000:]}"
    return int(m.group(1))


def _gui_breakdowns() -> list[tuple[str, int, list[int]]]:
    """Extract (label, headline, parts) for every documented GUI count."""
    found = []
    for doc in ("README.md", "README.cn.md", "Agent.md"):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "npm test" not in line:
                continue
            # CJK docs use full-width parens and "（N 项：..." instead of "(N: ..."
            line = line.replace("（", "(").replace("）", ")")
            line = re.sub(r"(\d+) 项：", r"\1: ", line)
            m = re.search(r"\((\d+): ([^)]+)\)", line)
            if not m:
                continue
            headline = int(m.group(1))
            # each breakdown part starts with its count ("22 daemon_client + ...");
            # take the first number per part (avoids false digits inside names like i18n)
            parts = [
                int(re.match(r"\s*(\d+)", part).group(1))
                for part in m.group(2).split("+")
                if re.match(r"\s*\d+", part)
            ]
            found.append((f"{doc}: {line.strip()[:70]}", headline, parts))
    return found


def test_python_count_matches_docs() -> None:
    collected = _collected_pytest_count()
    for doc in ("README.md", "README.cn.md", "Agent.md"):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        # README: "run tests (currently N items)" | README.cn: "（当前 N 项）"
        # Agent.md: "pytest tests/ -v` (N)"
        m = re.search(r"currently (\d+) items", text) or re.search(
            r"当前 (\d+) 项", text
        ) or re.search(
            r"uv run pytest tests/ -v` \((\d+)\)", text
        )
        assert m, f"no documented Python count found in {doc}"
        documented = int(m.group(1))
        assert documented == collected, (
            f"{doc} documents {documented} Python tests but {collected} are collected "
            f"(--collect-only). Sync the doc (and this guard) when adding/removing tests."
        )


def test_gui_breakdown_sums_to_headline() -> None:
    breakdowns = _gui_breakdowns()
    assert breakdowns, "no GUI test breakdowns found in README.md/Agent.md"
    for label, headline, parts in breakdowns:
        assert sum(parts) == headline, (
            f"{label}: breakdown {parts} sums to {sum(parts)} but headline says {headline}"
        )
