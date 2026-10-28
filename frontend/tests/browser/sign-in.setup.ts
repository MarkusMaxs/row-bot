import { writeFile } from 'node:fs/promises';
import { request } from '@playwright/test';
import { SIGNED_IN_STATE } from './signed-in';

/**
 * Sign the suite in as the launcher signs in Row-Bot's window: ask the backend
 * for a one-time invitation with the launch secret, claim it, keep the cookie.
 * A backend without a launch secret leaves the suite view-only on purpose.
 */
export default async function signIn(): Promise<void> {
  const baseURL =
    process.env.ROW_BOT_BROWSER_BASE_URL ?? 'http://127.0.0.1:4173';
  const secret = process.env.ROW_BOT_LAUNCH_SECRET;
  if (!secret) {
    await writeFile(
      SIGNED_IN_STATE,
      JSON.stringify({ cookies: [], origins: [] }),
    );
    return;
  }
  const context = await request.newContext({ baseURL });
  try {
    const minted = await context.post('/api/launcher/sign-in', {
      headers: { Authorization: `Bearer ${secret}` },
    });
    if (!minted.ok()) throw new Error(`launcher sign-in: ${minted.status()}`);
    const { invitation } = (await minted.json()) as { invitation: string };
    const claimed = await context.post('/api/access/invitations/claim', {
      headers: { Origin: new URL(baseURL).origin, Accept: 'application/json' },
      data: { invitation, display_name: 'Browser tests' },
    });
    if (!claimed.ok()) throw new Error(`claim: ${claimed.status()}`);
    await context.storageState({ path: SIGNED_IN_STATE });
  } finally {
    await context.dispose();
  }
}
