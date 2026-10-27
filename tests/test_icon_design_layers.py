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
2. **One light for the fan.** The branches are all stroked with a single radial
   gradient in `userSpaceOnUse` units centred on the hub — `branchGrad` on the
   shipped file. Under the default `objectBoundingBox` units each branch resolves
   the gradient against its own bounding box, and the illumination points a
   different way per branch.
3. **Four nodes.** A fifth branch close to its neighbours shrinks to a blob
   below 128 px.
4. **The body rectangle.** 56 inset, 912 square, `rx` 206 on a 1024 canvas — the
   shape every platform clips an app icon into.

The guard is a function over SVG **text**, so both directions cost a literal
rather than a filesystem: the shipped file must come back faultless, a document
with the previous layering must be refused, and a redesign that keeps the four
properties must come back faultless too. An assertion that can only ever see the
good file proves nothing about the bad one, and one that pins the shipped
coordinates rather than the design reports every redesign as a broken design.

It reads no generated product: those are gitignored build output, and producing
them needs a renderer the test runners cannot be assumed to have.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SVG = REPO / "packaging" / "assets" / "icon.svg"

NS = "{http://www.w3.org/2000/svg}"

#: How many branches the fan has, and therefore how many nodes.
BRANCHES = 4

#: A node's **glow halo** — the one layer of a node's three that exists exactly once
#: per node, and the only one named by what it is rather than by a size. On the
#: shipped file the halo is `r="88"`, the body `r="42"` and the highlight `r="15"`,
#: so counting by radius counted the halo while calling it the body, and resizing a
#: node — a redesign that keeps four nodes — read as the fan losing all of them.
#: The bodies cannot serve this purpose: the hub has one, and the hub is not a node.
NODE_HALO_FILL = "url(#nodeGlow)"

#: The app-icon body: 1024 canvas, inset 56, so a 912 square with a 206 radius.
BODY = {"x": "56", "y": "56", "width": "912", "height": "912", "rx": "206"}


def _start_point(element: ET.Element) -> tuple[float, float] | None:
    """Where a path begins, in user units; `None` when its `d` does not say."""
    match = re.match(r"\s*M\s*(-?[\d.]+)[\s,]+(-?[\d.]+)", element.get("d") or "")
    if match is None:
        return None
    return (float(match.group(1)), float(match.group(2)))


def _centre(element: ET.Element) -> tuple[float, float] | None:
    """A shape's centre, or `None` when it carries no placeable `cx`/`cy`."""
    try:
        return (float(element.get("cx")), float(element.get("cy")))
    except (TypeError, ValueError):
        return None


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def hub_centre(elements: list[ET.Element]) -> tuple[float, float] | None:
    """The point the fan emanates from, read off the paths instead of pinned.

    The branches begin at the hub, so the start point the most stroked paths share
    *is* the hub; a blurred copy of a branch starts where its crisp original does,
    which only makes that point more common. `None` when no path declares a start,
    which the caller reports rather than supplying a centre for.

    Deriving it is the difference between pinning the design and pinning a pair of
    coordinates: a redesign that moves the hub, its highlight, its light and the
    branch origins together is still the same design, and a literal at the top of
    this file cannot tell that from a fan detached from its hub.
    """
    starts = [
        _start_point(element)
        for element in elements
        if element.tag == NS + "path" and element.get("stroke")
    ]
    counted = Counter(point for point in starts if point is not None)
    if not counted:
        return None
    return counted.most_common(1)[0][0]


def faults(svg_text: str) -> list[str]:
    """Every way `svg_text` breaks the design, in the design's own terms.

    Returns an empty list for a document that keeps all four properties — the
    shipped one, and any redesign that keeps them, which the accept arms at the end
    of this file hold it to. Parsing is the caller's problem to see: an unreadable
    document raises, because "could not measure" is not "passed".
    """
    root = ET.fromstring(svg_text)
    elements = list(root.iter())
    centre = hub_centre(elements)

    problems: list[str] = []

    # 1 + 3. The branches, in document order, and the hub's place among them.
    painted = [
        i
        for i, element in enumerate(elements)
        if element.tag in (NS + "path", NS + "circle", NS + "rect", NS + "ellipse")
    ]
    # A branch is a path that leaves the hub lit by a gradient. Neither the gradient's
    # id nor the hub's coordinates are named here: an id is a name and a coordinate is
    # where the last designer put it, while the property is where the light resolves to
    # and which point the fan leaves from.
    branches = [
        i
        for i in painted
        if centre is not None
        and elements[i].tag == NS + "path"
        and _start_point(elements[i]) == centre
        and (elements[i].get("stroke") or "").startswith("url(#")
    ]
    concentric = [
        i
        for i in painted
        if centre is not None
        and elements[i].tag == NS + "circle"
        and _centre(elements[i]) == centre
    ]
    # The hub's third layer is its specular highlight, and it is deliberately not
    # concentric: it sits up and left of the centre, the direction every node's core
    # leans (a node's body is at `(x, y)`, its core at `(x-6, y-8)`). So it is read as
    # the nearest circle beside the centre — the lean is the property, and how far it
    # leans is the design's to choose, which a maximum distance would take away.
    beside = [
        i
        for i in painted
        if centre is not None
        and elements[i].tag == NS + "circle"
        and _centre(elements[i]) is not None
        and _centre(elements[i]) != centre
    ]
    highlight = (
        min(beside, key=lambda i: _distance(_centre(elements[i]), centre))
        if beside and centre is not None
        else None
    )
    hub = concentric + ([] if highlight is None else [highlight])

    if len(branches) != BRANCHES:
        problems.append(
            f"the fan has {len(branches)} branch(es) leaving the hub lit by a "
            f"gradient, expected {BRANCHES}"
        )
    if centre is not None:
        if len(concentric) != 2:
            problems.append(
                f"the hub has {len(concentric)} layer(s) centred on the point its "
                "branches leave, expected 2: a glow halo and a body"
            )
        if highlight is None:
            problems.append(
                "the hub has no layer beside its centre, so nothing on it says which "
                "way the light comes from and the sphere reads as a flat disc"
            )
        else:
            hx, hy = _centre(elements[highlight])
            dx, dy = hx - centre[0], hy - centre[1]
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

    # 3. One node per branch. Counted by the layer that exists once per node, so a
    # node resized is still a node; a node the fan does not have still reads as one
    # the fan is missing.
    haloes = [
        element
        for element in elements
        if element.tag == NS + "circle" and element.get("fill") == NODE_HALO_FILL
    ]
    if len(haloes) != BRANCHES:
        problems.append(
            f"the fan has {len(haloes)} node halo(s) painted with {NODE_HALO_FILL}, "
            f"expected {BRANCHES}"
        )

    # 2. One light for the fan: the gradient its branches reference, in user space,
    # centred on the hub they leave.
    referenced = [
        match.group(1)
        for i in branches
        if (match := re.fullmatch(r"\s*url\(#([^)]+)\)\s*", elements[i].get("stroke") or ""))
    ]
    if branches and len(set(referenced)) != 1:
        problems.append(
            f"the fan's {len(branches)} branch(es) reference {len(set(referenced))} "
            f"different gradient(s) ({', '.join(sorted(set(referenced)))}): one light "
            "for the fan means one gradient"
        )
    elif referenced:
        wanted = referenced[0]
        gradient = next(
            (
                element
                for element in elements
                if element.tag == NS + "radialGradient" and element.get("id") == wanted
            ),
            None,
        )
        if gradient is None:
            problems.append(
                f"the branches are stroked with url(#{wanted}) and no radialGradient "
                "declares that id, so the light they carry cannot be read"
            )
        else:
            if gradient.get("gradientUnits") != "userSpaceOnUse":
                problems.append(
                    f"{wanted} is not in userSpaceOnUse units: under the default "
                    "objectBoundingBox each branch resolves the light against its own "
                    "box, so the fan is lit from a different direction per branch"
                )
            lit = _centre(gradient)
            if lit is None:
                problems.append(
                    f"{wanted} has no cx/cy that read as a centre, so where the fan "
                    "is lit from cannot be measured"
                )
            elif centre is not None and lit != centre:
                problems.append(
                    f"{wanted} is centred on ({lit[0]:g}, {lit[1]:g}) and the branches "
                    f"leave the hub at ({centre[0]:g}, {centre[1]:g}): the light must "
                    "come from the hub, or the fan is lit from beside it"
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


# --- the accept direction: a redesign that keeps the properties --------------------------------
#
# The three arms above prove the guard refuses what it means to refuse. They cannot prove it
# accepts what it means to accept, and each of these three was refused by an earlier version of
# this file: it counted a radius, it pinned a hub's coordinates, and it named the gradient. A
# guard that reports every redesign as a broken design is measuring the shipped file, not the
# design, and it will be answered by loosening the guard until it measures nothing.


def test_the_check_accepts_a_fan_whose_nodes_were_resized() -> None:
    """A halo's size is a size, not the node: four haloes stay four nodes at 92.

    Counting `r="88"` refused a fan with four branches, four nodes, one light and an
    untouched body rect, and its message named the radius instead of the design. What
    makes four nodes four is that four of them are painted at all.
    """
    text = SVG.read_text(encoding="utf-8")
    resized = text.replace('r="88"', 'r="92"')
    assert resized != text, "the substitution did not apply, so this arm measures nothing"
    found = resized.count('r="92"')
    assert found == BRANCHES, f"expected {BRANCHES} resized haloes, found {found}"

    problems = faults(resized)
    assert problems == [], (
        "the check refuses a fan whose nodes were resized, though every property holds:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )


def test_the_check_accepts_a_hub_that_moved_with_its_light() -> None:
    """The hub, its highlight, its light and the branch origins moved together: one design.

    Moved down 40 units as a set, the sphere is the same sphere in a different place.
    Four literals had to agree for the shipped file to pass, so a coherent move broke all
    four at once while breaking nothing a reader of the icon could see.
    """
    text = SVG.read_text(encoding="utf-8")
    moved = text.replace("M 512 650 ", "M 512 690 ")
    moved = moved.replace('cx="512" cy="650"', 'cx="512" cy="690"')
    moved = moved.replace('cx="505" cy="642"', 'cx="505" cy="682"')

    assert moved != text, "the substitution did not apply, so this arm measures nothing"
    at_new_centre = moved.count('cy="690"')
    assert at_new_centre == 3, (
        "the hub, its body and the gradient must all sit at the new centre, found "
        f"{at_new_centre} of 3"
    )
    assert "M 512 650 " not in moved, "a branch still leaves the old centre"

    problems = faults(moved)
    assert problems == [], (
        "the check refuses a hub that moved together with its light:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )


def test_the_check_accepts_a_fan_lit_by_a_renamed_gradient() -> None:
    """`branchGrad` is a name; the property is user-space units centred on the hub.

    Renamed everywhere it appears — the gradient and every stroke that references it — the
    light is the same light. Pinning the id made the name the requirement.
    """
    text = SVG.read_text(encoding="utf-8")
    renamed = text.replace("branchGrad", "fanLight")
    assert renamed != text and "branchGrad" not in renamed, (
        "the substitution did not apply, so this arm measures nothing"
    )
    assert renamed.count("url(#fanLight)") == BRANCHES

    problems = faults(renamed)
    assert problems == [], (
        "the check refuses a fan whose one gradient was renamed:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )
