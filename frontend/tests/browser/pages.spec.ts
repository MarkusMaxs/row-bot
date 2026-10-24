import type { FrameLocator, Page, TestInfo } from '@playwright/test';
import { assertNoOverflow, expect, screenshot, test } from './evidence';
import { fixtureHeaders } from './surface-helpers';
import { composer, dismissNotices, newConversation } from './unified-helpers';

/*
 * N5 · Pages: plans, checklists and dashboards Row-Bot makes for the person.
 * The model's calls are scripted; the page tools, the skill's own templates,
 * the sandboxed frame, the bridge, the decisions footer and the message the
 * person's Send posts all run through the real server. A page can't reach the
 * network, Row-Bot's page, the top window or a popup.
 */

// What the person asks for; the fixture's scripted model makes the page.
const PLAN = 'Make me a beta launch plan I can approve step by step';
const DASHBOARD = 'Show me sales by week as a dashboard';
const CHECKLIST = 'Make a launch day checklist for the beta';
const PROBE = 'Make a page that checks its own sandbox';
const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 900 };

// Each journey starts with no pages: the Library and the sidebar show only its own.
test.beforeEach(async ({ page }) => {
  const done = await page.request.post('/__n5_fixture/reset', {
    headers: fixtureHeaders(),
  });
  expect(done.ok()).toBe(true);
});

/** An open page is loaded and has saved what the person did. */
async function settled(page: Page) {
  const wrap = page.locator('.page-frame-wrap');
  if (!(await wrap.count())) return;
  await expect(wrap).toHaveAttribute('data-status', 'ready');
  await expect(page.locator('.page-toolbar-meta')).not.toHaveText('Saving…');
}

/** A new size: an open page moves between the side panel and full screen, and loads there again. */
async function resize(page: Page, size: { width: number; height: number }) {
  const frame = page.locator('iframe.page-frame');
  const before = (await frame.count()) ? await frame.getAttribute('src') : null;
  await page.setViewportSize(size);
  if (before) await expect(frame).not.toHaveAttribute('src', before);
  await settled(page);
}

async function views(page: Page, info: TestInfo, name: string) {
  await settled(page);
  await expect(page.getByText('Buddy is loading…')).toHaveCount(0);
  await dismissNotices(page);
  for (const appearance of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: appearance });
    await expect(page.locator('html')).toHaveAttribute(
      'data-theme',
      appearance,
    );
    // The page follows the app's theme without loading again.
    const frames = page.locator('iframe.page-frame');
    if (await frames.count())
      await expect(
        page.frameLocator('iframe.page-frame').locator('html'),
      ).toHaveAttribute('data-theme', appearance);
    await assertNoOverflow(page);
    await screenshot(page, info, `${name}-${appearance}`);
  }
  await page.emulateMedia({ colorScheme: 'light' });
}

/** Evidence at desktop and phone size, light and dark; on a phone the open page is full screen. */
async function everywhere(page: Page, info: TestInfo, name: string) {
  await views(page, info, name);
  await resize(page, PHONE);
  await views(page, info, `${name}-phone`);
  await resize(page, DESKTOP);
}

async function ask(page: Page, text: string, title: string) {
  await composer(page).fill(text);
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  const card = page
    .getByRole('log', { name: 'Conversation' })
    .getByRole('group', { name: `${title} · page` });
  await expect(card.getByRole('button', { name: 'Open' })).toBeEnabled();
  return card;
}

async function open(page: Page, title: string): Promise<FrameLocator> {
  const card = page
    .getByRole('log', { name: 'Conversation' })
    .getByRole('group', { name: `${title} · page` })
    .last();
  await card.getByRole('button', { name: 'Open' }).click();
  const panel = page.getByRole('region', { name: `${title} page` });
  await expect(panel).toBeVisible();
  // Scripts only: no same origin, forms, pop-ups, downloads or top navigation.
  await expect(panel.locator('iframe')).toHaveAttribute(
    'sandbox',
    'allow-scripts',
  );
  const frame = page.frameLocator('iframe.page-frame');
  await expect(frame.locator('h1')).toBeVisible();
  return frame;
}

async function decide(frame: FrameLocator, step: string, choice: string) {
  await frame
    .getByRole('group', { name: `Your choice on ${step}` })
    .getByRole('button', { name: choice })
    .click();
}

async function planDecisions(frame: FrameLocator) {
  await decide(frame, 'Draft the announcement', 'Approve');
  await decide(frame, 'Send invites to the waitlist', 'Approve');
  await decide(frame, 'Book the launch call', 'Change');
  await frame
    .getByRole('textbox', {
      name: 'What should change in Book the launch call',
    })
    .fill('Make it Friday at 4pm UK time instead');
  await frame.getByRole('button', { name: 'Save change' }).click();
}

test('a plan: approve two steps, change one, send; the message arrives and the plan moves on', async ({
  page,
}, info) => {
  test.slow();
  await page.setViewportSize(DESKTOP);
  await newConversation(page);
  await ask(page, PLAN, 'Beta launch plan');
  await everywhere(page, info, 'chat-card');

  let frame = await open(page, 'Beta launch plan');
  await planDecisions(frame);
  const footer = page.getByRole('region', { name: 'Decisions' });
  await expect(
    footer.getByRole('button', { name: '3 decisions' }),
  ).toBeVisible();
  await expect(
    footer.getByRole('button', { name: 'Send to Row-Bot' }),
  ).toBeEnabled();
  await expect(
    frame
      .getByRole('group', { name: 'Your choice on Draft the announcement' })
      .getByRole('button', { name: 'Approve' }),
  ).toHaveAttribute('aria-pressed', 'true');
  await everywhere(page, info, 'plan-page');

  // The person can see exactly what will be sent before sending it.
  await footer.getByRole('button', { name: '3 decisions' }).click();
  await expect(footer.getByRole('list')).toContainText(
    'Change: Book the launch call “Make it Friday at 4pm UK time instead”',
  );
  await footer.getByRole('button', { name: '3 decisions' }).click();

  // The decisions are Row-Bot's, not the page's: they survive a reload.
  await settled(page);
  await page.reload();
  frame = await open(page, 'Beta launch plan');
  await expect(
    frame
      .getByRole('group', { name: 'Your choice on Book the launch call' })
      .getByRole('button', { name: 'Change' }),
  ).toHaveAttribute('aria-pressed', 'true');
  await expect(
    footer.getByRole('button', { name: '3 decisions' }),
  ).toBeVisible();

  // Only the person's click sends them: one message, labelled as from the page.
  await footer.getByRole('button', { name: 'Send to Row-Bot' }).click();
  const transcript = page.getByRole('log', { name: 'Conversation' });
  const sent = transcript
    .getByRole('article', { name: /message$/ })
    .filter({ hasText: 'Sent from a page' });
  await expect(sent).toContainText('Beta launch plan · 3 decisions');
  await expect(sent).toContainText('Approved: Draft the announcement');
  await expect(sent).toContainText(
    'Change: Book the launch call (“Make it Friday at 4pm UK time instead”)',
  );
  await expect(
    transcript.getByText(/The announcement is ready/).first(),
  ).toBeVisible();
  // The agent moved the plan on: the open page shows the new version.
  frame = page.frameLocator('iframe.page-frame');
  await expect(frame.getByText('2 of 6 done')).toBeVisible();
  await expect(
    footer.getByRole('button', { name: 'Send to Row-Bot' }),
  ).toHaveCount(0);

  await views(page, info, 'sent-message');
  await resize(page, PHONE);
  await page.getByRole('button', { name: 'Back to conversation' }).click();
  await expect(sent).toBeVisible();
  await sent.scrollIntoViewIfNeeded();
  await views(page, info, 'sent-message-phone');
  await page.setViewportSize(DESKTOP);
});

test('a page on a phone is full screen, with Back to the chat', async ({
  page,
}, info) => {
  await page.setViewportSize(PHONE);
  await newConversation(page);
  await ask(page, PLAN, 'Beta launch plan');
  const frame = await open(page, 'Beta launch plan');
  await decide(frame, 'Draft the announcement', 'Approve');
  const back = page.getByRole('button', { name: 'Back to conversation' });
  await expect(back).toBeVisible();
  // Back and × are the ways out: no panel strip under the page too.
  await expect(
    page.getByRole('complementary', { name: 'Panel rail' }),
  ).toHaveCount(0);
  await views(page, info, 'full-screen-page-phone');
  await back.click();
  await expect(composer(page)).toBeVisible();
  await page.setViewportSize(DESKTOP);
});

test('a dashboard with a chart and a table, sorted and filtered in the page', async ({
  page,
}, info) => {
  test.slow();
  await page.setViewportSize(DESKTOP);
  await newConversation(page);
  await ask(page, DASHBOARD, 'Sales by week');
  const frame = await open(page, 'Sales by week');
  await expect(frame.locator('canvas')).toBeVisible();
  const rows = frame.locator('tbody tr');
  await expect(rows).toHaveCount(6);
  await everywhere(page, info, 'dashboard-page');
  await frame.getByRole('button', { name: 'Revenue (£)' }).click();
  await frame.getByRole('button', { name: 'Revenue (£) ↑' }).click();
  await expect(rows.first()).toContainText('Trail runner');
  await frame.getByRole('searchbox', { name: 'Filter rows' }).fill('sock');
  await expect(rows).toHaveCount(1);
});

test('a checklist remembers what is ticked', async ({ page }, info) => {
  test.slow();
  await page.setViewportSize(DESKTOP);
  await newConversation(page);
  await ask(page, CHECKLIST, 'Launch day checklist');
  let frame = await open(page, 'Launch day checklist');
  for (const item of [
    'Write the release notes',
    'Check the status page is green',
    'Publish the beta build',
  ])
    await frame.getByRole('checkbox', { name: item }).check();
  await expect(frame.getByText('3 of 7 done')).toBeVisible();
  await everywhere(page, info, 'checklist-page');
  await settled(page);
  await page.reload();
  frame = await open(page, 'Launch day checklist');
  await expect(
    frame.getByRole('checkbox', { name: 'Check the status page is green' }),
  ).toBeChecked();
  await expect(frame.getByText('3 of 7 done')).toBeVisible();
});

test('Library › Pages lists every page with its chat and opens it there', async ({
  page,
}, info) => {
  test.slow();
  await page.setViewportSize(DESKTOP);
  const plan = await newConversation(page);
  await ask(page, PLAN, 'Beta launch plan');
  const frame = await open(page, 'Beta launch plan');
  await decide(frame, 'Draft the announcement', 'Approve');
  await expect(
    page
      .getByRole('region', { name: 'Decisions' })
      .getByRole('button', { name: '1 decision' }),
  ).toBeVisible();
  await settled(page);
  await newConversation(page);
  await ask(page, DASHBOARD, 'Sales by week');
  await newConversation(page);
  await ask(page, CHECKLIST, 'Launch day checklist');
  await page.goto('/app-v2/library?show=pages');
  const library = page.getByRole('region', { name: 'Pages' });
  await expect(library.getByRole('link')).toHaveCount(3);
  const row = library.getByRole('link', { name: /Beta launch plan/ });
  await expect(row).toContainText('1 not sent');
  await expect(
    library.getByRole('link', { name: /Sales by week/ }),
  ).toBeVisible();
  await everywhere(page, info, 'library-pages');
  await row.click();
  await expect(page).toHaveURL(new RegExp(`/conversations/${plan}`));
  await expect(
    page.getByRole('region', { name: 'Beta launch plan page' }),
  ).toBeVisible();
});

/**
 * The browser says what it refused, once each (Chromium's words). The top
 * window's navigation is named by its origin (Edge on Windows) or its address
 * (Chromium on Linux); either way, the sandboxed page's own frame tried it.
 */
function expectRefusals(frameUrl: string, topUrl: string) {
  const origin = new URL(process.env.ROW_BOT_BROWSER_BASE_URL!).origin;
  const api = `${origin}/api/v1/handshake`;
  const sandboxed =
    "The frame attempting navigation of the top-level window is sandboxed, but the flag of 'allow-top-navigation' or 'allow-top-navigation-by-user-activation' is not set.\n";
  for (const signature of [
    `Connecting to '${api}' violates the following Content Security Policy directive: "connect-src 'none'". The action has been blocked.`,
    `Fetch API cannot load ${api}. Refused to connect because it violates the document's Content Security Policy.`,
    "Blocked opening 'https://example.com/' in a new window because the request was made in a sandboxed frame whose 'allow-popups' permission is not set.",
    `Unsafe attempt to initiate navigation for frame with origin '${origin}' from frame with URL '${frameUrl}'. ${sandboxed}`,
    `Unsafe attempt to initiate navigation for frame with URL '${topUrl}' from frame with URL '${frameUrl}'. ${sandboxed}`,
  ])
    test.info().annotations.push({
      type: 'expected-console-error',
      description: JSON.stringify({
        owner: 'pages',
        fixture: 'sandbox check page',
        signature,
        count: 1,
        upTo: true,
      }),
    });
}

test("a page can't fetch, reach Row-Bot's page, navigate the top window or open a pop-up", async ({
  page,
  context,
  browserName,
}) => {
  test.skip(
    browserName !== 'chromium',
    "The refusals are checked in Chromium's words",
  );
  await page.setViewportSize(DESKTOP);
  await newConversation(page);
  const address = page.url();
  await ask(page, PROBE, 'Sandbox check');
  const served = page.waitForResponse((response) =>
    response.url().includes('/page-frames/'),
  );
  const frame = await open(page, 'Sandbox check');
  const response = await served;
  expectRefusals(response.url(), address);
  const headers = response.headers();
  expect(headers['content-security-policy']).toContain('sandbox allow-scripts');
  expect(headers['content-security-policy']).toContain("connect-src 'none'");
  expect(headers['x-content-type-options']).toBe('nosniff');
  await expect(frame.locator('body')).toHaveAttribute('data-done', 'yes');
  const results = frame.locator('#results');
  for (const line of [
    'fetch: blocked',
    'parent: blocked',
    'cookies: blocked',
    'popup: blocked',
    'rowbot: bridge only',
  ])
    await expect(results).toContainText(line);
  // The top window stayed on the chat, and nothing else opened.
  expect(page.url()).toBe(address);
  expect(context.pages()).toHaveLength(1);
  // Loaded directly later, the frame's address serves nothing.
  expect((await page.request.get(response.url())).status()).toBe(404);
});
