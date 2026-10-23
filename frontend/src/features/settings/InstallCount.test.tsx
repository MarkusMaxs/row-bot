import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import type { InstallCountSnapshot } from '../../api/types';
import { OverlayProvider } from '../../ui/overlays';
import { Privacy, type PrivacyIo } from './InstallCount';

const counted: InstallCountSnapshot = {
  available: true,
  turned_off_here: false,
  enabled: true,
  needs_disclosure: false,
  service: 'row-bot-count.row-bot.workers.dev',
  version: '5.3.0',
  os: 'macos',
  last_sent_on: new Date().toISOString().slice(0, 10),
  can_delete: true,
};

function show(
  value: InstallCountSnapshot,
  remove: PrivacyIo['remove'] = async () => ({
    outcome: 'deleted',
    snapshot: { ...value, enabled: false, can_delete: false },
  }),
) {
  const io = {
    load: vi.fn(async () => value),
    choose: vi.fn(async (enabled: boolean) => ({ ...value, enabled })),
    remove: vi.fn(remove),
  };
  render(
    <MemoryRouter>
      <OverlayProvider>
        <Privacy io={io} />
      </OverlayProvider>
    </MemoryRouter>,
  );
  return io;
}

it('turns counting off in one click and says what is sent behind one tap', async () => {
  const io = show(counted);
  const privacy = await screen.findByRole('region', { name: 'Privacy' });
  const toggle = await within(privacy).findByRole('switch', {
    name: 'Count this installation',
  });
  expect(toggle).toBeChecked();
  expect(privacy).toHaveTextContent('Last sent today');
  fireEvent.click(
    within(privacy).getByText('What is sent', { selector: 'summary' }),
  );
  expect(privacy).toHaveTextContent(
    'the Row-Bot version (5.3.0) and the system (macOS)',
  );
  fireEvent.click(toggle);
  await waitFor(() => expect(io.choose).toHaveBeenCalledWith(false));
  expect(await screen.findByText('Counting turned off')).toBeVisible();
});

it('deletes counted data only after confirming, and counting is then off', async () => {
  const io = show(counted);
  fireEvent.click(await screen.findByRole('button', { name: 'Delete…' }));
  const confirm = await screen.findByRole('alertdialog', {
    name: 'Delete counted data?',
  });
  expect(confirm).toHaveTextContent('turns counting off');
  expect(io.remove).not.toHaveBeenCalled();
  fireEvent.click(within(confirm).getByRole('button', { name: 'Delete' }));
  await waitFor(() => expect(io.remove).toHaveBeenCalledTimes(1));
  expect(
    await screen.findByText('Counted data deleted. Counting is off.'),
  ).toBeVisible();
  expect(
    screen.getByRole('switch', { name: 'Count this installation' }),
  ).not.toBeChecked();
  expect(screen.queryByRole('button', { name: 'Delete…' })).toBeNull();
});

it('says so when the counting service could not be reached to delete', async () => {
  show(counted, async () => ({
    outcome: 'failed',
    snapshot: { ...counted, enabled: false },
  }));
  fireEvent.click(await screen.findByRole('button', { name: 'Delete…' }));
  fireEvent.click(
    within(
      await screen.findByRole('alertdialog', { name: 'Delete counted data?' }),
    ).getByRole('button', { name: 'Delete' }),
  );
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Counting is off, but Row-Bot couldn’t reach its counting service',
  );
  expect(screen.getByRole('button', { name: 'Delete…' })).toBeVisible();
});

it('names the set-up that turned counting off, with no switch', async () => {
  show({
    ...counted,
    available: false,
    turned_off_here: true,
    can_delete: false,
  });
  expect(
    await screen.findByText(/this installation’s set-up turns counting off/),
  ).toBeVisible();
  expect(
    screen.queryByRole('switch', { name: 'Count this installation' }),
  ).toBeNull();
});

it('lists what else Row-Bot contacts, even while counting is dormant', async () => {
  const io = show({
    ...counted,
    available: false,
    service: '',
    last_sent_on: null,
    can_delete: false,
  });
  await waitFor(() => expect(io.load).toHaveBeenCalled());
  const privacy = screen.getByRole('region', { name: 'Privacy' });
  expect(
    within(privacy).queryByRole('switch', { name: 'Count this installation' }),
  ).toBeNull();
  for (const words of [
    'Update checks',
    'asks GitHub for a new version',
    'Connection checks',
    'Every hour',
    'Catalogs',
    'OpenRouter’s public list',
  ])
    expect(privacy).toHaveTextContent(words);
  expect(
    within(privacy).getByRole('link', { name: 'Settings › Updates' }),
  ).toHaveAttribute('href', '/settings/updates');
});
