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
  GitHubAccessSnapshot,
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

type Accounts = SettingsSnapshot['accounts'];
type Item = Accounts['github'];
const item = (patch: Partial<Item>): Item => ({
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
/** GitHub connected, Google's sign-in expired, X not signed in yet. */
const accounts: Accounts = {
  availability: 'available',
  github: item({
    configured: true,
    authentication_state: 'connected',
    credential: { configured: false, source: '', fingerprint: '' },
  }),
  gmail: item({
    account_id: 'gmail',
    enabled: true,
    configured: true,
    authentication_state: 'expired',
    operations: ['search_gmail'],
  }),
  calendar: item({
    account_id: 'calendar',
    enabled: false,
    configured: true,
    authentication_state: 'connected',
    operations: ['search_events'],
  }),
  x: item({
    account_id: 'x',
    enabled: true,
    configured: true,
    authentication_state: 'not_authenticated',
    read_operations: ['x_search'],
    post_operations: [],
    engage_operations: [],
    callback_url: 'http://127.0.0.1:9/callback',
  }),
};
const github: GitHubAccessSnapshot = {
  schema_version: 1,
  revision: 'g'.repeat(64),
  state: 'connected',
  credential_source: 'github_cli',
  connected: true,
  anonymous_ok: true,
  cli_installed: true,
  cli_authenticated: true,
  remaining: 4000,
  retry_after_seconds: null,
};
/** How the server says this device signs in. */
type Route = Pick<AccountAuthSnapshot, 'sign_in' | 'sign_in_blocker'>;
const DESKTOP: Route = { sign_in: 'this_computer', sign_in_blocker: null };
const NO_PUBLIC_ADDRESS: Route = {
  sign_in: 'unavailable',
  sign_in_blocker: 'public_address_required',
};
const PUBLIC_ADDRESS: Route = { sign_in: 'link', sign_in_blocker: null };
const X_PAGE =
  'https://x.com/i/oauth2/authorize?client_id=fixture&state=fixture-state';

const auth = (
  account: 'google' | 'x',
  patch: Partial<AccountAuthSnapshot> = {},
): AccountAuthSnapshot => ({
  schema_version: 1,
  account,
  revision: 'a'.repeat(64),
  configured: true,
  state: account === 'google' ? 'expired' : 'not_authenticated',
  token_files: account === 'google' ? 2 : 0,
  ...DESKTOP,
  ...patch,
});

function page(
  snapshot: Accounts = accounts,
  prepare: (controller: ReturnType<typeof fakeController>) => void = () =>
    undefined,
  /** The desktop app on the computer Row-Bot runs on, or another device. */
  here = true,
  route: Route = here ? DESKTOP : NO_PUBLIC_ADDRESS,
) {
  const controller = fakeController(here, route);
  prepare(controller);
  return mount(snapshot, controller);
}
function fakeController(here = true, route: Route = DESKTOP) {
  const state = {
    handshake: { authentication_kind: here ? 'local_owner' : 'session' },
  };
  return {
    subscribe: () => () => undefined,
    getSnapshot: () => state,
    accountAuth: vi.fn(async (account: 'google' | 'x') => auth(account, route)),
    accountAuthCommand: vi.fn(
      async (
        account: 'google' | 'x',
        command: AccountAuthCommand,
      ): Promise<AccountAuthReceipt> => ({
        schema_version: 1,
        command_id: command.command_id,
        account,
        action: command.action,
        phase: command.action === 'start' ? 'running' : 'completed',
        message: 'Synthetic account result',
        snapshot: auth(account, route),
        authorization_url:
          command.action === 'start' && route.sign_in === 'link'
            ? X_PAGE
            : null,
      }),
    ),
    accountAuthReceipt: vi.fn(
      async (
        account: 'google' | 'x',
        commandId: string,
      ): Promise<AccountAuthReceipt> => ({
        schema_version: 1,
        command_id: commandId,
        account,
        action: 'start',
        phase: 'completed',
        message: 'Synthetic sign-in complete',
        snapshot: auth(account, {
          state: 'saved_unchecked',
          token_files: 1,
          ...route,
        }),
      }),
    ),
    cancelAccountAuth: vi.fn(),
    githubAccess: vi.fn(async () => github),
    githubAccessCommand: vi.fn(),
    githubAccessReceipt: vi.fn(),
  };
}
function mount(
  snapshot: Accounts,
  controller: ReturnType<typeof fakeController>,
) {
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
  const platform = { openExternal: vi.fn(async () => null) };
  const view = render(
    <RuntimeContext.Provider
      value={{
        controller: controller as unknown as ClientController,
        platform: platform as unknown as ClientPlatform,
      }}
    >
      <MemoryRouter>
        <OverlayProvider>
          <AccountsSnapshotPanel
            snapshot={snapshot}
            mutation={mutation}
            showActions
          />
        </OverlayProvider>
      </MemoryRouter>
    </RuntimeContext.Provider>,
  );
  return { controller, mutation, view, platform };
}
/** An account's row: its name, one status and its actions. */
function row(name: string) {
  return screen
    .getByText(name, { selector: '.settings-row-label' })
    .closest('.settings-row') as HTMLElement;
}

beforeEach(() => sessionStorage.clear());

it('shows each account as one row with one status and labelled actions', async () => {
  const { controller } = page();
  // Google counts once, and needs reconnecting because Gmail's sign-in expired.
  expect(screen.getByText('1 connected')).toBeVisible();
  expect(screen.getByText('1 needs reconnecting')).toBeVisible();
  expect(screen.getByText('1 not connected')).toBeVisible();
  expect(within(row('GitHub')).getByText('Connected')).toBeVisible();
  expect(within(row('Google')).getByText('Needs reconnecting')).toBeVisible();
  expect(within(row('X')).getByText('Not connected')).toBeVisible();
  expect(
    await within(row('GitHub')).findByRole('button', { name: 'Check GitHub' }),
  ).toBeVisible();
  expect(
    await within(row('Google')).findByRole('button', {
      name: 'Reconnect Google',
    }),
  ).toBeVisible();
  expect(
    within(row('Google')).getByRole('button', { name: 'Disconnect Google' }),
  ).toBeVisible();
  // Not connected: no actions in the row; its guide shows the steps.
  expect(within(row('X')).queryByRole('button')).toBeNull();
  expect(screen.getByRole('region', { name: 'Connect X' })).toBeVisible();
  expect(
    await screen.findByRole('button', { name: 'Connect X' }),
  ).toBeEnabled();
  // Signed in: the guide is folded away.
  expect(
    screen.getByRole('region', { name: 'Connect Google' }),
  ).not.toBeVisible();
  expect(screen.getByText('How to set up Google')).toBeVisible();
  // Opening the page contacts nothing and refreshes no token.
  expect(controller.accountAuthCommand).not.toHaveBeenCalled();
  expect(controller.githubAccessCommand).not.toHaveBeenCalled();
});

it('labels Gmail and Calendar access and keeps their actions behind Choose', async () => {
  page();
  expect(screen.getByRole('switch', { name: 'Gmail access' })).toBeChecked();
  expect(
    screen.getByRole('switch', { name: 'Calendar access' }),
  ).not.toBeChecked();
  expect(screen.getByText('1 of 5 actions')).toBeVisible();
  expect(screen.getByText('1 of 7 actions')).toBeVisible();
  expect(screen.queryByRole('checkbox', { name: 'Search email' })).toBeNull();
  fireEvent.click(
    screen.getByRole('button', { name: 'Choose gmail access actions' }),
  );
  const dialog = screen.getByRole('dialog', { name: 'Gmail access: actions' });
  expect(
    within(dialog).getByRole('checkbox', { name: 'Search email' }),
  ).toBeChecked();
  expect(
    within(dialog).getByRole('checkbox', { name: 'Send email' }),
  ).not.toBeChecked();
});

it('chooses Google’s sign-in file with a file picker and never shows its contents', async () => {
  const { controller, view } = page();
  const choose = await screen.findByRole('button', {
    name: 'Choose Google sign-in file',
  });
  const input =
    view.container.querySelector<HTMLInputElement>('input[type="file"]')!;
  const opened = vi.spyOn(input, 'click');
  fireEvent.click(choose);
  expect(opened).toHaveBeenCalledOnce();
  const file = new File(
    ['{"installed":{"client_secret":"private-fixture"}}'],
    'client.json',
    { type: 'application/json' },
  );
  fireEvent.change(input, { target: { files: [file] } });
  await waitFor(() =>
    expect(controller.accountAuthCommand).toHaveBeenCalledWith(
      'google',
      expect.objectContaining({
        action: 'import_credentials',
        credentials_json: expect.stringContaining('private-fixture'),
      }),
    ),
  );
  expect(screen.queryByText(/private-fixture/)).toBeNull();
});

it('signs in to X from its guide and recovers the original receipt, then reloads the page', async () => {
  const { controller, mutation } = page();
  fireEvent.click(await screen.findByRole('button', { name: 'Connect X' }));
  expect(controller.accountAuthCommand).toHaveBeenCalledWith(
    'x',
    expect.objectContaining({ action: 'start' }),
  );
  expect(await screen.findByText('Synthetic sign-in complete')).toBeVisible();
  expect(controller.accountAuthReceipt).toHaveBeenCalledTimes(1);
  expect(
    sessionStorage.getItem('row-bot:account-auth:x:pending:v1'),
  ).toBeNull();
  expect(mutation.refreshSnapshot).toHaveBeenCalled();
});

it('confirms a disconnect before removing the local sign-in', async () => {
  const { controller } = page();
  fireEvent.click(
    await within(row('Google')).findByRole('button', {
      name: 'Disconnect Google',
    }),
  );
  expect(
    screen.getByRole('alertdialog', {
      name: 'Confirm local account disconnect',
    }),
  ).toBeVisible();
  expect(controller.accountAuthCommand).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Remove local tokens' }));
  await waitFor(() =>
    expect(controller.accountAuthCommand).toHaveBeenCalledWith(
      'google',
      expect.objectContaining({ action: 'disconnect', confirmed: true }),
    ),
  );
});

it('reconnects an expired Google sign-in in the browser', async () => {
  const { controller } = page();
  fireEvent.click(
    await within(row('Google')).findByRole('button', {
      name: 'Reconnect Google',
    }),
  );
  expect(controller.accountAuthCommand).toHaveBeenCalledWith(
    'google',
    expect.objectContaining({ action: 'start' }),
  );
});

it('another device without a public address checks and disconnects, and says how it could sign in', async () => {
  // A sign-in returns to a loopback address on the computer Row-Bot runs on,
  // or to Row-Bot's public HTTPS address, which isn't set up.
  const { controller } = page(accounts, undefined, false);
  fireEvent.click(await screen.findByRole('button', { name: 'Check Google' }));
  expect(controller.accountAuthCommand).toHaveBeenCalledWith(
    'google',
    expect.objectContaining({ action: 'check' }),
  );
  expect(
    screen.getByRole('button', { name: 'Disconnect Google' }),
  ).toBeVisible();
  expect(
    (
      await screen.findAllByText(
        /returns to Row-Bot’s public HTTPS address, and Row-Bot doesn’t have one yet/,
      )
    ).length,
  ).toBeGreaterThan(0);
  expect(screen.queryByRole('button', { name: 'Connect X' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Reconnect Google' })).toBeNull();
  expect(screen.queryByText(/local owner/)).toBeNull();
  expect(controller.accountAuthCommand).not.toHaveBeenCalledWith(
    expect.anything(),
    expect.objectContaining({ action: 'start' }),
  );
});

it('another device signs in to X in its own browser through Row-Bot’s public address', async () => {
  const callback =
    'https://rowbot.example.test/api/v1/accounts/x/auth/callback';
  const { controller, platform } = page(
    { ...accounts, x: { ...accounts.x, callback_url: callback } },
    (fake) =>
      // Still waiting for X to send the browser back.
      fake.accountAuthReceipt.mockImplementation(() => new Promise(() => {})),
    false,
    PUBLIC_ADDRESS,
  );
  // The callback this device's sign-in returns to, to register with X.
  expect(screen.getByText(callback)).toBeVisible();
  fireEvent.click(await screen.findByRole('button', { name: 'Connect X' }));
  expect(controller.accountAuthCommand).toHaveBeenCalledWith(
    'x',
    expect.objectContaining({ action: 'start' }),
  );
  await waitFor(() =>
    expect(platform.openExternal).toHaveBeenCalledWith(X_PAGE),
  );
  // A link too, for a browser that blocked the page from opening.
  expect(
    screen.getByRole('link', { name: 'Open X’s sign-in page' }),
  ).toHaveAttribute('href', X_PAGE);
  expect(
    screen.getByText(/Finish signing in to X in the page that opened/),
  ).toBeVisible();
});

it('tells another device a Google Desktop app client can’t return to it, and which redirect to list', async () => {
  const redirect =
    'https://rowbot.example.test/api/v1/accounts/google/auth/callback';
  page(
    {
      ...accounts,
      gmail: { ...accounts.gmail, callback_url: redirect },
      calendar: { ...accounts.calendar, callback_url: redirect },
    },
    undefined,
    false,
    { sign_in: 'unavailable', sign_in_blocker: 'desktop_client' },
  );
  expect(
    (await screen.findAllByText(/Your Google client is a Desktop app/)).length,
  ).toBeGreaterThan(0);
  expect(
    screen.getAllByText(/Create a client of type Web application/).length,
  ).toBeGreaterThan(0);
  expect(screen.getAllByText(redirect).length).toBeGreaterThan(0);
  expect(
    screen.getAllByRole('button', { name: 'Copy Google redirect URI' }).length,
  ).toBeGreaterThan(0);
  expect(screen.queryByText(/type Desktop app/)).toBeNull();
  expect(screen.queryByRole('button', { name: 'Reconnect Google' })).toBeNull();
});

it('recovers a retained sign-in without starting another one', async () => {
  sessionStorage.setItem(
    'row-bot:account-auth:google:pending:v1',
    '33333333-3333-4333-8333-333333333333',
  );
  const { controller } = page();
  expect(await screen.findByText('Synthetic sign-in complete')).toBeVisible();
  expect(controller.accountAuthReceipt).toHaveBeenCalledWith(
    'google',
    '33333333-3333-4333-8333-333333333333',
  );
  expect(controller.accountAuthCommand).not.toHaveBeenCalled();
});

it('lists each X account with its handle, name and ID, adds another and removes only the one chosen (N7)', async () => {
  const twoAccounts: Accounts = {
    ...accounts,
    x: {
      ...accounts.x,
      authentication_state: 'connected',
      x_accounts: [
        {
          id: '111',
          username: 'rowbot_ai',
          name: 'Row-Bot',
          state: 'connected',
        },
        { id: '222', username: 'sidd', name: 'Sidd', state: 'expired' },
      ],
    },
  };
  const { controller } = page(twoAccounts);
  const list = screen.getByRole('list', { name: 'X accounts' });
  const items = within(list).getAllByRole('listitem');
  expect(items.map((item) => item.textContent)).toEqual([
    expect.stringContaining('@rowbot_ai · Row-Bot'),
    expect.stringContaining('@sidd · Sidd'),
  ]);
  expect(within(items[0]).getByText('ID 111')).toBeVisible();
  expect(within(items[0]).getByText('Connected')).toBeVisible();
  expect(within(items[1]).getByText('Needs reconnecting')).toBeVisible();
  // No row-wide Disconnect: an account is removed on its own.
  expect(screen.queryByRole('button', { name: 'Disconnect X' })).toBeNull();

  fireEvent.click(
    await screen.findByRole('button', { name: 'Add another account' }),
  );
  expect(controller.accountAuthCommand).toHaveBeenCalledWith(
    'x',
    expect.objectContaining({ action: 'start' }),
  );
  await screen.findByText('Synthetic sign-in complete');

  fireEvent.click(
    within(items[1]).getByRole('button', { name: 'Remove @sidd' }),
  );
  const confirm = screen.getByRole('alertdialog', {
    name: 'Confirm local account disconnect',
  });
  expect(within(confirm).getByText(/your other X accounts stay/)).toBeVisible();
  expect(controller.accountAuthCommand).not.toHaveBeenCalledWith(
    'x',
    expect.objectContaining({ action: 'disconnect' }),
  );
  fireEvent.click(
    within(confirm).getByRole('button', { name: 'Remove @sidd' }),
  );
  await waitFor(() =>
    expect(controller.accountAuthCommand).toHaveBeenCalledWith(
      'x',
      expect.objectContaining({
        action: 'disconnect',
        confirmed: true,
        x_account: '222',
      }),
    ),
  );
});
