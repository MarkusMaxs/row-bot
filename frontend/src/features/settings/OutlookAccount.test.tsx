import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';
import type { ClientController } from '../../api/controller';
import type {
  AccountAuthCommand,
  AccountAuthReceipt,
  AccountAuthSnapshot,
  SettingsSnapshot,
} from '../../api/types';
import type { ClientPlatform } from '../../platform';
import { RuntimeContext } from '../../runtime';
import { OverlayProvider } from '../../ui/overlays';
import {
  AccountsSnapshotPanel,
  SettingsDraftOwner,
  type SettingsMutationIO,
} from './SettingsSnapshotPanels';

// Outlook through your own Microsoft app (N10): its registration guide, its
// sign-in here or with a code from another device, and its states.

type Accounts = SettingsSnapshot['accounts'];
type State = Exclude<AccountAuthSnapshot['state'], 'partial'>;
const ADDRESS = 'you@yourdomain.com';

const item = (patch: Partial<Accounts['github']>): Accounts['github'] => ({
  account_id: 'github',
  enabled: null,
  configured: false,
  authentication_state: 'not_configured',
  credential: null,
  operations: [],
  read_operations: [],
  post_operations: [],
  engage_operations: [],
  ...patch,
});

function accounts(state: State, configured = state !== 'not_configured') {
  const outlook = (account_id: 'outlook_mail' | 'outlook_calendar') =>
    item({
      account_id,
      enabled: true,
      configured,
      authentication_state: state,
      address: state === 'not_authenticated' || !configured ? null : ADDRESS,
    });
  return {
    availability: 'available',
    github: item({}),
    gmail: item({ account_id: 'gmail' }),
    calendar: item({ account_id: 'calendar' }),
    x: item({ account_id: 'x' }),
    outlook_mail: outlook('outlook_mail'),
    outlook_calendar: outlook('outlook_calendar'),
  } satisfies Accounts;
}

function mount(state: State, signIn: 'this_computer' | 'device') {
  const snapshot = (patch: Partial<AccountAuthSnapshot> = {}) =>
    ({
      schema_version: 1,
      account: 'microsoft',
      revision: 'a'.repeat(64),
      configured: state !== 'not_configured',
      state,
      token_files: 0,
      sign_in: signIn,
      sign_in_blocker: null,
      address: state === 'connected' || state === 'invalid' ? ADDRESS : null,
      ...patch,
    }) satisfies AccountAuthSnapshot;
  const controller = {
    subscribe: () => () => undefined,
    getSnapshot: () => ({ handshake: { authentication_kind: 'local_owner' } }),
    accountAuth: vi.fn(async () => snapshot()),
    accountAuthCommand: vi.fn(
      async (
        account: AccountAuthSnapshot['account'],
        command: AccountAuthCommand,
      ): Promise<AccountAuthReceipt> => ({
        schema_version: 1,
        command_id: command.command_id,
        account,
        action: command.action,
        phase: command.action === 'start' ? 'running' : 'completed',
        message:
          command.action === 'start'
            ? 'Enter the code on Microsoft’s page, then allow access.'
            : 'Your Microsoft app is saved.',
        snapshot: snapshot(),
        ...(command.action === 'start' && signIn === 'device'
          ? {
              device_code: {
                user_code: 'ABCD-EFGH',
                verification_uri: 'https://microsoft.com/devicelogin',
                verification_uri_complete: '',
              },
              expires_in: 900,
            }
          : {}),
      }),
    ),
    // The sign-in is still waiting for the person.
    accountAuthReceipt: vi.fn(() => new Promise(() => {})),
    cancelAccountAuth: vi.fn(
      async (
        _account: string,
        commandId: string,
      ): Promise<AccountAuthReceipt> => ({
        schema_version: 1,
        command_id: commandId,
        account: 'microsoft',
        action: 'start',
        phase: 'cancelled',
        message: 'Authentication cancelled.',
        snapshot: snapshot(),
      }),
    ),
    githubAccess: vi.fn(() => new Promise(() => {})),
  };
  const mutation: SettingsMutationIO = {
    revision: 'settings-a',
    page: 'accounts',
    review: vi.fn(),
    execute: vi.fn(),
    receipt: vi.fn(),
    drafts: new SettingsDraftOwner(),
    onSnapshot: vi.fn(),
    refreshSnapshot: vi.fn(() => new Promise<SettingsSnapshot>(() => {})),
  };
  const platform = {
    openExternal: vi.fn(async () => null),
    writeClipboard: vi.fn(async () => true),
  };
  render(
    <RuntimeContext.Provider
      value={{
        controller: controller as unknown as ClientController,
        platform: platform as unknown as ClientPlatform,
      }}
    >
      <MemoryRouter>
        <OverlayProvider>
          <AccountsSnapshotPanel
            snapshot={accounts(state)}
            mutation={mutation}
            showActions
            only="microsoft"
          />
        </OverlayProvider>
      </MemoryRouter>
    </RuntimeContext.Provider>,
  );
  return { controller, platform };
}

function outlookRow() {
  return screen
    .getByText('Outlook', { selector: '.settings-row-label' })
    .closest('.settings-row') as HTMLElement;
}

beforeEach(() => sessionStorage.clear());

it('guides the one-time app registration and saves the app’s client ID', async () => {
  const { controller } = mount('not_configured', 'this_computer');
  const guide = screen.getByRole('region', { name: 'Connect Outlook' });
  expect(within(outlookRow()).getByText('Not connected')).toBeVisible();
  expect(guide).toHaveTextContent('Name it Row-Bot');
  expect(guide).toHaveTextContent(
    'any organizational directory and personal Microsoft accounts',
  );
  expect(guide).toHaveTextContent('Allow public client flows');
  expect(guide).toHaveTextContent('No client secret is needed');
  expect(
    within(guide).getByRole('button', { name: 'Copy Redirect URI' }),
  ).toBeVisible();
  expect(guide).toHaveTextContent('http://localhost');
  expect(guide).toHaveTextContent(
    'User.Read Mail.ReadWrite Mail.Send Calendars.ReadWrite offline_access',
  );
  expect(
    within(guide).getByRole('link', { name: /Open App registrations/ }),
  ).toHaveAttribute('href', expect.stringContaining('entra.microsoft.com'));
  // Signing in waits for the app.
  expect(
    await within(guide).findByRole('button', {
      name: 'Sign in with Microsoft',
    }),
  ).toBeDisabled();
  expect(guide).toHaveTextContent('Need admin approval');
  fireEvent.change(within(guide).getByLabelText('Application (client) ID'), {
    target: { value: ' 11111111-2222-3333-4444-555555555555 ' },
  });
  fireEvent.click(within(guide).getByRole('button', { name: 'Save' }));
  await waitFor(() =>
    expect(controller.accountAuthCommand).toHaveBeenCalledWith(
      'microsoft',
      expect.objectContaining({
        action: 'import_credentials',
        credentials_json: JSON.stringify({
          client_id: '11111111-2222-3333-4444-555555555555',
          tenant: '',
        }),
      }),
    ),
  );
  expect(await screen.findByText('Your Microsoft app is saved.')).toBeVisible();
});

it('signs in from another device with Microsoft’s code', async () => {
  const { controller, platform } = mount('not_authenticated', 'device');
  const start = await screen.findByRole('button', {
    name: 'Sign in with a code',
  });
  fireEvent.click(start);
  const code = await screen.findByText('ABCD-EFGH');
  expect(code).toBeVisible();
  expect(screen.getByText('microsoft.com/devicelogin')).toBeVisible();
  expect(await screen.findByRole('timer')).toHaveTextContent(/1[45]:dd/);
  fireEvent.click(
    screen.getByRole('button', { name: /Open microsoft.com\/devicelogin/ }),
  );
  expect(platform.openExternal).toHaveBeenCalledWith(
    'https://microsoft.com/devicelogin',
  );
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
  await waitFor(() => expect(controller.cancelAccountAuth).toHaveBeenCalled());
  await waitFor(() => expect(screen.queryByText('ABCD-EFGH')).toBeNull());
});

it('shows who is signed in, and a sign-in that stopped working asks to sign in again', async () => {
  mount('connected', 'this_computer');
  expect(within(outlookRow()).getByText('Connected')).toBeVisible();
  expect(within(outlookRow()).getByText(ADDRESS)).toBeVisible();
  expect(
    await within(outlookRow()).findByRole('button', { name: 'Check Outlook' }),
  ).toBeVisible();
  expect(screen.getByText('How to set up Outlook')).toBeVisible(); // Folded, done.
  expect(
    screen.getByRole('switch', { name: 'Outlook mail access' }),
  ).toBeChecked();
});

it('a revoked sign-in reads Needs reconnecting with Sign in again', async () => {
  const { controller } = mount('invalid', 'this_computer');
  expect(within(outlookRow()).getByText('Needs reconnecting')).toBeVisible();
  expect(outlookRow()).toHaveTextContent(
    `${ADDRESS} · sign in again to keep using it`,
  );
  fireEvent.click(
    await within(outlookRow()).findByRole('button', {
      name: 'Sign in again to Outlook',
    }),
  );
  await waitFor(() =>
    expect(controller.accountAuthCommand).toHaveBeenCalledWith(
      'microsoft',
      expect.objectContaining({ action: 'start' }),
    ),
  );
});
