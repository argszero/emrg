"""A tool argument that is not the type its own schema names, refused by name.

The class this exists for
------------------------
Every tool hands the model a JSON Schema (`ToolDefinition.parameters`) and then
reads the parsed call as if the model had obeyed it. Measured 2026-10-03 on this
host, with each tool's `execute()` called directly and the argument the schema
declares as a **string** sent as a list:

======  ==========================  ==============================
tool    argument                    what `execute()` did
======  ==========================  ==============================
bash    `command: ["echo","hi"]`    `TypeError` out of the command scan
read    `file_path: ["a"]`          `TypeError` out of `Path(...)`
write   `file_path: ["a"]`          `TypeError` out of `Path(...)`
edit    `file_path: ["a"]`          `TypeError` out of `Path(...)`
glob    `pattern: ["*.py"]`         `TypeError` out of `pathlib`
grep    `pattern: ["x"]`            `TypeError: unhashable type: 'list'`
======  ==========================  ==============================

and, measured the same day (`cyc20261003-063623`), the *boolean* ones — the
other half of this rule, and the only half where the wrong value is **not** a
crash but a silent act:

======  ================================  ==============================
tool    argument                          what `execute()` did
======  ================================  ==============================
edit    `replace_all: "false"`            **rewrote every occurrence** and
                                          answered `Made 2 replacements`
edit    `replace_all: "no"`               same
grep    `ignore_case: "false"`            searched case-insensitively
======  ================================  ==============================

A boolean has no type error to raise: the tool reads it for truthiness, so **the
caller's own word for *no* is read as *yes***. `""` and `0` happen to be falsy and
`"true"` happens to be true, which is why the shape survived review — but
`"false"` is neither, and for `replace_all` the consequence is a file whose other
occurrences the caller explicitly asked to have left intact. This is the worse
half: the string leak prints Python at the caller, and this one writes.

Seven tools, seven leaks, one cause: the loop's only guard is
`except Exception` around `tool.execute(...)`, so the model is answered

    Tool execution error: argument should be a str or an os.PathLike object
    where __fspath__ returns a str, not 'list'

— a message that names neither the tool, the argument, nor the call. The
repository already settled this spelling twice: `_images_fault` refuses a bad
`images` **by name** (field and element), and the guard tools answer with
"expected a list of PRs, got dict". The tool boundary is where the model's own
schema is the contract, so that is where the refusal belongs — once, for every
tool, present and future, instead of seven times.

The rule
--------
A property the tool's schema declares as `"string"` or `"boolean"`, **present**
in a call with a value of neither that type nor a JSON null, is refused: the
sentence names the property and the shape that arrived. Two declared types, and
no third — each is here because it was measured, and each has a reader that
silently does the wrong thing without it.

A boolean is refused rather than interpreted, and that is the whole point of
covering it. Python would happily read `"no"` as `True`, so there is no error to
surface; the only way to be right is to *decline to guess*, because the text a
caller writes for false is not decidable from the schema (`"false"`, `"no"`,
`"0"`, `"off"`, `""` — and `""` already means false today while `"false"` does
not).

Two deliberate boundaries, each with a measured reason:

* **Null is not a wrong string.** A model that emits `{"command": "ls",
  "workdir": null}` means "I did not set this", and every tool's optional-argument
  read is an `or` chain that already treats null as absent (`arguments.get("workspace")
  or arguments.get("workdir") or os.getcwd()`). Refusing it would invent a
  failure for a call that runs today. So null follows the schema's own
  `required` list: **required + null is a wrong value** (there is nothing else it
  can mean — `read` with `file_path: null` raises today), **optional + null is
  absent**, as it is everywhere else.
* **Integers and enums stay the tools' own business.** `start_line: "3"` is
  accepted today (`int("3")`) and a rule that turned that into a refusal would be
  a behaviour change dressed as a defect fix. The count parameters' *domain* is
  its own rule with its own home (`emrg.tools.base.as_count`), which is where a
  nonsense count takes the documented default rather than raising. The two types
  read here are the two whose leaks were measured — a string one, and the boolean
  one above, whose "accepted today" case (`ignore_case: "true"`, true by luck)
  is a *refusal* now: the alternative is a rule that keeps `"true"` working by
  the same accident that makes `"false"` destructive, and a loud refusal of a
  value the schema does not declare is the smaller failure.

An unknown tool and a non-dict argument are answered `None` — not because they
are fine, but because they are other rules with their own readers: the loop
already names the available tools for the first, and the arguments-object rule
is its own defect. A rule that answered for them would be a second, disagreeing
opinion about something this module cannot see.
"""

from __future__ import annotations

from emrg.server.tool_types import ToolDefinition, shape_of


#: The declared types this rule reads, each with the word its refusal uses and
#: the Python type a value of it is. Two entries, because two were measured; the
#: module docstring carries both tables.
#:
#: ``bool`` is checked with ``isinstance`` and **not** by ``type(x) is bool``,
#: which is the same thing for every JSON value that can arrive here — a subclass
#: of ``bool`` cannot be built and no decoder produces one. Spelled this way so
#: the check reads like the string one beside it.
_DECLARED: dict[str, tuple[str, type]] = {
    "string": ("string", str),
    "boolean": ("boolean", bool),
}


def argument_shape_problem(
    definition: ToolDefinition | None, arguments: object,
) -> str | None:
    """Why this call's arguments do not fit the tool's own schema, or None.

    :param definition: the tool's definition, or ``None`` for a name no tool
        answers to (not this rule's question — the loop names what it knows).
    :param arguments: the parsed call arguments, whatever shape they arrived in.
    :returns: a refusal sentence naming the property and the shape it received,
        or ``None`` when every string and boolean property the schema declares
        holds a value of that type.
    """
    if definition is None or not isinstance(arguments, dict):
        return None
    parameters = definition.parameters
    if not isinstance(parameters, dict):
        return None
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return None
    required = parameters.get("required")
    required_names = set(required) if isinstance(required, list) else set()
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        declared = _DECLARED.get(spec.get("type"))
        if declared is None:
            continue
        word, python_type = declared
        if name not in arguments:
            continue
        value = arguments[name]
        if isinstance(value, python_type):
            continue
        if value is None and name not in required_names:
            # Optional and null: ``absent``, the reading every tool's own
            # optional-argument chain gives it. See the module docstring.
            continue
        return f"the `{name}` argument must be a {word}; got {shape_of(value)}"
    return None
