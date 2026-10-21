import type { Page, TestInfo } from '@playwright/test';
import { accessibility, assertNoOverflow, expect, test } from './evidence';
import { fixtureHeaders } from './surface-helpers';

// Learning (preview), L2x: Row-Bot runs personal models itself (O75), and the larger tiers. The
// fixture fakes an RTX 5090 (or no NVIDIA card), the model server's files installed or not, and a
// version at each of its states in Row-Bot's own server; the fake trainer makes v1. Nothing starts a
// server, reaches the network or needs a GPU. Setup on a 32 GB card, "Runs in" with Row-Bot
// recommended, not on this computer and downloading with the engine, the version starting, ready,
// and each way out when it can't start, and the server alone ready to download. Each test puts the
// profile back.

async function fixture(page: Page, path: string) {
  const response = await page.request.post(path, {
    headers: fixtureHeaders(),
  });
  expect(response.ok(), await response.text()).toBe(true);
}

const l1a = (page: Page, action: string) =>
  fixture(page, `/__l1a_fixture/learning/${action}`);
const l2b = (page: Page, action: string, name = '') =>
  fixture(
    page,
    `/__l2b_fixture/learning/${action}${name ? `?name=${name}` : ''}`,
  );
const l2c = (page: Page, action: string, name = '') =>
  fixture(
    page,
    `/__l2c_fixture/learning/${action}${name ? `?name=${name}` : ''}`,
  );
const l2x = (page: Page, action: string, name = '') =>
  fixture(
    page,
    `/__l2x_fixture/learning/${action}${name ? `?name=${name}` : ''}`,
  );

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    if (window !== window.top) return;
    localStorage.setItem(
      'row-bot.appearance.v1',
      JSON.stringify({
        version: 1,
        appearance: 'system',
        accent: 'blue',
        density: 'compact',
        reduce_transparency: false,
      }),
    );
  });
});

test.afterEach(async ({ page }) => {
  await l2x(page, 'restore');
  await l2c(page, 'restore');
  await l1a(page, 'restore');
});

const phone = (info: TestInfo) =>
  (info.project.use.viewport?.width ?? 1440) < 700;

/** `<surface>-<state>-{desktop,phone}-{light,dark}.png`, each with an axe scan. */
async function shoot(page: Page, info: TestInfo, name: string) {
  const size = phone(info) ? 'phone' : 'desktop';
  for (const appearance of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: appearance });
    await assertNoOverflow(page);
    const file = info.outputPath(`${name}-${size}-${appearance}.png`);
    await page.screenshot({
      path: file,
      fullPage: true,
      animations: 'disabled',
    });
    await info.attach(`${name}-${size}-${appearance}`, {
      path: file,
      contentType: 'image/png',
    });
    await accessibility(page, info, `${name}-${size}-${appearance}`);
  }
  await page.emulateMedia({ colorScheme: 'light' });
}

function notice(page: Page, text: string | RegExp) {
  return page.locator('.toast-message').filter({ hasText: text });
}

const GPU = 'NVIDIA GeForce RTX 5090 (32 GB)';

async function turnOn(page: Page, info: TestInfo) {
  await l1a(page, 'preview');
  await l2x(page, 'card', '5090');
  await page.goto('/app-v2/settings/learning');
  await page.getByRole('button', { name: 'Turn on Learning' }).click();
  const sheet = page.getByRole('dialog', { name: 'Turn on Learning' });
  await expect(sheet.getByText(GPU)).toBeVisible();
  // The card's own tier: what it trains, measured, and the models it can start from.
  const starting = sheet.getByRole('combobox', { name: 'Starting model' });
  await expect(starting).toBeVisible();
  expect(await starting.locator('option').count()).toBeGreaterThan(0);
  await shoot(page, info, 'setup-32gb');
  await sheet.getByRole('button', { name: 'Turn on Learning' }).click();
  await page
    .getByRole('dialog', { name: /^Download the training engine and / })
    .getByRole('button', { name: 'Not now', exact: true })
    .click();
  await expect(notice(page, 'Learning is on.')).toBeVisible();
}

async function trainV1(page: Page) {
  await l2c(page, 'setup');
  await l2c(page, 'train');
  await l2c(page, 'finish');
}

async function openModel(page: Page) {
  await page.goto('/app-v2/?tab=learning&view=model');
  await expect(
    page.getByRole('heading', { name: 'Learning', exact: true }),
  ).toBeVisible();
}

function version(page: Page) {
  return page.getByRole('region', { name: 'Personal model v1' });
}

function facts(page: Page) {
  return page.getByLabel('Personal model', { exact: true });
}

async function runsIn(page: Page) {
  await page
    .getByRole('button', { name: 'Change where your personal model runs' })
    .click();
  return page.getByRole('dialog', { name: 'Where your personal model runs' });
}

test('Row-Bot runs v1 itself: starting, ready, Runs in, and each way out when it can’t start', async ({
  page,
}, info) => {
  test.setTimeout(240_000);
  await turnOn(page, info);
  await trainV1(page);
  await l2x(page, 'server', 'installed');

  await l2x(page, 'version', 'starting');
  await openModel(page);
  await expect(version(page).getByText('Starting v1…')).toBeVisible();
  await shoot(page, info, 'model-starting-row-bot');

  await l2x(page, 'version', 'ready');
  await openModel(page);
  await expect(version(page).getByText('v1 is ready to compare')).toBeVisible();
  await expect(
    version(page).getByText(
      'Trained on 154 examples of your work · runs in Row-Bot.',
    ),
  ).toBeVisible();
  await expect(facts(page)).toContainText('Row-Bot ✓');
  await expect(facts(page)).toContainText(GPU);
  await shoot(page, info, 'model-ready-row-bot');

  const sheet = await runsIn(page);
  await expect(
    sheet.getByRole('radio', { name: /^Row-Bot · recommended/ }),
  ).toBeChecked();
  await expect(
    sheet.getByText(
      'Row-Bot runs it for you with llama.cpp, and stops it when it isn’t needed.',
    ),
  ).toBeVisible();
  await expect(sheet.getByRole('radio', { name: /^Ollama/ })).toBeEnabled();
  await shoot(page, info, 'runs-in-row-bot');
  await sheet.getByRole('button', { name: 'Cancel' }).click();

  // The runner isn't downloaded yet (an engine from before L2x): Download, or Ollama instead.
  await l2x(page, 'version', 'missing');
  await openModel(page);
  await expect(
    version(page).getByText(
      'v1 couldn’t start in Row-Bot. Your main model is still in use.',
    ),
  ).toBeVisible();
  await expect(
    version(page).getByText(
      'Row-Bot needs one more part of the training engine to run it.',
    ),
  ).toBeVisible();
  await expect(
    version(page).getByRole('button', { name: 'Download' }),
  ).toBeVisible();
  await expect(
    version(page).getByRole('button', { name: 'Try Ollama' }),
  ).toBeVisible();
  await shoot(page, info, 'model-server-missing');

  // It didn't load: the card may be full. Try again, or Ollama.
  await l2x(page, 'version', 'memory');
  await openModel(page);
  await expect(
    version(page).getByText(
      'It didn’t load. The graphics card may not have enough free memory.',
    ),
  ).toBeVisible();
  await expect(
    version(page).getByRole('button', { name: 'Try again' }),
  ).toBeVisible();
  await shoot(page, info, 'model-load-failed');
  await version(page).getByRole('button', { name: 'Try Ollama' }).click();
  await expect(
    notice(page, 'Personal models run in Ollama. Making v1 ready there.'),
  ).toBeVisible();
});

test('Runs in says when Row-Bot’s runner can’t run here, and while it downloads with the engine', async ({
  page,
}, info) => {
  test.setTimeout(180_000);
  await turnOn(page, info);
  await trainV1(page);
  await l2x(page, 'version', 'ready');

  // Downloading with the training engine (the runner isn't on this computer yet).
  await l2x(page, 'server', 'missing');
  await l2b(page, 'state', 'downloading');
  await openModel(page);
  let sheet = await runsIn(page);
  await expect(
    sheet.getByText('Downloading with the training engine…'),
  ).toBeVisible();
  await shoot(page, info, 'runs-in-downloading');
  await sheet.getByRole('button', { name: 'Cancel' }).click();
  await l2b(page, 'state', 'installed');

  // No NVIDIA card: not on this computer, and Ollama is the recommendation.
  await l2x(page, 'card', 'none');
  await openModel(page);
  sheet = await runsIn(page);
  await expect(
    sheet.getByRole('radio', { name: /^Row-Bot · not on this computer/ }),
  ).toBeDisabled();
  await expect(
    sheet.getByText(
      'It needs an NVIDIA graphics card. Your personal model can run in Ollama instead.',
    ),
  ).toBeVisible();
  await expect(
    sheet.getByRole('radio', { name: /^Ollama · recommended/ }),
  ).toBeVisible();
  await shoot(page, info, 'runs-in-not-here');
});

test('an engine from before Row-Bot ran personal models itself downloads only the runner', async ({
  page,
}, info) => {
  await turnOn(page, info);
  await l2c(page, 'setup');
  await l2b(page, 'state', 'server');
  await openModel(page);
  const card = page.getByText('Ready to download').first();
  await expect(card).toBeVisible();
  await expect(
    page.getByText(
      'Row-Bot can now run your personal model itself. It needs one more part of the training engine (578 MB).',
    ),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Download' })).toBeVisible();
  await shoot(page, info, 'model-server-ready-to-download');
});
