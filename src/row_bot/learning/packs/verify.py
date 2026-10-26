"""Verifiers for pack tasks: executable checks of what a run produced (E11).

:func:`verify` checks an :class:`Outcome` (the final answer, the workspace's files and
the fake apps' final state) against a task's :class:`~row_bot.learning.packs.Verifier`.
Code tests run in a fresh sandbox with the network off (``learning.sandbox``).
:func:`solve` plays a :class:`~row_bot.learning.packs.Solution` (a task's reference or
decoy) in a sandbox, so every verifier can be shown to pass the reference and fail the
decoy.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from row_bot.learning.packs import PackTask, Solution
from row_bot.learning.packs.fake_apps import FakeApps

_CHECK_TESTS = "_row_bot_sandbox_check_tests.py"
_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w])")
MAX_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class Outcome:
    """What a run left: its final answer, the workspace's text files and the fake apps' state."""

    answer: str = ""
    files: Mapping[str, str] = field(default_factory=dict)
    apps: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    passed: bool
    kind: str
    detail: str = ""
    backend: str = ""

    @property
    def guarded(self) -> bool:
        """It failed only because it offered more than one answer (METRIC_VERSION 3's guard): it passed
        under version 2."""

        return not self.passed and self.detail == GUARD_DETAIL


def normalize(text: Any) -> str:
    """Lower case, markdown emphasis and surrounding punctuation removed, spaces collapsed."""

    cleaned = re.sub(r"[`*_]", "", str(text if text is not None else "")).strip().casefold()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" .!\"'")


def numbers_in(text: str) -> list[float]:
    found: list[float] = []
    for match in _NUMBER.finditer(str(text or "")):
        try:
            found.append(float(match.group(0).replace(",", "")))
        except ValueError:
            continue
    return found


def parse_json(text: str) -> Any:
    """A JSON value from an answer (a fenced block allowed)."""

    body = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    try:
        return json.loads(body)
    except ValueError:
        starts = [index for index in (body.find("{"), body.find("[")) if index >= 0]
        if not starts:
            raise
        start = min(starts)
        end = max(body.rfind("}"), body.rfind("]"))
        return json.loads(body[start:end + 1])


def _same(left: Any, right: Any) -> bool:
    """JSON values equal: numbers by value, strings trimmed, objects by key, lists in order."""

    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(left, str) and isinstance(right, str):
        return left.strip() == right.strip()
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _rows(text: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(str(text or "").strip()))
    return [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]


def _text_lines(text: str) -> list[str]:
    return [line.rstrip() for line in str(text or "").replace("\r\n", "\n").strip("\n").split("\n")]


# ── Answer, JSON and file checks ────────────────────────────────────────────


def _phrase_at(text: str, value: str) -> int:
    """Where ``value`` stands in ``text`` as a whole phrase (both normalized), or -1."""

    target = normalize(value)
    if not target:
        return -1
    found = re.search(rf"(?<![\w@]){re.escape(target)}(?![\w@-])", normalize(text))
    return found.start() if found else -1


def _number_at(text: str, value: float, tolerance: float) -> int:
    """Where a number equal to ``value`` first stands in ``text``, or -1."""

    for match in _NUMBER.finditer(str(text or "")):
        try:
            if abs(float(match.group(0).replace(",", "")) - value) <= tolerance + 1e-9:
                return match.start()
        except ValueError:
            continue
    return -1


def _states(task: PackTask, answer: str) -> bool:
    """The reply states the expected value in a sentence and doesn't give the task's known wrong
    value (its decoy) first (METRIC_VERSION 2): "Old laptops go to Brightside Schools." answers
    "Who receives old laptops?" as well as "Brightside Schools" does, and "60, or 85 under the new
    policy" doesn't answer "85"."""

    verifier = task.verifier
    wrong = str(task.decoy.answer or "")
    if verifier.match == "number":
        right = _number_at(answer, float(verifier.expected), verifier.tolerance)
        wrongs = [_number_at(answer, value, 0.0) for value in numbers_in(wrong)
                  if abs(value - float(verifier.expected)) > verifier.tolerance + 1e-9]
    else:
        values = verifier.expected if verifier.match == "any_of" else [verifier.expected]
        places = [place for place in (_phrase_at(answer, str(item)) for item in values) if place >= 0]
        right = min(places) if places else -1
        wrongs = [_phrase_at(answer, wrong)] if wrong and normalize(wrong) not in {normalize(item) for item in values} \
            else []
    return right >= 0 and not any(0 <= place < right for place in wrongs)


def _check_answer(task: PackTask, answer: str) -> Verdict:
    verifier = task.verifier
    expected = verifier.expected
    if verifier.match == "exact":
        ok = normalize(answer) == normalize(expected)
    elif verifier.match == "contains":
        ok = normalize(expected) in normalize(answer)
    elif verifier.match == "any_of":
        ok = normalize(answer) in {normalize(item) for item in expected}
    else:  # number: the answer's numbers are all the expected one
        found = numbers_in(answer)
        target = float(expected)
        ok = bool(found) and all(abs(value - target) <= verifier.tolerance + 1e-9 for value in found)
    if not ok and verifier.match != "contains":
        ok = _states(task, answer)
    if ok and offers_several(answer, expected, match=verifier.match, wrong=str(task.decoy.answer or ""),
                             prompt=task.prompt, tolerance=verifier.tolerance):
        return Verdict(False, "answer", GUARD_DETAIL)
    return Verdict(ok, "answer", "" if ok else f"expected {expected!r}")


def _check_json(task: PackTask, answer: str) -> Verdict:
    try:
        value = parse_json(answer)
    except ValueError:
        return Verdict(False, "json_answer", "the answer is not JSON")
    ok = _same(value, task.verifier.expected)
    if ok and _several_json(answer):
        return Verdict(False, "json_answer", GUARD_DETAIL)
    return Verdict(ok, "json_answer", "" if ok else "the JSON differs")


# ── Commits to one answer (METRIC_VERSION 3, O67) ───────────────────────────
#
# Version 2 passes a reply that states the expected value unless it gives the known wrong value first, so a
# reply that lists several candidates could pass. From version 3 a final message that **offers two or more
# distinct answers of the expected kind** fails. It offers them when it holds two distinct values of that kind
# (numbers, dates, times, weekdays, months, email addresses, identifiers shaped like the expected one; for a name
# or other phrase, the expected phrase and the task's known wrong one) and
#
# - two of them are joined by a word or sign of choice ("or", "either", "/", "otherwise", "alternatively",
#   "if", "unless", "maybe", "perhaps", "possibly", "might", "could") with no sentence end between them, or
#   given as a range ("80-90", "between 80 and 90"); or
# - the message hedges ("not sure", "it depends", "one of", "possible values", "candidates", …).
#
# A phrase also offers alternatives when it is joined to other words by "or" or "/" ("Ines Duarte or Tomas
# Berg"), but not by a qualifier ("Python 3.12 or newer"). Context that names another value without offering it
# ("52,500 EUR is approved; the draft said 50,000") still commits to one answer. Numbers that are part of an
# identifier, a date (a day of a month too: "19 or 20 November") or a time, follow a reference word ("section 3",
# "page 2"), restate the task's own numbers or are years are not candidates. Two or more different JSON answers
# in one message offer several.

GUARD_DETAIL = "offered more than one answer"

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")
_MONTH_WORDS = "|".join(_MONTHS + ("jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov",
                                   "dec"))
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ISO_DATE = re.compile(r"(?<![\d-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d-])")
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_WORDS})\.?,?\s+(\d{{4}})\b", re.I)
_MONTH_DAY = re.compile(rf"\b({_MONTH_WORDS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)
_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IDENTIFIER = re.compile(r"(?<![\w-])[A-Za-z]{1,8}[-_]?\d+(?:[-_][A-Za-z0-9]+)*(?![\w-])")
_WEEKDAY = re.compile(r"\b(" + "|".join(_WEEKDAYS) + r")s?\b", re.I)
_MONTH = re.compile(r"\b(" + "|".join(_MONTHS) + r")\b", re.I)
_REFERENCE = re.compile(
    r"(?:section|sections|page|pages|p\.|line|lines|row|rows|item|step|steps|part|chapter|clause|paragraph|para|"
    r"no\.|number|#|version|ticket|issue|invoice|order|ref|reference|amendment|phase|floor|room|table|figure|"
    r"appendix|rule|article|task|question|option|case|week|level|grade|tier)\s*$", re.I)
_LIST_MARKER = re.compile(r"\s*\d+[.)]\s")
_CHOICE = re.compile(r"\bor\b|/|\beither\b|\botherwise\b|\balternatively\b|\bif\b|\bunless\b|\bmaybe\b|\bperhaps\b|"
                     r"\bpossibly\b|\bmight\b|\bcould\b", re.I)
_RANGE = re.compile(r"^\s*(?:-|–|—)\s*$")
_HEDGE = re.compile(r"\bnot sure\b|\bunsure\b|\bunclear\b|\bcan(?:'|’)?t tell\b|\bcannot tell\b|\bcan(?:'|’)?t be sure\b|"
                    r"\bit depends\b|\bdepending on\b|\bone of\b|\bpossible (?:values|answers)\b|\bcandidates?\b|"
                    r"\bpossibilities\b|\boptions\b", re.I)
_BREAK = re.compile(r"[.!?;\n](?:\s|$)|\n")
# "or" followed by one of these qualifies the answer ("3.12 or newer"); it offers no other one.
_QUALIFIER = r"(?:newer|later|higher|above|more|less|greater|fewer|earlier|before|after|so|thereabouts|similar|" \
             r"equivalent|better|above|below|over|under)\b"
_NUMERIC = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
# Days of a month written beside it, one or several ("19 or 20 November", "19-21 November", "November 19").
_DAYS_OF = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?(?:\s*(?:[-–—,/]|or|and|to)\s*\d{{1,2}}(?:st|nd|rd|th)?)*\s+"
                      rf"(?:{_MONTH_WORDS})\b|\b(?:{_MONTH_WORDS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.I)


@dataclass(frozen=True)
class _Found:
    start: int
    end: int
    value: Any


def _shape(text: str) -> str:
    return re.sub(r"\d", "9", re.sub(r"[A-Za-z]", "A", str(text)))


def _kind_of(expected: Any, match: str) -> str:
    if match == "number":
        return "number"
    text = str(expected[0] if isinstance(expected, (list, tuple)) and expected else expected or "").strip()
    if _NUMERIC.fullmatch(text):
        return "number"
    if _ISO_DATE.fullmatch(text) or _DAY_MONTH.fullmatch(text) or _MONTH_DAY.fullmatch(text):
        return "date"
    if _TIME.fullmatch(text):
        return "time"
    if text.casefold() in _WEEKDAYS:
        return "weekday"
    if text.casefold() in _MONTHS:
        return "month"
    if _EMAIL.fullmatch(text):
        return "email"
    if _IDENTIFIER.fullmatch(text):
        return "identifier"
    return "phrase"


def _dates(text: str) -> list[_Found]:
    found = [_Found(m.start(), m.end(), (int(m.group(1)), int(m.group(2)), int(m.group(3))))
             for m in _ISO_DATE.finditer(text)]
    for pattern, order in ((_DAY_MONTH, (3, 2, 1)), (_MONTH_DAY, (3, 1, 2))):
        for m in pattern.finditer(text):
            month_word = m.group(order[1]).casefold()[:3]
            month = next(index for index, name in enumerate(_MONTHS, 1) if name.startswith(month_word))
            found.append(_Found(m.start(), m.end(), (int(m.group(order[0])), month, int(m.group(order[2])))))
    return found


def _masked(text: str, *patterns: re.Pattern[str]) -> str:
    for pattern in patterns:
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _number_values(text: str) -> list[_Found]:
    masked = _masked(text, _EMAIL, _ISO_DATE, _DAY_MONTH, _MONTH_DAY, _DAYS_OF, _TIME, _IDENTIFIER)
    found = []
    for m in _NUMBER.finditer(masked):
        if _REFERENCE.search(masked[max(0, m.start() - 24):m.start()]):
            continue
        line_start = masked.rfind("\n", 0, m.start()) + 1
        if not masked[line_start:m.start()].strip() and _LIST_MARKER.match(masked, m.start()):
            continue  # a numbered list's own marker ("1. ")
        try:
            found.append(_Found(m.start(), m.end(), float(m.group(0).replace(",", ""))))
        except ValueError:
            continue
    return found


def _candidates(answer: str, expected: Any, kind: str, *, wrong: str, prompt: str,
                tolerance: float) -> list[_Found]:
    """Every value of the expected kind the message gives, in order (values normalized)."""

    if kind == "number":
        target = float(str(expected[0] if isinstance(expected, (list, tuple)) else expected).replace(",", ""))
        asked = [item.value for item in _number_values(prompt)]
        found = []
        for item in _number_values(answer):
            same = abs(item.value - target) <= tolerance + 1e-9
            if not same and any(abs(item.value - value) <= 1e-9 for value in asked):
                continue  # restates the task's own numbers
            if not same and item.value.is_integer() and 1900 <= item.value <= 2100 and not 1900 <= target <= 2100:
                continue  # a year
            found.append(_Found(item.start, item.end, target if same else item.value))
        return found
    if kind == "date":
        return _dates(answer)
    if kind == "time":
        return [_Found(m.start(), m.end(), (int(m.group(1)), int(m.group(2)))) for m in _TIME.finditer(answer)]
    if kind == "weekday":
        return [_Found(m.start(), m.end(), m.group(1).casefold()) for m in _WEEKDAY.finditer(answer)]
    if kind == "month":
        masked = _masked(answer, _DAY_MONTH, _MONTH_DAY)
        return [_Found(m.start(), m.end(), m.group(1).casefold()) for m in _MONTH.finditer(masked)]
    if kind == "email":
        return [_Found(m.start(), m.end(), m.group(0).casefold()) for m in _EMAIL.finditer(answer)]
    if kind == "identifier":
        shape = _shape(str(expected[0] if isinstance(expected, (list, tuple)) else expected).upper())
        return [_Found(m.start(), m.end(), m.group(0).upper()) for m in _IDENTIFIER.finditer(answer)
                if _shape(m.group(0).upper()) == shape]
    values = [str(item) for item in (expected if isinstance(expected, (list, tuple)) else [expected])]
    phrases = [(value, "expected") for value in values]
    if wrong and normalize(wrong) not in {normalize(item) for item in values}:
        phrases.append((wrong, "wrong"))
    found = []
    for phrase, label in phrases:
        target = normalize(phrase)
        if not target:
            continue
        for m in re.finditer(rf"(?<![\w@]){re.escape(target)}(?![\w@-])", answer.casefold()):
            found.append(_Found(m.start(), m.end(), label))
    return sorted(found, key=lambda item: item.start)


def _joined_phrase(answer: str, found: Sequence[_Found]) -> bool:
    """The expected phrase is joined to other words by "or" or "/" ("Ines Duarte or Tomas Berg")."""

    text = answer.casefold()
    for item in found:
        if item.value != "expected":
            continue
        after = text[item.end:item.end + 16]
        before = text[max(0, item.start - 16):item.start]
        offered_after = re.match(r"\s*,?\s*(?:or\b|/)\s*[\w\"'“(]", after) and not re.match(
            rf"\s*,?\s*or\s+{_QUALIFIER}", text[item.end:item.end + 32])
        if offered_after or re.search(r"(?:\bor|/|\beither)\s*$", before):
            return True
    return False


def offers_several(answer: str, expected: Any, *, match: str = "exact", wrong: str = "", prompt: str = "",
                   tolerance: float = 0.0, hedges: bool = True) -> bool:
    """Whether a final message offers two or more distinct answers of the expected kind (METRIC_VERSION 3).
    ``hedges=False`` reads only values joined as a choice or a range (a chat reply's own words, where "one of"
    or "options" says nothing about the answer)."""

    text = str(answer or "")
    kind = _kind_of(expected, match)
    found = _candidates(text, expected, kind, wrong=wrong, prompt=prompt, tolerance=tolerance)
    if kind == "phrase" and _joined_phrase(text, found):
        return True
    if len({item.value for item in found}) < 2:
        return False
    if hedges and _HEDGE.search(text):
        return True
    ordered = sorted(found, key=lambda item: item.start)
    for first, second in zip(ordered, ordered[1:]):
        if first.value == second.value:
            continue
        between = text[first.end:second.start]
        if len(between) > 80 or _BREAK.search(between):
            continue
        if _CHOICE.search(between) or _RANGE.match(between):
            return True
        if re.fullmatch(r"\s*and\s*", between, re.I) and re.search(r"\bbetween\s*$", text[:first.start], re.I):
            return True
    return False


def _several_json(answer: str) -> bool:
    """Two or more different JSON answers in fenced blocks."""

    values = set()
    for block in re.findall(r"```(?:json)?\s*(.*?)```", str(answer or ""), re.S):
        try:
            values.add(json.dumps(json.loads(block.strip()), sort_keys=True))
        except ValueError:
            continue
    return len(values) > 1


def _check_file(task: PackTask, files: Mapping[str, str]) -> Verdict:
    verifier = task.verifier
    actual = files.get(verifier.path)
    if actual is None:
        return Verdict(False, "file", f"{verifier.path} was not written")
    expected = verifier.expected
    if verifier.match == "json":
        try:
            ok = _same(json.loads(actual), expected if not isinstance(expected, str) else json.loads(expected))
        except ValueError:
            return Verdict(False, "file", f"{verifier.path} is not JSON")
    elif verifier.match == "csv":
        ok = _rows(actual) == _rows(expected)
    elif verifier.match == "csv_rows":
        got, want = _rows(actual), _rows(expected)
        ok = bool(got) and bool(want) and got[0] == want[0] and Counter(map(tuple, got[1:])) == Counter(
            map(tuple, want[1:]))
    elif verifier.match == "lines":
        ok = [line.strip() for line in _text_lines(actual) if line.strip()] == [
            line.strip() for line in _text_lines(expected) if line.strip()]
    else:
        ok = _text_lines(actual) == _text_lines(expected)
    return Verdict(ok, "file", "" if ok else f"{verifier.path} differs")


# ── Fake-app checks ─────────────────────────────────────────────────────────


def _matches(value: Any, wanted: Any) -> bool:
    if isinstance(wanted, Mapping) and "contains" in wanted:
        return normalize(wanted["contains"]) in normalize(value)
    if isinstance(wanted, list):
        if not isinstance(value, list):
            return False
        return sorted(normalize(item) for item in value) == sorted(normalize(item) for item in wanted)
    if isinstance(wanted, (int, float)) and not isinstance(wanted, bool):
        try:
            return float(value) == float(wanted)
        except (TypeError, ValueError):
            return False
    return normalize(value) == normalize(wanted)


def _check_apps(task: PackTask, apps: Mapping[str, Any]) -> Verdict:
    for check in task.verifier.expected:
        app, _, collection = str(check["in"]).partition(".")
        items = list(((apps.get(app) or {}).get(collection)) or [])
        where = dict(check.get("where") or {})
        found = [item for item in items if all(_matches(item.get(key), wanted) for key, wanted in where.items())]
        count = int(check.get("count", 1))
        if len(found) != count:
            return Verdict(False, "app_state", f"{check['in']} matching {where}: {len(found)}, expected {count}")
    return Verdict(True, "app_state")


# ── Code tests ──────────────────────────────────────────────────────────────


def _check_code(task: PackTask, files: Mapping[str, str], *, backend: str, timeout: float) -> Verdict:
    from row_bot.learning.sandbox import Sandbox, run_python

    if task.verifier.path not in files:
        return Verdict(False, "code_tests", f"{task.verifier.path} was not written")
    with Sandbox() as box:
        for name, content in files.items():
            if name.endswith(".py") or name in task.files:
                box.write(name, content)
        box.write(_CHECK_TESTS, str(task.verifier.expected))
        result = run_python(box, _CHECK_TESTS, timeout=timeout, backend=backend)  # type: ignore[arg-type]
    if result.timed_out:
        return Verdict(False, "code_tests", "the tests ran out of time", result.backend)
    detail = "" if result.passed else (
        f"{result.failures + result.errors} of {result.tests} tests failed" if result.tests else "the tests did not run")
    return Verdict(result.passed, "code_tests", detail, result.backend)


def verify(task: PackTask, outcome: Outcome, *, backend: str = "auto", timeout: float = 20.0) -> Verdict:
    """Check what a run produced against the task's verifier."""

    kind = task.verifier.kind
    if kind == "answer":
        return _check_answer(task, outcome.answer)
    if kind == "json_answer":
        return _check_json(task, outcome.answer)
    if kind == "file":
        return _check_file(task, outcome.files)
    if kind == "app_state":
        return _check_apps(task, outcome.apps)
    return _check_code(task, outcome.files, backend=backend, timeout=timeout)


# ── Playing a solution ──────────────────────────────────────────────────────


def workspace_files(folder: Path) -> dict[str, str]:
    """A folder's text files (task-relative, ``/`` separators); large files are skipped."""

    found: dict[str, str] = {}
    for item in sorted(Path(folder).rglob("*")):
        if not item.is_file() or item.is_symlink() or item.name.startswith("_row_bot_sandbox"):
            continue
        relative = item.relative_to(folder).as_posix()
        if "__pycache__" in relative or item.stat().st_size > MAX_FILE_BYTES:
            continue
        found[relative] = item.read_bytes().decode("utf-8", "replace")
    return found


def solve(task: PackTask, solution: Solution) -> Outcome:
    """What the task's workspace and apps look like after ``solution`` (no model involved)."""

    files = dict(task.files)
    files.update(solution.files)
    apps = FakeApps(task.apps)
    for call in solution.calls:
        apps.call(str(call["tool"]), dict(call.get("args") or {}))
    return Outcome(solution.answer, files, apps.state())


__all__ = ["GUARD_DETAIL", "Outcome", "Verdict", "normalize", "offers_several", "numbers_in", "parse_json", "solve", "verify", "workspace_files"]
