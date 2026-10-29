"""The one way to show a picture in a reply must be stated in the prompt every session reads.

Why this file exists
--------------------
The GUI chat renders a local image from a path, and the TUI has no image surface at all. Until this
block existed, `emrg/server/prompts/system.j2` said nothing about images — no occurrence of
``image``, of the Chinese word for it, or of ``base64`` anywhere in the template — so every session
rediscovered the question from zero, and the only technique that actually worked was inlining the
picture as base64: a single screenshot becomes hundreds of thousands of characters in one message,
and one broken newline ends the string so the rest of the payload floods the transcript as body
text.

The rendering half of that defect is guarded where it lives (`emrg/gui/renderer/src/lib/
vendorMarkdown.test.ts`, driven through the real vendored DOMPurify). This file guards the other
half: the prompt's silence, which is what made each session pay for the discovery again.

What is asserted
----------------
Two levels, two questions — the same pair `tests/test_plain_language_rule_reach.py` asks of the
plain-language rule, for the same reason. The file leg answers "does the source that ships state
it?"; the render leg answers "is this what a session receives?". Neither implies the other:
`system.j2` is a Jinja2 template, so a `{# … #}` or `{% if false %}` can remove the block from the
prompt while the file keeps every character of it. The render leg compares the block **verbatim**
rather than term by term, so a construct that reflowed the wording is visible too.

The prohibition and its alternative are pinned together. "Never inline base64" without the two
path forms is a rule that forbids the only working technique; the two path forms without the
prohibition leaves the expensive habit legal. A rewrite that keeps one half must fail here.

Named limit
-----------
This pins the rule's *presence* and its wording in the artifact a session receives. It cannot read
a message and decide whether the rule was followed: a session that inlines base64 under a prompt
that forbids it is a model-behaviour failure, not one this file can see.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The block's identity in `system.j2`. It sits inside `## Writing`, the one section that already
#: owns how a reply reads (language policy, lead-with-the-point), rather than in a section of its
#: own — a second copy appended anywhere in the template is visible here as a count of 2.
IMAGE_HEADING = "### Images in a reply"

#: Load-bearing terms, each a verbatim substring of the shipped wording, so this list cannot drift
#: away from the sentence it checks without this file saying so. They cover: the path-only rule,
#: both accepted forms, the half that differs per client, and the prohibition with its reason.
IMAGE_TERMS = (
    "renders a local image from a path and from nothing else",   # the rule itself
    "Markdown image syntax with an absolute path",               # how to write it
    "both forms render",                                         # /abs and file:/// alike
    "the alt text must carry what the picture shows",            # the TUI half
    "Never inline the picture as a `data:` URI",                 # the prohibition
    "never paste an encoded blob in place of a path",            # the habit it forbids
    "floods the transcript as body text",                        # why it is forbidden
)


def _block_after(text: str, heading: str) -> str:
    """The block starting at ``heading``, up to the next `## ` or `### ` heading, or EOF.

    Returns ``""`` when the heading is absent — the caller asserts, so a missing block reads as a
    failure to measure rather than as a pass.
    """
    start = text.find(heading)
    if start == -1:
        return ""
    rest = text[start + len(heading) :]
    ends = [pos for pos in (rest.find("\n## "), rest.find("\n### ")) if pos != -1]
    end = min(ends) if ends else -1
    return rest if end == -1 else rest[:end]


def _missing_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    """The terms this text does not carry. Empty means the rule is stated."""
    return [term for term in terms if term not in text]


def _render_system_prompt() -> str:
    """The prompt a session receives, rendered by the daemon's own environment.

    `_get_jinja_env` is what `_build_system_prompt` uses — a fresh `jinja2.Environment` could differ
    in `trim_blocks` / `lstrip_blocks` or in the loader path, and the prompt under test would then
    be a prompt nobody receives. The context is deliberately minimal: the block sits outside every
    conditional, so no context key can remove it.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )


def test_the_shared_prompt_states_the_image_rule() -> None:
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    block = _block_after(text, IMAGE_HEADING)
    assert block, (
        "emrg/server/prompts/system.j2 must carry the images-in-a-reply block: it is the only "
        "prompt every session is rendered under, and its silence on images is what left each "
        "session with base64 as the only way to show a picture"
    )
    missing = _missing_terms(block, IMAGE_TERMS)
    assert not missing, (
        f"the images-in-a-reply block must state the path rule, both forms, the TUI half and the "
        f"base64 prohibition with its reason; missing: {missing}"
    )
    assert text.count(IMAGE_HEADING) == 1, (
        f"the rule must be stated once; found {text.count(IMAGE_HEADING)} copies — a duplication "
        "costs prompt and leaves two copies free to drift apart"
    )


def test_the_rendered_prompt_carries_the_image_rule_verbatim() -> None:
    """The artifact the reader gets, measured where `system.j2` is a *template*."""
    rendered = _render_system_prompt()
    template_block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), IMAGE_HEADING)
    render_block = _block_after(rendered, IMAGE_HEADING)
    assert render_block, (
        "the rendered session prompt must carry the images-in-a-reply block: it is present in "
        "system.j2 but no render a session receives contains it"
    )
    assert template_block in rendered, (
        "the rendered prompt carries a *different* images-in-a-reply block than the template does "
        "— something in the render path edited it, and the wording a session reads is then not "
        "the wording this repository reviewed"
    )
    missing = _missing_terms(render_block, IMAGE_TERMS)
    assert not missing, f"the rendered images-in-a-reply block is missing terms: {missing}"
    assert rendered.count(IMAGE_HEADING) == 1, (
        "the rendered prompt must state the rule once; found "
        f"{rendered.count(IMAGE_HEADING)} copies"
    )


def test_the_prohibition_and_the_way_round_it_are_pinned_together() -> None:
    """Both halves, because either one alone is a defect in the other direction.

    The prohibition without the two path forms tells a session not to inline base64 while naming
    nothing that works instead, which is the state this block exists to end. The path forms without
    the prohibition leave the habit that floods a transcript legal. Asserting the pair means a
    rewrite that keeps the pleasant half fails here with the reason rather than passing as a
    wording change.
    """
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), IMAGE_HEADING)
    assert "Never inline the picture as a `data:` URI" in block, (
        "the block no longer forbids inlining the picture as a `data:` URI — the habit that turns "
        "one screenshot into hundreds of thousands of characters. It names the `data:` form rather "
        "than the encoding by name because `tests/test_system_prompt_structure.py` holds the "
        "template to zero digits, and the encoding's name carries two of them"
    )
    assert "/Users/me/shot.png" in block and "file:///Users/me/shot.png" in block, (
        "the block must show both accepted spellings; a prohibition whose alternative is unstated "
        "leaves the reader with nothing that works"
    )
    assert "The TUI has no image surface" in block, (
        "the block no longer says the TUI renders no image — a session that assumes otherwise "
        "writes a reply whose only content is invisible to half its readers"
    )


def test_the_scan_reports_absence() -> None:
    """The instrument's control: text without the block must read as missing.

    A check that reports the healthy answer whatever it is given is not a check. This feeds the
    extractors a prompt with no such block and requires the absence to be visible, a prompt with
    the heading twice and requires the duplication to be visible, and a prompt carrying one term
    and requires exactly the other terms to be reported missing.
    """
    sample = "# A prompt with no images block\n\n- Must push\n"
    assert _block_after(sample, IMAGE_HEADING) == ""
    assert _missing_terms(sample, IMAGE_TERMS) == list(IMAGE_TERMS)

    doubled = f"{IMAGE_HEADING}\n\n{IMAGE_HEADING}\n\n- Must push\n"
    assert doubled.count(IMAGE_HEADING) == 2

    partial = f"{IMAGE_HEADING}\n\nNever inline the picture as a `data:` URI\n"
    assert _block_after(partial, IMAGE_HEADING).strip().startswith("Never inline the picture")
    assert "Never inline the picture as a `data:` URI" not in _missing_terms(partial, IMAGE_TERMS)
    assert len(_missing_terms(partial, IMAGE_TERMS)) == len(IMAGE_TERMS) - 1
