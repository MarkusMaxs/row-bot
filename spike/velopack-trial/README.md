# Velopack trial (Phase 0 spike)

Throwaway code for the 5.2 install/update overhaul's go/no-go trial. It lives
only on the `spike/velopack-trial` branch and is never merged. Nothing here is
product code.

- `.github/workflows/velopack-trial.yml` (dispatch only) builds the v5.0.0 and
  v5.1.0 payloads on Windows and macOS with each tag's own build script, packs
  them with Velopack (`vpk`), and runs install/update scenarios from a local
  file feed.
- `build_payload.py` runs a tag's build script, stopped before Inno Setup or
  pkg, as built today (a) or deterministic (b: `--no-compile` +
  `compileall --invalidation-mode unchecked-hash`).
- `rowbot_stub.c` is the minimal Windows main executable; the macOS tray host
  (`installer/macos/RowBotTrayHost.m`) is the macOS one.
- `trial_launcher.py` replaces `app/launcher.py` in the trial payload; it calls
  `velopack.App().run()` first and is steered by `~/rowbot-velopack-trial/control.json`.
- `sign_mac.py` signs like `build_mac_app.sh`, optionally reusing signed bytes
  for unchanged inputs.
- `driver.py` measures sizes and drives the scenarios; results are JSON.
