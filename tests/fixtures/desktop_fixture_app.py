"""A tiny desktop app for the real-driver smoke: a magenta Count button and a cyan field.

Run as ``python desktop_fixture_app.py``: DPI-unaware, as Python and many
older apps are, so on a scaled display Windows stretches it; with ``--aware``
it draws at the display's scale (per-monitor v2), as Edge does. Once its
window shows it prints ``button L T R B`` and ``field L T R B`` (where its
controls really are on screen, in physical pixels) and then ``ready``; then
``clicked N`` after each click on Count and ``typed TEXT`` as the field
changes. Its colours let a test find the controls in a screenshot, as a model
would see them. It closes itself after a minute, or when its stdin closes.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import tkinter as tk
from ctypes import wintypes

TITLE = "Smoke Notes"


def _on_screen(root: tk.Tk, widget: tk.Widget) -> str:
    """The widget's rectangle on screen in physical pixels, whatever this process's DPI awareness."""

    corners = [wintypes.POINT(widget.winfo_rootx(), widget.winfo_rooty()),
               wintypes.POINT(widget.winfo_rootx() + widget.winfo_width(),
                              widget.winfo_rooty() + widget.winfo_height())]
    frame = int(root.wm_frame(), 16)
    for corner in corners:
        ctypes.windll.user32.LogicalToPhysicalPointForPerMonitorDPI(frame, ctypes.byref(corner))
    return f"{corners[0].x} {corners[0].y} {corners[1].x} {corners[1].y}"


def main() -> None:
    if "--aware" in sys.argv:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    root = tk.Tk()
    root.title(TITLE)
    root.geometry("420x260+160+160")
    root.configure(background="#F4F4F4")
    count = {"value": 0}
    label = tk.Label(root, text="Clicked 0 times", background="#F4F4F4", font=("Segoe UI", 11))
    label.place(x=24, y=20)

    def clicked() -> None:
        count["value"] += 1
        label.configure(text=f"Clicked {count['value']} times")
        print(f"clicked {count['value']}", flush=True)

    button = tk.Button(root, text="Count", command=clicked, background="#FF00FF", activebackground="#FF00FF",
                       foreground="#000000", relief="flat", font=("Segoe UI", 11))
    button.place(x=24, y=70, width=160, height=56)
    value = tk.StringVar()
    value.trace_add("write", lambda *_args: print(f"typed {value.get()}", flush=True))
    field = tk.Entry(root, textvariable=value, background="#00FFFF", relief="flat", font=("Segoe UI", 11))
    field.place(x=24, y=160, width=300, height=40)

    def ready() -> None:
        root.update_idletasks()
        print(f"button {_on_screen(root, button)}", flush=True)
        print(f"field {_on_screen(root, field)}", flush=True)
        print("ready", flush=True)

    def watch_stdin() -> None:
        sys.stdin.read()
        root.after(0, root.destroy)

    threading.Thread(target=watch_stdin, daemon=True).start()
    root.after(60_000, root.destroy)
    root.after(300, ready)
    root.mainloop()


if __name__ == "__main__":
    main()
