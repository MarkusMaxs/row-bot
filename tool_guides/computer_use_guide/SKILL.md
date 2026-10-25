---
name: computer_use_guide
display_name: Computer Use Guide
tools:
  - computer_use
inline: true
---

# COMPUTER USE WORKFLOW

- The `computer` tool uses an app on this computer with the real mouse and keyboard while the person watches. A bar on the screen says so; Esc or Stop ends your turn.
- Start with `list_apps` (open windows and their titles), then `screenshot` or `open_app` with `app=` (its name, or words from a window's title). Later actions work in that window. The first use of each app in a chat asks the person once.
- Each action returns what it did, the window's controls with their x,y and, if you see images, its picture. Check the picture yourself before the next step: if the click missed, try again or another way. Never ask the person to click for you.
- x,y are always in the latest screenshot. Click a field before you `type`; use `key` for enter, tab or a chord like ctrl+s; `scroll` with "down" or "up 3"; `wait` for a slow window.
- SIGNING IN: you never type a password, one-time code or card number. Ask the person (ask_person) to type it, then carry on.
- ASKING FIRST: paying, buying, booking with a charge, sending, posting, deleting files and system settings ask before they run (by the chat's approval level). Do everything up to that step, then make the click.
- If the person uses the mouse or keyboard, your next call asks them "Carry on?"; after they answer you get a fresh screenshot and nothing was done for that call.
- For websites use Row-Bot's browser (browser_* tools) unless the person asks for their own browser. To record the screen, use `computer_record`.
