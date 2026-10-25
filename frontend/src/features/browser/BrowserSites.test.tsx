import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import type { ClientController } from '../../api/controller';
import type { ClientPlatform } from '../../platform';
import { RuntimeContext } from '../../runtime';
import { BrowserSites } from './BrowserSites';

function setup(
  items = [
    { site: 'homeserve.co.uk', cookies: 12 },
    { site: 'github.com', cookies: 4 },
  ],
) {
  const controller = {
    browserSites: vi.fn(async () => ({ items })),
    clearBrowserSites: vi.fn(async (site: string | null) => ({
      cleared: site ? 1 : items.length,
      items: site ? items.filter((item) => item.site !== site) : [],
    })),
  } as unknown as ClientController & {
    clearBrowserSites: ReturnType<typeof vi.fn>;
  };
  render(
    <RuntimeContext.Provider
      value={{ controller, platform: {} as ClientPlatform }}
    >
      <BrowserSites />
    </RuntimeContext.Provider>,
  );
  return controller;
}

it('lists the signed-in sites and signs out of one', async () => {
  const controller = setup();
  expect(await screen.findByText('homeserve.co.uk')).toBeVisible();
  await userEvent.click(
    screen.getByRole('button', { name: 'Sign out of homeserve.co.uk' }),
  );
  expect(controller.clearBrowserSites).toHaveBeenCalledWith('homeserve.co.uk');
  expect(screen.queryByText('homeserve.co.uk')).toBeNull();
  expect(screen.getByText('github.com')).toBeVisible();
});

it('signing out of every site asks once more first', async () => {
  const controller = setup();
  await userEvent.click(
    await screen.findByRole('button', { name: 'Sign out of all…' }),
  );
  expect(controller.clearBrowserSites).not.toHaveBeenCalled();
  await userEvent.click(
    screen.getByRole('button', { name: 'Sign out of all' }),
  );
  expect(controller.clearBrowserSites).toHaveBeenCalledWith(null);
  expect(await screen.findByText(/Not signed in anywhere yet/)).toBeVisible();
});
