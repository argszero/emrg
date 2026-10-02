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
A property the tool's schema declares as `"string"`, **present** in a call with
a value that is neither a string nor a JSON null, is refused: the sentence names
the property and the shape that arrived.

Two deliberate boundaries, each with a measured reason:

* **Null is not a wrong string.** A model that emits `{"command": "ls",
  "workdir": null}` means "I did not set this", and every tool's optional-argument
  read is an `or` chain that already treats null as absent (`arguments.get("workspace")
  or arguments.get("workdir") or os.getcwd()`). Refusing it would invent a
  failure for a call that runs today. So null follows the schema's own
  `required` list: **required + null is a wrong value** (there is nothing else it
  can mean — `read` with `file_path: null` raises today), **optional + null is
  absent**, as it is everywhere else.
* **Nothing but strings is checked.** `start_line: "3"` is accepted today
  (`int("3")`), and a rule that turned that into a refusal would be a behaviour
  change dressed as a defect fix. Integers, booleans and enums are the tools'
  own business; the leak this module closes is the one measured above.

An unknown tool and a non-dict argument are answered `None` — not because they
are fine, but because they are other rules with their own readers: the loop
already names the available tools for the first, and the arguments-object rule
is its own defect. A rule that answered for them would be a second, disagreeing
opinion about something this module cannot see.
"""

from __future__ import annotations

from emrg.server.tool_types import ToolDefinition, shape_of


def argument_shape_problem(
    definition: ToolDefinition | None, arguments: object,
) -> str | None:
    """Why this call's arguments do not fit the tool's own schema, or None.

    :param definition: the tool's definition, or ``None`` for a name no tool
        answers to (not this rule's question — the loop names what it knows).
    :param arguments: the parsed call arguments, whatever shape they arrived in.
    :returns: a refusal sentence naming the property and the shape it received,
        or ``None`` when every string property the schema declares is a string.
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
        if not isinstance(spec, dict) or spec.get("type") != "string":
            continue
        if name not in arguments:
            continue
        value = arguments[name]
        if isinstance(value, str):
            continue
        if value is None and name not in required_names:
            # Optional and null: ``absent``, the reading every tool's own
            # optional-argument chain gives it. See the module docstring.
            continue
        return f"the `{name}` argument must be a string; got {shape_of(value)}"
    return None
