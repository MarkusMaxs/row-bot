"""Run by the trial tray host (`--trial-probe`) with the active version's Python.

Proves, without a person at the Mac: which Python the signed host started, that
macOS treats the host as the responsible process (the one TCC attributes
microphone/camera prompts to), and that torch, cv2 and the libraries changed by
the update import from that Python.

  probe.py <out.json> <host pid> <active version> <version dir>
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time


def responsible(pid: int) -> int | None:
    for lib in (None, "/usr/lib/system/libquarantine.dylib", "/usr/lib/libSystem.B.dylib"):
        try:
            fn = ctypes.CDLL(lib).responsibility_get_pid_responsible_for_pid
        except (OSError, AttributeError):
            continue
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_int]
        return int(fn(pid))
    return None


def main() -> int:
    out_path, host_pid, version, vdir = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
    res: dict = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "host_pid": host_pid,
        "version": version,
        "version_dir": vdir,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "python": sys.version,
    }
    res["responsible_pid"] = responsible(os.getpid())
    res["responsible_is_host"] = res["responsible_pid"] == host_pid
    imports: dict = {}
    for name in ("numpy", "torch", "cv2", "multidict", "pypdf", "sounddevice", "onnxruntime", "PIL"):
        started = time.time()
        try:
            module = __import__(name)
            imports[name] = {
                "ok": True,
                "seconds": round(time.time() - started, 2),
                "version": getattr(module, "__version__", None),
                "file": getattr(module, "__file__", None),
            }
        except Exception as exc:  # noqa: BLE001
            imports[name] = {"ok": False, "error": repr(exc)[:400]}
    res["imports"] = imports
    try:
        import torch

        res["torch_matmul"] = float((torch.ones(4, 4) @ torch.ones(4, 4)).sum())
    except Exception as exc:  # noqa: BLE001
        res["torch_matmul"] = repr(exc)[:300]
    try:
        import cv2
        import numpy as np

        res["cv2_cvtColor"] = list(cv2.cvtColor(np.zeros((8, 8, 3), np.uint8), cv2.COLOR_BGR2GRAY).shape)
    except Exception as exc:  # noqa: BLE001
        res["cv2_cvtColor"] = repr(exc)[:300]
    real_dir = os.path.realpath(vdir)
    res["all_imports_from_version_dir"] = all(
        os.path.realpath(v.get("file") or "").startswith(real_dir) for v in imports.values() if v.get("ok")
    )
    res["all_imports_ok"] = all(v.get("ok") for v in imports.values())
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(res, handle, indent=2)
    return 0 if res["all_imports_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
