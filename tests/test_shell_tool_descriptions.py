"""The two shell dialects' tool descriptions — the only channel for the rendering contract.

``pwsh_tool_v2``'s description stated the exit-marker contract and ``bash_tool_v2``'s was silent,
though the renderers behind them are one policy (issue #2045). A model driving the bash tool was
therefore never told that a non-zero exit arrives as ``[exit code: N]``, while the same model on
Windows was — and the dialects are meant to differ *only in the shell* (design §14.5 item 1, the
principle #2039 rests on). The sentence was found in the pwsh description alone; nothing else names
the marker, the system prompt included.

Two more statements were one-sided the same way (issue #2051), and one of them was not a nuance but the
execution model: **each call is a new process, and nothing carries between calls.** ``pwsh_tool_v2`` said
so; ``bash_tool_v2`` did not, though it runs ``[bash, "-c", command]`` in a fresh process per call —
measured through the tool: ``cd /tmp`` in one call leaves the next in the session's working directory,
under a different pid. The denial marker was the same shape: both descriptions told the model a write
would be refused, neither named the line it would see, and ``sandbox_denial_marker`` writes exactly one
for *both* enforcing families. Descriptions are prompt surface, so this file is where such a sentence
belongs: the two dialects describe one behaviour, and a fact true of both may not be stated by one.

A third is the other failure of the same channel, and it is a **regression** rather than an omission
(found 2026-10-10, R8's class 3). ``pwsh_tool_v2`` said *"stderr to its head, stdout head and tail"*,
which its own renderer contradicts: `#1594` aligned the twins' stderr cut to **head+tail** and left the
sentence as it was written, so for as long as nothing read it the description promised the model an end
the renderer keeps. ``bash_tool_v2`` said nothing about truncation at all. Both dialects now state the
contract that holds — each stream keeps both ends — and, as above, the sentence is read against the
renderer rather than against itself.

Each promise is read against the renderer it promises about, because a sentence in a tool description
is a declaration about behaviour. Pinning the sentence alone would leave a description free to
promise a marker no renderer writes; pinning the renderer alone is what left the two dialects
disagreeing in the first place. ``tests/test_glob_tool.py`` records the same lesson — *a promise in a
tool description that no test holds is how a claim survived review*.

A fourth promise was held the same way and still failed, because the half it was read against was the
schema rather than the behaviour (issue #2698). The fresh-process sentence's actionable clause told the
model to *"pass ``workdir``"*, and the daemon replaces a shell call's ``workdir`` with the session cwd
on every call — so the clause advised an action that could not work, and a model following it had its
directory discarded without being told. The guard standing there asserted only that ``workdir`` was a
declared **property** of the definition, a reading that cannot fail while the parameter exists, so the
sentence stood while every call ignored it. It is now read against the injection that decides the value.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from emrg.protocol import TaskRequest
from emrg.server.daemon import EmrgServer
from emrg.tools import bash_tool_v2 as bash
from emrg.tools import pwsh_tool_v2 as pwsh

#: The contract sentence, spelled once. Both dialects must state it — and it must be the contract
#: their own renderer keeps, because a description and a renderer are two halves of one promise.
CONTRACT_SENTENCE = "Non-zero exits are reported as `[exit code: N]`"

#: The execution model, in the words both dialects must share (issue #2051). `bash_tool_v2` was
#: silent about it while `pwsh_tool_v2` stated it, though both run one command in a **new** process
#: per call and carry nothing between them — measured through the tool itself: `cd /tmp` in one call
#: leaves the next call in the session's working directory, under a different pid. Its actionable
#: clause used to advise ``workdir``, which the daemon replaces on every call (issue #2698), so what
#: it names now is the mechanism that works and who decides the directory.
FRESH_PROCESS_SENTENCE = (
    "Each call is a fresh process — no state (working directory, variables, functions) "
    "persists between calls, so a command that needs another directory must `cd` into it "
    "itself: every run starts in the session's working directory, and the daemon supplies "
    "`workdir` rather than the call."
)

#: The denial marker, as both descriptions write it. `<mode>` is the variable half; the fixed half is
#: what the reading below matches, and `sandbox_denial_marker` is the one function that writes it —
#: *"one vocabulary for both enforcing families"*, the bash family's refused file effect included.
DENIAL_MARKER = "[sandbox: file access denied under <mode> mode]"

#: The truncation contract, in the words both dialects must share. Its predecessor promised **head
#: only** for stderr; `#1594` made that false when it aligned the twins' stderr cut to head+tail, and
#: nothing read the sentence, so it stood until 2026-10-10 — a promise the renderer contradicts is
#: worse than no promise, because the model acts on it.
TRUNCATION_SENTENCE = (
    "Long output is truncated, and each cut says so: stderr to its own cap and stdout to what "
    "the budget leaves, both keeping head and tail."
)

#: ``(the module that renders, the tool that describes)`` — the pair a promise is made between.
DIALECTS = (
    pytest.param(bash, bash.BashToolV2(), id="bash"),
    pytest.param(pwsh, pwsh.PwshToolV2(), id="pwsh"),
)


@pytest.mark.parametrize("module,tool", DIALECTS)
def test_each_dialect_tells_the_model_the_exit_marker_contract(module, tool):
    """Both descriptions state it, in the same words: only the shell may differ.

    A description is the model's whole view of what a finished run looks like, so a dialect that
    renders a marker and does not describe it is telling the model less than its twin tells the same
    model elsewhere — which is the asymmetry this pins shut.
    """
    assert CONTRACT_SENTENCE in tool.definition().description


@pytest.mark.parametrize("module,tool", DIALECTS)
def test_the_marker_each_description_promises_is_the_one_its_renderer_emits(module, tool):
    """The other half: a description can name a marker that no renderer writes.

    That is what makes this sentence worth holding against behaviour rather than reading — the
    promise is about what the model will see, so the reading is the text a non-zero exit produces.

    Only the non-zero direction is read here. Its converse — no status line on a *successful* run —
    is a property of the renderers rather than of the promise, and the test that holds it is the one
    comparing the two renderers, where the dialects' agreement belongs. This file reads the
    description against the marker the description names, which is a contract neither dialect is
    free to state and not keep.
    """
    run = module.ShellRunResult
    text = module.render_result(run(stdout="hi\n", exit_code=3))
    assert "[exit code: 3]" in text, text


@pytest.mark.parametrize("module,tool", DIALECTS)
def test_each_dialect_tells_the_model_a_call_is_a_fresh_process(module, tool):
    """Issue #2051: one execution model, two description lengths.

    The sentence was in the pwsh description alone, so a model driving the bash tool — the default
    one, and the one more calls go through — was never told that nothing carries between calls. The
    dialects are meant to differ only in the shell, and "each call is a new process" is a property of
    both, so both have to say it, in the same words.
    """
    assert FRESH_PROCESS_SENTENCE in tool.definition().description


@pytest.mark.parametrize("module,tool", DIALECTS)
def test_the_fresh_process_sentence_does_not_offer_a_directory_the_daemon_replaces(
    module, tool, tmp_path
):
    """The other half, read against the behaviour that decides it — where the last one failed.

    The sentence's actionable clause used to be *"pass `workdir` instead of using `cd`"*, and the
    daemon replaces a shell call's ``workdir`` with the session cwd **unconditionally**
    (``EmrgServer._inject_tool_arguments``) — as it must, because honouring a model-supplied directory
    let the model name the root it was trusted in. So the clause advised an action that could not work:
    measured on the host that filed issue #2698, a call carrying ``workdir=<session>/tmp`` — an
    existing directory — answered ``Get-Location`` = the session root, and a second call naming a
    directory that holds a ``package.json`` was told ``Test-Path`` = false.

    The guard this replaced asserted only that ``workdir`` was a **declared property** of the
    definition, a reading that cannot fail while the parameter exists, whatever the daemon does with
    its value — a presence reading of a sentence about behaviour. So the injection is driven here, and
    the pair is stated as one fact: while this tool's directory is the daemon's, its description may
    not offer that directory to the caller.
    """
    session = SimpleNamespace(cwd=tmp_path, sandbox_grants=[], sandbox_roots=[], session_id="stub")
    args = {"command": "echo hi", "workdir": "/a/directory/the/caller/named"}
    EmrgServer._inject_tool_arguments(
        tool.definition().name, args, session, TaskRequest(sandbox=None)
    )
    assert args["workdir"] == str(tmp_path), (
        "the daemon did not replace this shell tool's workdir, so the negative reading below is about "
        "nothing — a description offering the parameter would then be telling the truth"
    )
    description = tool.definition().description
    assert "pass `workdir`" not in description, (
        "the description advises a parameter the daemon replaces on every call, so the directory the "
        f"model names is silently discarded: {description[:240]!r}"
    )
    assert "supplies " in description and "rather than the call" in description, (
        "the description has to say who decides the directory, not only stop advising the parameter: "
        f"{description[:240]!r}"
    )


@pytest.mark.parametrize("module,tool", DIALECTS)
def test_the_workdir_field_says_the_daemon_replaces_it(module, tool):
    """A reader who sees the field is told it is not theirs to set (issue #2698).

    The field stays declared: both executors honour it when called directly, and the kernel fills it on
    every call through the daemon. So the schema's own sentence is where a caller learns that a value
    given there is replaced rather than used — the silent half of the old promise, which said only that
    the value would not widen the boundary and left the reader to assume it moved the command.
    """
    field = tool.definition().parameters["properties"]["workdir"]["description"]
    assert "daemon" in field and "replaces" in field, (
        f"the workdir field does not say the daemon supplies and replaces it: {field!r}"
    )


@pytest.mark.parametrize("module,tool", DIALECTS)
def test_each_dialect_names_the_denial_marker_its_renderer_writes(module, tool):
    """Both descriptions name it: the marker is one vocabulary, so naming it in one is not enough."""
    assert DENIAL_MARKER in tool.definition().description


@pytest.mark.parametrize("module,tool", DIALECTS)
def test_the_denial_marker_each_description_names_is_the_one_a_denied_run_renders(module, tool):
    """Read against behaviour, as the exit marker above is: a description can name a marker no
    renderer writes, and the promise is about what the model will actually see.

    The mode is the marker's variable half, so this reads a run denied under a *named* mode and
    requires the description's fixed half to be present in what came out.
    """
    from emrg.sandbox.contract import sandbox_denial_marker

    mode = "workspace-write"
    written = sandbox_denial_marker(mode)
    assert DENIAL_MARKER.replace("<mode>", mode) == written, written

    run = module.ShellRunResult
    text = module.render_result(
        run(stdout="hi\n", exit_code=1, sandbox={"mode": mode, "denied": True})
    )
    assert written in text, text
    # The fixed half of the description's marker is a prefix of the line the model reads, so the
    # description is not naming a differently-punctuated marker.
    assert DENIAL_MARKER.split("<mode>")[0] in text, text


@pytest.mark.parametrize("module,tool", DIALECTS)
def test_each_dialect_tells_the_model_how_long_output_is_cut(module, tool):
    """The truncation contract, stated by both: a cut stream that reads as a short one is the failure
    every one of these sentences exists to prevent, and the bash dialect is the one more calls go
    through, so a `pwsh`-only sentence tells most calls nothing."""
    assert TRUNCATION_SENTENCE in tool.definition().description


@pytest.mark.parametrize("module,tool", DIALECTS)
def test_the_truncation_each_description_states_is_the_one_its_renderer_does(module, tool):
    """Read against behaviour, as the two markers above are — and this is the reading that was missing.

    The sentence's predecessor said stderr is cut **to its head**. `#1594` gave the twins one stderr
    cut, head+tail, and the sentence was not revisited; nothing read it against the renderer, so it
    promised the model an end that is in fact kept, and a model told its error output is head-only
    reads a truncated stderr as if the tail — where a failing build puts the line that matters — were
    gone. The same reading covers the other half for free: a promise of head+tail that the renderer
    drops would be caught here too.

    Driven through `render_result`, so the subject is what the model actually sees rather than a helper
    the model never calls.
    """
    head_out, tail_out = "OUT-HEAD", "OUT-TAIL"
    head_err, tail_err = "ERR-HEAD", "ERR-TAIL"
    stdout = head_out + "\n" + "\n".join(f"out {i}" for i in range(40_000)) + "\n" + tail_out + "\n"
    stderr = head_err + "\n" + "\n".join(f"err {i}" for i in range(40_000)) + "\n" + tail_err + "\n"
    assert len(stdout) > module.MAX_OUTPUT_CHARS, len(stdout)
    assert len(stderr) > module._ERR_MAX, len(stderr)

    run = module.ShellRunResult
    text = module.render_result(run(stdout=stdout, stderr=stderr, exit_code=1))

    for line in (head_out, tail_out, head_err, tail_err):
        assert line in text, (
            f"{line} did not survive the cut, so the description's 'both keeping head and tail' is "
            f"not what this renderer does: {text[:120]!r} … {text[-120:]!r}"
        )
    assert "truncated" in text, (
        "and every cut says so: without a notice, a cut stream reads as a short one"
    )
