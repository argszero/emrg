"""`~/.emrg/config.toml` is private — one rule, four carriers, and nothing measured them together.

The file holds `api_key` in plaintext, so every path that writes it must leave it owner-only.
That rule is stated as code in four places:

* ``emrg/config.py`` — :data:`emrg.config.CONFIG_FILE_MODE`, applied by ``_make_config_private``
  (the canonical definition, and the one every other carrier is described as matching);
* ``install.sh`` — ``chmod 600 "$cfg"`` in ``generate_config_template``, the ``curl … | bash``
  installer, which runs before anything else and writes the file itself;
* ``emrg/gui/main.js`` — the ``mode: 0o600`` of both ``writeFileSync(configPath(), …)`` calls
  (``ensureConfigTemplate`` and ``writeConfig``, measured while reviewing #2149);
* ``emrg/gui/config_mode.js`` — ``CONFIG_FILE_MODE``, the GUI's copy of the rule, the module
  #2149 adds because ``fs.writeFileSync``'s ``mode`` option applies only when the file is
  **created**.

Each of those has its own arms, and every one of them is relative to its own spelling: the Python
arms compare the file's mode *against that same constant*, ``install.sh``'s arm asserts the
``mode & 0o077 == 0`` property of *its own* literal, and the GUI's asserts the literal ``0o600``.
**Nothing compares one carrier's statement with another's** — and that is not an oversight the
existing arms hide, it is written down as an open gap: ``TestTheInstallerWritesTheConfigPrivate``
declines to pin the digit because "a shell script and a Python module cannot share one constant, so
pinning the digit here would leave the two carriers free to drift apart one digit at a time with
every test green".

That objection is about a **runtime** constant, and this census is the instrument it does not
apply to: at test time both statements are text, and one test can read all four. Measured
2026-10-12 on master ``54aba67a``, one mutation arm each, everything else green:

* ``emrg/config.py``: ``CONFIG_FILE_MODE = 0o600`` → ``0o700`` — **SURVIVED** against
  ``tests/test_config.py`` (35 passed) and ``tests/test_config_template_carriers.py``.
* ``install.sh``: ``chmod 600 "$cfg"`` → ``chmod 700 "$cfg"`` — **SURVIVED** against
  ``TestTheInstallerWritesTheConfigPrivate``.

What this adds is **agreement**, not privacy, and the two must not be confused in either
direction. A drift that *widens* the file is already caught — ``0o640`` in ``emrg/config.py``
fails ``test_no_chmod_when_the_mode_is_already_the_rule`` incidentally (its fixture is the
constant, and the rule's own predicate rejects ``0o640``), and ``0o640`` in ``install.sh`` fails
the installer's property arm. What no arm can see is a carrier moving to a different
**owner-only** mode: `~/.emrg/config.toml` then ends up at whichever carrier ran last — the
installer's ``0600``, then the daemon's ``0700`` on the next start, then the GUI's ``0600`` on the
next save — with every test green and the next person to touch one carrier given no signal that
three others state the same rule.

**The carriers are discovered, not listed.** A tracked file that names the config file *and*
states a mode in a mode position is claiming this rule, whatever it is called and wherever it
lives — so a fifth writer copied from one of these appears here. Two exclusions are rules, named
because each was measured:

* **``*.md``** — a document writes nothing; ``DEVELOPMENT.md`` names the config file and its mode
  as prose.
* **a test path** — ``tests/``, ``test/`` or ``__tests__/`` anywhere in the path. A test writes a
  config to exercise the product, never to give a host one, and this file is the reason the rule
  has to be stated: it names the mode constructs as data, so without the exclusion discovery
  finds *itself*. Measured on the tight marker: without the test-path rule,
  ``emrg/gui/test/daemon_client.test.js`` is a false carrier (it names the config file and calls
  ``fs.chmodSync(file, 0o755)`` for its own fixture).

What must agree is the **mode**, not the spelling: ``0o600`` in Python and JavaScript, ``600`` in
the shell. One extractor per carrier is anchored on the statement rather than on the file, so a
number that appears elsewhere in the same file is not read as this rule — ``main.js`` writes a
``gui-state.json`` at ``mode: 0o600`` too, and ``install.sh`` runs ``chmod +x`` on the CLI.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import emrg.config as config_module

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The canonical statement, imported rather than repeated: this file's whole point is that the
#: other carriers are compared against it.
CANONICAL = config_module.CONFIG_FILE_MODE

#: Marker A — the carrier names the file it is setting the mode of.
_CONFIG_FILE = "config.toml"

#: Marker B — a permission statement in one of the shapes a mode is stated in: a `chmod` call (in
#: any dialect: `os.chmod`, `fs.chmodSync`, the shell's `chmod`), the rule's own constant name, or
#: a `mode:` option. A carrier stating its mode through some other construct (a `umask`, a
#: `setfacl`, a number built at runtime) is outside this census — the boundary is named here rather
#: than implied, and the both-directions test below is what turns a carrier that drifts out of it
#: into a visible failure.
_MODE_STATEMENT = re.compile(r"\bchmod(?:Sync)?\b|CONFIG_FILE_MODE|\bmode\s*:\s*0")

#: carrier -> the pattern that lifts **that carrier's mode statement for the config file** out of
#: it, group 1 holding the octal digits. Anchored on the statement, not the file: `main.js` sets a
#: `0o600` on its own state file as well, and `install.sh` chmods the CLI executable.
#:
#: The `emrg/gui/config_mode.js` entry names a carrier that is **in flight** (#2149): an entry
#: whose file does not exist yet is a placeholder, and the both-directions test reads the table
#: against the files that exist, so it becomes live the moment that module lands. Listing it here
#: rather than adding it later is deliberate — a later cycle would otherwise meet a red suite the
#: day a new carrier appears, which is a discovery read as a regression.
_EXTRACTORS = {
    "emrg/config.py": r"^CONFIG_FILE_MODE = (0o[0-7]+)$",
    "emrg/gui/config_mode.js": r"^const CONFIG_FILE_MODE = (0o[0-7]+);$",
    "emrg/gui/main.js": r"writeFileSync\(configPath\(\),.*?,\s*\{\s*mode:\s*(0o[0-7]+)\s*\}\)",
    "install.sh": r'chmod ([0-7]{3,4}) "\$cfg"',
}


def _tracked_files() -> list[str]:
    """Repo-relative paths of every tracked file, via `git ls-files`.

    git's own index rather than a directory walk: it skips the ignored trees (`.venv/`,
    `node_modules/`, `dist/`) and the untracked ones for free, and it is what "is published"
    means. It is also why a new carrier must be `git add`-ed before this census can see it —
    the trap `test_the_index_derived_scans_reach_new_files` exists to catch.
    """
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return out.stdout.split()


def _is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    return "tests" in parts or "test" in parts or "__tests__" in parts


def _discovered_carriers() -> set[str]:
    found: set[str] = set()
    for rel in _tracked_files():
        # A document writes nothing and a test writes a config to exercise the product — see the
        # module docstring; both exclusions were measured.
        if rel.endswith(".md") or _is_test_path(rel):
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _CONFIG_FILE in text and _MODE_STATEMENT.search(text):
            found.add(rel)
    return found


def _modes(rel: str) -> list[int]:
    """Every mode `rel` states for the config file, as integers."""
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    found = re.findall(_EXTRACTORS[rel], text, re.M)
    assert found, (
        f"no config-file mode could be lifted out of {rel} — the pattern "
        f"{_EXTRACTORS[rel]!r} has gone stale, and a stale pattern is not a pass"
    )
    return [int(value, 8) for value in found]


def _existing_carriers() -> set[str]:
    return {rel for rel in _EXTRACTORS if (REPO_ROOT / rel).is_file()}


def test_the_census_is_the_carriers_this_file_names():
    """Discovery and the extractor table must agree, in both directions.

    A fifth writer copied from one of these carries both markers and appears here; a carrier whose
    statement was reformatted out of the marker's reach appears here too. Either way the next step
    is a human reading this list, not a silent drop. An entry whose file does not exist yet is a
    placeholder for a carrier in flight and is not expected to be discovered.
    """
    assert _discovered_carriers() == _existing_carriers()


def test_every_carrier_states_a_mode():
    """The non-empty baseline: a carrier that states nothing is not a pass.

    `_modes` asserts this per carrier; this arm keeps the census itself non-empty, so a table that
    lost every entry cannot read as agreement.
    """
    carriers = _existing_carriers()
    assert len(carriers) >= 3, f"only {sorted(carriers)} were read — that is not this project's tree"
    for rel in sorted(carriers):
        assert _modes(rel), f"{rel} states no mode for the config file"


def test_every_stated_mode_is_the_one_rule():
    """The point of the census: whoever states the rule states the same rule.

    This is the assertion that was missing. Every carrier's own arms compare it against itself, so
    a drift of the canonical constant away from owner-only is invisible to all of them.
    """
    for rel in sorted(_existing_carriers()):
        for mode in _modes(rel):
            assert mode == CANONICAL, (
                f"{rel} states {oct(mode)} for {_CONFIG_FILE}, and emrg.config.CONFIG_FILE_MODE is "
                f"{oct(CANONICAL)}; the file holds `api_key` in plaintext, so two writers that "
                "disagree leave the host with whichever one ran last"
            )


def test_the_rule_itself_is_owner_only():
    """The definition site, guarded as a property rather than as the number.

    Every Python arm compares the file's mode *against this constant*, so nothing else in the tree
    can tell whether the constant is still private. Restated as the property ``install.sh``'s arm
    uses (nothing outside the owner), plus the owner's own read bit — a rule of ``0o000`` satisfies
    the first and is not a config file anyone can read.
    """
    assert CANONICAL & 0o077 == 0, (
        f"emrg.config.CONFIG_FILE_MODE is {oct(CANONICAL)}, which grants bits outside the owner; "
        "the config file holds a plaintext `api_key`"
    )
    assert CANONICAL & 0o400, (
        f"emrg.config.CONFIG_FILE_MODE is {oct(CANONICAL)}, which its own owner cannot read"
    )


def test_each_extractor_reads_the_digits_and_not_a_constant():
    """The instrument, one direction per carrier: it must read the number that is in the file.

    An extractor that matched a fixed string would make every arm above a comparison of the same
    value with itself. Each one is therefore re-run against the carrier's own text with the
    statement's mode edited — the reading has to move with it.
    """
    for rel in sorted(_existing_carriers()):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        match = re.search(_EXTRACTORS[rel], text, re.M)
        assert match is not None, f"nothing was found in {rel} to mutate"
        original = int(match.group(1), 8)
        edited = original | 0o040  # a group-read bit: outside the owner, so a rule violation
        assert edited != original, f"{rel} already grants 0o040, so this arm would prove nothing"
        rendered = ("0o" if match.group(1).startswith("0o") else "") + format(edited, "o")
        mutated = text[: match.start(1)] + rendered + text[match.end(1) :]

        assert edited in [int(v, 8) for v in re.findall(_EXTRACTORS[rel], mutated, re.M)], (
            f"the extractor for {rel} did not read a changed mode out of the carrier's own text — "
            "so it is not reading the statement, and the census above compares nothing"
        )
