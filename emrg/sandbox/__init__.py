"""Process-boundary confinement for the bash tool (bash tool v2).

The package mirrors deepseek-harness's sandbox packages one for one, which is the
blueprint named in ``.emrg/designs/bash-tool-v2-design.md`` §5.1:

===============================  ==========================================
``emrg/sandbox/policy.py``       ``packages/sandbox/sandbox-policy``
``emrg/sandbox/roots.py``        ``packages/sandbox/sandbox/src/roots.ts``
``emrg/sandbox/contract.py``     ``packages/sandbox/sandbox`` (the seam)
``emrg/sandbox/providers/``      ``packages/sandbox/sandbox-local``
``emrg/tools/bash_tool_v2.py``   ``packages/shell/{bash-sandbox,tool-bash}``
===============================  ==========================================

Splitting the boundary out of one 6,600-line tool file is not a convenience: it
is the blueprint's own packaging, and the reason each layer can be tested alone.

Nothing in this package imports ``emrg.tools.bash_tool``.  The old tool file is
frozen while v2 is built beside it (delivery rules R1/R2) and disappears at P7;
until then the two share no line, and a test asserts they share no import.
"""
