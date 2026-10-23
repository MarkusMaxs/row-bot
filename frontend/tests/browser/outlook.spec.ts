import type { Page, TestInfo } from '@playwright/test';
import { assertNoOverflow, expect, screenshot, test } from './evidence';
import { fixtureHeaders } from './surface-helpers';
import { composer, newConversation } from './unified-helpers';

/*
 * N10 · Outlook through your own Microsoft app, and B398 · Microsoft 365
 * asks for its tenant. The fixture seeds Outlook's states in an in-memory
 * keychain; a sign-in from another device gets a fixed code that is never
 * answered; the approval cards are written by the real Outlook card writer;
 * Microsoft 365's Registry check answers with the catalog's own record (or as
 * removed). Nothing reaches Microsoft or the Registry. Each test resets.
 */

const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 900 };
const OUTLOOK = `/app-v2/settings/apps/item?id=${encodeURIComponent('builtin:account:microsoft')}&edit=1`;
const WORK_IQ = `/app-v2/settings/apps/item?id=${encodeURIComponent('mcp:official:com.microsoft/workiq-admintools@1.0.0')}`;
const ADDRESS = 'you@yourdomain.com';

async function views(page: Page, info: TestInfo, name: string) {
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

/** Evidence at desktop and phone size, light and dark. */
async function everywhere(page: Page, info: TestInfo, name: string) {
  await views(page, info, name);
  await page.setViewportSize(PHONE);
  await views(page, info, `${name}-phone`);
  await page.setViewportSize(DESKTOP);
}

async function seed(page: Page, path: string) {
  const response = await page.request.post(path, {
    headers: fixtureHeaders(),
  });
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

test.beforeEach(async ({ page }) => page.setViewportSize(DESKTOP));
test.afterEach(async ({ page }) => {
  // Off the page first: what the reset removes is never read while it goes.
  await page.goto('/app-v2/');
  await seed(page, '/__n10_fixture/reset');
});

test('Outlook guides the one-time app registration, then shows who is signed in', async ({
  page,
}, info) => {
  await seed(page, '/__n10_fixture/outlook/none');
  await page.goto(OUTLOOK);
  const guide = page.getByRole('region', { name: 'Connect Outlook' });
  await expect(guide).toContainText('Name it Row-Bot');
  await expect(guide).toContainText('Allow public client flows');
  await expect(guide).toContainText('Need admin approval');
  await expect(
    guide.getByRole('button', { name: 'Sign in with Microsoft' }),
  ).toBeDisabled();
  await expect(guide.getByLabel('Application (client) ID')).toBeVisible();
  await expect(
    page.getByRole('switch', { name: 'Outlook mail access' }),
  ).toHaveCount(0); // Its switches come once signed in.
  await everywhere(page, info, 'outlook-setup-guide');
  await guide
    .getByRole('button', { name: 'Sign in with Microsoft' })
    .scrollIntoViewIfNeeded();
  await everywhere(page, info, 'outlook-setup-guide-sign-in');

  await seed(page, '/__n10_fixture/outlook/signed_in');
  await page.reload();
  await expect(page.getByText(ADDRESS).first()).toBeVisible();
  await expect(
    page.getByText('Connected', { exact: true }).filter({ visible: true }),
  ).toBeVisible();
  await expect(
    page.getByRole('switch', { name: 'Outlook mail access' }),
  ).toBeChecked();
  await expect(
    page.getByRole('switch', { name: 'Outlook calendar access' }),
  ).toBeChecked();
  await everywhere(page, info, 'outlook-signed-in');
});

test('from another device, Outlook signs in with Microsoft’s code', async ({
  page,
}, info) => {
  await seed(page, '/__n10_fixture/outlook/registered?route=device');
  await page.goto(OUTLOOK);
  await page.getByRole('button', { name: 'Sign in with a code' }).click();
  await expect(page.getByText('ABCD-EFGH')).toBeVisible();
  await expect(
    page.getByRole('button', { name: /Open microsoft\.com\/devicelogin/ }),
  ).toBeVisible();
  await expect(page.getByRole('timer')).toContainText(
    /The sign-in code works for another 1[45]:\d\d\./,
  );
  await everywhere(page, info, 'outlook-device-code');
  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  await expect(page.getByText('ABCD-EFGH')).toHaveCount(0);
});

test('a sign-in Microsoft turned down needs you, and Outlook offers Sign in again', async ({
  page,
}, info) => {
  await seed(page, '/__n10_fixture/outlook/signed_out');
  const needs = page.getByRole('list', { name: 'Needs you' });
  await expect(async () => {
    await page.goto('/app-v2/');
    await expect(needs.getByText(/Outlook/).first()).toBeVisible({
      timeout: 3000,
    });
  }).toPass({ timeout: 20_000 });
  await everywhere(page, info, 'outlook-needs-you');

  await page.goto(OUTLOOK);
  await expect(
    page
      .getByText('Needs reconnecting', { exact: true })
      .filter({ visible: true }),
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Sign in again to Outlook' }),
  ).toBeVisible();
  await everywhere(page, info, 'outlook-signed-out');
});

test('sending an Outlook email asks first, saying who it goes to, what it says and its file', async ({
  page,
}, info) => {
  await seed(page, '/__n10_fixture/outlook/signed_in');
  await newConversation(page);
  await composer(page).fill('n10 mail fixture');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  const card = page.getByRole('complementary', {
    name: /^Approval required/,
  });
  await expect(card).toContainText(`Send this email from ${ADDRESS}?`);
  await expect(card).toContainText('To: amy@contoso.com');
  await expect(card).toContainText('Cc: ben@contoso.com');
  await expect(card).toContainText('Subject: Q3 numbers');
  await expect(card).toContainText(/Attachments: q3-report\.pdf \(\d+ KB\)/);
  await expect(card).toContainText('The Q3 numbers are attached.');
  await everywhere(page, info, 'outlook-send-card');
  await card.getByRole('button', { name: 'Deny' }).click();
  await expect(card).toHaveCount(0);
});

test('an event that invites people asks first, with its time, attendees and place', async ({
  page,
}, info) => {
  await seed(page, '/__n10_fixture/outlook/signed_in');
  await newConversation(page);
  await composer(page).fill('n10 event fixture');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  const card = page.getByRole('complementary', {
    name: /^Approval required/,
  });
  await expect(card).toContainText('Send invitations for “Q3 review”?');
  await expect(card).toContainText(
    'When: Thu 29 Oct 2026 14:00–14:45 (Europe/London)',
  );
  await expect(card).toContainText('Where: Room 4');
  await expect(card).toContainText(
    'Attendees: amy@contoso.com, ben@contoso.com',
  );
  await expect(card).toContainText('Teams meeting: Yes');
  await everywhere(page, info, 'outlook-event-card');
  await card.getByRole('button', { name: 'Deny' }).click();
  await expect(card).toHaveCount(0);
});

test('Microsoft 365 asks for its tenant ID, and a step that fails says what failed (B398)', async ({
  page,
}, info) => {
  await seed(page, '/__n10_fixture/registry/same');
  await page.goto('/app-v2/settings/apps/microsoft-365');
  await expect(
    page.getByText(/Needs a Microsoft 365 admin to turn on Work IQ/).first(),
  ).toBeVisible();
  await page.goto(WORK_IQ);
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  const consent = page.getByRole('dialog');
  await consent.getByRole('button', { name: 'Connect', exact: true }).click();
  const tenant = page.getByLabel('Microsoft Entra tenant ID');
  await expect(tenant).toBeVisible();
  await expect(
    page.getByText('Microsoft Entra admin center › Overview › Tenant ID'),
  ).toBeVisible();
  await everywhere(page, info, 'm365-tenant-step');
  await tenant.fill('contoso');
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  await expect(
    page.getByText(/Check Microsoft Entra tenant ID: a tenant ID looks like/),
  ).toBeVisible();
  await everywhere(page, info, 'm365-tenant-check');
  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'Continue', exact: true }),
  ).toHaveCount(0);
  await page.goto('/app-v2/');
  await seed(page, '/__n10_fixture/reset');

  await seed(page, '/__n10_fixture/registry/removed');
  await page.goto(WORK_IQ);
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Connect', exact: true })
    .click();
  await expect(
    page
      .getByText(
        /^Couldn’t add .*: it’s no longer listed in the MCP Registry\.$|^Couldn't add .*: it's no longer listed in the MCP Registry\.$/,
      )
      .first(),
  ).toBeVisible();
  await everywhere(page, info, 'm365-plain-error');
});
