"""A directory listing is a read result, so it takes the same bound a file read does.

`ReadTool` bounds a file three ways — ``MAX_READ_SIZE`` bytes, ``DEFAULT_MAX_LINES``
lines by default, ``MAX_LINES`` for an explicit call — and its module docstring states
the reason ("Default limits prevent oversized tool results from consuming excessive
tokens in the LLM context"). The directory branch had **none** of them. Measured on
master `bc114ab9`, 2026-10-02, on the same call:

    a 5,000-line FILE      -> 1,002 lines, ending `truncated at start_line=1001 ... total 5000 lines`
    a 5,000-entry DIRECTORY -> 5,002 lines / 85,098 characters, no notice, error=False

The trees a caller actually lands in are the ones where this matters — ``node_modules``,
``dist``, ``.git/objects`` — so the unbounded answer is the ordinary case, not the
corner. These tests pin the bound, the notice, and the arithmetic in the notice, and
they pin that the small case did not change.
"""

import asyncio
import re

import pytest

from emrg.tools.read_tool import DEFAULT_MAX_LINES, ReadTool


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def small_dir(tmp_path):
    """A directory that fits: two files, one subdirectory, one hidden file."""
    d = tmp_path / "small"
    d.mkdir()
    (d / "a.txt").write_text("a\n", encoding="utf-8")
    (d / "b.txt").write_text("b\n", encoding="utf-8")
    (d / "sub").mkdir()
    (d / ".hidden").write_text("h\n", encoding="utf-8")
    return d


def _entries(d, count):
    d.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (d / f"file_{i:05d}.txt").write_text("x", encoding="utf-8")
    return d


def _listing_lines(content):
    """The entry lines of a listing — the ``  name`` rows, not the header or notice."""
    return [ln for ln in content.split("\n") if ln.startswith("  ")]


# The notice's own arithmetic, read back out of the message rather than recomputed.
_REMAINDER_RE = re.compile(r"\[(\d+) more entries not shown")
_CAP_RE = re.compile(r"capped at (\d+) entries")


class TestABigDirectoryIsBounded:
    def test_the_result_never_grows_with_the_directory(self, tmp_path):
        """The bound, over sizes — not the one size that was reported.

        A cap asserted on a single input is satisfied by any cap that happens to be
        larger; the reading that means something is that the answer stops growing.
        """
        d = tmp_path / "many"
        d.mkdir()
        sizes = (0, 1, DEFAULT_MAX_LINES - 1, DEFAULT_MAX_LINES, DEFAULT_MAX_LINES + 1, 3000)
        biggest = 0
        for size in sizes:
            _entries(d, size - len(list(d.iterdir())))
            result = _run(ReadTool().execute({"file_path": str(d), "intent": "probe"}))
            assert not result.error, (size, result.content[:200])
            shown = len(_listing_lines(result.content))
            assert shown <= DEFAULT_MAX_LINES, (
                f"{size} entries -> {shown} lines shown, over the {DEFAULT_MAX_LINES} bound"
            )
            biggest = max(biggest, shown)
        assert biggest == DEFAULT_MAX_LINES, (
            f"the listing never reached its own bound (max seen {biggest}) — "
            "these sizes should have exercised it"
        )

    def test_the_notice_names_the_real_remainder(self, tmp_path):
        """The number in the notice is a fact about the directory, read back out."""
        d = tmp_path / "many"
        d.mkdir()
        total = DEFAULT_MAX_LINES + 137
        _entries(d, total)
        result = _run(ReadTool().execute({"file_path": str(d), "intent": "probe"}))

        remainder = _REMAINDER_RE.search(result.content)
        assert remainder, f"no notice for {total} entries: {result.content[-200:]!r}"
        assert int(remainder.group(1)) == total - DEFAULT_MAX_LINES

        cap = _CAP_RE.search(result.content)
        assert cap and int(cap.group(1)) == DEFAULT_MAX_LINES

        # The two numbers must add up to the directory, not merely look plausible.
        assert len(_listing_lines(result.content)) + int(remainder.group(1)) == total

    def test_the_notice_says_what_to_do_instead(self, tmp_path):
        """A bound with no remedy leaves the caller stuck, which is the point of saying it."""
        d = _entries(tmp_path / "many", DEFAULT_MAX_LINES + 10)
        content = _run(ReadTool().execute({"file_path": str(d), "intent": "probe"})).content
        assert "glob" in content, f"the notice offers no alternative: {content[-200:]!r}"


class TestTheOrdinaryListingIsUnchanged:
    def test_a_small_directory_lists_everything_with_no_notice(self, small_dir):
        content = _run(
            ReadTool().execute({"file_path": str(small_dir), "intent": "probe"})
        ).content
        assert "Directory listing for" in content
        for name in ("a.txt", "b.txt", ".hidden"):
            assert name in content, content
        assert "sub/" in content, f"a subdirectory loses its marker: {content!r}"
        assert "not shown" not in content, f"a listing that fits got a notice: {content!r}"

    def test_a_directory_at_the_bound_is_not_annotated(self, tmp_path):
        """Exactly ``DEFAULT_MAX_LINES`` entries is a complete answer, not a truncated one."""
        d = _entries(tmp_path / "exact", DEFAULT_MAX_LINES)
        content = _run(ReadTool().execute({"file_path": str(d), "intent": "probe"})).content
        assert len(_listing_lines(content)) == DEFAULT_MAX_LINES
        assert "not shown" not in content, content

    def test_a_directory_one_over_the_bound_is(self, tmp_path):
        """The boundary from the other side: one past it is not."""
        d = _entries(tmp_path / "over", DEFAULT_MAX_LINES + 1)
        content = _run(ReadTool().execute({"file_path": str(d), "intent": "probe"})).content
        assert len(_listing_lines(content)) == DEFAULT_MAX_LINES
        assert _REMAINDER_RE.search(content), content


class TestTheFilePathIsUntouched:
    def test_a_large_file_is_still_bounded_the_way_it_was(self, tmp_path):
        """The control: this change is about the directory branch and nothing else."""
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line {i}" for i in range(5000)), encoding="utf-8")
        content = _run(ReadTool().execute({"file_path": str(f), "intent": "probe"})).content
        assert content.count("\n") <= DEFAULT_MAX_LINES + 2
        assert "truncated at start_line=1001" in content, content[-120:]

    def test_a_small_file_is_still_exact(self, tmp_path):
        f = tmp_path / "s.txt"
        f.write_text("one\ntwo\n", encoding="utf-8")
        content = _run(ReadTool().execute({"file_path": str(f), "intent": "probe"})).content
        assert "one" in content and "two" in content
        assert "truncated" not in content
