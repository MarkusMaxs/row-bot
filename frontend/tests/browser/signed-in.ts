import os from 'node:os';
import path from 'node:path';

/**
 * The browser tests' sign-in (A0). On this computer only a window or browser
 * a person opened may change anything, so the suite signs in once, as the
 * launcher signs in Row-Bot's window, and every context reuses that cookie.
 * Kept out of the evidence folder: it is a session for the fixture backend.
 */
export const SIGNED_IN_STATE = path.join(
  os.tmpdir(),
  `row-bot-browser-signed-in-${
    new URL(process.env.ROW_BOT_BROWSER_BASE_URL ?? 'http://127.0.0.1:4173')
      .port
  }.json`,
);
