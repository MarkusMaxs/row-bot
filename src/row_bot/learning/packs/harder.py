"""The harder split (O31): tasks where the main model really fails, so "a change vs none" can show a
difference.

GPT-6.1 Sol passes nearly every task of the visible and hidden splits (38/38 at L0, 192/192 in S1's
release check), so a lesson or a way of working tested only there can't show a gain. These tasks keep the
packs' four kinds of work and their executable checks, and add what real work has and the packs don't:

- **long documents with distractors**: a current handbook beside a superseded one, amendments at the end
  that change a figure stated early on, a draft that proposes what was never agreed, values stated per
  month beside values per year, a project whose name is one letter from another's;
- **messy data**: mixed date formats, the same row twice with different spacing, refunds in another file,
  daily exchange rates with weekend gaps, managers named by email;
- **multi-step tool workflows**: finding a free slot across three days of a full calendar, an inbox worked
  through by rules, duplicates that only an issue's comments reveal, a week's meetings moved but not the
  tentative ones;
- **coding edge cases** (they need a container, like the packs' coding tasks).

Every task has a reference that passes and a decoy (the plausible mistake) that fails, like the packs. They
are **visible** (``split="visible"``): testing changes and the paper's runs use them; drafting, practice and
training never read them, and the audit vault never holds them. The content is generated deterministically
here (no file is read).
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from textwrap import dedent
from typing import Any

from row_bot.learning.packs import PackTask, Solution, Verifier

PREFIX = "hard_"


def _rng(number: int) -> random.Random:
    return random.Random(20_261_023 + number)


_FILLER_TOPICS = ("approvals", "receipts", "bookings", "reporting", "records", "exceptions", "reviews", "audits",
                  "deadlines", "systems", "contacts", "training")
_FILLER_VERBS = ("is recorded", "is reviewed", "is checked", "is kept", "is confirmed", "is shared", "is archived")
_FILLER_WHO = ("the requester", "the approver", "the finance team", "the team lead", "the office manager",
               "the budget owner", "the travel desk")


def _filler(rng: random.Random, sentences: int) -> str:
    """Plausible procedure text that answers nothing the tasks ask."""

    out = []
    for _ in range(sentences):
        topic, verb, who = rng.choice(_FILLER_TOPICS), rng.choice(_FILLER_VERBS), rng.choice(_FILLER_WHO)
        days = rng.choice((2, 3, 5, 7, 10, 14))
        out.append(f"Every change to {topic} {verb} by {who} within {days} working days, and the outcome is noted "
                   f"in the {topic} log.")
    return " ".join(out)


def _doc(doc_id: str, title: str, owner: str, updated: str, body: str, status: str = "current") -> str:
    return f"ID: {doc_id}\nTitle: {title}\nOwner: {owner}\nUpdated: {updated}\nStatus: {status}\n\n{body.strip()}\n"


def _answer_task(number: int, pack: str, prompt: str, files: dict[str, str], expected: Any, wrong: str, *,
                 match: str = "exact", answer: str | None = None, max_steps: int = 14) -> PackTask:
    return PackTask(
        id=f"{PREFIX}doc_{number:02d}", pack=pack, split="visible", prompt=prompt,
        verifier=Verifier("answer", expected, match=match),
        reference=Solution(answer=answer if answer is not None else str(expected)), decoy=Solution(answer=wrong),
        files=files, tools=("files",), max_steps=max_steps,
    )


# ── Document lookups ────────────────────────────────────────────────────────


def _handbook() -> dict[str, str]:
    rng = _rng(1)
    sections = []
    for number, name in enumerate(("Purpose", "Who this applies to", "Booking travel", "Meals and client dinners",
                                   "Hotels", "Rail and air", "Claims", "Receipts", "Capital cities",
                                   "Approvals", "Exceptions", "Records"), start=1):
        body = _filler(rng, 14)
        if name == "Meals and client dinners":
            body = ("Client dinners: up to 70 EUR per person. In a capital city (section 9) the cap is 90 EUR per "
                    "person instead. Team meals are covered only when a director approves them in advance. " + body)
        if name == "Capital cities":
            body = ("The capital cities for this handbook are: Amsterdam, Berlin, Brussels, Copenhagen, Dublin, "
                    "Lisbon, Madrid, Paris, Rome, Stockholm, Vienna and Warsaw. Porto, Barcelona and Milan are not "
                    "capital cities. " + body)
        sections.append(f"## {number}. {name}\n\n{body}")
    amendments = (
        "## Amendments\n\n"
        "Amendment 1 (2025-03-01): withdrawn before it took effect; ignore it.\n\n"
        "Amendment 2 (2025-09-01): the client dinner cap in a capital city rises to 95 EUR per person.\n\n"
        "Amendment 3 (2026-01-15): the client dinner cap outside capital cities rises to 75 EUR per person.\n"
    )
    current = _doc("FIN-207", "Expenses handbook (v7)", "Finance", "2026-01-15",
                   "\n\n".join(sections) + "\n\n" + amendments)
    old_rng = _rng(101)
    old = _doc("FIN-188", "Expenses handbook (v6)", "Finance", "2024-05-02",
               "This handbook is superseded by FIN-207.\n\n## 4. Meals and client dinners\n\nClient dinners: up to "
               "110 EUR per person in capital cities and 80 EUR elsewhere. " + _filler(old_rng, 40),
               status="superseded")
    return {"docs/fin-207-expenses-handbook.md": current, "docs/fin-188-expenses-handbook.md": old}


def _minutes() -> dict[str, str]:
    rng = _rng(2)
    files: dict[str, str] = {}
    special = {
        104: ("2026-01-12", "Project Kite: the launch is planned for 2026-04-01."),
        109: ("2026-02-02", "Project Kite: the launch moves to 2026-04-22 because of the vendor's API limits."),
        115: ("2026-02-23", "Project Kite: the launch is confirmed for 2026-05-06 after the security review."),
        118: ("2026-03-02", "Proposal (not agreed): move the Kite launch to 2026-05-20."),
        120: ("2026-03-09", "Project Kestrel: its launch moves to 2026-05-27."),
        121: ("2026-03-16", "Project Kite: no change to the launch date."),
    }
    day = 0
    for number in range(100, 124):
        date, line = special.get(number, (f"2026-{1 + day // 28:02d}-{1 + day % 28:02d}", ""))
        day += 3
        decisions = [line] if line else []
        decisions += [f"Project {rng.choice(('Heron', 'Atlas', 'Kestrel', 'Lark'))}: "
                      f"{rng.choice(('budget reviewed', 'risks updated', 'owners confirmed', 'no change'))}."
                      for _ in range(3)]
        status = "draft" if number == 118 else "current"
        body = "Attendees: Ines Duarte, Tomas Berg, Mara Lindqvist.\nDecisions:\n" + "\n".join(
            f"- {item}" for item in decisions) + "\n\nNotes: " + _filler(rng, 6)
        files[f"minutes/mtg-{number}.md"] = _doc(f"MTG-{number}", "Programme sync", "PMO", date, body, status)
    return files


_SUPPLIERS = (
    # name, (value, "year"|"month"), term end, addendum (new end) or None
    ("Alder Freight", (18_500, "year"), "2026-08-31", None),
    ("Brightwater Logistics", (26_000, "year"), "2026-09-30", None),
    ("Calder Office", (2_400, "month"), "2026-06-30", None),
    ("Dunmore Cleaning", (20_000, "year"), "2026-07-31", None),
    ("Eskdale Security", (3_100, "month"), "2026-08-15", "2027-08-15"),
    ("Fenwick IT", (54_000, "year"), "2026-12-31", None),
    ("Grayson Catering", (1_200, "month"), "2026-09-01", None),
    ("Halden Print", (1_800, "month"), "2026-07-15", None),
    ("Ingram Couriers", (9_000, "year"), "2026-09-30", None),
    ("Juniper Energy", (61_000, "year"), "2026-10-01", None),
    ("Kestrel Supply", (22_500, "year"), "2027-01-31", None),
    ("Linden Waste", (1_650, "month"), "2026-08-31", None),
    ("Marlow Legal", (40_000, "year"), "2026-06-30", "2026-09-30"),
    ("Northwind Paper", (12_000, "year"), "2026-05-15", None),
    ("Orrin Analytics", (33_000, "year"), "2026-08-01", None),
    ("Pell Facilities", (19_999, "year"), "2026-07-20", None),
)


def _contracts() -> tuple[dict[str, str], list[str], list[str]]:
    rng = _rng(3)
    files: dict[str, str] = {}
    for index, (name, (amount, per), end, addendum) in enumerate(_SUPPLIERS, start=1):
        value = f"{amount:,} EUR a {per}"
        body = (f"Supplier: {name}\nValue: {value}\nTerm ends: {end}\n\n" + _filler(rng, 5))
        if addendum:
            body += f"\n\nAddendum (signed 2026-02-10): the term is extended; it now ends on {addendum}.\n"
        files[f"contracts/sup-{index:03d}.md"] = _doc(f"SUP-{index:03d}", f"Contract: {name}", "Procurement",
                                                    "2026-02-10", body)

    def chosen(*, addenda: bool) -> list[str]:
        found = []
        for name, (amount, per), end, addendum in _SUPPLIERS:
            final = addendum if addenda and addendum else end
            yearly = amount * 12 if per == "month" else amount
            if "2026-07-01" <= final <= "2026-09-30" and yearly > 20_000:
                found.append(name)
        return sorted(found)

    return files, chosen(addenda=True), chosen(addenda=False)


def _leave_policy() -> dict[str, str]:
    rng = _rng(4)
    body = "\n\n".join((
        "## 1. Annual leave\n\nFull-time staff (5 days a week) get 26 days of annual leave a year. " + _filler(rng, 10),
        "## 2. Service\n\nAfter 5 full years of service, full-time staff get 2 more days a year (28 in all). "
        "After 10 years, 3 more days instead of 2. " + _filler(rng, 10),
        "## 3. Carry-over\n\nUp to 5 unused days carry over into the next year. " + _filler(rng, 10),
        "## 4. Part-time staff\n\nPart-time staff get the full-time entitlement (sections 1 and 2) in proportion to "
        "the days they work each week, rounded up to the next half day. " + _filler(rng, 10),
        "## 5. Public holidays\n\nPublic holidays are not part of annual leave. " + _filler(rng, 10),
    ))
    return {"docs/hr-310-leave.md": _doc("HR-310", "Leave policy", "People", "2026-01-02", body),
            "docs/hr-210-leave.md": _doc("HR-210", "Leave policy", "People", "2024-11-20",
                                         "Superseded by HR-310. Part-time staff get 20 days a year whatever they work.",
                                         status="superseded")}


def _price_lists() -> dict[str, str]:
    rng = _rng(5)
    products = (("X100", 98.00, 112.00), ("X200", 145.00, 168.00), ("X200-Pro", 189.00, 219.00),
                ("X300", 240.00, 279.00))
    uk = "\n".join(f"| {code} | {gbp:.2f} GBP |" for code, gbp, _eur in products)
    eu = "\n".join(f"| {code} | {eur:.2f} EUR |" for code, _gbp, eur in products)
    return {
        "docs/prc-12-uk-prices.md": _doc("PRC-12", "UK price list 2026", "Sales", "2026-01-05",
                                         "Prices per unit, before VAT.\n\n| Product | Price |\n" + uk + "\n\n"
                                         + _filler(rng, 6)),
        "docs/prc-11-eu-prices.md": _doc("PRC-11", "EU price list 2026", "Sales", "2026-01-05",
                                         "Prices per unit, before VAT.\n\n| Product | Price |\n" + eu + "\n\n"
                                         + _filler(rng, 6)),
        "docs/prc-20-volume-discounts.md": _doc(
            "PRC-20", "Volume discounts", "Sales", "2026-01-20",
            "Business customers get a volume discount on the units of one product in one order: 10 to 24 units, "
            "8%; 25 units or more, 12%. For UK customers the discount applies to the price before VAT, and VAT at "
            "20% is added after the discount. Consumers get no volume discount. " + _filler(rng, 6)),
        "docs/prc-15-launch-offer.md": _doc(
            "PRC-15", "X200 launch offer", "Sales", "2025-12-01",
            "Launch offer: 10 GBP off each X200 until 2026-02-28. The offer has ended.", status="expired"),
    }


def _document_tasks() -> list[PackTask]:
    contracts, suppliers, suppliers_wrong = _contracts()
    x200 = (Decimal("145.00") * 12 * Decimal("0.92") * Decimal("1.2")).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return [
        _answer_task(1, "document_lookups",
                     "Under the current expenses handbook in docs/, what is the per-person cap for a client dinner "
                     "in Lisbon, in EUR? Reply with the number.",
                     _handbook(), 95, "90", match="number"),
        _answer_task(2, "document_lookups",
                     "From the programme sync minutes in minutes/, what is the agreed launch date for Project Kite? "
                     "Reply with the date as YYYY-MM-DD.",
                     _minutes(), "2026-05-06", "2026-05-20"),
        PackTask(
            id=f"{PREFIX}doc_03", pack="document_lookups", split="visible",
            prompt="Read every contract in contracts/. Which suppliers have a contract that ends between 2026-07-01 "
                   "and 2026-09-30 (both included) and is worth more than 20,000 EUR a year? Reply with only a "
                   "JSON list of the supplier names, sorted alphabetically.",
            verifier=Verifier("json_answer", suppliers), reference=Solution(answer=json.dumps(suppliers)),
            decoy=Solution(answer=json.dumps(suppliers_wrong)), files=contracts, tools=("files",), max_steps=24),
        _answer_task(4, "document_lookups",
                     "Using the current leave policy in docs/, how many days of annual leave a year does someone get "
                     "who works 3 days a week and has 7 full years of service? Reply with the number.",
                     _leave_policy(), 17, "16.8", match="number"),
        _answer_task(5, "document_lookups",
                     "Using the documents in docs/, what do 12 units of the X200 cost a UK business customer in one "
                     "order, including VAT, in GBP? Reply with the amount, rounded to 2 decimals.",
                     _price_lists(), float(x200), "2088.00", match="number"),
    ]


# ── Structured transforms ───────────────────────────────────────────────────


def _file_task(number: int, prompt: str, files: dict[str, str], path: str, match: str, expected: Any,
               wrong: str, *, max_steps: int = 12) -> PackTask:
    text = expected if isinstance(expected, str) else json.dumps(expected, indent=2) + "\n"
    stored = json.loads(text) if match == "json" else text
    return PackTask(
        id=f"{PREFIX}st_{number:02d}", pack="structured_transforms", split="visible",
        prompt=prompt + " Write only that file, then reply with: done",
        verifier=Verifier("file", stored, match=match, path=path),
        reference=Solution(answer="done", files={path: text}), decoy=Solution(answer="done", files={path: wrong}),
        files=files, tools=("files",), max_steps=max_steps,
    )


def _csv(rows: Iterable[Iterable[object]]) -> str:
    return "".join(",".join(str(cell) for cell in row) + "\n" for row in rows)


def _transactions() -> tuple[dict[str, str], dict[str, int], dict[str, int]]:
    a = [["id", "date", "customer", "amount_eur"],
         ["T-101", "2026-03-02", "Acme", "120.50"], ["T-102", "03/03/2026", "Globex", "80.00"],
         ["T-103", "March 4, 2026", "ACME ", "19.99"], ["T-104", "2026-03-05", "Initech", "300.00"],
         ["T-105", "06/03/2026", "globex", "45.10"], ["T-106", "2026-03-07", "Umbrella", "12.00"],
         ["T-107", "March 9, 2026", "Initech", "75.25"]]
    b = [["id", "date", "customer", "amount_eur"],
         ["T-105", "2026-03-06", " Globex", "45.10"], ["T-108", "2026-03-10", "acme", "60.00"],
         ["T-109", "11/03/2026", "Umbrella", "88.40"], ["T-103", "2026-03-04", "Acme", "19.99"],
         ["T-110", "March 12, 2026", "Globex", "5.05"]]
    refunds = [["refunded_id", "reason"], ["T-104", "damaged"], ["T-109", "duplicate charge"]]
    totals: dict[str, int] = defaultdict(int)
    wrong: dict[str, int] = defaultdict(int)
    seen: set[str] = set()
    refunded = {row[0] for row in refunds[1:]}
    for row in [*a[1:], *b[1:]]:
        cents = int(Decimal(row[3]) * 100)
        wrong[row[2].strip().lower()] += cents  # counts duplicates and refunds
        if row[0] in seen or row[0] in refunded:
            continue
        seen.add(row[0])
        totals[row[2].strip().lower()] += cents
    files = {"transactions_a.csv": _csv(a), "transactions_b.csv": _csv(b), "refunds.csv": _csv(refunds)}
    return files, dict(totals), dict(wrong)


def _payments() -> tuple[dict[str, str], str, str]:
    payments = [["date", "amount", "currency"],
                ["2026-02-26", "100.00", "EUR"], ["2026-02-27", "200.00", "USD"], ["2026-02-28", "50.00", "GBP"],
                ["2026-03-01", "80.00", "USD"], ["2026-03-02", "40.00", "GBP"], ["2026-03-03", "19.99", "EUR"],
                ["2026-03-06", "120.00", "USD"], ["2026-03-07", "60.00", "GBP"]]
    # Rates are per 1 EUR; none on weekends (2026-02-28, 03-01, 03-07, 03-08): use the last earlier rate.
    rates = [["date", "usd_per_eur", "gbp_per_eur"],
             ["2026-02-26", "1.0820", "0.8550"], ["2026-02-27", "1.0850", "0.8570"],
             ["2026-03-02", "1.0790", "0.8530"], ["2026-03-03", "1.0800", "0.8540"],
             ["2026-03-04", "1.0830", "0.8560"], ["2026-03-05", "1.0810", "0.8550"],
             ["2026-03-06", "1.0870", "0.8590"]]
    table = {row[0]: (Decimal(row[1]), Decimal(row[2])) for row in rates[1:]}

    def rate(day: str, *, latest_earlier: bool) -> tuple[Decimal, Decimal]:
        if day in table:
            return table[day]
        before = [key for key in table if key < day]
        after = [key for key in table if key > day]
        if latest_earlier or not after:
            return table[max(before)]
        return table[min(after)]  # the decoy: the next day's rate

    def totals(*, latest_earlier: bool) -> str:
        months: dict[str, Decimal] = defaultdict(Decimal)
        for day, amount, currency in payments[1:]:
            value = Decimal(amount)
            usd, gbp = rate(day, latest_earlier=latest_earlier)
            eur = value if currency == "EUR" else value / (usd if currency == "USD" else gbp)
            months[day[:7]] += eur.quantize(Decimal("0.01"), ROUND_HALF_UP)
        return _csv([["month", "total_eur"], *[[month, f"{total:.2f}"] for month, total in sorted(months.items())]])

    files = {"payments.csv": _csv(payments), "rates.csv": _csv(rates)}
    return files, totals(latest_earlier=True), totals(latest_earlier=False)


def _org() -> tuple[dict[str, str], str, str]:
    people = [
        {"name": "Ruth Okafor", "email": "ruth@example.test", "manager": ""},
        {"name": "Ari Cohen", "email": "ari@example.test", "manager": "ruth@example.test"},
        {"name": "Mei Tan", "email": "mei@example.test", "manager": "ruth@example.test"},
        {"name": "Bo Lind", "email": "bo@example.test", "manager": "ari@example.test"},
        {"name": "Cleo Park", "email": "cleo@example.test", "manager": "ari@example.test"},
        {"name": "Dev Rao", "email": "dev@example.test", "manager": "cleo@example.test"},
        {"name": "Esme Ward", "email": "esme@example.test", "manager": "mei@example.test"},
        {"name": "Finn Shaw", "email": "finn@example.test", "manager": "cleo@example.test"},
        {"name": "Gail Moss", "email": "gail@example.test", "manager": "ruth@example.test"},
    ]
    by_email = {item["email"]: item["name"] for item in people}
    children: dict[str, list[str]] = defaultdict(list)
    for item in people:
        if item["manager"]:
            children[item["manager"]].append(item["email"])

    def below(email: str) -> int:
        return sum(1 + below(child) for child in children.get(email, []))

    rows = [["manager", "direct_reports", "all_reports"]]
    wrong = [["manager", "direct_reports", "all_reports"]]
    for email in sorted(children, key=lambda key: by_email[key]):
        rows.append([by_email[email], len(children[email]), below(email)])
        wrong.append([by_email[email], len(children[email]), len(children[email])])
    return {"people.json": json.dumps({"people": people}, indent=2) + "\n"}, _csv(rows), _csv(wrong)


def _actions() -> tuple[dict[str, str], list[dict[str, str]], list[dict[str, str]]]:
    notes = dedent("""
        Meeting date: Thursday 2026-03-12
        Present: Ines, Tomas, Mara, Leo

        - Ines will send the supplier shortlist in 10 days.
        - Tomas owns the warehouse audit plan, due by the end of the month.
        - Mara: update the hiring plan two weeks after this meeting.
        - Leo to book the venue the day before the board meeting on 2026-04-02.
        - Ines also drafts the Q2 budget, due 2026-03-20.
        - (Done already: Tomas sent the scanner order.)
        """).lstrip()
    actions = [{"owner": "Ines", "due": "2026-03-20"}, {"owner": "Ines", "due": "2026-03-22"},
               {"owner": "Mara", "due": "2026-03-26"}, {"owner": "Tomas", "due": "2026-03-31"},
               {"owner": "Leo", "due": "2026-04-01"}]
    wrong = [{"owner": "Ines", "due": "2026-03-20"}, {"owner": "Ines", "due": "2026-03-22"},
             {"owner": "Mara", "due": "2026-03-26"}, {"owner": "Tomas", "due": "2026-03-31"},
             {"owner": "Leo", "due": "2026-04-02"}]
    return {"notes.txt": notes}, actions, wrong


def _transform_tasks() -> list[PackTask]:
    files, totals, wrong = _transactions()
    payments, monthly, monthly_wrong = _payments()
    org, reports, reports_wrong = _org()
    notes, actions, actions_wrong = _actions()
    return [
        _file_task(1, "transactions_a.csv and transactions_b.csv hold card payments (in EUR); the same payment can "
                      "appear in both files, and dates and customer names aren't written the same way everywhere. "
                      "refunds.csv lists payments that were refunded in full. Write totals.json: a JSON object with "
                      "each customer's name in lower case (trimmed) as the key and their total of payments that "
                      "were not refunded, counting each payment once, in euro cents as an integer.",
                   files, "totals.json", "json", totals, json.dumps(wrong) + "\n"),
        _file_task(2, "payments.csv lists payments in EUR, USD and GBP. rates.csv gives, for each working day, how "
                      "many USD and GBP one euro bought; on a day with no rate, use the most recent earlier rate. "
                      "Convert each payment to EUR at its own date's rate, round each to 2 decimals (half up), and "
                      "write monthly.csv with the columns month (YYYY-MM) and total_eur (2 decimals), one row per "
                      "month, oldest first.",
                   payments, "monthly.csv", "csv", monthly, monthly_wrong),
        _file_task(3, "people.json lists everyone and their manager's email. Write reports.csv with the columns "
                      "manager (their name), direct_reports and all_reports (everyone below them, at any depth), "
                      "one row for each person who manages anyone, sorted by manager name.",
                   org, "reports.csv", "csv", reports, reports_wrong),
        _file_task(4, "notes.txt holds a meeting's notes. Write actions.json: a JSON list of the open action items, "
                      "each as {\"owner\": first name, \"due\": the due date as YYYY-MM-DD worked out from the "
                      "meeting date}, sorted by due date and then owner. Leave out anything already done.",
                   notes, "actions.json", "json", actions, json.dumps(actions_wrong) + "\n"),
    ]


# ── Tool workflows ──────────────────────────────────────────────────────────


def _event(number: int, title: str, date: str, start: str, end: str, attendees: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"id": f"evt_{number}", "title": title, "date": date, "start": start, "end": end,
            "attendees": list(attendees), "location": ""}


def _mail(identifier: str, sender: str, subject: str, body: str, date: str, folder: str = "inbox") -> dict[str, Any]:
    return {"id": identifier, "folder": folder, "from": sender, "to": "me@example.test", "subject": subject,
            "body": body, "date": date}


def _issue(number: int, title: str, state: str = "open", labels: tuple[str, ...] = (),
           comments: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"id": number, "title": title, "state": state, "labels": list(labels), "assignee": "",
            "comments": list(comments)}


def _count(where: str, count: int, **match: Any) -> dict[str, Any]:
    return {"in": where, "where": match, "count": count}


def _call(tool: str, **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args}


EVENTS, MESSAGES, ISSUES = "calendar.events", "mail.messages", "issues.issues"
PRIYA, LEO, TOMAS, DANA = "priya@example.test", "leo@example.test", "tomas@example.test", "dana@example.test"


def _busy_week() -> dict[str, Any]:
    events = [
        # 16 March: gaps of 30 and 40 minutes only (lunch is never free).
        _event(1, "Standup", "2026-03-16", "09:00", "09:30", ("team@example.test",)),
        _event(2, "Hiring panel", "2026-03-16", "09:30", "10:30", (PRIYA,)),
        _event(3, "Budget prep", "2026-03-16", "11:00", "12:00"),
        _event(4, "Vendor call", "2026-03-16", "13:40", "15:00", (LEO,)),
        _event(5, "Design crit", "2026-03-16", "15:00", "16:30", (LEO, PRIYA)),
        _event(6, "Focus time", "2026-03-16", "16:30", "17:00"),
        # 17 March: the first 45 minutes free from a quarter hour are 14:15 to 15:00.
        _event(7, "Standup", "2026-03-17", "09:00", "09:30", ("team@example.test",)),
        _event(8, "Ops review", "2026-03-17", "09:30", "11:20"),
        _event(9, "1:1 with Priya", "2026-03-17", "11:30", "12:00", (PRIYA,)),
        _event(10, "Customer demo", "2026-03-17", "13:00", "14:10", (LEO,)),
        _event(11, "Interview", "2026-03-17", "15:00", "16:00"),
        # 18 March: wide open from 09:30, but later.
        _event(12, "Standup", "2026-03-18", "09:00", "09:30", ("team@example.test",)),
    ]
    return {"calendar": {"events": events}}


def _inbox_rules() -> dict[str, Any]:
    messages = [
        _mail("msg_1", "mara@example.test", "Inbox rules for this week",
              "Please work through the inbox: archive every newsletter; for each bug report that names an issue "
              "number, add the label reported to that issue and comment 'Reported again by <their email>' on it; "
              "leave everything else in the inbox. Mara", "2026-03-09"),
        _mail("msg_2", "news@weekly.example", "This week in logistics", "Top stories...", "2026-03-09"),
        _mail("msg_3", "sam@customer.example", "Export broken again",
              "Bug report: the CSV export still drops the last row (issue 12). Sam", "2026-03-09"),
        _mail("msg_4", "digest@tools.example", "Your weekly digest", "Newsletter: tips and tricks...", "2026-03-10"),
        _mail("msg_5", "kim@customer.example", "Crash on save",
              "Bug report: the app crashes when I save a draft. Kim", "2026-03-10"),
        _mail("msg_6", "lee@customer.example", "Search is slow",
              "Bug report for issue 18: search takes 20 seconds on our big project. Lee", "2026-03-10"),
        _mail("msg_7", "ana@example.test", "Lunch", "Lunch on Thursday? Ana", "2026-03-11"),
        _mail("msg_8", "news@weekly.example", "This week in logistics (2)", "More stories...", "2026-03-11"),
    ]
    issues = [_issue(7, "Crash when saving a draft", labels=("bug",)), _issue(12, "CSV export drops the last row",
                                                                             labels=("bug",)),
              _issue(18, "Slow search on large projects", labels=("bug",)),
              _issue(21, "PDF export ignores page size", labels=("bug",))]
    return {"mail": {"messages": messages}, "issues": {"issues": issues}}


def _duplicates() -> dict[str, Any]:
    issues = [
        _issue(30, "Login fails with SSO", comments=("Seen on Okta tenants.",)),
        _issue(31, "Can't sign in through single sign-on", comments=("Duplicate of #30.",)),
        _issue(32, "Report totals off by one cent", comments=("Rounding in the PDF only.",)),
        _issue(33, "PDF totals rounding", comments=("Looks like #32.", "Confirmed: duplicate of #32.")),
        _issue(34, "Dark mode for settings", labels=("feature",)),
        _issue(35, "Settings in dark theme", labels=("feature",),
               comments=("Duplicate of #34?", "Not a duplicate of #34 after all: this one is the mobile app.")),
        _issue(36, "Slow search", comments=("Related to #18, not the same.",)),
        _issue(37, "Search slow on big projects", state="closed", comments=("Duplicate of #36.",)),
        _issue(38, "Typo on the pricing page"),
        _issue(39, "Pricing page typo", comments=("This duplicates #38.",)),
    ]
    return {"issues": {"issues": issues}}


def _tomas_week() -> dict[str, Any]:
    events = [
        _event(1, "Warehouse sync", "2026-03-23", "10:00", "10:30", (TOMAS,)),
        _event(2, "Tentative: supplier visit", "2026-03-24", "14:00", "15:00", (TOMAS,)),
        _event(3, "Budget review", "2026-03-25", "09:00", "10:00", (TOMAS, PRIYA)),
        _event(4, "Planning", "2026-03-25", "15:00", "16:00", (PRIYA,)),
        _event(5, "Audit walk-through", "2026-03-27", "11:00", "12:00", (TOMAS,)),
        _event(6, "Warehouse sync", "2026-03-30", "10:00", "10:30", (TOMAS,)),
    ]
    return {"calendar": {"events": events}, "mail": {"messages": []}}


def _dana_times() -> dict[str, Any]:
    events = [_event(1, "Board prep", "2026-03-19", "10:00", "11:30"),
              _event(2, "Lunch with Leo", "2026-03-19", "12:00", "12:50", (LEO,)),
              _event(3, "Customer call", "2026-03-20", "09:00", "09:45")]
    messages = [_mail("msg_1", DANA, "Times for our catch-up",
                      "Could we meet for 30 minutes? Any of these works for me: Thursday 2026-03-19 at 11:00, "
                      "Thursday 2026-03-19 at 13:00, or Friday 2026-03-20 at 09:30. Dana", "2026-03-16"),
                _mail("msg_2", "dana.k@example.test", "Lunch?", "Lunch on Friday? Dana K.", "2026-03-16")]
    return {"calendar": {"events": events}, "mail": {"messages": messages}}


def _tool_task(number: int, prompt: str, apps: dict[str, Any], tools: tuple[str, ...], checks: list[dict[str, Any]],
               reference: tuple[dict[str, Any], ...], decoy: tuple[dict[str, Any], ...], *, max_steps: int = 20) -> PackTask:
    return PackTask(
        id=f"{PREFIX}tool_{number:02d}", pack="tool_workflows", split="visible",
        prompt=prompt + " When you are finished, reply with: done",
        verifier=Verifier("app_state", checks), reference=Solution(answer="done", calls=reference),
        decoy=Solution(answer="done", calls=decoy), apps=apps, tools=tools, max_steps=max_steps,
    )


def _tool_tasks() -> list[PackTask]:
    moved = (("evt_1", "2026-03-30"), ("evt_3", "2026-04-01"), ("evt_5", "2026-04-03"))
    return [
        _tool_task(1, "Book a 45-minute event called Roadmap review with priya@example.test and leo@example.test at "
                      "the earliest time on 2026-03-16, 2026-03-17 or 2026-03-18, between 09:00 and 17:00, that "
                      "starts on a quarter hour (:00, :15, :30 or :45), overlaps nothing in my calendar, and stays "
                      "clear of lunch (12:00 to 13:00).",
                   _busy_week(), ("calendar",),
                   [_count(EVENTS, 1, title="Roadmap review", date="2026-03-17", start="14:15", end="15:00",
                           attendees=[PRIYA, LEO]), _count(EVENTS, 13)],
                   (_call("calendar_create_event", title="Roadmap review", date="2026-03-17", start="14:15",
                          end="15:00", attendees=[PRIYA, LEO]),),
                   (_call("calendar_create_event", title="Roadmap review", date="2026-03-16", start="13:00",
                          end="13:45", attendees=[PRIYA, LEO]),)),
        _tool_task(2, "Read Mara's email about inbox rules and do what it says.", _inbox_rules(), ("mail", "issues"),
                   [_count(MESSAGES, 0, folder="inbox", subject={"contains": "logistics"}),
                    _count(MESSAGES, 0, folder="inbox", subject="Your weekly digest"),
                    _count(MESSAGES, 3, folder="archive"), _count(MESSAGES, 5, folder="inbox"),
                    _count(ISSUES, 1, id=12, labels=["bug", "reported"]),
                    _count(ISSUES, 1, id=18, labels=["bug", "reported"]),
                    _count(ISSUES, 1, id=7, labels=["bug"]), _count(ISSUES, 1, id=21, labels=["bug"]),
                    _count(ISSUES, 1, id=12, comments=["Reported again by sam@customer.example"]),
                    _count(ISSUES, 1, id=18, comments=["Reported again by lee@customer.example"])],
                   (_call("mail_archive", message_id="msg_2"), _call("mail_archive", message_id="msg_4"),
                    _call("mail_archive", message_id="msg_8"),
                    _call("issues_update", issue_id="12", add_labels=["reported"]),
                    _call("issues_comment", issue_id="12", body="Reported again by sam@customer.example"),
                    _call("issues_update", issue_id="18", add_labels=["reported"]),
                    _call("issues_comment", issue_id="18", body="Reported again by lee@customer.example")),
                   (_call("mail_archive", message_id="msg_2"), _call("mail_archive", message_id="msg_8"),
                    _call("issues_update", issue_id="12", add_labels=["reported"]),
                    _call("issues_update", issue_id="7", add_labels=["reported"]),
                    _call("issues_update", issue_id="18", add_labels=["reported"]))),
        _tool_task(3, "Close every open issue whose comments say it duplicates another issue, and add the label "
                      "duplicate to each one you close. Don't change any other issue.", _duplicates(), ("issues",),
                   [_count(ISSUES, 1, id=31, state="closed", labels=["duplicate"]),
                    _count(ISSUES, 1, id=33, state="closed", labels=["duplicate"]),
                    _count(ISSUES, 1, id=39, state="closed", labels=["duplicate"]),
                    _count(ISSUES, 1, id=35, state="open", labels=["feature"]),
                    _count(ISSUES, 1, id=36, state="open", labels=[]),
                    _count(ISSUES, 4, state="closed"), _count(ISSUES, 3, labels=["duplicate"])],
                   (_call("issues_update", issue_id="31", state="closed", add_labels=["duplicate"]),
                    _call("issues_update", issue_id="33", state="closed", add_labels=["duplicate"]),
                    _call("issues_update", issue_id="39", state="closed", add_labels=["duplicate"])),
                   (_call("issues_update", issue_id="31", state="closed", add_labels=["duplicate"]),
                    _call("issues_update", issue_id="33", state="closed", add_labels=["duplicate"]),
                    _call("issues_update", issue_id="35", state="closed", add_labels=["duplicate"]),
                    _call("issues_update", issue_id="39", state="closed", add_labels=["duplicate"]))),
        _tool_task(4, "Move every event I have with tomas@example.test in the week of Monday 2026-03-23 to the same "
                      "weekday and time one week later, except events whose title starts with Tentative. Then email "
                      "tomas@example.test with the subject Moved meetings, listing each moved meeting's new date as "
                      "YYYY-MM-DD.",
                   _tomas_week(), ("calendar", "mail"),
                   [*(_count(EVENTS, 1, id=event, date=date) for event, date in moved),
                    _count(EVENTS, 1, id="evt_2", date="2026-03-24"), _count(EVENTS, 1, id="evt_4", date="2026-03-25"),
                    _count(EVENTS, 1, id="evt_6", date="2026-03-30"), _count(EVENTS, 6),
                    _count(MESSAGES, 1, folder="sent", to=TOMAS, subject="Moved meetings",
                           body={"contains": "2026-04-03"}),
                    _count(MESSAGES, 1, folder="sent")],
                   (*(_call("calendar_update_event", event_id=event, date=date) for event, date in moved),
                    _call("mail_send", to=TOMAS, subject="Moved meetings",
                          body="Moved: Warehouse sync to 2026-03-30, Budget review to 2026-04-01, Audit walk-through "
                               "to 2026-04-03.")),
                   (*(_call("calendar_update_event", event_id=event, date=date) for event, date in moved),
                    _call("calendar_update_event", event_id="evt_6", date="2026-04-06"),
                    _call("mail_send", to=TOMAS, subject="Moved meetings", body="Moved: three meetings."))),
        _tool_task(5, "Dana (dana@example.test) emailed me three times for a 30-minute catch-up. Book the earliest one "
                      "that doesn't clash with my calendar as an event called Catch-up with Dana, with her as the "
                      "attendee, then reply to her saying which date and time you booked (as YYYY-MM-DD HH:MM).",
                   _dana_times(), ("calendar", "mail"),
                   [_count(EVENTS, 1, title="Catch-up with Dana", date="2026-03-19", start="13:00", end="13:30"),
                    _count(EVENTS, 4),
                    _count(MESSAGES, 1, folder="sent", to=DANA, body={"contains": "13:00"}),
                    _count(MESSAGES, 0, folder="sent", to="dana.k@example.test")],
                   (_call("calendar_create_event", title="Catch-up with Dana", date="2026-03-19", start="13:00",
                          end="13:30", attendees=[DANA]),
                    _call("mail_send", to=DANA, subject="Re: Times for our catch-up",
                          body="Booked Thursday 2026-03-19 at 13:00.")),
                   (_call("calendar_create_event", title="Catch-up with Dana", date="2026-03-19", start="11:00",
                          end="11:30", attendees=[DANA]),
                    _call("mail_send", to=DANA, subject="Re: Times for our catch-up",
                          body="Booked Thursday 2026-03-19 at 11:00."))),
    ]


# ── Small coding ────────────────────────────────────────────────────────────

_HEADER = "import unittest\nfrom solution import {name}\n\n\nclass Check(unittest.TestCase):\n"


def _tests(name: str, body: str) -> str:
    return _HEADER.format(name=name) + dedent(body).strip("\n").replace("\n", "\n    ").join(("    ", "\n"))


def _code_task(number: int, name: str, prompt: str, tests: str, reference: str, decoy: str) -> PackTask:
    return PackTask(
        id=f"{PREFIX}code_{number:02d}", pack="small_coding", split="visible",
        prompt=f"In the workspace, write solution.py with {prompt} Use only the Python standard library. "
               "When the file is written, reply with: done",
        verifier=Verifier("code_tests", _tests(name, tests), path="solution.py"),
        reference=Solution(answer="done", files={"solution.py": dedent(reference).lstrip()}),
        decoy=Solution(answer="done", files={"solution.py": dedent(decoy).lstrip()}),
        tools=("files",), max_steps=10,
    )


def _code_tasks() -> list[PackTask]:
    return [
        _code_task(1, "business_days",
                   "a function business_days(start, end, holidays) that counts the weekdays (Monday to Friday) from "
                   "start to end, both included, leaving out any date in holidays. start and end are ISO date strings "
                   "(YYYY-MM-DD); holidays is a list of ISO date strings. When end is before start, return the count "
                   "for end to start, negated.", """
            def test_one_week(self):
                self.assertEqual(business_days("2026-03-02", "2026-03-08", []), 5)
            def test_holiday(self):
                self.assertEqual(business_days("2026-03-02", "2026-03-06", ["2026-03-04"]), 4)
            def test_weekend_holiday_ignored(self):
                self.assertEqual(business_days("2026-03-02", "2026-03-08", ["2026-03-07"]), 5)
            def test_same_day_weekend(self):
                self.assertEqual(business_days("2026-03-07", "2026-03-07", []), 0)
            def test_reversed(self):
                self.assertEqual(business_days("2026-03-06", "2026-03-02", []), -5)
            def test_across_years(self):
                self.assertEqual(business_days("2025-12-29", "2026-01-02", ["2026-01-01"]), 4)
            """, """
            from datetime import date, timedelta

            def business_days(start, end, holidays):
                first, last = date.fromisoformat(start), date.fromisoformat(end)
                sign = 1
                if last < first:
                    first, last, sign = last, first, -1
                skip = {date.fromisoformat(item) for item in holidays}
                count, day = 0, first
                while day <= last:
                    if day.weekday() < 5 and day not in skip:
                        count += 1
                    day += timedelta(days=1)
                return sign * count
            """, """
            from datetime import date, timedelta

            def business_days(start, end, holidays):
                first, last = date.fromisoformat(start), date.fromisoformat(end)
                count, day = 0, first
                while day < last:
                    if day.weekday() < 5 and day.isoformat() not in holidays:
                        count += 1
                    day += timedelta(days=1)
                return count
            """),
        _code_task(2, "parse_duration",
                   "a function parse_duration(text) that turns a duration such as '1h 30m', '90s', '2d4h' or "
                   "'1H 5M 3S' into a number of seconds. Units are d, h, m and s (any case), each used at most once, "
                   "in that order, with or without spaces between them. Raise ValueError for anything else, "
                   "including an empty string or a unit used twice.", """
            def test_simple(self):
                self.assertEqual(parse_duration("90s"), 90)
                self.assertEqual(parse_duration("1h 30m"), 5400)
            def test_no_spaces_and_case(self):
                self.assertEqual(parse_duration("2d4h"), 187200)
                self.assertEqual(parse_duration("1H 5M 3S"), 3903)
            def test_invalid(self):
                for bad in ("", "5", "1h1h", "30m 1h", "1x", "h"):
                    with self.assertRaises(ValueError):
                        parse_duration(bad)
            """, """
            import re

            _PATTERN = re.compile(r"\\s*(?:(\\d+)d)?\\s*(?:(\\d+)h)?\\s*(?:(\\d+)m)?\\s*(?:(\\d+)s)?\\s*", re.I)

            def parse_duration(text):
                found = _PATTERN.fullmatch(text or "")
                if not found or not any(found.groups()):
                    raise ValueError(text)
                days, hours, minutes, seconds = (int(value or 0) for value in found.groups())
                return ((days * 24 + hours) * 60 + minutes) * 60 + seconds
            """, """
            import re

            def parse_duration(text):
                total = 0
                for amount, unit in re.findall(r"(\\d+)([dhms])", text.lower()):
                    total += int(amount) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
                return total
            """),
    ]


_TASKS: tuple[PackTask, ...] | None = None


def all_tasks() -> tuple[PackTask, ...]:
    """Every task of the harder split, in pack order (built once)."""

    global _TASKS
    if _TASKS is None:
        from row_bot.learning.packs import validate

        _TASKS = tuple(validate(task) for task in (*_document_tasks(), *_transform_tasks(), *_tool_tasks(),
                                                   *_code_tasks()))
    return _TASKS


def tasks(packs: Iterable[str] | None = None) -> list[PackTask]:
    """The harder split's tasks of the named packs (all four by default)."""

    wanted = None if packs is None else set(packs)
    return [task for task in all_tasks() if wanted is None or task.pack in wanted]


__all__ = ["PREFIX", "all_tasks", "tasks"]
