"""Every diagnostic the release workflow captures must be able to reach the log.

Measured on tag `v0.3.8`, run `36956685533`: the macOS leg died in **3 seconds** with
nothing in the log but `Process completed with exit code 1.` — the same signature a
discarded capture leaves, because the step that discards it cannot be told from one
whose command was never found.

The mechanism, measured on this host rather than reasoned about (2026-10-02):

    X="$(printf 'a\\n' | grep 'zzz-nomatch' | head -1)"
    bash --noprofile --norc -e            pf.sh   ->  REACHED the line after; X=[]
    bash --noprofile --norc -eo pipefail  pf.sh   ->  rc=1, nothing printed

GitHub expands an explicit `shell: bash` to `bash --noprofile --norc -eo pipefail {0}`
(it is *not* the same as omitting `shell:`, which gives `bash -e {0}`), and every step in
this workflow declares `shell: bash`. So a failing command anywhere in a substitution
aborts the step **at the assignment**.

That is harmless when the failure is the point (a red step must be red) and a defect when
the assignment is the *only* channel through which the command's own words reach the log:
the `echo` on the next line never runs, the `::error::` below it is unreachable in the
state it names, and the operator is left with an exit code and no evidence.

The predicate is deliberately narrow, so it has no exemptions to maintain: **an assignment
that captures stderr (`2>&1`) is an assignment whose output the step intends to print**, so
it must carry a status recovery (`|| X_RC=$?` when the two failures are different failures,
`|| true` when emptiness is legitimate). An assignment that does *not* capture stderr makes
no such promise and is not this rule's business — which is why the four `PKG="$(find … |
head -1)"` sites stay silent here: `head` exits 0 on empty input, so their `no pkg found`
branch really is reachable (measured, and the reason a "warn on every assignment" rule was
rejected as +4 false positives).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

WORKFLOW = ".github/workflows/build-release.yml"

# The runner's expansion of `shell: bash` — `-o pipefail` is the defect's mechanism, so
# the arms must run under it and not under a friendlier interpreter default.
RUNNER_SHELL = ["--noprofile", "--norc", "-eo", "pipefail"]

IMPORT_STEP = "Import signing certificate (macOS only)"


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.is_file(), f"{rel} is missing — the guard cannot measure what it guards"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{rel} is empty — a vacuous read must not read as a pass"
    return text


# ── the rule, as a function over workflow text ────────────────────────────────
#
# It reads the *statement*, not a line: these calls are wrapped with a backslash and the
# closing `2>&1)"` sits on the next line, so a line-wise rule would see neither half.
_ASSIGN_OPEN = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)="\$\(', re.M)
# Any variable holding `$?` counts, not only the `*_RC` spelling this repo happens to
# use: the control below writes `RC=$?` and is a correct recovery, so a pattern keyed on
# the suffix would report it. `|| exit`/`|| return` deliberately do NOT satisfy the rule —
# they abort, which is the very thing that discards the captured output.
_STATUS_RECOVERY = re.compile(r'\|\|\s*(?:true|:|[A-Za-z_][A-Za-z0-9_]*=\$\?)')


# ── the second shape: a `grep` pipeline whose emptiness the step tests ────────
#
# `2>&1` is not the only way a capture loses the status it needed. `grep` exits **1**
# when it matches nothing — that is what it is for — so a `VAR="$(… | grep … | head -1)"
# assignment fails whenever the answer is "nothing here", and under `-e -o pipefail`
# that aborts the step *at the assignment*. Executed on this host (2026-10-02): the same
# pipeline with `head` in place of `grep` exits 0 on empty input, which is why the four
# `PKG="$(find … | head -1)"` sites are not this rule's business.
#
# What makes the abort a defect rather than a retirement is the step's own next move: a
# later `-z` test on that variable says the step has decided an empty value is a
# legitimate outcome it intends to handle. The abort happens *before* that decision, so
# the branch is unreachable in exactly the state it was written for — measured on the
# Sign pkg step, which printed nothing on stdout and exited 1 with only an Application
# identity in the keychain.
_GREP_STAGE = re.compile(r'(?:^|[|(]\s*|\$\(\s*|\s)grep\s')


def unreachable_emptiness_tests(text: str) -> list[str]:
    """`name (step): VAR` for every `grep` pipeline the step then tests for emptiness.

    Statements carrying `2>&1` are left to `unrecovered_captures`: both rules name the
    same remedy there, and a site reported twice reads as two defects.
    """
    out: list[str] = []
    doc = yaml.safe_load(text)
    for job, cfg in (doc.get("jobs") or {}).items():
        for step in (cfg.get("steps") or []):
            if not isinstance(step, dict) or not step.get("run"):
                continue
            lines = str(step["run"]).splitlines()
            for i, line in enumerate(lines):
                m = _ASSIGN_OPEN.match(line)
                if not m:
                    continue
                var = m.group(1)
                statement, j = [], i
                while j < len(lines):
                    statement.append(lines[j])
                    if ')"' in lines[j]:
                        break
                    j += 1
                joined = "\n".join(statement)
                if "2>&1" in joined or not _GREP_STAGE.search(joined):
                    continue
                if _STATUS_RECOVERY.search(joined):
                    continue
                rest = "\n".join(lines[i + len(statement):])
                if not re.search(r'-\s*z\s+"\$\{?' + re.escape(var) + r'\}?"', rest):
                    continue
                out.append(f"{step.get('name') or '(unnamed step)'}: {var}")
    return out


def unrecovered_captures(text: str) -> list[str]:
    """`name (step): VAR` for every stderr capture that discards its command's status."""

    def captures(body: str) -> list[str]:
        lines = body.splitlines()
        found: list[str] = []
        for i, line in enumerate(lines):
            m = _ASSIGN_OPEN.match(line)
            if not m:
                continue
            statement, j = [], i
            while j < len(lines):
                statement.append(lines[j])
                if ')"' in lines[j]:
                    break
                j += 1
            joined = "\n".join(statement)
            # Only a deliberate `2>&1` is this rule's business: it says the output is
            # meant for the log. `2>/dev/null` says the opposite (`_kind` below).
            if "2>&1" not in joined:
                continue
            if _STATUS_RECOVERY.search(joined):
                continue
            found.append(m.group(1))
        return found

    out: list[str] = []
    doc = yaml.safe_load(text)
    for job, cfg in (doc.get("jobs") or {}).items():
        for step in (cfg.get("steps") or []):
            if not isinstance(step, dict) or not step.get("run"):
                continue
            for var in captures(str(step["run"])):
                out.append(f"{step.get('name') or '(unnamed step)'}: {var}")
    return out


_STUB = '''#!/bin/sh
# Stand in for `security`, answering each subcommand the import step uses.
sub="$1"; shift
case "$sub" in
  import)
    p12="${1:-}"
    if [ "${p12#*installer}" != "$p12" ]; then scenario="$INSTALLER_SCENARIO"; else scenario="$IMPORT_SCENARIO"; fi
    case "$scenario" in
      refused)
        # What a wrong password really prints: the evidence the step used to discard.
        printf 'security: SecKeychainItemImport: MAC verification failed during PKCS12 import (wrong password?)\\n' >&2
        exit 1 ;;
      certs-only)
        printf '1 certificate imported.\\n'; exit 0 ;;
      *)
        printf '1 identity imported.\\n'; exit 0 ;;
    esac ;;
  find-certificate)
    printf 'keychain: "/tmp/ci.keychain"\\nversion: 512\\n'; exit 0 ;;
  find-identity)
    printf '  1) 0123456789ABCDEF "Developer ID Application: Example (TEAMID)"\\n'; exit 0 ;;
  list-keychains)
    printf '    "/tmp/ci.keychain"\\n'; exit 0 ;;
esac
exit 0
'''


def _import_step() -> dict:
    """The step, found by name in the parsed job — never by a text search."""
    jobs = yaml.safe_load(_read(WORKFLOW)).get("jobs")
    assert isinstance(jobs, dict) and "build" in jobs, "build-release.yml has no `build` job"
    named = [
        s for s in jobs["build"].get("steps", [])
        if isinstance(s, dict) and s.get("name") == IMPORT_STEP
    ]
    assert len(named) == 1, f"expected exactly one {IMPORT_STEP!r} step, got {len(named)}"
    assert named[0].get("run"), "the import step carries no `run:` body"
    return named[0]


def _body(step: dict) -> str:
    """The body with comment lines dropped — a read must be a read, not a mention of one."""
    body = "\n".join(
        ln for ln in str(step["run"]).splitlines() if not ln.strip().startswith("#")
    )
    assert body.strip(), "the step body is nothing but comments"
    return body


def _run_import_step(tmp_path, *, import_scenario: str, installer_scenario: str = "ok",
                     installer_p12: str | None = "BASE64"):
    """Run the import step as the runner runs it, with `security` stubbed."""
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "security"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(0o755)
    # `base64` writes the p12 the step then "imports"; the stub keeps it out of the way.
    b64 = bindir / "base64"
    b64.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    b64.chmod(0o755)

    script = tmp_path / "step.sh"
    script.write_text(_body(_import_step()), encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env.update({
        "MACOS_SIGNING_P12_BASE64": "cDEy",
        "MACOS_SIGNING_P12_PASSWORD": "pw",
        "IMPORT_SCENARIO": import_scenario,
        "INSTALLER_SCENARIO": installer_scenario,
        "HOME": str(tmp_path / "home"),
    })
    if installer_p12 is None:
        env.pop("MACOS_INSTALLER_P12_BASE64", None)
    else:
        env["MACOS_INSTALLER_P12_BASE64"] = installer_p12
        env["MACOS_INSTALLER_P12_PASSWORD"] = "pw"
    return subprocess.run(
        [shell, *RUNNER_SHELL, str(script)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
        # Pinned: the step prints macOS's own English `security` text, but a host whose
        # locale is cp936/cp1252 would decode the child's UTF-8 with the wrong codec
        # (`tests/test_script_decode_is_locale_independent.py` is the rule, and this is
        # the call it names).
        encoding="utf-8", errors="replace",
    )


# ── the rule, over the real workflow ──────────────────────────────────────────


def test_every_captured_diagnostic_carries_its_command_status() -> None:
    """The guard proper: no step may capture stderr and then lose the command's status.

    On `v0.3.8` this is what made the macOS failure unreadable (run `36956685533`): the
    capture held the answer and the step died before printing it.
    """
    offenders = unrecovered_captures(_read(WORKFLOW))
    assert not offenders, (
        "these assignments capture stderr, so the step means to print it, but under the "
        "runner's `-eo pipefail` a failing command aborts at the assignment and the "
        "output never reaches the log. Give each a status recovery — `|| X_RC=$?` when "
        f"the command's failure is a different state from the one being tested: {offenders}"
    )


def test_the_guard_reads_the_thing_it_names() -> None:
    """Both directions on a synthetic body, so the guard cannot pass vacuously.

    A pattern that matches nothing reports no offenders and looks like a clean tree; this
    is the arm that tells the two apart. The negative half also pins that the recovery is
    what satisfies the rule, not the mere presence of an assignment.
    """

    def step(body: str) -> str:
        return yaml.safe_dump({"jobs": {"build": {"steps": [{"name": "x", "run": body}]}}})

    unrecovered = (
        'OUT="$(security import p12 -P "$PW" 2>&1)"\n'
        'echo "$OUT"\n'
        'if [[ ! "$OUT" =~ identit ]]; then exit 1; fi\n'
    )
    assert unrecovered_captures(step(unrecovered)) == ["x: OUT"], (
        "the guard did not flag an unrecovered stderr capture — it measures nothing"
    )

    recovered = (
        'RC=0\n'
        'OUT="$(security import p12 -P "$PW" 2>&1)" || RC=$?\n'
        'echo "$OUT"\n'
        'if [ "$RC" -ne 0 ]; then exit 1; fi\n'
    )
    assert unrecovered_captures(step(recovered)) == [], (
        "a capture that reads its exit code was reported — the guard is over-broad"
    )

    # `2>/dev/null` is the opposite intent: the output is deliberately NOT kept, so
    # there is nothing for the step to print and nothing to recover.
    discarded = 'OUT="$(security import p12 -P "$PW" 2>/dev/null)"\n'
    assert unrecovered_captures(step(discarded)) == [], (
        "a capture that discards its output was reported — the rule is about kept output"
    )


def test_a_grep_pipeline_does_not_make_the_emptiness_branch_unreachable() -> None:
    """The second shape, over the real workflow and over synthetic bodies both ways.

    Measured on `main`'s Sign pkg step before this rule existed: with only an Application
    identity in the keychain the step printed nothing on stdout and exited 1, so the
    `::error::` naming that state — and the remedy it carries — could not be read.
    """
    offenders = unreachable_emptiness_tests(_read(WORKFLOW))
    assert not offenders, (
        "these assignments pipe through `grep`, whose exit 1 on no-match aborts the step "
        "at the assignment, and the step then tests the result with `-z` — a branch that "
        f"can no longer be reached in the state it was written for: {offenders}"
    )

    def step(body: str) -> str:
        return yaml.safe_dump({"jobs": {"build": {"steps": [{"name": "x", "run": body}]}}})

    lookup = 'ID="$(security find-identity -v /tmp/k | grep \'Developer ID Installer\' | head -1)"\n'
    handles_empty = 'if [ -z "$ID" ]; then echo "::error::no installer identity"; exit 1; fi\n'
    assert unreachable_emptiness_tests(step(lookup + handles_empty)) == ["x: ID"], (
        "the guard did not flag a `grep` lookup whose empty case the step handles — it "
        "measures nothing"
    )
    assert unreachable_emptiness_tests(step(lookup + 'productsign --sign "$ID" p.pkg\n')) == [], (
        "a lookup whose empty result the step never handles was reported — that is a "
        "different shape, and reporting it would be the over-broad rule rejected above"
    )
    assert unreachable_emptiness_tests(
        step('ID="$(security find-identity -v /tmp/k | grep X | head -1 || true)"\n' + handles_empty)
    ) == [], (
        "a lookup that lets `grep`'s no-match through was reported — emptiness is the "
        "answer here, so it needs no recovery"
    )
    assert unreachable_emptiness_tests(
        step('PKG="$(find dist | head -1)"\nif [ -z "$PKG" ]; then exit 0; fi\n')
    ) == [], (
        "a `head` pipeline was reported — `head` exits 0 on empty input (measured), so "
        "that branch is reachable and this rule must not claim otherwise"
    )


# ── the step, executed ────────────────────────────────────────────────────────


def test_a_refused_import_prints_securitys_words_and_not_the_wrong_diagnosis(tmp_path) -> None:
    """The defect, end to end: the refused import must be visible *as* a refused import.

    Before the fix this run printed nothing at all (the assignment aborted the step), and
    the `::error::` beneath it — which names a missing private key — could not be reached.
    A later variant that reached it anyway would be worse than silence: it would blame the
    host's export method for a password the host typed wrong.
    """
    r = _run_import_step(tmp_path, import_scenario="refused", installer_p12=None)

    assert r.returncode != 0, "a refused import must fail the step"
    assert "MAC verification failed" in r.stdout, (
        "security's own words are the evidence this step exists to surface, and they are "
        f"captured with `2>&1` — they must reach the log. stdout was: {r.stdout!r}"
    )
    assert "could not be imported at all" in r.stdout, (
        f"the state must be named as what it is: {r.stdout!r}"
    )
    assert "未包含可签名私钥" not in r.stdout, (
        "the missing-private-key diagnosis is a *different* state and must not be printed "
        f"for a refused import — that is the wrong cause, stated confidently: {r.stdout!r}"
    )


def test_a_refused_installer_import_says_so_too(tmp_path) -> None:
    """The same rule one branch over: the Installer p12 is a second capture."""
    r = _run_import_step(
        tmp_path, import_scenario="ok", installer_scenario="refused",
        installer_p12="cDEy",
    )

    assert r.returncode != 0, "a refused Installer import must fail the step"
    assert "MAC verification failed" in r.stdout, r.stdout
    assert "could not be imported at all" in r.stdout, r.stdout
    assert "MACOS_INSTALLER_P12_BASE64 未包含可签名私钥" not in r.stdout, (
        f"and it must not blame the Installer export method either: {r.stdout!r}"
    )


def test_a_p12_without_a_private_key_still_gets_the_original_diagnosis(tmp_path) -> None:
    """The control: the branch that was always right must still be reached.

    `security import` succeeds and reports only certificates — the v0.2.7 root cause. The
    fix adds a state *before* this one; it must not shadow it. Without this arm, "make the
    message reachable" could be satisfied by a message that fires for everything.
    """
    r = _run_import_step(tmp_path, import_scenario="certs-only", installer_p12=None)

    assert r.returncode != 0, "a p12 without a private key must fail the step"
    assert "MACOS_SIGNING_P12_BASE64 未包含可签名私钥" in r.stdout, (
        f"the real diagnosis must survive the change: {r.stdout!r}"
    )
    assert "could not be imported at all" not in r.stdout, (
        f"an import that succeeded is not a refused import: {r.stdout!r}"
    )


# ── the notarize step ─────────────────────────────────────────────────────────
#
# The guard above covers only the `2>&1` shape, because that is the one it can decide
# precisely (see its docstring). These captures are the other shape — `2>/dev/null`, the
# value is what matters — so they are covered by running the step rather than by a rule.
# That is a deliberate boundary, not an oversight: a rule broad enough to catch them
# ("any assignment whose value a later `::error::` branch tests") was measured against
# this workflow and reported 7 further sites, every one of which must abort on an empty
# value — the four `gh api` reads whose emptiness would print a definite claim about a
# release (`still a DRAFT (draft=)`), and the two `find … | head -1` reads that cannot
# fail at all.

NOTARY_STEP = "Notarize pkg (macOS only)"

_NOTARY_STUB = '''#!/bin/sh
# Stand in for `xcrun notarytool`, answering the way the scenario says.
sub=""
for a in "$@"; do case "$a" in submit|log|info) sub="$a"; break;; esac; done
case "$sub" in
  submit)
    case "$NOTARY_SCENARIO" in
      noisy-json)
        # SUCCEEDS, but `2>&1` mixes a progress line into the JSON document, so the
        # output is not parseable as one document. This is the measured shape: the
        # step used to die at the parse with no cause printed.
        printf 'Conducting pre-submission checks for EMRG.pkg...\\n'
        printf '{"id": "sub-1", "message": "uploaded", "status": "Accepted"}\\n'
        exit 0 ;;
      no-fields)
        # Parses fine, but Apple's reply carries neither field.
        printf '{"message": "uploaded"}\\n'; exit 0 ;;
      invalid)
        printf '{"id": "sub-1", "message": "see log", "status": "Invalid"}\\n'; exit 0 ;;
      *)
        printf '{"id": "sub-1", "message": "uploaded", "status": "Accepted"}\\n'; exit 0 ;;
    esac ;;
  log) printf 'no rejection detail\\n'; exit 0 ;;
esac
exit 0
'''


def _notary_body() -> str:
    jobs = yaml.safe_load(_read(WORKFLOW)).get("jobs")
    assert isinstance(jobs, dict) and "build" in jobs, "build-release.yml has no `build` job"
    named = [
        s for s in jobs["build"].get("steps", [])
        if isinstance(s, dict) and s.get("name") == NOTARY_STEP
    ]
    assert len(named) == 1, f"expected exactly one {NOTARY_STEP!r} step, got {len(named)}"
    return _body(named[0])


def _run_notary_step(tmp_path, scenario: str):
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "xcrun"
    stub.write_text(_NOTARY_STUB, encoding="utf-8")
    stub.chmod(0o755)
    (tmp_path / "dist" / "artifacts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dist" / "artifacts" / "EMRG-0.3.8-macos-arm64.pkg").write_bytes(b"pkg")

    script = tmp_path / "step.sh"
    script.write_text(_notary_body(), encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env.update({
        "APPLE_ID": "a@b.c", "MACOS_NOTARY_APP_PASSWORD": "pw",
        "MACOS_NOTARY_TEAM_ID": "TEAMID", "NOTARY_SCENARIO": scenario,
        "HOME": str(tmp_path / "home"),
    })
    return subprocess.run(
        [shell, *RUNNER_SHELL, str(script)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def test_a_successful_submit_whose_output_is_unparseable_says_so(tmp_path) -> None:
    """The silent case: `submit` exits 0, the output is not JSON, and nobody said why.

    Measured on this tree by running the body under the runner's flags: `rc=1`, the raw
    output on stdout, and **no** `::error::` at all — the parse assignment aborted the
    step before the branch written for that state. An operator reading that log learns
    the step failed and nothing else, which is the v0.3.8 signature again.
    """
    r = _run_notary_step(tmp_path, "noisy-json")

    assert r.returncode != 0, "an unreadable verdict must still fail the step"
    assert "could not be parsed as JSON" in r.stdout, (
        "the step must name the state it is in rather than dying at an assignment: "
        f"{r.stdout!r}"
    )
    assert "still a DRAFT" not in r.stdout and "DRAFT" not in r.stdout, (
        f"nothing here is a verdict about the release: {r.stdout!r}"
    )


def test_output_that_parses_but_lacks_the_fields_is_a_different_message(tmp_path) -> None:
    """The two states are separate, and this arm is what keeps them so.

    "The parser could not read it" and "it read fine and the fields were absent" have
    different remedies (notarytool emitted something unexpected / Apple's reply lacked
    the field), so a single message for both would name the wrong cause for one of them
    — the defect §112 records, one branch over.
    """
    r = _run_notary_step(tmp_path, "no-fields")

    assert r.returncode != 0, "a reply with no id/status must fail the step"
    assert "输出解析失败" in r.stdout, (
        f"the missing-fields diagnosis must still be reachable: {r.stdout!r}"
    )
    assert "could not be parsed as JSON" not in r.stdout, (
        f"it parsed fine — that is not this state: {r.stdout!r}"
    )


def test_a_clean_accepted_reply_still_reports_success(tmp_path) -> None:
    """The control: the happy path must not be shadowed by the two new branches."""
    r = _run_notary_step(tmp_path, "ok")

    assert r.returncode == 0, f"an Accepted reply must pass: {r.stdout!r}{r.stderr!r}"
    assert "公证通过" in r.stdout, f"and say so: {r.stdout!r}"
    assert "could not be parsed as JSON" not in r.stdout
