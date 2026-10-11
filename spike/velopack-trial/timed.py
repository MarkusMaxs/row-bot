"""Run a command, stream its output, append {label, seconds, rc} to a JSONL file."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    sep = argv.index("--")
    label, out = argv[0], Path(argv[1])
    cmd = argv[sep + 1 :]
    started = time.time()
    rc = subprocess.call(cmd)  # noqa: S603 - CI helper
    seconds = round(time.time() - started, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"label": label, "seconds": seconds, "rc": rc}) + "\n")
    print(f"[timing] {label}: {seconds} s (rc={rc})", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
