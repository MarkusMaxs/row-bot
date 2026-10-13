"""Background updates for installed Row-Bot ("split and switch").

A release publishes small parts next to its full installers:

- the code archive (Row-Bot's own files, every release),
- the assets archive (``static/`` and ``sounds/``, downloaded only when their
  content changed),
- a ``pylock.toml`` export of ``uv.lock`` (only when the lock changed), and
- ``row-bot-update-<version>.json`` describing them (:mod:`.manifest`).

The engine (:mod:`.engine`) downloads what changed (:mod:`.downloader`),
plans the libraries (:mod:`.planner`), builds the new version next to the
running one in ``versions/<v>.staging`` (:mod:`.stager`), self-tests it and
renames it to ``versions/<v>``, then marks it ``ready`` in the install's
``state.json``. Restarting switches to it (:mod:`.switcher`); quitting does
too. If preparing fails nothing changes; if the new version fails to start the
native launcher goes back to the previous one.

Installs whose host applies updates (``ROW_BOT_UPDATES=external``, Docker and
the cloud appliance) never run the engine: :mod:`.external` reads the host's
status and writes requests for it.
"""
