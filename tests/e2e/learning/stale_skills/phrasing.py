"""The person's words for the stale-skills world, from a phrasing model (Design A; two phrasing models supported).

The timeline's messages come from templates (:func:`world.slots`: each procedure's later-run requests and how each
kind of change is told). A phrasing model writes a few variants of each **once**, in the person's voice, and they
are kept as plain files next to this module (``phrasings/<person>.<label>.json``) that :func:`world.build` reads:
the same seed and file give the same world, and a reader can see every message. A variant is kept only when it
keeps exactly the template's ``{fields}`` and states nothing else that could be a fact (no digits, no email
address). **The simulated person supplies wording only**; every value comes from the timeline.

Two phrasing models are supported:

- ``--model main``: the main model on the ChatGPT lane (the eval profile's sign-in, read only; no refresh);
- ``--model qwen3.8:27b``: a local model through Ollama (books the card).

    ROW_BOT_DATA_DIR=<scratch> uv run python -m tests.e2e.learning.stale_skills.phrasing --model main
        --eval-profile <folder> --scratch <scratch>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tests.e2e.learning.stale_skills import world

FORMAT = "row_bot.learning.stale_skills_phrasings"
FOLDER = Path(__file__).resolve().parent / "phrasings"
VARIANTS = 4
_FIELD = re.compile(r"\{(\w+)\}")
_FACTLIKE = re.compile(r"\d|@")


def label_of(model: str) -> str:
    return "main" if model == "main" else model.replace(":", "-").replace("/", "-")


def path_for(person: str, model: str) -> Path:
    return FOLDER / f"{person}.{label_of(model)}.json"


def load(person: str, model: str) -> dict[str, Any] | None:
    """A kept phrasing file, or None (templates only)."""

    try:
        return json.loads(path_for(person, model).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def keeps(template: str, variant: str) -> bool:
    """A variant is kept when it has exactly the template's fields and states no fact of its own."""

    variant = str(variant or "").strip()
    if not variant or len(variant) > 400 or "\n" in variant:
        return False
    if set(_FIELD.findall(variant)) != set(_FIELD.findall(template)) or len(_FIELD.findall(variant)) != len(
            _FIELD.findall(template)):
        return False
    return not _FACTLIKE.search(_FIELD.sub("", variant))


def prompt_for(person: Mapping[str, Any], template: str) -> str:
    return (
        f"You are writing chat messages for {person['name']}. {person['about']}\n"
        f"How she writes: {person.get('asks', '')}\n\n"
        f"Write {VARIANTS} different ways she might type this message to her assistant:\n\n{template}\n\n"
        "Keep every {field} in curly braces exactly as written (they are filled in later) and keep the meaning. "
        "Don't add numbers, dates, names, email addresses or any other facts. One line each. Reply with a JSON "
        "list of strings only."
    )


def parse(text: str) -> list[str]:
    text = str(text or "").strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    try:
        found = json.loads(match.group(0) if match else text)
    except ValueError:
        return []
    return [str(item) for item in found if isinstance(item, str)] if isinstance(found, list) else []


def phrase(person: Mapping[str, Any], ask: Callable[[str], str]) -> dict[str, Any]:
    """Every slot's kept variants (``ask(prompt) -> text``), with counts."""

    slots: dict[str, Any] = {}
    asked = kept = 0
    for slot, template in sorted(world.slots().items()):
        variants = parse(ask(prompt_for(person, template)))
        asked += len(variants)
        good = sorted({item.strip() for item in variants if keeps(template, item)} - {template})
        kept += len(good)
        slots[slot] = {"template": template, "variants": good}
    return {"slots": slots, "asked": asked, "kept": kept}


def write(person: str, model: str, found: Mapping[str, Any]) -> Path:
    path = path_for(person, model)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"format": FORMAT, "version": 1, "person": person, "model": label_of(model),
            "note": "The person's wording for the stale-skills world, written once by a phrasing model "
                    "(tests/e2e/learning/stale_skills/phrasing.py). Made-up people; no fact comes from here.",
            "slots": found["slots"]}
    path.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8",
                    newline="\n")
    return path


def _ollama_ask(model: str) -> Callable[[str], str]:
    import httpx

    def ask(prompt: str) -> str:
        response = httpx.post("http://127.0.0.1:11434/api/chat", timeout=600,
                              json={"model": model, "stream": False, "think": False,
                                    "messages": [{"role": "user", "content": prompt}]})
        response.raise_for_status()
        return str((response.json().get("message") or {}).get("content") or "")

    return ask


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the stale-skills world's wording with a phrasing model")
    parser.add_argument("--model", required=True, help="main (the ChatGPT lane) or an Ollama model name")
    parser.add_argument("--eval-profile", default="")
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--gpu-booking", default="")
    args = parser.parse_args(argv)
    from tests.e2e.learning.seeded import generator
    from tests.e2e.learning.seeded.scratch import GpuGate, check_scratch

    scratch = check_scratch(Path(args.scratch), Path(args.eval_profile) if args.eval_profile else None)
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["ROW_BOT_DATA_DIR"] = str(scratch)
    person = generator.load_person(world.PERSON)
    tokens: dict[str, int] = {"input": 0, "output": 0, "calls": 0}
    gate = None
    if args.model == "main":
        from langchain_core.messages import HumanMessage

        from row_bot.providers import codex
        from tests.e2e.learning.seeded.live import SignIn
        from tests.e2e.learning.seeded.person import SimulatedPerson
        from tests.e2e.learning.stale_skills.run import guard_sign_in
        from tests.e2e.live_eval import eval_profile

        guard_sign_in()
        SignIn(Path(args.eval_profile)).install()
        model = eval_profile.choose_model([item.model_id for item in codex.fetch_codex_model_infos()])
        writer = SimulatedPerson({"id": "phrasing", "name": person["name"]}, 0, model_ref=f"model:codex:{model}")

        def ask(prompt: str) -> str:
            message = writer._model().invoke([HumanMessage(content=prompt)])  # noqa: SLF001 - the person's model
            usage = getattr(message, "usage_metadata", None) or {}
            tokens["input"] += int(usage.get("input_tokens") or 0)
            tokens["output"] += int(usage.get("output_tokens") or 0)
            tokens["calls"] += 1
            content = message.content
            return content if isinstance(content, str) else " ".join(
                str(block.get("text", "")) for block in content if isinstance(block, dict))
    else:
        gate = GpuGate(Path(args.gpu_booking) if args.gpu_booking else None, "lane-B",
                       label=f"SA1 phrasing ({args.model})")
        gate.acquire("phrasing the stale-skills world's messages", expect="10 min, 20 GB")
        ask = _ollama_ask(args.model)
    try:
        found = phrase(person, ask)
    finally:
        if gate is not None:
            gate.release()
    print(write(world.PERSON, args.model, found))
    print(json.dumps({"asked": found["asked"], "kept": found["kept"], "tokens": tokens}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
