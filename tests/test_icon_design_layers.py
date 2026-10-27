"""The icon's design source must keep the properties nothing renders for us.

`tests/test_gen_assets_keeps_the_icon_transparent.py` guards the *products* the
generator emits, and reads the generator's script; its only statement about the
design source is `MARGIN = 56`. So every property `packaging/assets/icon.svg`
carries on its own went unmeasured, and a revert of any of them passes every leg.

Four properties, each one a defect this project has already shipped or nearly
shipped:

1. **Layer order at the hub.** The hub's circles are painted *after* the
   branches. Painted before them, the branch caps converge on the hub's face and
   each branch's own gradient starts at its darkest stop there, which reads as a
   dark blotch on the sphere.
2. **One light for the fan.** `branchGrad` is a `userSpaceOnUse` radial gradient
   centred on the hub. Under the default `objectBoundingBox` units each branch
   resolves the gradient against its own bounding box, and the illumination
   points five different ways.
3. **Four nodes.** A fifth branch close to its neighbours shrinks to a blob
   below 128 px.
4. **The body rectangle.** 56 inset, 912 square, `rx` 206 on a 1024 canvas — the
   shape every platform clips an app icon into.

The guard is a function over SVG **text**, so the refuse direction costs a
literal rather than a filesystem: the shipped file must come back faultless, and
a document with the previous layering must be refused. An assertion that can only
ever see the good file proves nothing about the bad one.

It reads no generated product: those are gitignored build output, and producing
them needs a renderer the test runners cannot be assumed to have.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SVG = REPO / "packaging" / "assets" / "icon.svg"

NS = "{http://www.w3.org/2000/svg}"

#: Every branch's stroke is this gradient; the count is the fan's branch count.
BRANCH_STROKE = "url(#branchGrad)"
BRANCHES = 4

#: A node's body radius. The card is the two other layers of each node (glow halo,
#: bright core), so counting the bodies counts the nodes.
NODE_BODY_R = "88"

#: The hub is centred here; its glow halo and body share that centre exactly.
HUB = ("512", "650")

#: The hub's third layer is its specular highlight, and it is deliberately **not**
#: concentric: it sits up and left of the centre, the direction every node's core
#: leans (a node's body is at `(x, y)` and its core at `(x-6, y-8)`). So "a layer
#: at the hub" means within this many user units of the centre, not on it — on the
#: shipped file the highlight is 10.6 units out.
HUB_HIGHLIGHT_OFFSET_MAX = 16.0

#: The app-icon body: 1024 canvas, inset 56, so a 912 square with a 206 radius.
BODY = {"x": "56", "y": "56", "width": "912", "height": "912", "rx": "206"}


def distance_from_hub(element: ET.Element) -> float:
    """How far a shape's centre sits from the hub's, in user units.

    `nan` for anything whose `cx`/`cy` is not a number, which every comparison
    rejects: a circle that cannot be placed is not one of the hub's layers.
    """
    try:
        dx = float(element.get("cx", "nan")) - float(HUB[0])
        dy = float(element.get("cy", "nan")) - float(HUB[1])
    except (TypeError, ValueError):
        return float("nan")
    return (dx * dx + dy * dy) ** 0.5


def faults(svg_text: str) -> list[str]:
    """Every way `svg_text` breaks the design, in the design's own terms.

    Returns an empty list for a document that keeps all four properties. Parsing
    is the caller's problem to see: an unreadable document raises, because "could
    not measure" is not "passed".
    """
    root = ET.fromstring(svg_text)
    elements = list(root.iter())

    problems: list[str] = []

    # 1 + 3. The branches, in document order, and the hub's place among them.
    painted = [
        i
        for i, element in enumerate(elements)
        if element.tag in (NS + "path", NS + "circle", NS + "rect", NS + "ellipse")
    ]
    branches = [
        i
        for i in painted
        if elements[i].tag == NS + "path" and elements[i].get("stroke") == BRANCH_STROKE
    ]
    hub = [
        i
        for i in painted
        if elements[i].tag == NS + "circle"
        and distance_from_hub(elements[i]) <= HUB_HIGHLIGHT_OFFSET_MAX
    ]

    if len(branches) != BRANCHES:
        problems.append(
            f"the fan has {len(branches)} branch(es) stroked with {BRANCH_STROKE}, "
            f"expected {BRANCHES}"
        )
    if len(hub) != 3:
        problems.append(
            f"the hub has {len(hub)} layer(s) within {HUB_HIGHLIGHT_OFFSET_MAX:g} units "
            f"of cx={HUB[0]} cy={HUB[1]}, expected 3 (glow halo, body, bright core)"
        )
    else:
        # Two of the three sit on the centre; the third is the highlight, and it
        # must lean up and left like every node's, or the hub reads as a flat disc.
        concentric = [i for i in hub if distance_from_hub(elements[i]) == 0.0]
        offset = [i for i in hub if i not in concentric]
        if len(concentric) != 2 or len(offset) != 1:
            problems.append(
                f"the hub has {len(concentric)} concentric layer(s) and "
                f"{len(offset)} offset one(s), expected 2 and 1: a halo and a body on "
                "the centre, and one specular highlight beside it"
            )
        else:
            core = elements[offset[0]]
            dx = float(core.get("cx")) - float(HUB[0])
            dy = float(core.get("cy")) - float(HUB[1])
            if not (dx < 0 and dy < 0):
                problems.append(
                    f"the hub's bright core is offset ({dx:+g}, {dy:+g}) from the "
                    "centre; it must sit up and left of it, the direction every "
                    "node's core leans, or the sphere reads as a flat disc"
                )
    if not branches or not hub:
        problems.append(
            "no branch and hub to order: the layer-order property cannot be read "
            "without both, so this document is not the icon this test knows"
        )
    elif min(hub) < max(branches):
        problems.append(
            "the hub is painted before the branches: its circles must come after "
            f"every branch (first hub layer at position {min(hub)}, last branch at "
            f"{max(branches)}), or the branch caps smear across its face"
        )

    nodes = [
        element
        for element in elements
        if element.tag == NS + "circle" and element.get("r") == NODE_BODY_R
    ]
    if len(nodes) != BRANCHES:
        problems.append(
            f"the fan has {len(nodes)} node(s) of radius {NODE_BODY_R}, expected {BRANCHES}"
        )

    # 2. One light, in user space, centred on the hub.
    match = re.search(r"<radialGradient\b[^>]*\bid=\"branchGrad\"[^>]*>", svg_text)
    if match is None:
        problems.append("no radialGradient declares id=\"branchGrad\"")
    else:
        head = match.group(0)
        if "gradientUnits=\"userSpaceOnUse\"" not in head:
            problems.append(
                "branchGrad is not in userSpaceOnUse units: under the default "
                "objectBoundingBox each branch resolves the light against its own "
                "box, so the fan is lit from a different direction per branch"
            )
        if f"cx=\"{HUB[0]}\"" not in head or f"cy=\"{HUB[1]}\"" not in head:
            problems.append(
                f"branchGrad is not centred on the hub (cx={HUB[0]} cy={HUB[1]})"
            )

    # 4. The app-icon body every platform clips against.
    rects = [element for element in elements if element.tag == NS + "rect"]
    if len(rects) != 1:
        problems.append(f"expected exactly one body rect, found {len(rects)}")
    else:
        for name, value in BODY.items():
            if rects[0].get(name) != value:
                problems.append(
                    f"the body rect's {name} is {rects[0].get(name)!r}, expected {value!r}"
                )

    return problems


def test_the_shipped_icon_keeps_every_designed_property() -> None:
    """The file that ships is the file these four properties describe."""
    problems = faults(SVG.read_text(encoding="utf-8"))
    assert problems == [], "the shipped icon.svg breaks its own design:\n" + "\n".join(
        f"  - {problem}" for problem in problems
    )


def test_the_check_refuses_the_layering_it_replaced() -> None:
    """Below the branches, and five of them: refused, and refused for both reasons.

    The document is synthetic on purpose. The previous design is not in this tree
    any more, so a refusal proved against a missing file would measure nothing;
    this one carries the two defects in the smallest document that can hold them.
    """
    old_layering = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <defs>
    <radialGradient id="branchGrad" cx="0.5" cy="0.5" r="0.5"/>
  </defs>
  <rect x="56" y="56" width="912" height="912" rx="206" fill="#111c16"/>
  <circle cx="512" cy="650" r="150"/>
  <circle cx="512" cy="650" r="58"/>
  <circle cx="505" cy="642" r="20"/>
  <path d="M 512 650 C 436 618 362 568 316 508" stroke="url(#branchGrad)"/>
  <path d="M 512 650 C 470 576 448 492 444 414" stroke="url(#branchGrad)"/>
  <path d="M 512 650 C 512 570 512 490 512 410" stroke="url(#branchGrad)"/>
  <path d="M 512 650 C 554 576 576 492 580 414" stroke="url(#branchGrad)"/>
  <path d="M 512 650 C 588 618 662 568 708 508" stroke="url(#branchGrad)"/>
  <circle cx="316" cy="508" r="88"/>
  <circle cx="444" cy="414" r="88"/>
  <circle cx="512" cy="410" r="88"/>
  <circle cx="580" cy="414" r="88"/>
  <circle cx="708" cy="508" r="88"/>
</svg>"""

    problems = faults(old_layering)

    assert any("painted before the branches" in problem for problem in problems), (
        "the check accepted the hub painted under the branches; it reports:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )
    assert any("5 branch(es)" in problem for problem in problems), (
        "the check accepted the five-branch fan; it reports:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )
    assert any("userSpaceOnUse" in problem for problem in problems), (
        "the check accepted a branchGrad in objectBoundingBox units; it reports:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )


def test_the_check_refuses_a_body_rect_that_moved() -> None:
    """A whole-file replacement is where the 56/912/206 body quietly moves.

    Every product is clipped to this rectangle, so a shifted square is invisible
    to the transparency guards and visible to every user's Dock.
    """
    moved = SVG.read_text(encoding="utf-8").replace(
        'x="56" y="56" width="912" height="912" rx="206"',
        'x="64" y="56" width="896" height="912" rx="206"',
        1,
    )
    assert moved != SVG.read_text(encoding="utf-8"), (
        "the substitution did not apply, so this arm measures nothing"
    )

    problems = faults(moved)
    assert any("x is '64'" in problem for problem in problems), (
        "the check accepted a body rect that moved; it reports:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )


def test_the_check_refuses_a_highlight_that_leans_the_wrong_way() -> None:
    """The hub's bright core is where the light comes from, and it leans up-left.

    Mirroring it across the centre says the light comes from below right, and the
    sphere then reads as a flat disc lit from nowhere the branches agree with. The
    shipped file carries the one substitution, so a fault reported here can only
    be the lean.
    """
    mirrored = SVG.read_text(encoding="utf-8")
    bent = mirrored.replace('cx="505" cy="642"', 'cx="519" cy="658"', 1)
    assert bent != mirrored, "the substitution did not apply, so this arm measures nothing"

    problems = faults(bent)
    assert len(problems) == 1, (
        "the check reports more than the lean, so this arm cannot attribute it:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )
    assert "up and left" in problems[0], (
        f"the check reported {problems[0]!r}, which does not name the lean"
    )
