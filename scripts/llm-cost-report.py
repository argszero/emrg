#!/usr/bin/env python3
"""Estimate LLM API spend from session llm.jsonl usage records.

Comparable-tool inspiration: Claude Code v2.1.247 added `/claude-api
cost-optimize` (API cost profiling). EMRG already persists real token usage
per request — emrg/session.py ``append_llm`` writes every exchange to
``llm.jsonl`` with ``type: request`` (carrying ``model``) followed by
``type: response`` (carrying the ``usage`` dict: prompt_tokens /
completion_tokens / reasoning_tokens / cache_hit_tokens). The missing piece
was only an aggregator + pricing table, which this script provides.

Behavior:

  * Scans session directories (default: every session in
    ``~/.emrg/sessions_index.json``; ``--root`` for one ``.emrg/sessions``
    dir; ``--session-id`` for a single indexed session).
  * Reads ``llm.jsonl`` plus rotated ``.N`` backups in chronological order
    (``.3`` oldest → ``llm.jsonl`` newest) so a response in an old backup is
    still paired with its request.
  * Pairs each response's usage with the model of the nearest preceding
    request record; a response with no preceding request (truncated log) is
    reported under ``(unknown)``.
  * Applies a per-model pricing table ($ per 1M tokens, prompt/completion).
    Prices are public-list approximations — output is an ESTIMATE, not an
    invoice.
  * Prompt-cache semantics: if ``prompt_tokens_details.cached_tokens`` is
    present (OpenAI style, prompt_tokens excludes cache), it is billed at 10%
    of the prompt price. Otherwise ``cache_hit_tokens`` is assumed to be
    included in prompt_tokens (DeepSeek style) and billed at 10% of the
    prompt price.
  * Unknown models are listed separately at $0 with a warning; add or
    override prices with ``--pricing model:prompt_ppm:completion_ppm``.

Exit code is always 0 for a successful report (broken lines are skipped with
a warning on stderr — a report tool must not fail on one corrupt record).

Usage examples::

    scripts/llm-cost-report.py                          # all indexed sessions
    scripts/llm-cost-report.py --session-id emrg-evolution-emrg-task
    scripts/llm-cost-report.py --root ~/.emrg/.emrg/sessions --json
    scripts/llm-cost-report.py --pricing my-model:0.5:1.5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Iterator

# ($ per 1,000,000 tokens: prompt, completion) — public-list approximations.
PPM_DEFAULTS: dict[str, tuple[float, float]] = OrderedDict(
    [
        ("deepseek-v4-flash", (0.20, 1.20)),
        ("deepseek-v4", (1.00, 3.00)),
        ("deepseek-reasoner", (0.55, 2.19)),
        ("deepseek-chat", (0.27, 1.10)),
        ("claude-sonnet-4-5", (3.00, 15.00)),
        ("claude-opus-4-5", (5.00, 25.00)),
        ("claude-3-5-sonnet", (3.00, 15.00)),
        ("gpt-5", (1.25, 10.00)),
        ("gpt-5-mini", (0.25, 2.00)),
        ("gpt-4o", (2.50, 10.00)),
        ("qwen-max", (1.60, 6.40)),
        ("qwen-plus", (0.40, 1.20)),
    ]
)

CACHE_PRICE_FRACTION = 0.10  # prompt-cache hits are typically ~10% of prompt price

UNKNOWN = "(unknown)"


def default_index_path() -> Path:
    return Path.home() / ".emrg" / "sessions_index.json"


def session_dirs(
    root: str | None = None,
    session_id: str | None = None,
    index_path: Path | None = None,
) -> list[Path]:
    """Resolve the session directories to scan."""
    if root:
        base = Path(root).expanduser()
        if not base.is_dir():
            sys.stderr.write(f"warning: --root {root} is not a directory\n")
            return []
        return sorted(p for p in base.iterdir() if p.is_dir())
    index = index_path or default_index_path()
    try:
        mapping = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"warning: cannot read {index}: {exc}\n")
        return []
    if session_id:
        path = mapping.get(session_id)
        if not path:
            sys.stderr.write(f"warning: session {session_id!r} not in {index}\n")
            return []
        return [Path(path)]
    return [Path(p) for p in mapping.values() if Path(p).is_dir()]


def _llm_log_files(session_dir: Path) -> list[Path]:
    """llm.jsonl + rotated backups, oldest first (chronological)."""

    def key(f: Path) -> int:
        suffix = f.name[len("llm.jsonl"):]  # "" or ".1", ".2", ...
        # Rotation shifts main -> .1 -> .2 -> .3, so .3 is the OLDEST backup
        # and the main file is the newest. Read chronologically: .3 first.
        return -int(suffix[1:]) if suffix else 10**9

    files = sorted(session_dir.glob("llm.jsonl*"), key=key)
    return files


def iter_llm_records(session_dir: Path) -> Iterator[tuple[str, dict]]:
    """Yield (model, usage) for every response record that carries usage.

    ``model`` is taken from the nearest preceding request record in
    chronological order (across rotation files); ``(unknown)`` when none.
    Corrupt lines are skipped with a warning.
    """
    last_model: str | None = None
    for path in _llm_log_files(session_dir):
        try:
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError as exc:
            sys.stderr.write(f"warning: cannot open {path}: {exc}\n")
            continue
        with fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    sys.stderr.write(
                        f"warning: {path.name}:{lineno}: corrupt line skipped\n"
                    )
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") == "request":
                    if isinstance(rec.get("model"), str):
                        last_model = rec["model"]
                    continue
                if rec.get("type") == "response" and isinstance(rec.get("usage"), dict):
                    yield (last_model or UNKNOWN, rec["usage"])


def estimate_cost(usage: dict, prompt_ppm: float, completion_ppm: float) -> float:
    """Dollar estimate for one response, honoring prompt-cache pricing."""
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    details = usage.get("prompt_tokens_details")
    cache = 0
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        # OpenAI style: prompt_tokens excludes the cache; bill cache separately.
        cache = details["cached_tokens"]
        billed_prompt = prompt
    else:
        # DeepSeek style: cache_hit_tokens ⊂ prompt_tokens.
        cache = usage.get("cache_hit_tokens") or 0
        billed_prompt = max(prompt - cache, 0)
    return (
        prompt_ppm * billed_prompt / 1e6
        + completion_ppm * completion / 1e6
        + CACHE_PRICE_FRACTION * prompt_ppm * cache / 1e6
    )


def report(session_dirs_: list[Path], pricing: dict[str, tuple[float, float]]) -> dict:
    """Aggregate usage+cost across sessions. Returns per-model rows + totals."""
    agg: dict[str, dict] = {}
    unknown_models: set[str] = set()
    for sd in session_dirs_:
        for model, usage in iter_llm_records(sd):
            row = agg.setdefault(
                model,
                {
                    "requests": 0,
                    "prompt_tokens": 0,
                    "cache_hit_tokens": 0,
                    "completion_tokens": 0,
                    "cost": 0.0,
                    "priced": False,
                },
            )
            row["requests"] += 1
            row["prompt_tokens"] += usage.get("prompt_tokens") or 0
            row["cache_hit_tokens"] += usage.get("cache_hit_tokens") or 0
            row["completion_tokens"] += usage.get("completion_tokens") or 0
            pp, cp = pricing.get(model, (0.0, 0.0))
            row["cost"] += estimate_cost(usage, pp, cp)
            if model in pricing:
                row["priced"] = True
            else:
                unknown_models.add(model)
    totals = {
        "requests": sum(r["requests"] for r in agg.values()),
        "prompt_tokens": sum(r["prompt_tokens"] for r in agg.values()),
        "cache_hit_tokens": sum(r["cache_hit_tokens"] for r in agg.values()),
        "completion_tokens": sum(r["completion_tokens"] for r in agg.values()),
        "cost": sum(r["cost"] for r in agg.values()),
    }
    return {"models": agg, "totals": totals, "unknown_models": sorted(unknown_models)}


def render_human(result: dict) -> str:
    models = result["models"]
    if not models:
        return "No usage records found in the given sessions.\n"
    rows = sorted(models.items(), key=lambda kv: -kv[1]["cost"])
    lines = [
        f"{'Model':<22}{'Requests':>9}{'Prompt tok':>13}{'Cache tok':>11}"
        f"{'Compl tok':>12}{'Est cost':>14}"
    ]
    lines.append("-" * len(lines[0]))
    for name, r in rows:
        priced = " *" if not r["priced"] else ""
        cost = f"${r['cost']:,.4f}"
        lines.append(
            f"{name:<22}{r['requests']:>9,}{r['prompt_tokens']:>13,}"
            f"{r['cache_hit_tokens']:>11,}{r['completion_tokens']:>12,}"
            f"{cost:>14}{priced}"
        )
    t = result["totals"]
    lines.append("-" * len(lines[0]))
    cost = f"${t['cost']:,.4f}"
    lines.append(
        f"{'TOTAL':<22}{t['requests']:>9,}{t['prompt_tokens']:>13,}"
        f"{t['cache_hit_tokens']:>11,}{t['completion_tokens']:>12,}"
        f"{cost:>14}"
    )
    lines.append("")
    if result["unknown_models"]:
        lines.append(
            "(*) no price — add with --pricing model:prompt_ppm:completion_ppm: "
            + ", ".join(result["unknown_models"])
        )
    else:
        lines.append("Estimated cost — public-list approximations, not an invoice.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Estimate LLM API spend from session llm.jsonl usage records."
    )
    parser.add_argument("--root", help="scan this .emrg/sessions directory")
    parser.add_argument("--session-id", help="scan one indexed session by id")
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON instead of a table"
    )
    parser.add_argument(
        "--pricing",
        action="append",
        default=[],
        metavar="MODEL:PROMPT_PPM:COMPLETION_PPM",
        help="add/override a model price ($ per 1M tokens); repeatable",
    )
    args = parser.parse_args(argv)

    pricing = dict(PPM_DEFAULTS)
    for spec in args.pricing:
        parts = spec.split(":")
        if len(parts) != 3:
            parser.error(f"--pricing expects MODEL:PROMPT_PPM:COMPLETION_PPM, got {spec!r}")
        model, prompt_ppm, completion_ppm = parts
        pricing[model] = (float(prompt_ppm), float(completion_ppm))

    dirs = session_dirs(root=args.root, session_id=args.session_id)
    if not dirs:
        sys.stderr.write("No session directories to scan.\n")
        return 1
    result = report(dirs, pricing)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_human(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
