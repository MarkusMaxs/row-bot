"""What computer use asks before doing (D19), what it never does, and what it can't use.

Paying, buying, booking with a charge, sending, posting, deleting files and
changing system settings ask first: under Ask the person decides; under Auto
the reviewer may approve what clearly matches the person's request; Hands-off
runs it. Everything else just runs, as the person's own click would.

An action is judged by the control under the pointer (its accessible name and
role, the same words the browser judges by), the app and its window, and for
keys, what the key does in that kind of app (Enter sends in a chat app,
Delete removes files in a file manager).

Row-Bot never types into a password, one-time code or card field, never
controls itself, a password manager or a protected system screen, and never
presses the keys that open other apps or the secure screen. In a terminal it
types commands, each checked as its shell tool checks one (5.3.1 A3), and
never pastes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from row_bot.browser.policy import PageFacts, classify as classify_control

Kind = Literal["pay", "book", "send", "delete", "account", "settings"]


def _words(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _key(value: object) -> str:
    """``microsoftedge`` for "Microsoft Edge", ``msedge`` for "msedge.exe"."""

    text = _words(value).casefold()
    text = re.sub(r"\.(?:exe|app)$", "", text)
    return "".join(character for character in text if character.isalnum())


# ── Apps Row-Bot doesn't use ─────────────────────────────────────────────────

_TERMINALS = frozenset({
    "terminal", "windowsterminal", "wt", "powershell", "windowspowershell", "pwsh", "commandprompt", "cmd",
    "conhost", "openconsole", "iterm", "iterm2", "alacritty", "kitty", "wezterm", "hyper", "warp", "ghostty",
    "gnometerminal", "konsole", "xterm", "mintty", "gitbash",
})
_PASSWORD_MANAGERS = frozenset({
    "1password", "bitwarden", "keepass", "keepassxc", "lastpass", "dashlane", "keeper", "nordpass", "enpass",
    "keychainaccess", "passwords", "credentialmanager",
})
_PROTECTED_TITLES = re.compile(
    r"^\s*(?:user account control|windows security|lock screen|login window|"
    r"privacy\s*(?:&|and)\s*security|security\s*(?:&|and)\s*privacy)\s*$",
    re.IGNORECASE,
)


def own_window(app: str, title: str = "") -> bool:
    """Row-Bot's own windows (the app, its Stop bar) and Cua Driver's."""

    app_key = _key(app)
    if app_key in {"rowbot", "rowbotserver"} or "cuadriver" in app_key:
        return True
    return app_key in {"python", "pythonw"} and "rowbot" in _key(title)


def is_terminal(app: str) -> bool:
    return _key(app) in _TERMINALS


TERMINAL_PASTE_REFUSAL = ("In a terminal Row-Bot types each command, so it's checked first; it doesn't paste. "
                          "Type the command instead.")


def terminal_paste(action: str, keys: tuple[tuple[str, ...], str] | None) -> bool:
    """Whether the action pastes in a terminal (a right-click pastes in Windows terminals)."""

    if action == "right_click":
        return True
    if action != "key" or keys is None:
        return False
    modifiers, key = set(keys[0]), keys[1]
    return ((key == "v" and modifiers in ({"ctrl"}, {"ctrl", "shift"}, {"cmd"}))
            or (key == "insert" and modifiers == {"shift"}))


def unusable_app(app: str, title: str = "") -> str:
    """Why Row-Bot doesn't use this app, in plain words; empty when it may."""

    app_key = _key(app)
    if own_window(app, title):
        return "Row-Bot can't control its own window."
    if app_key in _PASSWORD_MANAGERS:
        return "Row-Bot doesn't use password managers: the person signs in themselves."
    if _PROTECTED_TITLES.match(_words(title)):
        return "That's a protected system screen: only the person can use it."
    return ""


# ── Keys Row-Bot never presses ───────────────────────────────────────────────

_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "option": "alt", "opt": "alt",
              "cmd": "cmd", "command": "cmd", "win": "win", "windows": "win", "super": "win", "meta": "win"}
_KEY_NAMES = {
    "enter": "return", "return": "return", "esc": "escape", "escape": "escape", "del": "delete",
    "delete": "delete", "backspace": "backspace", "bksp": "backspace", "space": "space", "spacebar": "space",
    "tab": "tab", "up": "up", "down": "down", "left": "left", "right": "right", "arrowup": "up",
    "arrowdown": "down", "arrowleft": "left", "arrowright": "right", "home": "home", "end": "end",
    "pageup": "pageup", "pgup": "pageup", "pagedown": "pagedown", "pgdn": "pagedown", "insert": "insert",
}


def parse_keys(keys: str) -> tuple[tuple[str, ...], str] | None:
    """``(("ctrl", "shift"), "s")`` for "Ctrl+Shift+S"; None when it isn't one key or chord."""

    parts = [part.strip().casefold() for part in str(keys or "").replace(" ", "").split("+")]
    if not parts or any(not part for part in parts):
        return None
    *mods, last = parts
    modifiers: list[str] = []
    for part in mods:
        modifier = _MODIFIERS.get(part)
        if modifier is None or modifier in modifiers:
            return None
        modifiers.append(modifier)
    if last in _MODIFIERS:
        if mods:
            return None
        return (), _MODIFIERS[last]
    name = _KEY_NAMES.get(last, last)
    if not (len(name) == 1 or re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", name) or name in set(_KEY_NAMES.values())):
        return None
    return tuple(modifiers), name


def refused_keys(modifiers: Iterable[str], key: str) -> str:
    """Why Row-Bot doesn't press this key; empty when it may."""

    mods = set(modifiers)
    if key == "win" or "win" in mods:
        return "Row-Bot doesn't press the Windows key: it opens other apps. Use open_app to open an app."
    if mods == {"cmd"} and key == "space":
        return "Row-Bot doesn't open Spotlight: use open_app to open an app."
    if {"ctrl", "alt"} <= mods and key == "delete":
        return "Row-Bot doesn't press Ctrl+Alt+Delete: only the person uses the secure screen."
    return ""


# ── Fields Row-Bot never types into ──────────────────────────────────────────

_EDIT_ROLES = frozenset({"edit", "textfield", "textbox", "textarea", "entry", "input", "combobox", "searchfield",
                         "securetextfield", ""})
# A field's own label, clicked: it puts the caret in the field.
_LABEL_ROLES = frozenset({"text", "statictext", "label"})
_PASSWORD = re.compile(r"(?<!\w)(?:password|passcode|passphrase|pin(?: code)?)(?!\w)", re.IGNORECASE)
_CODE = re.compile(r"(?<!\w)(?:one[- ]?time (?:code|password)|verification code|security code sent|otp|2fa|"
                   r"two[- ]factor|authenticat(?:ion|or) code|sms code)(?!\w)", re.IGNORECASE)
_CARD = re.compile(r"(?<!\w)(?:card ?number|credit card|debit card|cvv2?|cvc2?|csc|security code|expiry|"
                   r"expiration(?: date)?|name on card)(?!\w)", re.IGNORECASE)

SECRET_REFUSAL = {
    "password": ("That's a password field. Row-Bot never types passwords: ask the person (ask_person) to type it "
                 "on the computer, then carry on."),
    "code": "That's a one-time code field. The person types it: ask them (ask_person), then carry on.",
    "card": ("That's a card field. Row-Bot never types card numbers: ask the person (ask_person) to enter their "
             "card details, then carry on."),
}


def role_of(raw: object) -> str:
    """A plain role from a driver role: ``AXSecureTextField`` → ``securetextfield``, ``Button`` → ``button``."""

    role = _key(raw)
    for prefix in ("controltype", "uia", "ax"):
        if role.startswith(prefix) and len(role) > len(prefix):
            role = role[len(prefix):]
            break
    return role


def secret_field(role: object, label: object, *, clicked: bool = False) -> str:
    """``password``, ``code`` or ``card`` for a field Row-Bot never types into; empty otherwise.

    ``clicked``: the control Row-Bot clicked last, which may be the field's own label.
    """

    plain = role_of(role)
    if "secure" in plain or "password" in plain:
        return "password"
    if plain not in _EDIT_ROLES and not (clicked and plain in _LABEL_ROLES):
        return ""
    words = _words(label)
    if _CARD.search(words):
        return "card"
    if _CODE.search(words):
        return "code"
    if _PASSWORD.search(words):
        return "password"
    return ""


# ── What asks first (D19) ────────────────────────────────────────────────────

_CLICK_ROLES = {
    "button": "button", "splitbutton": "button", "popupbutton": "button", "menubutton": "button",
    "togglebutton": "button", "hyperlink": "link", "link": "link", "menuitem": "menuitem", "checkbox": "checkbox",
    "radiobutton": "radio", "radio": "radio", "tabitem": "tab", "tab": "tab", "listitem": "option",
    "option": "option", "switch": "switch", "cell": "generic", "text": "generic", "statictext": "generic",
    "image": "generic", "group": "generic", "custom": "generic", "": "generic",
}
_SETTING_ROLES = frozenset({"button", "checkbox", "radio", "switch"})
_SETTINGS_APPS = frozenset({
    "settings", "systemsettings", "systemsettingsadminflows", "systempreferences", "controlpanel", "control",
    "registryeditor", "regedit", "groupolicyeditor", "gpedit", "devicemanager", "services", "mmc",
    "windowsupdate", "networkconnections",
})
_FILE_MANAGERS = frozenset({"explorer", "fileexplorer", "windowsexplorer", "finder"})
_MESSAGING_APPS = frozenset({
    "outlook", "newoutlook", "olk", "mail", "thunderbird", "slack", "teams", "msteams", "microsoftteams",
    "discord", "whatsapp", "telegram", "telegramdesktop", "signal", "messages", "messenger", "skype", "zoom",
    "spark", "airmail", "mailspring", "element", "wechat", "line", "viber",
})
_PRICE = re.compile(r"[£$€¥₹]\s?\d|\d\s?(?:£|€|usd|gbp|eur)(?!\w)", re.IGNORECASE)
_SEARCH_FIELD = re.compile(r"search|find", re.IGNORECASE)

_QUESTIONS: dict[str, str] = {
    "pay": "Pay in {app}?",
    "book": "Book in {app}?",
    "send": "Send from {app}?",
    "delete": "Delete in {app}?",
    "account": "Change your account in {app}?",
    "settings": "Change a setting in {app}?",
}
_LABELS: dict[str, str] = {
    "pay": "Pay or buy in an app",
    "book": "Book in an app",
    "send": "Send or post from an app",
    "delete": "Delete in an app",
    "account": "Change account settings in an app",
    "settings": "Change a setting on this computer",
}
_WHAT: dict[str, str] = {
    "pay": "This can charge you.",
    "book": "This books it, and the window shows a charge.",
    "send": "This sends it from your account.",
    "delete": "This may not be undoable.",
    "account": "This changes your account.",
    "settings": "This changes how this computer works.",
}
RISKS: dict[str, str] = {
    "pay": "pays or buys something in an app on this computer, which can charge the person",
    "book": "books something in an app on this computer that shows a charge",
    "send": "sends or posts something from the person's account in an app on this computer",
    "delete": "deletes something in an app on this computer, which may not be undoable",
    "account": "changes the person's account settings in an app on this computer",
    "settings": "changes a system setting on this computer",
}


@dataclass(frozen=True)
class Control:
    """The control an action lands on: its plain role and accessible name."""

    role: str
    label: str


@dataclass(frozen=True)
class Consequence:
    kind: Kind
    app: str
    window: str
    doing: str  # 'click "Confirm and pay £60"', 'press Enter'

    def question(self) -> str:
        return _QUESTIONS[self.kind].format(app=self.app)

    def description(self) -> str:
        where = f'{self.app} ("{self.window[:80]}")' if self.window else self.app
        return f"Row-Bot will {self.doing} in {where}. {_WHAT[self.kind]}"

    @property
    def risk(self) -> str:
        return RISKS[self.kind]


@dataclass(frozen=True)
class WindowFacts:
    """What the window shows around the control (never its values)."""

    title: str = ""
    shows_price: bool = False
    card_fields: bool = False


def window_facts(title: str, labels: Iterable[str]) -> WindowFacts:
    text = list(labels)
    return WindowFacts(
        title=title,
        shows_price=any(_PRICE.search(label) for label in [title, *text]),
        card_fields=any(_CARD.search(label) for label in text),
    )


def key_words(modifiers: Iterable[str], key: str) -> str:
    names = {"return": "Enter", "escape": "Esc", "delete": "Delete", "backspace": "Backspace", "space": "Space",
             "ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "cmd": "Cmd"}
    return "+".join([names.get(part, part.upper() if len(part) == 1 else part.title())
                     for part in [*modifiers, key]])


def classify(
    action: str,
    *,
    app: str,
    window: WindowFacts,
    control: Control | None = None,
    keys: tuple[tuple[str, ...], str] | None = None,
    text: str = "",
) -> Consequence | None:
    """The consequence of one action, or None when it just runs.

    ``action`` is ``click``, ``double_click``, ``right_click``, ``type`` or
    ``key``. ``control`` is the control under the pointer (a click) or the
    field last clicked (typing).
    """

    app_key = _key(app)
    title = window.title
    if action in {"click", "double_click"} and control is not None:
        role = _CLICK_ROLES.get(role_of(control.role), "")
        if not role:
            return None
        name = _words(control.label)[:160]
        if app_key in _SETTINGS_APPS and role in _SETTING_ROLES and not _SEARCH_FIELD.search(name):
            return Consequence("settings", app, title, f'click "{name}"' if name else "click there")
        found = classify_control(
            action="click", control=name, role=role,
            page=PageFacts(title=title, card_fields=window.card_fields, shows_price=window.shows_price),
        )
        if found is not None and found.kind in {"pay", "book", "send", "delete", "account"}:
            return Consequence(found.kind, app, title, f'click "{found.control}"')
        return None
    if action == "key" and keys is not None:
        modifiers, key = keys
        pressed = f"press {key_words(modifiers, key)}"
        if app_key in _MESSAGING_APPS and key == "return" and set(modifiers) <= {"ctrl", "cmd"}:
            return Consequence("send", app, title, pressed)
        if app_key in _FILE_MANAGERS and (
            (key == "delete" and set(modifiers) <= {"shift"})
            or (key == "backspace" and set(modifiers) == {"cmd"})
        ):
            return Consequence("delete", app, title, f"{pressed} on the selected files")
        if app_key in _SETTINGS_APPS and key in {"return", "space"} and not modifiers:
            return Consequence("settings", app, title, pressed)
        return None
    if action == "type":
        field = _words(control.label if control else "")
        if app_key in _MESSAGING_APPS and "\n" in text:
            return Consequence("send", app, title, "type a message ending with Enter")
        if app_key in _SETTINGS_APPS and not _SEARCH_FIELD.search(field):
            return Consequence("settings", app, title, f'type into "{field}"' if field else "type there")
    return None


def approval_request(consequence: Consequence) -> dict[str, Any]:
    """The approval card for one consequential action (plain words, D19)."""

    return {
        "tool": "computer",
        "label": _LABELS[consequence.kind],
        "action": consequence.kind,
        "question": consequence.question(),
        "description": consequence.description(),
        "risk_class": "high" if consequence.kind in {"pay", "delete", "settings"} else "medium",
        "args": {"app": consequence.app, "window": consequence.window[:160], "action": consequence.doing[:200]},
    }
