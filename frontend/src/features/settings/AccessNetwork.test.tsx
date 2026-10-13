import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { AccessInvitationClient } from '../../api/access';
import { OverlayProvider } from '../../ui/overlays';
import AccessNetwork from './AccessNetwork';

// Moved from AccessInvitations.test.tsx with the controls (Phase 14).
function fixture(): AccessInvitationClient {
  return {
    routes: vi.fn().mockResolvedValue([]),
    routeSettings: vi.fn().mockResolvedValue({
      listen_mode: 'local_only',
      configured_origins: [],
      managed_externally: false,
    }),
    invitations: vi.fn().mockResolvedValue([]),
    create: vi.fn(),
    cancel: vi.fn(),
    setListenMode: vi.fn().mockResolvedValue({ restart_required: true }),
    changeOrigin: vi.fn().mockResolvedValue(undefined),
  };
}
function show(client: AccessInvitationClient) {
  return render(
    <OverlayProvider>
      <AccessNetwork client={client} />
    </OverlayProvider>,
  );
}

it('changes where Row-Bot listens only after a confirmation and reports a needed restart', async () => {
  const client = fixture();
  show(client);
  fireEvent.change(await screen.findByLabelText('Listen on'), {
    target: { value: 'local_network' },
  });
  expect(client.setListenMode).not.toHaveBeenCalled();
  fireEvent.click(
    within(
      await screen.findByRole('alertdialog', {
        name: 'Let devices on your network reach Row-Bot?',
      }),
    ).getByRole('button', { name: 'Restart and apply' }),
  );
  await waitFor(() =>
    expect(client.setListenMode).toHaveBeenCalledWith(
      'local_only',
      'local_network',
    ),
  );
  expect(
    await screen.findByText('Saved. Restart Row-Bot to apply it.'),
  ).toBeInTheDocument();
});

it('adds one allowed address, and asks first before removing the one this device uses', async () => {
  const client = fixture();
  const view = show(client);
  fireEvent.change(await screen.findByLabelText('Add an allowed address'), {
    target: { value: 'https://example.test' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Add' }));
  await waitFor(() =>
    expect(client.changeOrigin).toHaveBeenCalledWith(
      'add',
      'https://example.test',
      [],
    ),
  );
  // Any signed-in device may change these; removing its own address would
  // cut it off, so that one asks first. Another address goes at once.
  const here = window.location.origin;
  const origins = [here, 'https://other.example.test'];
  vi.mocked(client.routeSettings).mockResolvedValue({
    listen_mode: 'local_only',
    configured_origins: origins,
    managed_externally: false,
  });
  view.unmount();
  show(client);
  expect(await screen.findByLabelText('Listen on')).toBeEnabled();
  fireEvent.click(
    screen.getByRole('button', { name: 'Remove https://other.example.test' }),
  );
  await waitFor(() =>
    expect(client.changeOrigin).toHaveBeenLastCalledWith(
      'remove',
      'https://other.example.test',
      origins,
    ),
  );
  const own = await screen.findByRole('button', { name: `Remove ${here}` });
  await waitFor(() => expect(own).toBeEnabled());
  fireEvent.click(own);
  const dialog = await screen.findByRole('alertdialog', {
    name: 'Remove the address this device uses?',
  });
  expect(client.changeOrigin).not.toHaveBeenCalledWith('remove', here, origins);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
  await waitFor(() =>
    expect(client.changeOrigin).toHaveBeenLastCalledWith(
      'remove',
      here,
      origins,
    ),
  );
});
