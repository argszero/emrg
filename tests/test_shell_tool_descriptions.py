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

Each promise is read against the renderer it promises about, because a sentence in a tool description
is a declaration about behaviour. Pinning the sentence alone would leave a description free to
promise a marker no renderer writes; pinning the renderer alone is what left the two dialects
disagreeing in the first place. ``tests/test_glob_tool.py`` records the same lesson — *a promise in a
tool description that no test holds is how a claim survived review*.
"""

from __future__ import annotations

import pytest

from emrg.tools import bash_tool_v2 as bash
from emrg.tools import pwsh_tool_v2 as pwsh

#: The contract sentence, spelled once. Both dialects must state it — and it must be the contract
#: their own renderer keeps, because a description and a renderer are two halves of one promise.
CONTRACT_SENTENCE = "Non-zero exits are reported as `[exit code: N]`"

#: The execution model, in the words both dialects must share (issue #2051). `bash_tool_v2` was
#: silent about it while `pwsh_tool_v2` stated it, though both run one command in a **new** process
#: per call and carry nothing between them — measured through the tool itself: `cd /tmp` in one call
#: leaves the next call in the session's working directory, under a different pid.
FRESH_PROCESS_SENTENCE = (
    "Each call is a fresh process — no state (working directory, variables, functions) "
    "persists between calls, so pass `workdir` instead of using `cd`."
)

#: The denial marker, as both descriptions write it. `<mode>` is the variable half; the fixed half is
#: what the reading below matches, and `sandbox_denial_marker` is the one function that writes it —
#: *"one vocabulary for both enforcing families"*, the bash family's refused file effect included.
DENIAL_MARKER = "[sandbox: file access denied under <mode> mode]"

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
def test_the_fresh_process_sentence_advises_a_parameter_that_exists(module, tool):
    """The other half, and the reason this one is not only a string match.

    The sentence's actionable clause is *"pass `workdir` instead of using `cd`"*, so the definition it
    appears in has to really accept `workdir` — a description that tells the model to pass a parameter
    the schema does not declare is worse than the silence it replaces.
    """
    parameters = tool.definition().parameters
    assert "workdir" in parameters.get("properties", {}), parameters


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
