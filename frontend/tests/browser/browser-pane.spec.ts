import type { Page, TestInfo } from '@playwright/test';
import { assertNoOverflow, expect, screenshot, test } from './evidence';
import { fixtureHeaders } from './surface-helpers';
import { composer, newConversation, revealContext } from './unified-helpers';

/*
 * N12-A · Row-Bot's browser in the app. The Browser pane, its live stream,
 * the person's controls, the chat's live step, the payment approval and
 * Settings › Browser are real; the page is a picture the spec renders from
 * the task suite's fixture pages and hands the synthetic engine (no Chromium
 * starts in the fixture). Every state at desktop and phone, light and dark.
 */

const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 900 };
type Still = 'account' | 'claim' | 'sign-in' | 'checkout';

const RUNNING = {
  status: 'running',
  url: 'https://homecover.example/claim/1',
  title: 'Make a claim · 1 of 5',
  site: 'homecover.example',
  loading: false,
  sign_in: '',
  acting: '',
  waiting: '',
  tabs: [
    {
      id: 't1',
      title: 'Make a claim · 1 of 5',
      url: 'https://homecover.example/claim/1',
      active: true,
    },
  ],
};

/** The fixture page as Row-Bot's browser would show it (a JPEG at its 1280×800 page). */
async function still(page: Page, name: Still): Promise<string> {
  const tab = await page.context().newPage();
  await tab.setViewportSize({ width: 1280, height: 800 });
  await tab.goto(`/__n12_fixture/site/${name}`);
  const jpeg = await tab.screenshot({ type: 'jpeg', quality: 75 });
  await tab.close();
  return jpeg.toString('base64');
}

async function show(
  page: Page,
  conversation: string,
  state: Record<string, unknown>,
  frame?: string,
) {
  const response = await page.request.post('/__n12_fixture/browser', {
    headers: fixtureHeaders(),
    data: { conversation_id: conversation, state, frame },
  });
  expect(response.ok()).toBe(true);
}

function pane(page: Page) {
  return page.getByRole('region', { name: 'Browser' });
}

async function openPane(page: Page) {
  const context = await revealContext(page);
  await context.getByRole('button', { name: 'Conversation actions' }).click();
  await page.getByRole('menuitem', { name: 'Open browser' }).click();
  await expect(pane(page)).toBeVisible();
}

/** On a phone the pane fills the screen: open it from the chat if the chat shows. */
async function paneInView(page: Page) {
  if (await pane(page).isVisible()) return;
  const open = page
    .getByRole('log', { name: 'Conversation' })
    .getByRole('button', { name: 'Open browser' })
    .last();
  if (await open.isVisible()) await open.click();
  else
    await page
      .getByRole('tab', { name: /Browser/ })
      .first()
      .click();
  await expect(pane(page)).toBeVisible();
}

/** On a phone the pane covers the chat: go back to the conversation. */
async function chatInView(page: Page) {
  const back = page.getByRole('button', { name: 'Back to conversation' });
  if (await back.isVisible()) await back.click();
  await expect(page.getByRole('log', { name: 'Conversation' })).toBeVisible();
}

async function views(page: Page, info: TestInfo, name: string) {
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
  await expect(settle).toPass();
  await views(page, info, name);
  await page.setViewportSize(PHONE);
  await expect(settle).toPass();
  await views(page, info, `${name}-phone`);
  await page.setViewportSize(DESKTOP);
}

test('the Browser pane in every state, from nothing open to a crash', async ({
  page,
}, info) => {
  await page.setViewportSize(DESKTOP);
  const conversation = await newConversation(page);
  await openPane(page);

  await expect(pane(page)).toContainText('Nothing open yet');
  await expect(
    pane(page).getByRole('textbox', { name: 'Address' }),
  ).toBeVisible();
  await everywhere(page, info, 'pane-empty', () => paneInView(page));

  await show(page, conversation, { status: 'starting' });
  await expect(pane(page)).toContainText("Starting Row-Bot's browser");
  await everywhere(page, info, 'pane-starting', () => paneInView(page));

  const claim = await still(page, 'claim');
  await show(page, conversation, { ...RUNNING, loading: true }, claim);
  await expect(
    pane(page).getByRole('img', { name: 'Live page: Make a claim · 1 of 5' }),
  ).toBeVisible();
  await expect(
    pane(page).getByRole('progressbar', { name: 'Loading the page' }),
  ).toBeVisible();
  await expect(
    pane(page).getByRole('button', { name: 'Stop loading' }),
  ).toBeVisible();
  await everywhere(page, info, 'pane-loading', () => paneInView(page));

  await show(
    page,
    conversation,
    {
      ...RUNNING,
      url: 'https://homecover.example/login',
      title: 'Sign in',
      sign_in: 'password',
    },
    await still(page, 'sign-in'),
  );
  await expect(pane(page)).toContainText(
    "Sign in here Row-Bot doesn't type passwords.",
  );
  await everywhere(page, info, 'pane-sign-in', () => paneInView(page));

  // Signing in: the person takes control, and gives it back when done.
  await pane(page).getByRole('button', { name: 'Sign in' }).click();
  await expect(pane(page)).toContainText('You have control.');
  await show(
    page,
    conversation,
    { ...RUNNING, sign_in: '' },
    await still(page, 'account'),
  );
  await everywhere(page, info, 'pane-person-control', () => paneInView(page));
  await pane(page).getByRole('button', { name: 'Give back' }).click();
  await expect(pane(page)).not.toContainText('You have control.');

  await show(page, conversation, { status: 'crashed', tabs: [] });
  const crash = pane(page).getByRole('alert');
  await expect(crash).toContainText("Row-Bot's browser stopped. Restart it?");
  await everywhere(page, info, 'pane-crash', () => paneInView(page));
  await crash.getByRole('button', { name: 'Restart' }).click();
  await expect(pane(page).getByRole('alert')).toHaveCount(0);
});

test('a turn using the browser shows live in the chat, then asks before paying', async ({
  page,
}, info) => {
  await page.setViewportSize(DESKTOP);
  const conversation = await newConversation(page);
  await openPane(page);
  await composer(page).fill('n12 claim fixture');
  await page.getByRole('button', { name: 'Send', exact: true }).click();

  await show(
    page,
    conversation,
    {
      ...RUNNING,
      url: 'https://homecover.example/account',
      title: 'Your cover',
      acting: 'Clicking link "Make a claim"',
    },
    await still(page, 'account'),
  );
  const transcript = page.getByRole('log', { name: 'Conversation' });
  const step = transcript.locator('.browser-step');
  await expect(step).toContainText('Using the browser · homecover.example');
  await expect(step).toContainText('Clicking "Make a claim"');
  await expect(pane(page)).toContainText('Row-Bot: Clicking "Make a claim"');
  await expect(
    pane(page).getByRole('button', { name: 'Take control' }),
  ).toBeVisible();
  await everywhere(page, info, 'agent-working', async () => {
    await chatInView(page);
    await step.scrollIntoViewIfNeeded();
  });
  await everywhere(page, info, 'agent-working-pane', () => paneInView(page));

  await show(
    page,
    conversation,
    {
      ...RUNNING,
      url: 'https://homecover.example/claim/5',
      title: 'Make a claim · 5 of 5',
      waiting: 'Waiting for your OK in the chat',
    },
    await still(page, 'checkout'),
  );
  const released = await page.request.post('/__n12_fixture/release', {
    headers: fixtureHeaders(),
    data: { conversation_id: conversation },
  });
  expect(released.ok()).toBe(true);
  const card = page.getByRole('complementary', {
    name: 'Approval required: Pay on homecover.example?',
  });
  await expect(card).toContainText(
    'Row-Bot will click "Confirm and pay £60" on homecover.example. This can charge you.',
  );
  await expect(
    card.getByRole('button', { name: 'Open the browser: homecover.example' }),
  ).toBeVisible();
  await expect(pane(page)).toContainText('Waiting for your OK in the chat');
  await everywhere(page, info, 'payment-approval', async () => {
    await chatInView(page);
    await card.scrollIntoViewIfNeeded();
  });
  await card.getByRole('button', { name: 'Approve', exact: true }).click();
  await expect(
    transcript.getByText(/Booked: a plumber comes tomorrow/),
  ).toBeVisible();
});

test('Settings › Browser lists the signed-in sites with Sign out', async ({
  page,
}, info) => {
  await page.setViewportSize(DESKTOP);
  await page.goto('/app-v2/settings/system');
  const item = page.locator('[data-setting-anchor="browser-computer-use"]');
  await item.getByRole('button', { name: 'Set up…' }).click();
  const dialog = page.getByRole('dialog', { name: 'Browser and computer use' });
  await expect(dialog).toContainText('Signed-in sites');
  await expect(dialog.getByText('homecover.example')).toBeVisible();
  await everywhere(page, info, 'settings-browser', () =>
    dialog.getByText('Signed-in sites').scrollIntoViewIfNeeded(),
  );
  await dialog.getByRole('button', { name: 'Sign out of github.com' }).click();
  await expect(dialog.getByText('github.com')).toHaveCount(0);
});

test('popped out: only this conversation’s browser, full size', async ({
  page,
}, info) => {
  await page.setViewportSize(DESKTOP);
  const conversation = await newConversation(page);
  await show(page, conversation, RUNNING, await still(page, 'claim'));
  await page.goto(`/app-v2/browser/${encodeURIComponent(conversation)}`);
  await expect(
    pane(page).getByRole('img', { name: 'Live page: Make a claim · 1 of 5' }),
  ).toBeVisible();
  await expect(pane(page).getByRole('button', { name: 'Pop out' })).toHaveCount(
    0,
  );
  await views(page, info, 'pop-out');
});
