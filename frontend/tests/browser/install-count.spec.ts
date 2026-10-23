import type { Page, TestInfo } from '@playwright/test';
import {
  accessibility,
  assertNoOverflow,
  expect,
  screenshot,
  test,
} from './evidence';

// N6: counting active installations, disclosed first and off in one click.
// The fixture keeps the count dormant unless a spec turns it on
// (/__n6_fixture/install-count/{state}); its counting service only records
// what would have been sent, so nothing leaves this computer.

function control() {
  const token = process.env.ROW_BOT_BROWSER_CONTROL_TOKEN;
  const base = process.env.ROW_BOT_BROWSER_BASE_URL;
  if (!token || !base) throw new Error('Use the isolated browser runner');
  return { 'X-Fixture-Token': token, Origin: new URL(base).origin };
}

async function fixture(page: Page, path: string) {
  const response = await page.request.post(path, { headers: control() });
  expect(response.ok(), await response.text()).toBe(true);
}

async function sent(page: Page): Promise<{ path: string; body: object }[]> {
  const response = await page.request.get('/__n6_fixture/install-count/sent', {
    headers: control(),
  });
  expect(response.ok()).toBe(true);
  return (await response.json()).sent;
}

test.afterEach(async ({ page }) => {
  await fixture(page, '/__n6_fixture/install-count/restore');
  await fixture(page, '/__p10_fixture/first-run/restore');
});

const phone = (info: TestInfo) =>
  (info.project.use.viewport?.width ?? 1440) < 700;

/** Light and dark, then an accessibility scan of what shows. */
async function shoot(page: Page, info: TestInfo, name: string) {
  const suffix = phone(info) ? '-phone' : '';
  for (const appearance of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: appearance });
    await assertNoOverflow(page);
    await screenshot(page, info, `${name}${suffix}-${appearance}`);
  }
  await accessibility(page, info, `${name}${suffix}`);
  await page.emulateMedia({ colorScheme: 'light' });
}

const card = (page: Page) =>
  page.getByRole('dialog', { name: 'Count this installation' });

async function expectDisclosure(page: Page) {
  const dialog = card(page);
  await expect(dialog).toContainText(
    'installations reporting in the last 7 and 15 days',
  );
  await expect(dialog).toContainText(
    'A random ID made for this count, the Row-Bot version',
  );
  await expect(dialog).toContainText('Once a day while Row-Bot is running.');
  await expect(dialog).toContainText('row-bot-count.row-bot.workers.dev');
  await expect(dialog).toContainText('deleted after 35 days');
  await expect(dialog).toContainText('Row-Bot doesn’t store it.');
  await expect(
    dialog.getByRole('switch', { name: 'Count this installation' }),
  ).toBeChecked();
}

test('first run: the card follows Setup, before the first chat', async ({
  page,
}, info) => {
  await fixture(page, '/__n6_fixture/install-count/fresh');
  await fixture(
    page,
    '/__p10_fixture/first-run/fresh?runtime=running&models=qwen3:8b,gemma3:12b',
  );
  await page.goto('/app-v2/');
  await expect(page).toHaveURL(/\/app-v2\/setup$/);
  // Setup comes first: the card never covers it.
  await page
    .getByRole('group', { name: 'How should Row-Bot think?' })
    .getByRole('button', { name: /On this computer/ })
    .click();
  await expect(card(page)).toHaveCount(0);
  await page
    .getByRole('list', { name: 'Recommended' })
    .getByRole('button', { name: /qwen3:8b/ })
    .click();
  await expect(card(page)).toBeVisible({ timeout: 15_000 });
  await expectDisclosure(page);
  expect(await sent(page)).toEqual([]);
  await shoot(page, info, 'install-count-first-run');

  await card(page).getByRole('button', { name: 'Continue' }).click();
  await expect(card(page)).toHaveCount(0);
  await page.goto('/app-v2/settings/system#privacy');
  await expect(
    page
      .getByRole('region', { name: 'Privacy' })
      .getByRole('switch', { name: 'Count this installation' }),
  ).toBeChecked();
});

test('an updating installation sees the same card once, and off stays off', async ({
  page,
}, info) => {
  await fixture(page, '/__n6_fixture/install-count/fresh');
  await page.goto('/app-v2/');
  await expect(card(page)).toBeVisible({ timeout: 15_000 });
  await expectDisclosure(page);
  await shoot(page, info, 'install-count-update');

  await card(page)
    .getByRole('switch', { name: 'Count this installation' })
    .click();
  await card(page).getByRole('button', { name: 'Continue' }).click();
  await expect(card(page)).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByRole('tablist', { name: 'Home capabilities' }),
  ).toBeVisible({ timeout: 15_000 });
  await expect(card(page)).toHaveCount(0);
  await page.goto('/app-v2/settings/system#privacy');
  await expect(
    page
      .getByRole('region', { name: 'Privacy' })
      .getByRole('switch', { name: 'Count this installation' }),
  ).not.toBeChecked();
  // Nothing was ever sent: it was off before its first count.
  expect(await sent(page)).toEqual([]);
});

test('Settings › System › Privacy: on, off and deleting what was counted', async ({
  page,
}, info) => {
  await fixture(page, '/__n6_fixture/install-count/on');
  await page.goto('/app-v2/settings/system#privacy');
  const privacy = page.getByRole('region', { name: 'Privacy' });
  const toggle = privacy.getByRole('switch', {
    name: 'Count this installation',
  });
  await expect(toggle).toBeChecked();
  await expect(privacy).toContainText('Last sent today');
  await expect(privacy).toContainText('What else Row-Bot contacts');
  for (const words of ['Update checks', 'Connection checks', 'Catalogs'])
    await expect(privacy).toContainText(words);
  await privacy.locator('summary', { hasText: 'Details' }).click();
  await expect(privacy).toContainText('Cloudflare sees it');
  await privacy.scrollIntoViewIfNeeded();
  await shoot(page, info, 'privacy-on');

  await toggle.click();
  await expect(toggle).not.toBeChecked();
  await expect(page.getByText('Counting turned off').first()).toBeVisible();
  await privacy.scrollIntoViewIfNeeded();
  await shoot(page, info, 'privacy-off');

  await privacy.getByRole('button', { name: 'Delete…' }).click();
  const confirm = page.getByRole('alertdialog', {
    name: 'Delete counted data?',
  });
  await expect(confirm).toContainText('turns counting off');
  await shoot(page, info, 'delete-counted-data');
  await confirm.getByRole('button', { name: 'Delete' }).click();
  await expect(
    page.getByText('Counted data deleted. Counting is off.').first(),
  ).toBeVisible();
  await expect(privacy.getByRole('button', { name: 'Delete…' })).toHaveCount(0);
  const requests = await sent(page);
  expect(requests.map((request) => request.path)).toEqual([
    '/v1/ping',
    '/v1/delete',
  ]);
  expect(Object.keys(requests[0].body)).toEqual([
    'schema',
    'install',
    'version',
    'os',
  ]);
});
