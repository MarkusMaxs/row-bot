"""The page as the agent reads it: a short accessibility outline with stable refs.

Chromium's accessibility tree comes from Playwright's AI snapshot
(``aria_snapshot(mode="ai")``), whose refs (``e12``, ``f1e3`` in a frame)
belong to DOM nodes: a re-render or a new page renumbers them. Row-Bot gives
the agent its own refs instead, keyed by what a control is (its role, name
and which of the same it is), so a ref stays the same while the control
does, across re-renders and snapshots, and is resolved to the page's current
node at the moment of the action.

The outline drops unnamed layout containers and refs on things that can't be
acted on, and is cut at a size the model can afford.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

# Roles the agent acts on; other nodes keep their words but not a ref.
ACTIONABLE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "option", "checkbox", "radio",
    "switch", "slider", "spinbutton", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
    "treeitem", "gridcell", "row", "cell", "columnheader", "img", "summary",
})
MAX_CHARS = 12_000
_LINE = re.compile(r"^(?P<indent>\s*)- (?P<body>.*)$")
_HEAD = re.compile(r'^(?P<role>[A-Za-z][\w-]*)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')
_REF = re.compile(r" \[ref=(?P<ref>(?:f\d+)?e\d+)\]")
_POINTER = " [cursor=pointer]"


@dataclass(frozen=True)
class Node:
    """One node of the outline that has a page ref."""

    page_ref: str
    role: str
    name: str
    frame: str


@dataclass
class RefMap:
    """Row-Bot's refs for one tab, stable while the controls they name stay."""

    _by_key: dict[tuple[str, str, str, int], str] = field(default_factory=dict)
    _targets: dict[str, tuple[tuple[str, str, str, int], str | None]] = field(default_factory=dict)
    _next: int = 0

    def reset(self) -> None:
        self._by_key.clear()
        self._targets.clear()

    def assign(self, nodes: Iterable[Node]) -> dict[str, str]:
        """Our ref for each page ref in this snapshot; refs not seen now stay known but unresolved."""

        seen: dict[tuple[str, str, str], int] = {}
        current: dict[str, str] = {}
        for ref in self._targets:
            key, _ = self._targets[ref]
            self._targets[ref] = (key, None)
        for node in nodes:
            base = (node.frame, node.role, node.name)
            index = seen.get(base, 0)
            seen[base] = index + 1
            key = (*base, index)
            ours = self._by_key.get(key)
            if ours is None:
                self._next += 1
                ours = f"e{self._next}"
                self._by_key[key] = ours
            self._targets[ours] = (key, node.page_ref)
            current[node.page_ref] = ours
        return current

    def page_ref(self, ref: str) -> str | None:
        """The page's current ref for one of ours (None: not on the page in the last snapshot)."""

        entry = self._targets.get(str(ref or "").strip())
        return entry[1] if entry else None

    def describe(self, ref: str) -> tuple[str, str] | None:
        """(role, name) a ref was given for, even when it's gone from the page."""

        entry = self._targets.get(str(ref or "").strip())
        return (entry[0][1], entry[0][2]) if entry else None


def _unquote(name: str | None) -> str:
    return (name or "").replace('\\"', '"').replace("\\\\", "\\")


def _quote(name: str) -> str:
    return name.replace("\\", "\\\\").replace('"', '\\"')


def _split_suffix(rest: str) -> tuple[str, str]:
    """`` [level=1]: text`` → (`` [level=1]``, ``: text``): the colon outside brackets starts the suffix."""

    depth = 0
    for index, char in enumerate(rest):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        elif char == ":" and depth == 0:
            return rest[:index].rstrip(), rest[index:]
    return rest.rstrip(), ""


def compact(raw: str, refs: RefMap, *, max_chars: int = MAX_CHARS) -> tuple[str, int]:
    """(the outline with Row-Bot's refs, how many controls it names) for one AI snapshot."""

    lines = str(raw or "").splitlines()
    parsed: list[tuple[int, str, str, str, str, str | None]] = []  # indent, role, name, head, tail, page ref
    nodes: list[Node] = []
    for line in lines:
        match = _LINE.match(line)
        if not match:
            continue
        indent = len(match["indent"])
        body = match["body"]
        head_match = _HEAD.match(body)
        if not head_match or body.startswith(("text:", "/")):
            parsed.append((indent, "", "", body, "", None))
            continue
        role = head_match["role"]
        name = _unquote(head_match["name"])
        rest = head_match["rest"]
        ref_match = _REF.search(rest)
        page_ref = ref_match["ref"] if ref_match else None
        pointer = _POINTER in rest
        rest = _REF.sub("", rest).replace(_POINTER, "")
        keep_ref = page_ref is not None and (role in ACTIONABLE_ROLES or pointer)
        if keep_ref:
            # Not the page's frame prefix: a new document renumbers frames (f1e4), so a ref is keyed by
            # what the control is and where it comes in the page, whatever frame it's in.
            nodes.append(Node(page_ref, role, name, ""))
        parsed.append((indent, role, name, body, rest, page_ref if keep_ref else None))
    ours = refs.assign(nodes)

    out: list[str] = []
    dropped: list[int] = []  # indents of dropped containers, to pull their children up
    for indent, role, name, body, rest, page_ref in parsed:
        while dropped and indent <= dropped[-1]:
            dropped.pop()
        depth = max(0, indent - 2 * len(dropped))
        if role in {"generic", "document", "none", "presentation", "group"} and not name and page_ref is None:
            if rest.rstrip().endswith(":"):
                dropped.append(indent)
                continue
            if not rest.strip():
                continue
        if not role:
            out.append(f"{' ' * depth}- {body}")
            continue
        quoted = f' "{_quote(name)}"' if name else ""
        ref = f" [ref={ours[page_ref]}]" if page_ref else ""
        attributes, suffix = _split_suffix(rest)
        out.append(f"{' ' * depth}- {role}{quoted}{attributes}{ref}{suffix}")
    text = "\n".join(_strip_empty_containers(out))
    if len(text) > max_chars:
        cut = text.rfind("\n", 0, max_chars)
        shown = text[: cut if cut > 0 else max_chars]
        hidden = text.count("\n", len(shown)) + 1
        text = shown + f"\n… {hidden} more lines not shown: scroll, or use browser_extract for the page's text."
    return text, len(nodes)


def _strip_empty_containers(lines: list[str]) -> list[str]:
    """Drop ``- list:``-style lines left with no children after compaction."""

    kept: list[str] = []
    for index, line in enumerate(lines):
        if line.rstrip().endswith(":") and "[ref=" not in line:
            depth = len(line) - len(line.lstrip())
            following = lines[index + 1] if index + 1 < len(lines) else ""
            if not following or len(following) - len(following.lstrip()) <= depth:
                continue
        kept.append(line)
    return kept
