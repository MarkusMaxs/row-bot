import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { InstallCountSnapshot } from '../../api/types';
import { InstallCountCard, type InstallCountCardIo } from './InstallCountCard';

const card: InstallCountSnapshot = {
  available: true,
  turned_off_here: false,
  enabled: true,
  needs_disclosure: true,
  service: 'row-bot-count.row-bot.workers.dev',
  version: '5.3.0',
  os: 'windows',
  last_sent_on: null,
  can_delete: false,
};

function show(
  value: InstallCountSnapshot | null = card,
  choose: InstallCountCardIo['choose'] = async (enabled) => ({
    ...card,
    enabled,
    needs_disclosure: false,
  }),
) {
  const io = {
    load: vi.fn(async () => value),
    choose: vi.fn(choose),
  };
  render(<InstallCountCard io={io} ready="instance:session" />);
  return io;
}

it('says what is sent, when, where and for how long, with counting on and one Continue', async () => {
  const io = show();
  const dialog = await screen.findByRole('dialog', {
    name: 'Count this installation',
  });
  expect(dialog).toHaveTextContent(
    'A random ID made for this count, the Row-Bot version (5.3.0) and the system (Windows).',
  );
  expect(dialog).toHaveTextContent(
    'no name, account, location, conversations, files or usage',
  );
  expect(dialog).toHaveTextContent('Once a day while Row-Bot is running.');
  expect(dialog).toHaveTextContent('row-bot-count.row-bot.workers.dev');
  expect(dialog).toHaveTextContent('deleted after 35 days');
  expect(dialog).toHaveTextContent(
    'Cloudflare sees it, as it does for any connection. Row-Bot doesn’t store it.',
  );
  expect(dialog).toHaveTextContent('Settings › System › Privacy');
  expect(dialog).toHaveTextContent(
    'installations reporting in the last 7 and 15 days',
  );
  expect(
    screen.getByRole('switch', { name: 'Count this installation' }),
  ).toBeChecked();
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
  await waitFor(() => expect(io.choose).toHaveBeenCalledWith(true));
  await waitFor(() =>
    expect(
      screen.queryByRole('dialog', { name: 'Count this installation' }),
    ).toBeNull(),
  );
});

it('keeps counting off when the switch is turned off before Continue', async () => {
  const io = show();
  fireEvent.click(
    await screen.findByRole('switch', { name: 'Count this installation' }),
  );
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
  await waitFor(() => expect(io.choose).toHaveBeenCalledWith(false));
});

it('closing it chooses nothing, so it asks again next time', async () => {
  const io = show();
  await screen.findByRole('dialog', { name: 'Count this installation' });
  fireEvent.click(screen.getByRole('button', { name: 'Close dialog' }));
  await waitFor(() =>
    expect(
      screen.queryByRole('dialog', { name: 'Count this installation' }),
    ).toBeNull(),
  );
  expect(io.choose).not.toHaveBeenCalled();
});

it('stays until the choice is saved', async () => {
  show(card, async () => {
    throw { code: 'network_unavailable' };
  });
  fireEvent.click(await screen.findByRole('button', { name: 'Continue' }));
  expect(await screen.findByRole('alert')).toBeVisible();
  expect(
    screen.getByRole('dialog', { name: 'Count this installation' }),
  ).toBeVisible();
});

it('shows nothing once answered, while Setup comes first, or while counting is dormant', async () => {
  for (const value of [
    { ...card, needs_disclosure: false },
    null,
    { ...card, available: false, needs_disclosure: false },
  ]) {
    const io = show(value);
    await waitFor(() => expect(io.load).toHaveBeenCalled());
    expect(
      screen.queryByRole('dialog', { name: 'Count this installation' }),
    ).toBeNull();
  }
});
