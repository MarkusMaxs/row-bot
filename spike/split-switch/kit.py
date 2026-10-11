"""macOS manual-check kit, run from the trial tray host's "Trial: ..." menu items
with the active version's Python (Phase 1 spike; the check itself is Phase 5).

  kit.py status | mic | camera | stage   [--host-pid=N]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent  # Row-Bot.app/Contents/Resources/trial
sys.path.insert(0, str(HERE))
ROOT = Path.home() / "Library" / "Application Support" / "RowBotSplitTrial"
TARGET = "5.1.0"


def host_pid() -> int:
    for arg in sys.argv:
        if arg.startswith("--host-pid="):
            return int(arg.split("=", 1)[1])
    return 0


def seed_version() -> str:
    text = (HERE.parent / "app" / "src" / "row_bot" / "version.py").read_text(encoding="utf-8")
    match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", text)
    return match.group(1) if match else "0"


def status() -> None:
    import engine
    from probe import responsible

    print("Python:", sys.executable)
    print("Version dir:", os.path.realpath(os.path.join(os.path.dirname(sys.executable), "..", "..")))
    print("Seed (bundle) version:", seed_version())
    pid = responsible(os.getpid())
    print(f"Responsible process: {pid} (tray host {host_pid()}) -> {'Row-Bot' if pid == host_pid() else 'NOT the tray host'}")
    print("state.json:", json.dumps(engine.read_state(ROOT), indent=1))


def mic() -> None:
    import sounddevice as sd

    print("Default input:", sd.query_devices(kind="input")["name"])
    print("Recording 2 s... (macOS asks for microphone access the first time; the prompt must name Row-Bot)")
    rec = sd.rec(32000, samplerate=16000, channels=1, dtype="float32")
    sd.wait()
    peak = float(abs(rec).max())
    print(f"peak level = {peak:.4f}")
    print("OK: audio arrived." if peak > 0 else "Silence (0.0): macOS denied access, or the room is silent. Check System Settings > Privacy > Microphone.")


def camera() -> None:
    import cv2

    print("Opening camera 0... (macOS asks for camera access the first time; the prompt must name Row-Bot)")
    cap = cv2.VideoCapture(0)
    ok, frame = cap.read()
    for _ in range(20):
        if ok:
            break
        time.sleep(0.5)
        ok, frame = cap.read()
    cap.release()
    print("Frame captured:", frame.shape if ok and frame is not None else None)
    print("OK." if ok else "No frame: if you just allowed access, run the test again. Check System Settings > Privacy > Camera.")


def stage() -> None:
    import engine

    parts = HERE / "parts"
    state = engine.read_state(ROOT) or engine.new_state(seed_version())
    layout = engine.Layout(ROOT, "macos", seed=HERE.parent, seed_version=seed_version())
    label = f"{TARGET}-kit{int(time.time())}"
    print(f"Staging {label} from {state['active']} (clonefile + pip, wheels from PyPI)... this takes a few minutes.")
    rep = engine.stage(
        layout=layout,
        current=state["active"],
        new=label,
        expect_version=TARGET,
        manifest=json.loads((parts / f"row-bot-update-{TARGET}.json").read_text(encoding="utf-8")),
        parts_base=str(parts),
        pylock=parts / f"pylock-{TARGET}.toml",
        export=parts / f"requirements-hashed-{TARGET}.txt",
        clone_method="clonefile",
        tool="pip",
        source="plan",
        uv=None,
        proxy=None,
        log=ROOT / "kit-stage.log",
    )
    (ROOT / "kit-stage.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    print("OK: staged. Now choose 'Trial: Restart to update'." if rep.get("ok") else f"FAILED: {rep.get('error')}")
    print("timings:", rep.get("timings"))


def main() -> int:
    command = next((a for a in sys.argv[1:] if not a.startswith("--")), "status")
    {"status": status, "mic": mic, "camera": camera, "stage": stage}[command]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
