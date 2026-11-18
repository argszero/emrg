"""The renderer typechecks without Node types, so a Node dependency is a CI-only failure.

Measured 2026-11-18 (cycle `cyc20261118-121836`): PR #2962's `test` leg failed with

    src/lib/cockpit.test.ts(1,16): error TS2591: Cannot find name 'node:fs'
    src/lib/cockpit.test.ts(251,32): error TS2591: Cannot find name 'process'

while `npm run typecheck` answered `rc 0` in the same tree on this host. The difference is
not the file: `emrg/gui/renderer/tsconfig.json` declares

    "types": ["vite/client", "@testing-library/jest-dom"]

so `@types/node` is not a type library of the renderer at all — but `emrg/gui/node_modules`
hoists it (electron, fs-extra and friends depend on it), and TypeScript resolves the module
`node:fs` from any reachable `@types` package, globals included. A runner that runs
`npm ci` inside `emrg/gui/renderer` has no such neighbour, so the same file compiles here
and fails there.

**A reading that passes only because a sibling package hoists a dependency is not a reading
of the file.** The cure is to stop needing Node in a browser-targeted test (the stylesheet
scan reads through Vite's own glob), and this guard is what keeps the dependency from
arriving again: it reads the tsconfig rather than restating it, so if the renderer ever
declares Node types on purpose the rule follows that declaration instead of contradicting it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDERER = REPO_ROOT / "emrg" / "gui" / "renderer"

#: `import fs from "node:fs"`, `require("node:path")`, `await import("node:os")`.
NODE_BUILTIN = re.compile(
    r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*)["']node:([A-Za-z0-9_/.-]+)["']"""
)

#: The Node globals `@types/node` declares. Named one by one: a bare `process` match would
#: also hit a string in a fixture, and a guard that reds on prose is a guard someone weakens.
NODE_GLOBAL = re.compile(
    r"\bprocess\.(?:env|cwd|argv|platform|exit|stdout|stderr|pid|versions)\b"
)


def _compiler_options(root: Path) -> dict:
    cfg = json.loads((root / "tsconfig.json").read_text(encoding="utf-8"))
    return cfg.get("compilerOptions") or {}


def _type_libraries(root: Path) -> list[str]:
    """The tsconfig's own `types` list — the subject, not a copy of it."""
    return list(_compiler_options(root).get("types") or [])


def _compiled_files(root: Path) -> list[Path]:
    """Every file `npm run typecheck` compiles, discovered from the tsconfig's `include`."""
    cfg = json.loads((root / "tsconfig.json").read_text(encoding="utf-8"))
    found: list[Path] = []
    for entry in cfg.get("include") or []:
        target = root / entry
        if target.is_dir():
            found.extend(sorted(target.rglob("*.ts")))
            found.extend(sorted(target.rglob("*.tsx")))
        elif target.exists():
            found.append(target)
    return found


def _node_dependencies(root: Path) -> list[str]:
    """Sites that name a Node built-in or a Node global, while the typecheck has no Node types."""
    if "node" in _type_libraries(root):
        return []
    hits: list[str] = []
    for path in _compiled_files(root):
        text = path.read_text(encoding="utf-8")
        for pattern, label in ((NODE_BUILTIN, None), (NODE_GLOBAL, True)):
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                what = match.group(0) if label else f"node:{match.group(1)}"
                hits.append(f"{path.relative_to(root)}:{line}: {what}")
    return hits


def test_no_renderer_file_depends_on_types_the_typecheck_does_not_declare() -> None:
    """The tree leg: the renderer compiles without Node types, and names none of them."""
    files = _compiled_files(RENDERER)
    # 名字先数出来：一个空扫描会让下面那句 vacuously 通过
    assert len(files) > 20, f"the scan found {len(files)} compiled file(s) — no subject to read"

    hits = _node_dependencies(RENDERER)
    assert hits == [], (
        "the renderer typechecks with types="
        f"{_type_libraries(RENDERER)} and has no @types/node of its own, so these fail\n"
        "`npm run typecheck` on a clean `npm ci` even though they pass on a host that\n"
        "hoists it — read the files through Vite (`import.meta.glob(..., { query: '?raw' })`)\n"
        "or declare `node` in the renderer's tsconfig types:\n  " + "\n  ".join(hits)
    )


def test_the_reading_flags_a_planted_dependency_and_stops_once_node_types_are_declared(
    tmp_path: Path,
) -> None:
    """Both directions of the predicate, driven on a tree the test builds."""
    (tmp_path / "src").mkdir()
    tsconfig = tmp_path / "tsconfig.json"
    tsconfig.write_text(
        json.dumps({"compilerOptions": {"types": ["vite/client"]}, "include": ["src"]}),
        encoding="utf-8",
    )
    (tmp_path / "src" / "a.test.ts").write_text(
        'import fs from "node:fs";\nconst here = process.cwd();\nexport const f = fs;\n',
        encoding="utf-8",
    )

    hits = _node_dependencies(tmp_path)
    assert len(hits) == 2, hits
    assert "node:fs" in hits[0] and "process.cwd" in hits[1]

    # 声明了 node 类型库之后，同一份文件不再算作越界依赖 —— 这条不是豁免，是配对
    tsconfig.write_text(
        json.dumps(
            {"compilerOptions": {"types": ["vite/client", "node"]}, "include": ["src"]}
        ),
        encoding="utf-8",
    )
    assert _node_dependencies(tmp_path) == []
