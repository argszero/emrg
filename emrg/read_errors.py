"""The one home for "this reader could not read the file".

Reading a state file — `config.toml`, `projects.yml`, a session's `meta.json`, the
sessions index, an evolution log — can fail in three shapes, and they are **one
class** because a caller cannot tell them apart by asking "could I read it?":

* **`OSError`** — no such file, a directory where the file should be, a permission
  bit. (`FileNotFoundError` and `IsADirectoryError` are members.)
* **the format's own parse error** — `json.JSONDecodeError`, `yaml.YAMLError`,
  `tomllib.TOMLDecodeError`. Not an `OSError`.
* **`UnicodeDecodeError`** — bytes that are not valid UTF-8. It is a
  **`ValueError`, not an `OSError`**, which is why a tuple spelled
  `(OSError, json.JSONDecodeError)` — which reads like "every way reading this file
  can fail" — catches two of the three and lets this one escape.

That last sentence is not a theory; it is where this module came from. Measured
2026-10-03 over three cycles (`cyc20261003-211427`, `-213507`, `-215322`), the same
miss was found **twenty times**, in three spellings, by three independent sweeps:

    family                          sites   what the third shape did instead
    emrg/config.py (tomllib)            2   raised where its siblings returned defaults
    projects.yml / tasks.yml (yaml)     8   one shape of twelve different answers -
                                            see `YAML_READ_ERRORS`, which kept the table
    json state files (json)            10   a declared contract broken, exactly:
                                            "never raises", "None if corrupt",
                                            "corrupt meta.json, skipping"

The twenty were not twenty decisions. The correct spelling was already in the tree,
in three readers that caught the class whole; the tuple was a spelling that drifted.
So the rule is not "add a word to a tuple" — it is the invariant an automated check
can hold: **a `try` that reads a file's text and parses it must name a decode error**,
or one shape of an unreadable file gets a different answer from the other two.

`tests/test_a_file_read_guard_names_its_three_shapes.py` is that check. It resolves
the constants below by name, so naming one of them counts as naming what it holds.

This module is a **leaf**: it imports nothing from `emrg` and only stdlib, so both
`emrg/` (core) and `emrg/server/` (the daemon) may import it downward. `json` lives
here rather than beside its readers because its readers sit on both sides of that
line, and a constant two layers need has to be importable by both.
"""

from __future__ import annotations

import json

#: The half of the class that belongs to the **read** rather than to the format.
#: A file the process cannot open, and a file whose bytes it cannot decode, are the
#: same failure to a caller: no content to interpret. Every format-specific constant
#: below is this plus that format's parser error, so the read half has one spelling.
FILE_READ_ERRORS = (OSError, UnicodeDecodeError)

#: `FILE_READ_ERRORS` plus "the bytes are not JSON".
JSON_READ_ERRORS = (*FILE_READ_ERRORS, json.JSONDecodeError)
