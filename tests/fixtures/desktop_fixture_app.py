"""A tiny desktop app for the real-driver smoke: a magenta Count button and a cyan field.

Run as ``python desktop_fixture_app.py``. It prints ``ready`` once its window
shows, ``clicked N`` after each click on Count and ``typed TEXT`` as the field
changes. Its colours let a test find the controls in a screenshot, as a model
would see them. It closes itself after a minute, or when its stdin closes.
"""

from __future__ import annotations

import sys
import threading
import tkinter as tk

TITLE = "Smoke Notes"


def main() -> None:
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

    tk.Button(root, text="Count", command=clicked, background="#FF00FF", activebackground="#FF00FF",
              foreground="#000000", relief="flat", font=("Segoe UI", 11)).place(x=24, y=70, width=160, height=56)
    value = tk.StringVar()
    value.trace_add("write", lambda *_args: print(f"typed {value.get()}", flush=True))
    tk.Entry(root, textvariable=value, background="#00FFFF", relief="flat",
             font=("Segoe UI", 11)).place(x=24, y=160, width=300, height=40)

    def watch_stdin() -> None:
        sys.stdin.read()
        root.after(0, root.destroy)

    threading.Thread(target=watch_stdin, daemon=True).start()
    root.after(60_000, root.destroy)
    root.after(300, lambda: print("ready", flush=True))
    root.mainloop()


if __name__ == "__main__":
    main()
