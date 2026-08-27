"""Tests for scripts/llm-cost-report.py (LLM API cost profiler).

Covers: token/cost aggregation math, model pairing across rotated llm.jsonl
backups, the unknown-model fallback, the OpenAI-style cache-details branch,
and the CLI end-to-end.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "llm-cost-report.py"


def _write_llm_log(session_dir: Path, records: list[dict], name: str = "llm.jsonl") -> None:
    (session_dir / name).write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


def _request(model: str) -> dict:
    return {"type": "request", "model": model, "messages": [], "tools": None, "payload": None}


def _response(prompt: int, completion: int, cache: int = 0) -> dict:
    return {
        "type": "response",
        "content": "",
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "reasoning_tokens": 0,
            "cache_hit_tokens": cache,
        },
    }


def _run_report(root: Path, *extra: str) -> dict:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--json", *extra],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def test_aggregates_tokens_and_cost_per_model(tmp_path: Path) -> None:
    sd = tmp_path / "s1"
    sd.mkdir()
    # deepseek-v4-flash @ $0.20/$1.20 per 1M: cache billed at 10% of prompt price.
    _write_llm_log(sd, [_request("deepseek-v4-flash"), _response(1000, 200, cache=900)])
    result = _run_report(tmp_path, "--pricing", "deepseek-v4-flash:0.2:1.2")

    model = result["models"]["deepseek-v4-flash"]
    assert model["requests"] == 1
    assert model["prompt_tokens"] == 1000
    assert model["cache_hit_tokens"] == 900
    assert model["completion_tokens"] == 200
    # billed prompt = 1000 - 900 = 100 -> 0.20*100/1e6
    # completion = 200 -> 1.20*200/1e6 ; cache = 0.1*0.20*900/1e6
    expected = (0.20 * 100 + 1.20 * 200 + 0.1 * 0.20 * 900) / 1e6
    assert model["cost"] == pytest.approx(expected)
    assert result["totals"] == {
        "requests": 1,
        "prompt_tokens": 1000,
        "cache_hit_tokens": 900,
        "completion_tokens": 200,
        "cost": pytest.approx(expected),
    }
    assert result["unknown_models"] == []


def test_model_pairing_across_rotation_and_unknown(tmp_path: Path) -> None:
    sd = tmp_path / "s2"
    sd.mkdir()
    # Old backup (.1): request only. Newer main file: response pairs with it.
    _write_llm_log(sd, [_request("deepseek-v4")], name="llm.jsonl.1")
    _write_llm_log(sd, [_response(500, 50)])
    # A response with no preceding request anywhere -> (unknown).
    sd2 = tmp_path / "s3"
    sd2.mkdir()
    _write_llm_log(sd2, [_response(10, 5)])

    result = _run_report(tmp_path, "--pricing", "deepseek-v4:1.0:3.0")
    assert result["models"]["deepseek-v4"]["requests"] == 1
    assert result["models"]["(unknown)"]["requests"] == 1
    assert result["models"]["(unknown)"]["cost"] == 0.0
    assert result["unknown_models"] == ["(unknown)"]


def test_openai_style_prompt_tokens_details(tmp_path: Path) -> None:
    sd = tmp_path / "s4"
    sd.mkdir()
    resp = {
        "type": "response",
        "content": "",
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": 400,  # excludes cache
            "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 300},
        },
    }
    _write_llm_log(sd, [_request("gpt-5"), resp])
    result = _run_report(tmp_path, "--pricing", "gpt-5:1.25:10.0")
    model = result["models"]["gpt-5"]
    # prompt 400 billed in full; cache 300 at 10% of prompt price
    expected = (1.25 * 400 + 10.0 * 100 + 0.1 * 1.25 * 300) / 1e6
    assert model["cost"] == pytest.approx(expected)


def test_cli_human_output_smoke(tmp_path: Path) -> None:
    sd = tmp_path / "s5"
    sd.mkdir()
    _write_llm_log(sd, [_request("deepseek-v4-flash"), _response(100, 10)])
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "deepseek-v4-flash" in out.stdout
    assert "TOTAL" in out.stdout
    assert "Estimated cost" in out.stdout


def test_cross_backup_pairing_reads_oldest_first(tmp_path: Path) -> None:
    """A response in a mid backup pairs with the request in the OLDEST backup.

    Rotation shifts main -> .1 -> .2 -> .3, so .3 is the oldest backup and
    must be read before .1. Reading .1 first would pair this response with
    a request that chronologically came AFTER it.
    """
    sd = tmp_path / "s6"
    sd.mkdir()
    _write_llm_log(sd, [_request("deepseek-v4")], name="llm.jsonl.3")
    _write_llm_log(sd, [_request("other-model")], name="llm.jsonl.1")
    _write_llm_log(sd, [_response(500, 50)], name="llm.jsonl.2")

    result = _run_report(tmp_path, "--pricing", "deepseek-v4:1.0:3.0")
    assert result["models"]["deepseek-v4"]["requests"] == 1
    assert "other-model" not in result["models"]
