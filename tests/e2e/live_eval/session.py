"""The pytest side of a live run: one run folder, one budget, and the recorded findings.

Used by ``tests/e2e/test_live_eval.py`` and ``tests/e2e/test_live_verification.py``.
In-process calls (the verification checks) go through the same call-level gate
as the worker processes; worker spending is carried into it.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import orchestrate
from .budget import BudgetGuard, Caps, Spend
from .pricing import RATES_ENV, Rates, load_rates
from .report import ProbeRecord, Report, write_report


@dataclass
class LiveSession:
    run_dir: Path
    caps: Caps
    rates: Mapping[str, Rates]
    standalone: bool
    gate: Any = None
    probes: list[ProbeRecord] = field(default_factory=list)
    _undo: list[Callable[[], None]] = field(default_factory=list)

    @classmethod
    def start(cls) -> LiveSession:
        standalone = not os.environ.get(orchestrate.OUT_ENV)
        run_dir = orchestrate.new_run_dir(suffix="-checks" if standalone else "")
        rates_path = os.environ.get(RATES_ENV, "")
        return cls(run_dir=run_dir, caps=Caps.from_env(), rates=load_rates(rates_path) if rates_path else {},
                   standalone=standalone)

    def install_gate(self) -> None:
        """Check every in-process model call against the caps (Row-Bot must be importable)."""
        from row_bot import data_paths

        from . import ledger as ledger_reader
        from .runner import BudgetGate, install_budget_gate

        def ledger_path() -> Path:
            return Path(data_paths.get_usage_db_path(create_parent=False))

        self.gate = BudgetGate(self.caps, self.rates, ledger_path, baseline_id=ledger_reader.max_id(ledger_path()))
        self._undo.append(install_budget_gate(self.gate))

    def guard(self) -> BudgetGuard:
        return BudgetGuard(self.caps, self.gate.spent() if self.gate else Spend())

    def over(self, tokens: int) -> str:
        return self.guard().over_by(tokens)

    def carry(self, spend: Mapping[str, Any] | Spend) -> None:
        """Add spending that the in-process ledger does not see (a worker, a raw HTTP probe)."""
        extra = spend if isinstance(spend, Spend) else Spend(**{k: spend[k] for k in ("tokens", "usd", "unpriced_tokens", "calls") if k in spend})
        if self.gate is not None:
            self.gate.carried.add(extra)

    def record(self, probe: ProbeRecord) -> ProbeRecord:
        self.probes.append(probe)
        orchestrate.append_probe(self.run_dir, probe)
        return probe

    def finish(self) -> None:
        spent = self.guard().spent
        (self.run_dir / "checks-budget.json").write_bytes(
            (json.dumps({"spent": asdict(spent)}, indent=2) + "\n").encode("utf-8")
        )
        # The checks' own report; a `scripts/live_eval.py run --checks` report also merges them.
        report = Report(run_id=self.run_dir.name, caps={"describe": self.caps.describe()}, spent=asdict(spent),
                        probes=list(self.probes))
        write_report(report, self.run_dir if self.standalone else self.run_dir / "checks", rates=self.rates)
        while self._undo:
            self._undo.pop()()
