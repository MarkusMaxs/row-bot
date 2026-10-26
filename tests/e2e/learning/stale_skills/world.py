"""The stale-skills world timeline (Design A generator v2): one seeded person's 120 days, ground truth only (Lane B).

Built on S1's seeded person (her 30-day past, workspace and apps, ``tests/e2e/learning/seeded/generator.py``) and
fixed by the draft pre-registration (``.local/plans/paper-ideas/PREREG-DESIGN-A.md`` §4). From one seed, the same
world (same seed, same :meth:`Timeline.fingerprint`):

- **Recurring procedures** (:data:`PROCEDURES`, sixteen for Mara): each is a task the person asks for on fixed
  weekdays, once or twice a week, whose right first action depends on one fact of one of six kinds (an address,
  a supplier, hours, an amount, who holds a role, a policy rule).
- **Change events** by the pre-registration's rule (:func:`change_days`): 8 per person, on days drawn uniformly
  from 20-110 at least 7 days apart, each touching a different procedure; the changing procedures cover all six
  kinds (one of each, then two more), and the other 8 procedures' facts never change. Which procedures change,
  on which day and to which value all come from the seed, blind to any arm.
- **Canonical entities with aliases** (a party's names: "Sandgate", "the bakery") and **homonyms** (two people
  called Sam, introduced early), **preference updates** and **misstated-then-corrected facts**.
- **Rendering**: each procedure's first run states the fact (the person explains it once); later runs don't. A
  change reaches the conversation on its day, told in a chat of its own or mentioned in passing in another
  request (seeded), and the world changes too: the workspace runbook (and, for a policy, S1's own policy document)
  is rewritten, and a letter arrives in the inbox. Wording comes from templates, or from a phrasing model's kept
  variants (``phrasing.py``; two phrasing models supported).
- **Consent**: the simulated person accepts "save this as a skill?" on a procedure's 2nd or 3rd passed run (seeded).
- **Probes** on days 30, 60, 90 and 120 (current fact, as-of fact, alias and homonym, update detection,
  abstention), each with its expected answer from the timeline.

**Ground truth comes only from this timeline** (:meth:`Timeline.value`), never from the simulated person or the
product's verifier. Nothing here imports Row-Bot beyond the pack format.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from tests.e2e.learning.seeded import generator
from tests.e2e.learning.seeded.person import draw
from tests.e2e.learning.seeded.pilot_timeline import _matching

FORMAT = "row_bot.learning.stale_skills_timeline"
VERSION = 1
PERSON = "mara"
SEED = 20261022
DAYS = 120
KINDS = ("address", "supplier", "hours", "amount", "role", "policy")
#: The pre-registration's change rule (§4).
CHANGES_PER_PERSON = 8
CHANGE_FIRST, CHANGE_LAST, CHANGE_GAP = 20, 110, 7
#: Procedures asked twice a week (seeded); the rest once a week (the rule asks "at least weekly").
TWICE_WEEKLY = 4
PROBE_DAYS = (30, 60, 90, 120)
#: Probes per category on each probe day.
PROBES_PER_CATEGORY = 2
PROBE_CATEGORIES = ("current", "as_of", "alias_homonym", "update", "abstention")
WINDOW_DAYS = 14  # the primary metric's window after a change (§6)
RUNBOOK = "admin/kitchen-runbook.md"
ROTA_POLICY = "policies/volunteer-rota-policy-2026.md"
HANDBOOK = "handbook/food-safety.md"
FINANCE = "finance@lanternlane.example"
VOLUNTEERS = "volunteers@lanternlane.example"
#: Where the person's runs are numbered (``<person>-sa-d<day>-<procedure>``).
RUN_PREFIX = "sa"

Value = dict[str, Any]


# ── The catalogue: Mara's procedures ─────────────────────────────────────────


@dataclass(frozen=True)
class Procedure:
    """One recurring procedure and its fact (see the module's docstring)."""

    id: str
    kind: str
    title: str  # what the person calls the task ("the bread order")
    fact: str  # the fact in words ("Sandgate Bakery's orders address")
    party: str  # who or what the fact belongs to, canonical ("Sandgate Bakery", "the treasurer")
    aliases: tuple[str, ...]  # other names the person uses for the party
    pool: tuple[Value, ...]  # the values the fact can take; pool[0] is the old one when fixed_old
    fixed_old: bool  # pool[0] is what S1's world already says (its past, its documents)
    intro: str  # the first run's words: states the fact ({value})
    short: tuple[str, ...]  # later runs' words: never state the fact
    variable: str  # "n", "date" or "who_date"
    low: int = 0
    high: int = 0
    unit: str = ""  # how ``n`` is said ("large loaves")
    to: str = ""  # amount and policy: the fixed recipient
    event: str = ""  # hours: the event's title ({who})
    keyword: str = ""  # hours: what the event's title must contain
    doc: str = ""  # an S1 document that states the fact too (rewritten when it changes)
    letter_from: str = ""  # who writes to the inbox when it changes ({old_email}: the old party)

    @property
    def action(self) -> str:
        return "event" if self.kind == "hours" else "mail"


def _recipient(name: str, email: str) -> Value:
    return {"name": name, "email": email}


PROCEDURES: tuple[Procedure, ...] = (
    Procedure(
        "bread_order", "address", "the bread order", "Sandgate Bakery's orders address", "Sandgate Bakery",
        ("Sandgate", "the bakery"),
        (_recipient("Sandgate Bakery", "hello@sandgate-bakery.example"),
         _recipient("Sandgate Bakery", "orders@sandgate-bakery.example"),
         _recipient("Sandgate Bakery", "bread@sandgate-bakery.example")), True,
        "Can you send tomorrow's bread order to Sandgate Bakery? {n} large loaves. Their orders address is {value}.",
        ("Send {alias} tomorrow's bread order: {n} large loaves.",
         "Bread order to {alias} for tomorrow please - {n} large loaves.",
         "Can you do tomorrow's bread order with {alias}? {n} large loaves."),
        "n", 14, 40, "large loaves", letter_from="{old_email}"),
    Procedure(
        "waste_collection", "address", "the extra bin collection", "the council's trade-waste booking address",
        "Harbourside Council trade waste", ("the council", "trade waste"),
        (_recipient("Harbourside Council trade waste", "tradewaste@harbourside-council.example"),
         _recipient("Harbourside Council trade waste", "waste.bookings@harbourside-council.example"),
         _recipient("Harbourside Council trade waste", "bins@harbourside-council.example")), False,
        "Can you book {n} extra bin collections this week with the council's trade waste team? Email them at {value}.",
        ("Book {n} extra bin collections with {alias} for this week.",
         "We need {n} extra bin pickups this week - can you book them with {alias}?"),
        "n", 2, 5, "extra bin collections", letter_from="{old_email}"),
    Procedure(
        "newsletter_update", "address", "the weekly newsletter update", "the Food Drive Network's newsletter address",
        "Food Drive Network newsletter", ("the network", "the Food Drive Network"),
        (_recipient("Food Drive Network newsletter", "submissions@fooddrive-network.example"),
         _recipient("Food Drive Network newsletter", "newsdesk@fooddrive-network.example"),
         _recipient("Food Drive Network newsletter", "stories@fooddrive-network.example")), False,
        "Send this week's kitchen update to the Food Drive Network newsletter at {value}: we served {n} meals.",
        ("Send this week's kitchen update to {alias} newsletter: we served {n} meals.",
         "Newsletter update for {alias} please - {n} meals served this week."),
        "n", 300, 900, "meals", letter_from="{old_email}"),
    Procedure(
        "veg_order", "supplier", "the weekly veg order", "our veg supplier", "the veg supplier",
        ("our veg people", "the veg supplier"),
        (_recipient("Hartwell Produce", "orders@hartwell-produce.example"),
         _recipient("Greenacre Growers", "orders@greenacre-growers.example"),
         _recipient("Fenwick Farm Supplies", "sales@fenwick-farm.example")), True,
        "Can you send the weekly veg order? {n} kg of potatoes. Our veg supplier is {name} - orders go to {value}.",
        ("Do the weekly veg order: {n} kg of potatoes.",
         "Veg order time - {n} kg of potatoes please, send it to {alias}.",
         "Weekly veg order: {n} kg of potatoes."),
        "n", 10, 40, "kg of potatoes", letter_from="{new_email}"),
    Procedure(
        "dry_goods_order", "supplier", "the dry-goods order", "our dry-goods supplier", "the dry-goods supplier",
        ("the wholesaler", "the dry-goods supplier"),
        (_recipient("Bramble Wholesale", "orders@bramble-wholesale.example"),
         _recipient("Northway Cash & Carry", "trade@northway-cc.example"),
         _recipient("Pantry Direct", "orders@pantrydirect.example")), False,
        "Please send the dry-goods order: {n} kg of rice. We buy dry goods from {name} - orders to {value}.",
        ("Dry-goods order please: {n} kg of rice.",
         "Can you order {n} kg of rice from {alias}?"),
        "n", 5, 25, "kg of rice", letter_from="{new_email}"),
    Procedure(
        "cleaning_order", "supplier", "the cleaning-supplies order", "our cleaning-supplies supplier",
        "the cleaning-supplies supplier", ("the cleaning supplier",),
        (_recipient("Brightwell Hygiene", "orders@brightwell-hygiene.example"),
         _recipient("Kestrel Cleaning Supplies", "sales@kestrel-clean.example"),
         _recipient("Ashby Janitorial", "orders@ashby-janitorial.example")), False,
        "Order {n} boxes of gloves for the kitchen, please. Cleaning supplies come from {name} ({value}).",
        ("Order {n} boxes of gloves from {alias}.",
         "We're low on gloves - order {n} boxes please."),
        "n", 2, 10, "boxes of gloves", letter_from="{new_email}"),
    Procedure(
        "foodbank_session", "hours", "the food-bank session", "the food-bank session's hours", "the food-bank session",
        ("the food bank",),
        ({"start": "13:00", "end": "16:00"}, {"start": "14:00", "end": "17:00"}, {"start": "12:00", "end": "15:00"}),
        False,
        "Put the food bank session on {date} in the kitchen calendar - it runs {value}.",
        ("Put the food bank session on {date} in the kitchen calendar.",
         "Add {date}'s food bank session to the calendar please."),
        "date", event="Food bank session", keyword="food bank", letter_from="trustees@lanternlane.example"),
    Procedure(
        "deep_clean", "hours", "the kitchen deep clean", "the deep-clean slot's hours", "the kitchen deep clean",
        ("the deep clean",),
        ({"start": "06:00", "end": "08:00"}, {"start": "07:00", "end": "09:00"}, {"start": "05:30", "end": "07:30"}),
        False,
        "Book the kitchen deep clean for {date} in the calendar. The cleaners come {value}.",
        ("Book the kitchen deep clean for {date} in the calendar.",
         "Deep clean on {date} - put it in the calendar."),
        "date", event="Kitchen deep clean", keyword="deep clean", letter_from="trustees@lanternlane.example"),
    Procedure(
        "induction", "hours", "a volunteer induction", "the volunteer induction's hours", "volunteer inductions",
        ("the induction",),
        ({"start": "17:00", "end": "18:00"}, {"start": "18:00", "end": "19:00"}, {"start": "16:30", "end": "17:30"}),
        False,
        "{who} is starting with us - add their volunteer induction on {date} to the calendar. Inductions run {value}.",
        ("{who} is starting - add their volunteer induction on {date} to the calendar.",
         "Can you put {who}'s induction in the calendar for {date}?"),
        "who_date", event="{who} - volunteer induction", keyword="induction",
        letter_from="trustees@lanternlane.example"),
    Procedure(
        "petty_cash", "amount", "the petty-cash float request", "the weekly petty-cash float", "the petty-cash float",
        ("the float",),
        ({"amount": 150}, {"amount": 200}, {"amount": 175}), False,
        "Email finance ({to}) the petty-cash float request for the week of {date}. The float is {value}.",
        ("Send finance the petty-cash float request for the week of {date}.",
         "Can you ask finance for this week's float? Week of {date}."),
        "date", to=FINANCE, letter_from=FINANCE),
    Procedure(
        "van_fuel", "amount", "the van fuel request", "the van's weekly fuel allowance", "the van fuel allowance",
        ("the fuel money",),
        ({"amount": 60}, {"amount": 85}, {"amount": 70}), False,
        "Ask finance ({to}) for the van's fuel allowance for the week of {date} - it's {value} a week.",
        ("Ask finance for the van's fuel allowance for the week of {date}.",
         "Fuel allowance request for the van please, week of {date}."),
        "date", to=FINANCE, letter_from=FINANCE),
    Procedure(
        "donations_report", "role", "the donations summary", "who our treasurer is", "the treasurer",
        ("the treasurer",),
        (_recipient("Olu Adeyemi", "olu@lanternlane.example"), _recipient("Priyanka Rao", "priyanka@lanternlane.example"),
         _recipient("Tomasz Wilk", "tomasz@lanternlane.example")), False,
        "Send the treasurer this week's donations summary: GBP {n} in total. Our treasurer is {name} ({value}).",
        ("Send the treasurer this week's donations summary: GBP {n} in total.",
         "Donations summary to the treasurer please - GBP {n} this week."),
        "n", 210, 940, "pounds", letter_from="chair@lanternlane.example"),
    Procedure(
        "maintenance_summary", "role", "the maintenance summary", "who the building manager is",
        "the building manager", ("the building manager",),
        (_recipient("Hal Brennan", "hal@lanternlane.example"), _recipient("Rosa Lind", "rosa@lanternlane.example"),
         _recipient("Joel Mensah", "joel@lanternlane.example")), False,
        "Send the building manager the weekly maintenance summary: {n} tickets still open. That's {name} ({value}).",
        ("Send the building manager the weekly maintenance summary: {n} tickets still open.",
         "Maintenance summary to the building manager - {n} open tickets."),
        "n", 2, 9, "open tickets", letter_from="chair@lanternlane.example"),
    Procedure(
        "swap_reminder", "policy", "the rota reminder", "the shift-swap notice rule", "the shift-swap rule",
        ("the swap rule",),
        ({"number": 48, "unit": "hours"}, {"number": 72, "unit": "hours"}, {"number": 36, "unit": "hours"}), True,
        "Send the volunteers ({to}) this week's rota reminder, with the swap rule: swaps need {value}' notice.",
        ("Send the volunteers this week's rota reminder, with the swap-notice rule.",
         "Rota reminder to the volunteers please - remind them how much notice a swap needs."),
        "none", to=VOLUNTEERS, doc=ROTA_POLICY, letter_from="trustees@lanternlane.example"),
    Procedure(
        "fridge_reminder", "policy", "the fridge-log reminder", "the maximum fridge temperature",
        "the fridge temperature rule", ("the fridge rule",),
        ({"number": 5, "unit": "°C"}, {"number": 4, "unit": "°C"}, {"number": 3, "unit": "°C"}), True,
        "Send the volunteers ({to}) the weekly fridge-log reminder: fridges must be at {value} or below.",
        ("Send the volunteers the weekly fridge-log reminder with the fridge temperature limit.",
         "Fridge-log reminder to the volunteers please, with the temperature limit."),
        "none", to=VOLUNTEERS, doc=HANDBOOK, letter_from="trustees@lanternlane.example"),
    Procedure(
        "shift_limit_reminder", "policy", "the shift-limit reminder", "the weekly shift limit per volunteer",
        "the shift-limit rule", ("the shift limit",),
        ({"number": 3, "unit": "shifts"}, {"number": 4, "unit": "shifts"}, {"number": 2, "unit": "shifts"}), True,
        "Email the volunteers ({to}) the weekly reminder that each person may do at most {value} a week.",
        ("Email the volunteers the weekly reminder about how many shifts each person may do.",
         "Send the volunteers the shift-limit reminder for this week."),
        "none", to=VOLUNTEERS, doc=ROTA_POLICY, letter_from="trustees@lanternlane.example"),
)
PROCEDURE = {item.id: item for item in PROCEDURES}

#: How a change is told (``{party}``, ``{fact}``, ``{old}``, ``{new}``, ``{new_name}``, ``{old_name}``,
#: ``{new_email}``): one template per kind; the phrasing model's variants keep the same fields.
TELL_TEMPLATES: dict[str, str] = {
    "address": "Heads up: {party} have a new address - from today use {new}. The old one ({old}) isn't checked any more.",
    "supplier": "We've switched {party}: from today it's {new_name}, not {old_name}. Orders go to {new_email}.",
    "hours": "From now on {fact} are {new} instead of {old}.",
    "amount": "Finance have changed {fact}: it's {new} from this week (it was {old}).",
    "role": "{new_name} has taken over as {party} from {old_name} - their email is {new_email}.",
    "policy": "The trustees changed {fact}: it's now {new} (it was {old}). The policy document is updated.",
}
IN_PASSING = "{prompt} Oh, and FYI: {tell}"

#: Preference updates (declarative; probed, never scored as a procedure).
PREFERENCES: tuple[dict[str, Any], ...] = (
    {"id": "pref_summary", "fact": "how Mara wants weekly summaries laid out",
     "values": ("a short paragraph", "a bullet list"), "markers": ("paragraph", "bullet"),
     "say_first": "I like weekly summaries as {value}.",
     "say_change": "Actually, I'd rather have weekly summaries as {value} from now on."},
    # (A preference that touched the calendar, "supplier calls on Tuesdays, don't book anything for me then",
    # made the main model refuse calendar procedures on Tuesdays in the first main run: replaced 26 Oct.)
    {"id": "pref_tea", "fact": "which tea Mara likes at trustee meetings",
     "values": ("peppermint", "rooibos"), "markers": ("peppermint", "rooibos"),
     "say_first": "For the record, I drink {value} tea at the trustee meetings.",
     "say_change": "I've gone off peppermint - it's {value} tea for me at trustee meetings now."},
)
#: Misstated, then corrected in the next message (declarative; probed).
MISSTATED: tuple[dict[str, Any], ...] = (
    {"id": "van_mot", "fact": "the date of the van's MOT",
     "pairs": (("14 January", "15 January"), ("21 January", "22 January"), ("3 February", "4 February")),
     "say": "The van's MOT is booked for {wrong}.", "fix": "Sorry, I misread the letter - the MOT is {right}, not {wrong}."},
    {"id": "hygiene_visit", "fact": "the date of the food hygiene inspection",
     "pairs": (("9 February", "10 February"), ("16 February", "17 February"), ("23 February", "24 February")),
     "say": "The food hygiene inspector is coming on {wrong}.",
     "fix": "Correction: the inspection is on {right}. I had the wrong week day."},
)
#: Two people called Sam (homonyms), introduced in the first week.
HOMONYMS: tuple[dict[str, Any], ...] = (
    {"id": "sam_okafor", "name": "Sam Okafor", "email": "sam.okafor@lanternlane.example", "role": "our new van driver",
     "say": "Sam Okafor has joined as our new van driver (volunteer). Their email is sam.okafor@lanternlane.example."},
    {"id": "sam_reyes", "name": "Sam Reyes", "email": "sam.reyes@bramble-wholesale.example",
     "role": "the account manager at Bramble Wholesale",
     "say": "Our account manager at Bramble Wholesale is Sam Reyes now - sam.reyes@bramble-wholesale.example."},
)
#: Facts never stated anywhere (abstention probes).
UNKNOWN: tuple[dict[str, str], ...] = (
    {"id": "okafor_phone", "question": "What's Sam Okafor's phone number?"},
    {"id": "bakery_driver", "question": "What's the name of Sandgate Bakery's delivery driver?"},
    {"id": "hal_mobile", "question": "What's the building manager's mobile number?"},
    {"id": "council_ref", "question": "What's our trade-waste account reference number with the council?"},
    {"id": "reyes_phone", "question": "What's Sam Reyes's direct phone line?"},
)
_WHOS = ("Ayo", "Bella", "Chidi", "Dev", "Elif", "Finn", "Grace", "Hamza", "Ines", "Jonah", "Kemi", "Luca")
_WEEKDAY_PAIRS = ((0, 3), (1, 4), (0, 2), (2, 4), (1, 3))


# ── Saying and recognising values ────────────────────────────────────────────


def say(kind: str, value: Value, *, with_name: bool = False) -> str:
    """A value in words, as the person or a document says it."""

    if kind in ("address", "supplier", "role"):
        return f"{value['name']} ({value['email']})" if with_name else str(value["email"])
    if kind == "hours":
        return f"{value['start']}-{value['end']}"
    if kind == "amount":
        return f"GBP {value['amount']}"
    unit = value["unit"]
    return f"{value['number']} {unit}" if unit != "°C" else f"{value['number']} °C"


_NUMBER_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 36: "thirty-six", 48: "forty-eight", 72: "seventy-two"}


def _hour_pattern(value: str) -> str:
    hour, minute = (int(part) for part in value.split(":"))
    twelve = hour % 12 or 12
    suffix = "pm" if hour >= 12 else "am"
    options = [rf"{hour:02d}[:.]{minute:02d}", rf"(?<!\d){hour}[:.]{minute:02d}"]
    options.append(rf"(?<!\d){twelve}\s?{suffix}\b" if minute == 0 else rf"(?<!\d){twelve}[:.]{minute:02d}\s?{suffix}\b")
    return "|".join(f"(?:{option})" for option in options)


def states(kind: str, value: Value, text: str) -> bool:
    """Whether ``text`` states ``value`` (the kind's own rule): the email address as written; the start time in any
    common form; the amount as a number; the rule's number with its unit (or the number in words)."""

    text = str(text or "")
    if kind in ("address", "supplier", "role"):
        return str(value["email"]).lower() in text.lower()
    if kind == "hours":
        return re.search(rf"(?<![\d:])(?:{_hour_pattern(value['start'])})(?![\d])", text, re.IGNORECASE) is not None
    if kind == "amount":
        return re.search(rf"(?<![\d.]){value['amount']}(?:\.00)?(?![\d])", text.replace(",", "")) is not None
    number = int(value["number"])
    words = [str(number)] + ([_NUMBER_WORDS[number]] if number in _NUMBER_WORDS else [])
    unit = {"hours": r"(?:-|\s)?(?:hours?|hrs?\b|h\b)", "°C": r"\s?(?:°\s?c|º\s?c|degrees|deg\b|c\b)",
            "shifts": r"\s(?:\w+\s)?shifts?"}[value["unit"]]
    return any(re.search(rf"(?<![\w.]){re.escape(word)}{unit}", text, re.IGNORECASE) for word in words)


# ── Records ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Change:
    """A change event: ``procedure``'s fact holds ``old`` before ``day`` and ``new`` from ``day`` on."""

    procedure: str
    kind: str
    day: int
    old: Value
    new: Value
    channel: str  # "told" (a chat of its own) or "in_passing" (in another request's message that day; drawn, and
    # "told" when no other procedure is asked that weekday)

    def to_dict(self) -> dict[str, Any]:
        return {"procedure": self.procedure, "kind": self.kind, "day": self.day, "old": self.old, "new": self.new,
                "channel": self.channel}


@dataclass(frozen=True)
class Tell:
    """The person telling Row-Bot something, in a chat of its own: one message, or a misstatement and its fix."""

    id: str
    day: int
    order: int
    about: str  # a procedure id, a preference, a misstated fact or a homonym
    what: str  # change | preference | misstated | homonym
    messages: tuple[str, ...]


@dataclass(frozen=True)
class Probe:
    """A declarative question on a probe day, with its expected answer from the timeline."""

    id: str
    day: int
    order: int
    category: str
    fact: str
    question: str
    expected: tuple[str, ...]  # what a right answer states (all of them)
    wrong: tuple[str, ...]  # values a right answer must not state instead (older, misstated, the other Sam)
    kind: str  # how values are recognised (a procedure kind, or "text")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Timeline:
    """One person's world over the live days (see the module's docstring)."""

    person: str
    seed: int
    days: int
    schedule: dict[str, tuple[int, ...]]
    changes: tuple[Change, ...]
    start: dict[str, Value]  # each procedure's value on day 1
    runs: tuple[generator.Request, ...]
    tells: tuple[Tell, ...]
    probes: tuple[Probe, ...]
    consent_run: dict[str, int]
    facts: dict[str, list[tuple[int, Any]]]  # every fact's values: [(from day, value), ...]
    phrasing: str
    history: generator.History = field(repr=False)

    # ── Ground truth ──────────────────────────────────────────────────

    def change_of(self, procedure: str) -> Change | None:
        return next((item for item in self.changes if item.procedure == procedure), None)

    def value(self, procedure: str, day: int) -> Value:
        """The procedure's fact in effect on ``day`` (a change's own day already has the new value)."""

        change = self.change_of(procedure)
        if change is not None and day >= change.day:
            return change.new
        return self.start[procedure]

    def fact_value(self, fact: str, day: int) -> Any:
        """Any fact's value on ``day`` (procedures, preferences, misstated facts, the Sams): None before it is known."""

        found = None
        for since, value in self.facts[fact]:
            if day >= since:
                found = value
        return found

    def procedure_of(self, run_id: str) -> str:
        return next(item.template.removeprefix("stale_") for item in self.runs if item.id == run_id)

    def by_day(self) -> dict[int, list[Any]]:
        """Each day's items in the order they come: what the person tells, the runs, then the probes."""

        found: dict[int, list[Any]] = {}
        for item in sorted((*self.tells, *self.runs, *self.probes), key=lambda item: (item.day, item.order)):
            found.setdefault(item.day, []).append(item)
        return found

    # ── The world ─────────────────────────────────────────────────────

    def runbook(self, day: int) -> str:
        return runbook_text(self, day)

    def files_on(self, day: int) -> dict[str, str]:
        """Workspace files the world writes on the morning of ``day`` (day 1: the runbook; a change's day: the
        runbook and the procedure's own document)."""

        changed = [item for item in self.changes if item.day == day]
        if day != 1 and not changed:
            return {}
        files = {RUNBOOK: self.runbook(day)}
        for change in changed:
            procedure = PROCEDURE[change.procedure]
            if procedure.doc:
                current = self.document(procedure.doc, day - 1)
                files[procedure.doc] = rewrite_document(procedure, current, change, day)
        return files

    def document(self, path: str, day: int) -> str:
        """An S1 document as it reads at the end of ``day`` (every change to it so far applied)."""

        text = self.history.start_files[path]
        for change in sorted(self.changes, key=lambda item: item.day):
            procedure = PROCEDURE[change.procedure]
            if procedure.doc == path and change.day <= day:
                text = rewrite_document(procedure, text, change, change.day)
        return text

    def letters(self, day: int) -> list[dict[str, Any]]:
        """The inbox's letters about the changes up to ``day``."""

        return [letter(change) for change in sorted(self.changes, key=lambda item: item.day) if change.day <= day]

    def apps_on(self, day: int) -> dict[str, Any]:
        apps = json.loads(json.dumps(self.history.start_apps))
        apps["mail"]["messages"].extend(self.letters(day))
        return apps

    # ── Record ────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {"format": FORMAT, "version": VERSION, "person": self.person, "seed": self.seed, "days": self.days,
                "phrasing": self.phrasing, "schedule": {key: list(value) for key, value in self.schedule.items()},
                "changes": [item.to_dict() for item in self.changes], "start": self.start,
                "consent_run": self.consent_run, "tells": [item.__dict__ for item in self.tells],
                "runs": [item.to_dict() for item in self.runs], "probes": [item.to_dict() for item in self.probes],
                "facts": self.facts, "past": self.history.fingerprint()}

    def fingerprint(self) -> str:
        text = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── The rule ─────────────────────────────────────────────────────────────────


def change_days(rng: random.Random, *, count: int = CHANGES_PER_PERSON, first: int = CHANGE_FIRST,
                last: int = CHANGE_LAST, gap: int = CHANGE_GAP) -> list[int]:
    """``count`` days drawn uniformly from ``first``-``last``, at least ``gap`` days apart (the pre-registration's
    rule: uniform over the allowed sets, by rejection)."""

    for _ in range(100_000):
        days = sorted(rng.randint(first, last) for _ in range(count))
        if all(later - earlier >= gap for earlier, later in zip(days, days[1:])):
            return days
    raise generator.GeneratorError("no change days fit the rule")


def changing_procedures(rng: random.Random, procedures: Iterable[Procedure] = PROCEDURES,
                        count: int = CHANGES_PER_PERSON) -> list[str]:
    """Which procedures change: one of each kind, then the rest drawn from those left (seeded)."""

    pool = list(procedures)
    chosen = [rng.choice([item for item in pool if item.kind == kind]).id for kind in KINDS]
    rest = sorted(item.id for item in pool if item.id not in chosen)
    chosen += rng.sample(rest, count - len(chosen))
    rng.shuffle(chosen)
    return chosen


# ── Rendering helpers ────────────────────────────────────────────────────────


def runbook_text(timeline: Timeline, day: int) -> str:
    when = generator.day_date(day)
    lines = ["# Kitchen runbook", "", "Lantern Lane Community Kitchen: who we deal with and the numbers we use.",
             f"Last updated {when.day} {when.strftime('%B %Y')}.", ""]
    for procedure in PROCEDURES:
        value = timeline.value(procedure.id, day)
        lines.append(f"- **{procedure.fact[:1].upper() + procedure.fact[1:]}** ({procedure.title}): "
                     f"{say(procedure.kind, value, with_name=procedure.kind in ('supplier', 'role'))}")
    lines.append("")
    return "\n".join(lines)


def rewrite_document(procedure: Procedure, text: str, change: Change, day: int) -> str:
    """S1's policy document with the rule changed (nothing else), as the world would rewrite it."""

    old, new = change.old["number"], change.new["number"]
    replacements = {
        "swap_reminder": (f"at least {old} hours before the shift", f"at least {new} hours before the shift"),
        "fridge_reminder": (f"Fridges run at {old} °C or below", f"Fridges run at {new} °C or below"),
        "shift_limit_reminder": (f"At most {old} shifts per volunteer per week",
                                 f"At most {new} shifts per volunteer per week"),
    }
    before, after = replacements[procedure.id]
    if before not in text:
        raise generator.GeneratorError(f"{procedure.doc} doesn't say {before!r}")
    when = generator.day_date(day)
    note = f"\n\nChanged on {when.day} {when.strftime('%B %Y')}: {procedure.fact} is now {say('policy', change.new)}.\n"
    return text.replace(before, after).rstrip("\n") + note


def letter(change: Change) -> dict[str, Any]:
    procedure = PROCEDURE[change.procedure]
    sender = procedure.letter_from.format(old_email=change.old.get("email", ""), new_email=change.new.get("email", ""))
    if change.kind == "address":
        body = (f"Hello Mara, from today please use {say('address', change.new)} - {say('address', change.old)} "
                "won't be checked any more.")
    elif change.kind == "supplier":
        body = (f"Hello Mara, welcome aboard as a customer of {change.new['name']}. Please send your orders to "
                f"{change.new['email']}.")
    elif change.kind == "role":
        body = (f"Hi Mara, {change.new['name']} is taking over as {procedure.party} from {change.old['name']}. "
                f"Please send everything for {procedure.party} to {change.new['email']}.")
    else:
        body = (f"Hi Mara, a change from today: {procedure.fact} is now {say(change.kind, change.new)} "
                f"(it was {say(change.kind, change.old)}).")
    return {"id": f"msg_change_{change.procedure}", "folder": "inbox", "from": sender, "to": "me@example.test",
            "subject": f"Change: {procedure.fact}", "body": body, "date": generator.day_date(change.day).isoformat()}


def tell_text(change: Change, template: str | None = None) -> str:
    procedure = PROCEDURE[change.procedure]
    fields = {"party": procedure.party, "fact": procedure.fact, "old": say(change.kind, change.old),
              "new": say(change.kind, change.new), "new_name": change.new.get("name", ""),
              "old_name": change.old.get("name", ""), "new_email": change.new.get("email", "")}
    return (template or TELL_TEMPLATES[change.kind]).format(**fields)


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


class Phrasings:
    """Templates and a phrasing model's kept variants (``phrasing.py``): a slot's options are its template and every
    kept variant. ``None`` means templates only."""

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        self.data = dict(data or {})
        self.label = str(self.data.get("model") or "templates")

    def options(self, slot: str, template: str) -> list[str]:
        kept = (self.data.get("slots") or {}).get(slot) or {}
        if kept.get("template") != template:
            return [template]  # the template changed since the variants were written: they don't apply
        variants = [item for item in kept.get("variants") or ()
                    if _placeholders(item) == _placeholders(template) and keeps_meaning(template, item)]
        return [template, *variants]


#: Words that turn a request into another one (a question, a draft, a check) when the template doesn't have them:
#: the first main run's phrasings had "Find out from finance how much ..." and "Draft a maintenance summary ...".
_OTHER_ACTION = re.compile(r"\b(draft|write up|put together|find out|check|what|how much|confirm|look up|update the|"
                           r"add to the|let \{alias\} know)\b", re.IGNORECASE)


def keeps_meaning(template: str, variant: str) -> bool:
    """Whether a phrasing keeps the request's action: no word that asks for something else, unless the template
    has it too."""

    lowered = template.lower()
    return not any(match.group(0).lower() not in lowered for match in _OTHER_ACTION.finditer(variant))


def slots() -> dict[str, str]:
    """Every phrasable message's template, by slot id (what ``phrasing.py`` asks a model to vary)."""

    found: dict[str, str] = {}
    for procedure in PROCEDURES:
        for number, text in enumerate(procedure.short):
            found[f"short/{procedure.id}/{number}"] = text
    for kind, text in TELL_TEMPLATES.items():
        found[f"tell/{kind}"] = text
    return found


# ── Building the timeline ────────────────────────────────────────────────────


def _date_after(day: int, rng: random.Random, *, weekday_only: bool) -> date:
    today = generator.day_date(day)
    options = [today + timedelta(days=step) for step in range(1, 8)
               if not weekday_only or (today + timedelta(days=step)).weekday() < 5]
    return rng.choice(options)


def _monday(day: int) -> date:
    today = generator.day_date(day)
    return today - timedelta(days=today.weekday())


def _run(procedure: Procedure, day: int, order: int, rng: random.Random, apps: dict[str, Any], value: Value,
         old: Value | None, first: bool, phrasings: Phrasings, words: random.Random,
         extra: str = "") -> generator.Request:
    from row_bot.learning.packs import PackTask, Solution, Verifier, validate

    fields: dict[str, Any] = {"alias": rng.choice((procedure.party, *procedure.aliases)), "to": procedure.to}
    if procedure.variable == "n":
        fields["n"] = rng.randint(procedure.low, procedure.high)
    when = None
    if procedure.variable in ("date", "who_date"):
        when = (_monday(day) if procedure.kind == "amount"
                else _date_after(day, rng, weekday_only=procedure.id != "deep_clean"))
        fields["date"] = when.isoformat()
    if procedure.variable == "who_date":
        fields["who"] = rng.choice(_WHOS)
    if first:
        fields.update(value=say(procedure.kind, value), name=value.get("name", ""))
        prompt = procedure.intro.format(**fields)
    else:
        # The words have their own draws, so a phrasing file changes the wording and nothing else.
        slot = words.randrange(len(procedure.short))
        options = phrasings.options(f"short/{procedure.id}/{slot}", procedure.short[slot])
        prompt = words.choice(options).format(**fields)
    if extra:
        prompt = IN_PASSING.format(prompt=prompt, tell=extra)
    run_id = f"{PERSON}-{RUN_PREFIX}-d{day:03d}-{procedure.id}"
    if procedure.action == "event":
        title = procedure.event.format(who=fields.get("who", ""))
        call = {"tool": "calendar_create_event",
                "args": {"title": title, "date": when.isoformat(), "start": value["start"], "end": value["end"]}}
        where = {"title": {"contains": procedure.keyword}, "date": when.isoformat(), "start": value["start"]}
        checks = [{"in": "calendar.events", "where": where, "count": _matching(apps, "calendar.events", where) + 1}]
        wrong = (dict(call["args"], start=old["start"], end=old["end"]) if old
                 else dict(call["args"], date=(when + timedelta(days=7)).isoformat()))
        intent = (f"One calendar event '{title}' on {when.isoformat()} from {value['start']} to {value['end']} "
                  f"({procedure.fact} on {generator.day_date(day).isoformat()}).")
        tools = ("calendar",)
    else:
        to = value["email"] if procedure.kind in ("address", "supplier", "role") else procedure.to
        must = str(fields["n"]) if procedure.variable == "n" else str(value.get("amount", value.get("number")))
        subject = procedure.title[4:].capitalize() if procedure.title.startswith("the ") else procedure.title
        body = {"amount": f"Hello, please send this week's {procedure.fact.removeprefix('the ')}: "
                          f"{say(procedure.kind, value)}.",
                "policy": f"Hello all, a reminder: {procedure.fact.removeprefix('the ')} is {say(procedure.kind, value)}."}
        text = body.get(procedure.kind) or f"Hello, {fields.get('n')} {procedure.unit}. Thanks, Mara"
        call = {"tool": "mail_send", "args": {"to": to, "subject": subject, "body": text}}
        where = {"folder": "sent", "to": to, "body": {"contains": must}}
        # S1's past left sent emails in the apps (its reminders to the volunteers, say): one more than those.
        checks = [{"in": "mail.messages", "where": where, "count": _matching(apps, "mail.messages", where) + 1}]
        if old and procedure.kind in ("address", "supplier", "role"):
            wrong = dict(call["args"], to=old["email"])
        elif old:
            wrong = dict(call["args"], body=text.replace(say(procedure.kind, value), say(procedure.kind, old)))
        else:
            wrong = dict(call["args"], to="someone@example.test")
        intent = (f"One email to {to} for {procedure.title}"
                  + (f" ({fields['n']} {procedure.unit})" if procedure.variable == "n" else "")
                  + (f", stating {procedure.fact}: {say(procedure.kind, value)}" if procedure.kind in ("amount", "policy")
                     else f" - {procedure.fact} on {generator.day_date(day).isoformat()} is "
                          f"{say(procedure.kind, value, with_name=True)}") + ".")
        tools = ("mail",)
    task = PackTask(id=run_id, pack="tool_workflows", split="visible", prompt=prompt,
                    verifier=Verifier("app_state", checks), reference=Solution(answer="Done.", calls=(call,)),
                    decoy=Solution(answer="Done.", calls=({"tool": call["tool"], "args": wrong},)),
                    files={}, apps=apps, tools=tools, max_steps=10)
    validate(task)
    return generator.Request(id=run_id, person=PERSON, day=day, order=order, template=f"stale_{procedure.id}",
                             intended="tool_workflows", prompt=prompt, intent=intent, task=task, inputs={})


def build(*, seed: int = SEED, days: int = DAYS, phrasing: Mapping[str, Any] | None = None) -> Timeline:
    """Mara's timeline for ``seed`` (the same seed and phrasing data, the same timeline)."""

    phrasings = Phrasings(phrasing)
    history = generator.generate(PERSON, seed=seed, days=0)

    def rng(name: str) -> random.Random:
        return random.Random(f"{int(seed)}/{PERSON}/stale/{name}")

    # Schedule: four procedures twice a week, the rest once (fixed weekdays, Monday 0).
    plan = rng("schedule")
    twice = set(plan.sample([item.id for item in PROCEDURES], TWICE_WEEKLY))
    schedule = {item.id: (tuple(plan.choice(_WEEKDAY_PAIRS)) if item.id in twice else (plan.randrange(5),))
                for item in PROCEDURES}
    # Change events, by the rule; values; channels.
    days_drawn = change_days(rng("change_days"))
    which = changing_procedures(rng("changing"))
    values = rng("values")
    start: dict[str, Value] = {}
    changes: list[Change] = []
    channel_rng = rng("channels")
    by_procedure = dict(zip(which, days_drawn))
    for procedure in PROCEDURES:
        old = procedure.pool[0] if procedure.fixed_old else values.choice(procedure.pool)
        start[procedure.id] = old
        if procedure.id in by_procedure:
            new = values.choice([item for item in procedure.pool if item != old])
            day = by_procedure[procedure.id]
            weekday = generator.day_date(day).weekday()
            hosts = any(weekday in schedule[item.id] for item in PROCEDURES if item.id != procedure.id)
            drawn = "in_passing" if channel_rng.random() < 0.5 else "told"
            changes.append(Change(procedure.id, procedure.kind, day, old, new, drawn if hosts else "told"))
    changes.sort(key=lambda item: item.day)
    change_by = {item.procedure: item for item in changes}

    # Declarative events: preferences (stated early, changed once), misstated facts, the Sams.
    events = rng("events")
    tells: list[Tell] = []
    facts: dict[str, list[tuple[int, Any]]] = {}
    for procedure in PROCEDURES:
        facts[procedure.id] = [(1, start[procedure.id])] + (
            [(change_by[procedure.id].day, change_by[procedure.id].new)] if procedure.id in change_by else [])
    day_items: dict[int, list[tuple[str, Any]]] = {}
    for homonym in HOMONYMS:
        day = events.randint(2, 6)
        facts[homonym["id"]] = [(day, homonym["email"])]
        day_items.setdefault(day, []).append(("homonym", homonym))
    for preference in PREFERENCES:
        first, later = events.randint(2, 12), events.randint(30, 105)
        facts[preference["id"]] = [(first, preference["values"][0]), (later, preference["values"][1])]
        day_items.setdefault(first, []).append(("preference", (preference, 0)))
        day_items.setdefault(later, []).append(("preference", (preference, 1)))
    for item in MISSTATED:
        day = events.randint(15, 100)
        wrong, right = events.choice(item["pairs"])
        facts[item["id"]] = [(day, right)]
        day_items.setdefault(day, []).append(("misstated", (item, wrong, right)))

    consent = {item.id: 2 + int(draw(seed, PERSON, item.id, "stale_consent") < 0.5) for item in PROCEDURES}
    request_rng = rng("requests")
    tell_rng = rng("tell_words")
    words_rng = rng("request_words")
    runs: list[generator.Request] = []
    ran: set[str] = set()
    for day in range(1, days + 1):
        order = 0
        weekday = generator.day_date(day).weekday()
        todays = [item for item in PROCEDURES if weekday in schedule[item.id]]
        for what, payload in day_items.get(day, []):
            if what == "homonym":
                tells.append(Tell(f"{PERSON}-{RUN_PREFIX}-d{day:03d}-tell-{payload['id']}", day, order,
                                  payload["id"], what, (payload["say"],)))
            elif what == "preference":
                preference, index = payload
                value = preference["values"][index]
                text = (preference["say_first"] if index == 0 else preference["say_change"]).format(value=value)
                tells.append(Tell(f"{PERSON}-{RUN_PREFIX}-d{day:03d}-tell-{preference['id']}-{index}", day, order,
                                  preference["id"], what, (text,)))
            else:
                item, wrong, right = payload
                tells.append(Tell(f"{PERSON}-{RUN_PREFIX}-d{day:03d}-tell-{item['id']}", day, order, item["id"], what,
                                  (item["say"].format(wrong=wrong), item["fix"].format(wrong=wrong, right=right))))
            order += 1
        passing: dict[str, str] = {}  # host procedure -> what it carries
        for change in [item for item in changes if item.day == day]:
            words = tell_text(change, tell_rng.choice(phrasings.options(f"tell/{change.kind}",
                                                                        TELL_TEMPLATES[change.kind])))
            hosts = [item.id for item in todays if item.id != change.procedure and item.id not in passing]
            if change.channel == "in_passing" and hosts:
                passing[hosts[0]] = words
            else:
                tells.append(Tell(f"{PERSON}-{RUN_PREFIX}-d{day:03d}-tell-{change.procedure}", day, order,
                                  change.procedure, "change", (words,)))
                order += 1
        apps = None
        hosts_first = sorted(todays, key=lambda item: (item.id not in passing, PROCEDURES.index(item)))
        for procedure in hosts_first:
            apps = apps if apps is not None else _apps_for(history, changes, day)
            change = change_by.get(procedure.id)
            after = change is not None and day >= change.day
            value = change.new if after else start[procedure.id]
            runs.append(_run(procedure, day, order, request_rng, apps, value, change.old if after else None,
                             procedure.id not in ran, phrasings, words_rng, passing.get(procedure.id, "")))
            ran.add(procedure.id)
            order += 1
    timeline = Timeline(person=PERSON, seed=int(seed), days=days, schedule=schedule, changes=tuple(changes),
                        start=start, runs=tuple(runs), tells=tuple(tells), probes=(), consent_run=consent,
                        facts=facts, phrasing=phrasings.label, history=history)
    probes = tuple(probe for day in PROBE_DAYS if day <= days
                   for probe in make_probes(timeline, day, rng(f"probes/{day}"), order=500))
    return Timeline(**{**timeline.__dict__, "probes": probes})


def _apps_for(history: generator.History, changes: list[Change], day: int) -> dict[str, Any]:
    apps = json.loads(json.dumps(history.start_apps))
    apps["mail"]["messages"].extend(letter(item) for item in changes if item.day <= day)
    return apps


# ── Probes ───────────────────────────────────────────────────────────────────


def _when(day: int) -> str:
    when = generator.day_date(day)
    return f"{when.day} {when.strftime('%B %Y')}"


def _values(procedure: Procedure, timeline: Timeline) -> list[Value]:
    return [value for _, value in timeline.facts[procedure.id]]


def make_probes(timeline: Timeline, day: int, rng: random.Random, *, order: int = 500) -> list[Probe]:
    """Day ``day``'s probes: two per category, each with its expected answer from the timeline."""

    changed = [item for item in timeline.changes if item.day <= day]
    # Update detection asks about the changes of the last 30 days first, then older ones.
    recent = sorted(changed, key=lambda item: (item.day <= day - 30, -item.day))
    probes: list[Probe] = []

    def add(category: str, fact: str, question: str, expected: Iterable[str], wrong: Iterable[str], kind: str) -> None:
        probes.append(Probe(f"{PERSON}-{RUN_PREFIX}-d{day:03d}-probe-{category}-{len(probes)}", day,
                            order + len(probes), category, fact, question, tuple(expected), tuple(wrong), kind))

    def procedure_json(procedure: Procedure, value: Value) -> str:
        return json.dumps(value, sort_keys=True)

    # Current fact: changed procedures first, then the declarative facts known by then, then unchanged ones.
    candidates: list[tuple[str, str]] = [("procedure", item.procedure) for item in changed]
    candidates += [("declarative", item["id"]) for item in (*PREFERENCES, *MISSTATED)
                   if timeline.fact_value(item["id"], day) is not None]
    rng.shuffle(candidates)
    unchanged = [item.id for item in PROCEDURES if timeline.change_of(item.id) is None]
    candidates += [("procedure", rng.choice(unchanged))]
    for source, fact in candidates[:PROBES_PER_CATEGORY]:
        if source == "procedure":
            procedure = PROCEDURE[fact]
            now = timeline.value(fact, day)
            others = [procedure_json(procedure, value) for value in _values(procedure, timeline) if value != now]
            add("current", fact, f"What's {procedure.fact} now?", [procedure_json(procedure, now)], others,
                procedure.kind)
        else:
            spec = next(item for item in (*PREFERENCES, *MISSTATED) if item["id"] == fact)
            now = timeline.fact_value(fact, day)
            if fact in {item["id"] for item in MISSTATED}:
                wrong = [pair[0] for pair in spec["pairs"] if pair[1] == now]
                add("current", fact, f"Remind me: what's {spec['fact']}?", [str(now)], wrong, "date")
            else:
                marker = dict(zip(spec["values"], spec["markers"]))
                add("current", fact, f"Remind me: what's {spec['fact']}?", [marker[now]],
                    [marker[value] for value in spec["values"] if value != now], "marker")
    # As-of fact: a changed procedure's value on a day before its change.
    for change in (rng.sample(changed, min(PROBES_PER_CATEGORY, len(changed))) if changed else []):
        procedure = PROCEDURE[change.procedure]
        before = max(1, change.day - rng.randint(2, 7))
        add("as_of", change.procedure, f"What was {procedure.fact} on {_when(before)}?",
            [procedure_json(procedure, change.old)], [procedure_json(procedure, change.new)], procedure.kind)
    # Alias and homonym: a party by another name; one of the Sams by name.
    procedure = PROCEDURE[rng.choice([item.id for item in PROCEDURES if item.kind in ("address", "supplier", "role")])]
    alias = rng.choice(procedure.aliases)
    now = timeline.value(procedure.id, day)
    add("alias_homonym", procedure.id, f"What's the email address for {alias}?", [procedure_json(procedure, now)],
        [procedure_json(procedure, value) for value in _values(procedure, timeline) if value != now], procedure.kind)
    sam = rng.choice(HOMONYMS)
    other = next(item for item in HOMONYMS if item is not sam)
    add("alias_homonym", sam["id"], f"What's the email address of the Sam who is {sam['role']}?", [sam["email"]],
        [other["email"]], "email")
    # Update detection: a recent change, both values.
    for change in recent[:PROBES_PER_CATEGORY]:
        procedure = PROCEDURE[change.procedure]
        add("update", change.procedure, f"Has {procedure.fact} changed lately? If it has, what was it before and what "
            "is it now?", [procedure_json(procedure, change.new), procedure_json(procedure, change.old)], [],
            procedure.kind)
    # Abstention: facts never stated.
    for item in rng.sample(list(UNKNOWN), PROBES_PER_CATEGORY):
        add("abstention", item["id"], item["question"], [], [], "abstain")
    return probes


__all__ = ["Change", "DAYS", "KINDS", "PERSON", "PROBE_DAYS", "PROCEDURE", "PROCEDURES", "Phrasings", "Probe",
           "Procedure", "SEED", "Tell", "Timeline", "WINDOW_DAYS", "build", "change_days", "changing_procedures",
           "letter", "say", "slots", "states", "tell_text"]
