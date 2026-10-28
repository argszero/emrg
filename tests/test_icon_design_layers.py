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
3. **Four nodes, one per branch.** A fifth branch close to its neighbours shrinks
   to a blob below 128 px; a branch that arrives bare, or a node beside the fan,
   breaks the same count. Which layer is a node's is read from the document — the
   largest circle where a branch arrives — so no id and no size is fixed here.
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

#: No constant names a node, because a node has no name in the design: it is a place
#: a branch arrives. Counting one named its layer's gradient id, so a fan whose
#: per-node layer was renamed coherently — the gradient and every `fill` that uses
#: it, nothing else touched — was refused as having no nodes at all. That is the
#: same fault as counting a radius, one constant over: an id is a name, and the
#: property is that each branch ends in a node shaped like the others.

#: The app-icon body: 1024 canvas, inset 56, so a 912 square with a 206 radius.
BODY = {"x": "56", "y": "56", "width": "912", "height": "912", "rx": "206"}


#: A path's command letters and its numbers, as the document writes them.
_PATH_TOKEN = re.compile(r"[MmZzLlHhVvCcSsQqTtAa]|-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")

#: How many numbers a command consumes before it states its own end point. An arc's
#: seven are `rx ry rot large-arc sweep x y`, so only its last two are a place.
_ARITY = {"M": 2, "L": 2, "T": 2, "H": 1, "V": 1, "Q": 4, "S": 4, "C": 6, "A": 7}

#: A paint-server reference: `url(#id)`, with the whitespace CSS allows inside it.
_URL_REFERENCE = re.compile(r"\s*url\(\s*#([^)\s]+)\s*\)\s*")


def _gradient_id(stroke: str | None) -> str | None:
    """The gradient a `stroke` references, or `None` when it references none.

    Deliberately one reading of one attribute. This file selected a branch with
    `stroke.startswith("url(#")` and read that same stroke's light with a second,
    different regex, so `stroke="url( #branchGrad )"` — which CSS strips to the same
    reference and every renderer draws identically — left the fan with no branch to
    select and no light to read. Two parsers of one attribute get one parser.
    """
    match = _URL_REFERENCE.fullmatch(stroke or "")
    return match.group(1) if match else None


def _path_ends(d: str) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Where a path begins and where it arrives, walking each command's arithmetic.

    The numbers in `d` are places only through the command that owns them: a relative
    `c` carries offsets, and an `m` at the head of a path offsets from `(0, 0)`.
    Reading the last pair anywhere in the string answered `(-196, -142)` for this
    icon's own branch curve written relative — a point nothing is painted at — and an
    `M`-only reader made a fan written `m 512 650` state no origin at all, so the hub
    was derived from the stem's foot. A guard that answers a different question than
    the one it states is not a stricter guard.
    """
    tokens = _PATH_TOKEN.findall(d or "")
    current = (0.0, 0.0)
    start: tuple[float, float] | None = None
    command = ""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.isalpha():
            command = token
            index += 1
            continue
        if not command:
            return None, None
        upper = command.upper()
        need = _ARITY.get(upper)
        if need is None or index + need > len(tokens):
            break
        values = [float(value) for value in tokens[index : index + need]]
        index += need
        if upper == "H":
            current = (current[0] + values[0] if command != upper else values[0], current[1])
        elif upper == "V":
            current = (current[0], current[1] + values[0] if command != upper else values[0])
        else:
            x, y = values[-2], values[-1]
            current = (x + current[0], y + current[1]) if command != upper else (x, y)
        if start is None:
            start = current
        if upper == "M":
            # Every pair after the first under one `M` is an implicit line to.
            command = "l" if command != upper else "L"
    return start, current


def _start_point(element: ET.Element) -> tuple[float, float] | None:
    """Where a path begins, in user units; `None` when its `d` does not say."""
    return _path_ends(element.get("d") or "")[0]


def _end_point(element: ET.Element) -> tuple[float, float] | None:
    """Where a path arrives, in user units; `None` when its `d` does not say.

    That is where the branch lands, which is where its node is.
    """
    return _path_ends(element.get("d") or "")[1]


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
        and _gradient_id(elements[i].get("stroke")) is not None
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

    # 3. One node per branch, read from where the branches arrive rather than from a
    # name or a size. Nothing here says what a node's layer is called or how big it
    # is: a node is a place a branch ends, its outermost layer is whatever is largest
    # there, and the property is that the fan's nodes are shaped alike. A renamed or
    # resized node therefore passes, while a node missing from one branch, an extra
    # one where no branch ends, or a fan of uneven nodes is refused.
    arrivals = sorted(
        {
            point
            for i in branches
            if (point := _end_point(elements[i])) is not None
        }
    )
    outermost: dict[tuple[float, float], float | None] = {}
    for point in arrivals:
        at_node = [
            float(element.get("r"))
            for element in elements
            if element.tag == NS + "circle"
            and _centre(element) == point
            and element.get("r") is not None
        ]
        outermost[point] = max(at_node) if at_node else None

    if len(arrivals) != BRANCHES:
        problems.append(
            f"the fan's branches arrive at {len(arrivals)} point(s), expected "
            f"{BRANCHES}, so it cannot carry one node per branch"
        )
    if not outermost:
        problems.append(
            "no node can be read: no branch's path says where it arrives, so the "
            "fan's node count cannot be measured from this document"
        )
    else:
        bare = [point for point, size in outermost.items() if size is None]
        if bare:
            problems.append(
                f"a branch arrives at {bare[0][0]:g}, {bare[0][1]:g} where no node is "
                "painted, so that branch ends bare"
            )
        sizes = sorted({size for size in outermost.values() if size is not None})
        if len(sizes) > 1:
            problems.append(
                f"the fan's nodes are not shaped alike: their outermost layers measure "
                f"{', '.join(f'{size:g}' for size in sizes)}, so at least one node is "
                "missing its outer layer or carries a second one"
            )
        # Every circle in the design belongs either to the hub or to one of its nodes,
        # and both give their layers the same shape: two rings concentric on a centre
        # and one bright core leaning off it. So a circle is accounted for when it
        # sits at an anchor — the hub, or a place a branch arrives — or when it is the
        # one circle leaning off that anchor. No radius takes part. A size is what the
        # four nodes happen to measure, not what makes a circle a node, and keying the
        # test to it read the hub's own resized halo as a fifth node while a fifth
        # node of any other size went unseen.
        circles = [
            element
            for element in elements
            if element.tag == NS + "circle" and _centre(element) is not None
        ]
        anchors = ([] if centre is None else [centre]) + list(outermost)
        accounted = set(anchors)
        for anchor in anchors:
            leaning = [
                point
                for point in (_centre(element) for element in circles)
                if point != anchor
            ]
            if leaning:
                accounted.add(min(leaning, key=lambda point: _distance(point, anchor)))
        stray = [
            point
            for point in (_centre(element) for element in circles)
            if point not in accounted
        ]
        if stray:
            problems.append(
                f"a circle sits at {stray[0][0]:g}, {stray[0][1]:g} where no branch "
                f"ends and no hub layer stands: {len(stray)} of them belong to neither "
                "the hub nor a node, so a fifth is painted beside the four"
            )

    # 2. One light for the fan: the gradient its branches reference, in user space,
    # centred on the hub they leave.
    referenced = [
        wanted
        for i in branches
        if (wanted := _gradient_id(elements[i].get("stroke"))) is not None
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


def test_the_check_accepts_a_fan_whose_node_layer_was_renamed() -> None:
    """A node's layer is named too, and its name is no more a property than the
    gradient's: renamed at the gradient and at every `fill` that uses it, the fan is
    the same fan with four nodes.

    This is the arm that was missing when the count was pinned to `url(#nodeGlow)`.
    The file already held that an id is a name for the light, and broke the rule one
    constant over for the nodes — a rename is a rename whichever layer carries it.
    """
    text = SVG.read_text(encoding="utf-8")
    renamed = text.replace("nodeGlow", "nodeHalo")
    assert renamed != text and "nodeGlow" not in renamed, (
        "the substitution did not apply, so this arm measures nothing"
    )
    assert renamed.count("url(#nodeHalo)") == text.count("url(#nodeGlow)"), (
        "the rename must reach the gradient and every fill that uses it, found "
        f"{renamed.count('url(#nodeHalo)')} of {text.count('url(#nodeGlow)')}"
    )

    problems = faults(renamed)
    assert problems == [], (
        "the check refuses a fan whose per-node layer was renamed, though every "
        "property holds:\n" + "\n".join(f"  - {problem}" for problem in problems)
    )


def test_the_check_accepts_a_hub_halo_as_small_as_the_nodes() -> None:
    """The hub's halo is a halo, not a node: resized to 88 it is still three layers.

    Read as "any circle of the nodes' size at no arrival", the hub's own halo became a
    fifth node the moment a designer shrank it to the radius the four nodes carry — a
    document with the layering, the light, the four nodes and the body rect all intact,
    refused for a size. Which layer belongs to whom is a place, not a measurement.
    """
    text = SVG.read_text(encoding="utf-8")
    resized = text.replace('cx="512" cy="650" r="150"', 'cx="512" cy="650" r="88"', 1)
    assert resized != text, "the substitution did not apply, so this arm measures nothing"
    assert resized.count('cx="512" cy="650" r="88"') == 1, (
        "exactly the hub's halo must carry the new radius, found "
        f"{resized.count('cx=\"512\" cy=\"650\" r=\"88\"')}"
    )

    problems = faults(resized)
    assert problems == [], (
        "the check refuses a hub whose halo was resized to the nodes' radius, though "
        "every property holds:\n" + "\n".join(f"  - {problem}" for problem in problems)
    )


def test_the_check_accepts_a_branch_written_with_a_relative_curve() -> None:
    """`c` states offsets from where it stands; where it lands is still where it lands.

    The shipped curve written relatively — `c -76 -32 -150 -82 -196 -142` from
    `M 512 650` — arrives at the same `316, 508`. Read as "the last pair in the string",
    it arrived at `-196, -142`, which reported the branch as both bare and a fifth node
    at a point no renderer draws at.
    """
    text = SVG.read_text(encoding="utf-8")
    absolute = "M 512 650 C 436 618 362 568 316 508"
    relative = "M 512 650 c -76 -32 -150 -82 -196 -142"
    curved = text.replace(absolute, relative)
    assert curved != text, "the substitution did not apply, so this arm measures nothing"
    assert curved.count(relative) == 2, (
        "a branch is drawn twice, crisp and blurred, and both must move to the relative "
        f"spelling; found {curved.count(relative)} of 2"
    )

    problems = faults(curved)
    assert problems == [], (
        "the check refuses a branch drawn with a relative curve, though it arrives "
        "where the absolute one does:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )


def test_the_check_accepts_a_fan_written_entirely_with_relative_moves() -> None:
    """`m 512 650` at the head of a path is the same origin as `M 512 650`.

    A reader that accepts only an absolute moveto made a fan written this way state no
    start at all, so the hub was derived from whatever starts were left — the stem's
    foot at `470, 950` — and the fan was then refused for being lit from a gradient the
    stem's hue carries. That is not a refusal of the design.
    """
    text = SVG.read_text(encoding="utf-8")
    rewritten = text
    for absolute, relative in (
        ("M 512 650 C 436 618 362 568 316 508", "m 512 650 c -76 -32 -150 -82 -196 -142"),
        ("M 512 650 C 470 576 448 492 444 414", "m 512 650 c -42 -74 -64 -158 -68 -236"),
        ("M 512 650 C 554 576 576 492 580 414", "m 512 650 c 42 -74 64 -158 68 -236"),
        ("M 512 650 C 588 618 662 568 708 508", "m 512 650 c 76 -32 150 -82 196 -142"),
    ):
        rewritten = rewritten.replace(absolute, relative)

    assert "M 512 650" not in rewritten, "a branch still states its origin absolutely"
    assert rewritten.count("m 512 650") == 2 * BRANCHES, (
        "every branch, crisp and blurred, must carry the relative moveto; found "
        f"{rewritten.count('m 512 650')} of {2 * BRANCHES}"
    )

    problems = faults(rewritten)
    assert problems == [], (
        "the check refuses a fan written with relative moves, though its origin, its "
        "light and its nodes are unchanged:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )


def test_the_check_accepts_whitespace_inside_the_stroke_reference() -> None:
    """`url( #branchGrad )` and `url(#branchGrad)` are the same paint server.

    CSS strips the space, so both render identically. Two readers of one `stroke` — a
    prefix test selecting branches and a stricter regex reading their light — left this
    document with no branches at all, and loosening either one alone would have left the
    other declining the same string.
    """
    text = SVG.read_text(encoding="utf-8")
    spaced = text.replace('stroke="url(#branchGrad)"', 'stroke="url( #branchGrad )"')
    assert spaced != text, "the substitution did not apply, so this arm measures nothing"
    assert spaced.count("url( #branchGrad )") == BRANCHES, (
        "every branch must carry the spaced reference, found "
        f"{spaced.count('url( #branchGrad )')} of {BRANCHES}"
    )

    problems = faults(spaced)
    assert problems == [], (
        "the check refuses a fan whose stroke reference carries whitespace, though it "
        "names the same gradient:\n" + "\n".join(f"  - {problem}" for problem in problems)
    )


def test_the_check_refuses_a_node_missing_its_outer_layer() -> None:
    """One branch arriving at a node that is not shaped like the rest: refused.

    The rename arm above must not be bought by making the count vacuous. A node whose
    outermost layer is gone still has a body and a highlight where its branch lands,
    so a count of places would call it a node; what marks it is that the four are no
    longer alike.
    """
    text = SVG.read_text(encoding="utf-8")
    dropped = text.replace(
        '  <circle cx="316" cy="508" r="88" fill="url(#nodeGlow)"/>\n', "", 1
    )
    assert dropped != text, "the substitution did not apply, so this arm measures nothing"

    problems = faults(dropped)
    assert any("not shaped alike" in problem for problem in problems), (
        "the check accepted a node that lost its outer layer; it reports:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )


def test_the_check_refuses_a_node_beside_the_fan() -> None:
    """A fifth node where no branch ends: refused at any size, since four are the design.

    Keyed to the radius the four nodes measure, this arm accepted a fifth node of `90`
    and one painted as a body only at `42` — both sitting beside the fan with nothing
    arriving at either. What makes a node is a branch ending at it, so the test is a
    circle at no arrival and on no hub ring, whatever its radius reads.
    """
    text = SVG.read_text(encoding="utf-8")
    anchor = '  <circle cx="708" cy="508" r="88" fill="url(#nodeGlow)"/>'
    assert anchor in text, "the anchor this arm appends after is gone, so it measures nothing"

    for shape in (
        '<circle cx="820" cy="650" r="88" fill="url(#nodeGlow)"/>',
        '<circle cx="820" cy="650" r="90" fill="url(#nodeGlow)"/>',
        '<circle cx="820" cy="650" r="42" fill="url(#nodeBody)"/>',
    ):
        added = text.replace(anchor, anchor + "\n  " + shape, 1)
        assert added != text, "the substitution did not apply, so this arm measures nothing"

        problems = faults(added)
        assert any("where no branch ends" in problem for problem in problems), (
            f"the check accepted a fifth node drawn as {shape}; it reports:\n"
            + "\n".join(f"  - {problem}" for problem in problems)
        )
