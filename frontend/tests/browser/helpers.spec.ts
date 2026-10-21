import type { Page, TestInfo } from '@playwright/test';
import { assertNoOverflow, expect, screenshot, test } from './evidence';
import { fixtureHeaders } from './surface-helpers';
import {
  composer,
  headerAction,
  newConversation,
  restoreThinkingDefault,
} from './unified-helpers';

/*
 * S11 · Helpers. A chat's helpers as the runner records them: each started on
 * the chat's model with a reasoning level (B386), one of them waiting on an
 * approval that no notice put in the chat (B348). The runs and the approval
 * are records the fixture writes; nothing runs.
 */

const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 900 };

async function views(page: Page, info: TestInfo, name: string) {
  // The passing new-approval notice would cover what the shot is of.
  const notices = page.getByRole('button', { name: 'Dismiss notification' });
  while (await notices.count()) await notices.first().click();
  for (const appearance of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: appearance });
    await expect(page.locator('html')).toHaveAttribute(
      'data-theme',
      appearance,
    );
    await assertNoOverflow(page);
    await screenshot(page, info, `${name}-${appearance}`);
  }
  await page.emulateMedia({ colorScheme: 'light' });
}

async function everywhere(
  page: Page,
  info: TestInfo,
  name: string,
  settle: () => Promise<void> = async () => {},
) {
  await settle();
  await views(page, info, name);
  await page.setViewportSize(PHONE);
  await settle();
  await views(page, info, `${name}-phone`);
  await page.setViewportSize(DESKTOP);
  await settle();
}

/** A chat on the model with thinking levels, with three helpers. */
async function helpersChat(page: Page) {
  await restoreThinkingDefault(page);
  const conversation = await newConversation(page);
  const seeded = await page.request.post('/__s11_fixture/helpers', {
    headers: fixtureHeaders(),
    data: { conversation_id: conversation },
  });
  expect(seeded.ok()).toBe(true);
  await page.reload();
  await expect(composer(page)).toBeVisible();
  return {
    conversation,
    ...((await seeded.json()) as {
      helpers: Record<string, { run_id: string; conversation_id: string }>;
    }),
  };
}

/** The Agents list: the details rail on a desktop, its sheet on a phone. */
async function agentsList(page: Page) {
  const details = page.getByRole('complementary', {
    name: 'Conversation details',
  });
  const sheet = page.getByRole('dialog', { name: 'Conversation details' });
  if (!(await details.isVisible()) && !(await sheet.isVisible()))
    await headerAction(page, 'Conversation details');
  const panel = details.or(sheet).first();
  const agents = panel.locator('summary', { hasText: /^Agents/ });
  await expect(agents).toBeVisible();
  const section = agents.locator('xpath=..');
  if ((await section.getAttribute('open')) === null) await agents.click();
  return panel.getByRole('region', { name: 'Delegated tasks' });
}

test.beforeEach(async ({ page }) => {
  await page.setViewportSize(DESKTOP);
});

test.afterEach(async ({ page }) => {
  const response = await page.request.post('/__s11_fixture/reset', {
    headers: fixtureHeaders(),
  });
  expect(response.ok()).toBe(true);
});

test('each helper says quietly which model and reasoning it runs on', async ({
  page,
}, info) => {
  test.setTimeout(120_000);
  await helpersChat(page);
  const list = await agentsList(page);
  await expect(list.getByText(/ · High$/)).toBeVisible();
  // Finished helpers fold under the live ones.
  const done = list.getByRole('button', { name: /done/ });
  if (await done.isVisible()) await done.click();
  await expect(
    list.getByText(/ · Provider default: XHigh isn’t offered$/),
  ).toBeVisible();
  await everywhere(page, info, 'helpers-agents', async () => {
    await agentsList(page);
  });
});

test('a helper’s approval waits in the chat that started it, naming the helper', async ({
  page,
}, info) => {
  test.setTimeout(120_000);
  await helpersChat(page);
  const transcript = page.getByRole('log', { name: 'Conversation' });
  const card = transcript.getByRole('group', {
    name: 'Catalogue helper needs your approval',
  });
  await expect(
    card.getByText('Catalogue helper is waiting for your approval.'),
  ).toBeVisible();
  await expect(
    card.getByRole('button', { name: 'Approve', exact: true }),
  ).toBeVisible();
  await expect(
    card.getByRole('button', { name: 'Deny', exact: true }),
  ).toBeVisible();
  await everywhere(page, info, 'helper-approval', async () => {
    await card.scrollIntoViewIfNeeded();
  });
});

test('/agent with a level the model lacks names the levels it has', async ({
  page,
}, info) => {
  test.setTimeout(120_000);
  await helpersChat(page);
  await composer(page).fill('/agent --reasoning turbo Check the dates');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(
    page.getByText(/“turbo” isn't a thinking level for this model\. Try /),
  ).toBeVisible();
  // Nothing was sent: the text stays to fix.
  await expect(composer(page)).toHaveValue(
    '/agent --reasoning turbo Check the dates',
  );
  await everywhere(page, info, 'agent-reasoning-refused');
});

test('a helper’s own conversation says how it thinks beside its model', async ({
  page,
}, info) => {
  test.setTimeout(120_000);
  const { helpers } = await helpersChat(page);
  await page.goto(
    `/app-v2/conversations/${helpers['Date review'].conversation_id}`,
  );
  await expect(page.getByTitle(/^Model: .+ · High$/)).toBeVisible();
  await views(page, info, 'helper-header');
});
