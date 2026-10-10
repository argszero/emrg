"""The two shell dialects' tool descriptions — the only channel for the rendering contract.

``pwsh_tool_v2``'s description stated the exit-marker contract and ``bash_tool_v2``'s was silent,
though the renderers behind them are one policy (issue #2045). A model driving the bash tool was
therefore never told that a non-zero exit arrives as ``[exit code: N]``, while the same model on
Windows was — and the dialects are meant to differ *only in the shell* (design §14.5 item 1, the
principle #2039 rests on). The sentence was found in the pwsh description alone; nothing else names
the marker, the system prompt included.

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
