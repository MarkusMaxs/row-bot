import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { ClaudePlanStatus } from '../../api/types';
import type { ClientController } from '../../api/controller';
import { createFakePlatform } from '../../platform/fake';
import type { ClientPlatform } from '../../platform/types';
import { RuntimeContext } from '../../runtime';
import ClaudePlanSetup, { CLAUDE_CODE_INSTALL_URL } from './ClaudePlanSetup';

function plan(overrides: Partial<ClaudePlanStatus> = {}): ClaudePlanStatus {
  return {
    state: 'signed_out',
    version: '2.1.282',
    minimum_version: '2.1.263',
    plan: '',
    detail: '',
    set_up_again: false,
    ...overrides,
  };
}

function show(
  status: ClaudePlanStatus | null,
  {
    platform = createFakePlatform(),
    checking = false,
  }: { platform?: ClientPlatform; checking?: boolean } = {},
) {
  const onCheck = vi.fn();
  const onDone = vi.fn();
  render(
    <RuntimeContext.Provider
      value={{ controller: {} as ClientController, platform }}
    >
      <ClaudePlanSetup
        plan={status}
        checking={checking}
        onCheck={onCheck}
        onDone={onDone}
      />
    </RuntimeContext.Provider>,
  );
  return { onCheck, onDone };
}

const desktop = (extra = {}) =>
  createFakePlatform({
    discover: {
      status: 'ok',
      value: {
        kind: 'pywebview',
        platform: 'windows',
        capabilities: ['terminal_external'],
      },
    },
    ...extra,
  });

it('says Row-Bot never sees the sign-in and that Claude Code runs without telemetry, in every state', () => {
  for (const state of [
    'not_installed',
    'signed_out',
    'too_old',
    'ready',
    'error',
  ] as const) {
    const view = show(plan({ state }));
    expect(
      screen.getByText(
        'Row-Bot never sees your Claude sign-in. It starts Claude Code with its non-essential traffic off: no telemetry, error reports or automatic updates from these runs.',
      ),
    ).toBeVisible();
    expect(view.onCheck).not.toHaveBeenCalled();
    cleanup();
  }
});

it('not installed: links to Anthropic’s install page and can check again', () => {
  const { onCheck } = show(plan({ state: 'not_installed', version: '' }));

  expect(screen.getByRole('status')).toHaveTextContent('Not set up');
  expect(
    screen.getByText('Install Claude Code on this computer, then come back.'),
  ).toBeVisible();
  expect(
    screen.getByRole('link', { name: 'Install Claude Code' }),
  ).toHaveAttribute('href', CLAUDE_CODE_INSTALL_URL);
  fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
  expect(onCheck).toHaveBeenCalledOnce();
});

it('not signed in: the one command to run, with Copy, and Check again as the next step', async () => {
  const writeClipboard = vi.fn(async () => ({
    status: 'ok' as const,
    value: null,
  }));
  show(plan(), {
    platform: { ...createFakePlatform(), writeClipboard },
  });

  expect(screen.getByRole('status')).toHaveTextContent('Not signed in');
  expect(
    screen.getByText(
      'Open a terminal and run this, then sign in with your Claude plan:',
    ),
  ).toBeVisible();
  expect(screen.getByText('claude auth login')).toBeVisible();
  expect(screen.getByRole('button', { name: 'Check again' })).toHaveClass(
    'primary',
  );
  fireEvent.click(screen.getByRole('button', { name: 'Copy command' }));
  await waitFor(() =>
    expect(writeClipboard).toHaveBeenCalledWith('claude auth login'),
  );
  expect(
    await screen.findByRole('button', { name: 'Copied' }),
  ).toBeInTheDocument();
  // No token field or device code: signing in happens in Claude Code.
  expect(screen.queryByRole('textbox')).toBeNull();
  expect(document.querySelector('input')).toBeNull();
});

it('signed in another way: says why it is not the plan', () => {
  show(
    plan({
      detail:
        'Claude Code is signed in without a Claude plan, so it would bill another way.',
    }),
  );

  expect(
    screen.getByText(
      'Claude Code is signed in without a Claude plan, so it would bill another way. Open a terminal and run this, then sign in with your Claude plan:',
    ),
  ).toBeVisible();
});

it('too old: says which version it needs and the update command', () => {
  show(plan({ state: 'too_old', version: '2.1.100' }));

  expect(screen.getByRole('status')).toHaveTextContent('Update needed');
  expect(
    screen.getByText(
      'Claude Code 2.1.100 is too old for Row-Bot; it needs 2.1.263 or newer. Open a terminal and run:',
    ),
  ).toBeVisible();
  expect(screen.getByText('claude update')).toBeVisible();
});

it('could not check: what went wrong and how to find out more', () => {
  show(
    plan({
      state: 'error',
      detail: 'Claude Code didn’t say whether it is signed in.',
    }),
  );

  expect(screen.getByRole('status')).toHaveTextContent('Couldn’t check');
  expect(
    screen.getByText(
      'Claude Code didn’t say whether it is signed in. To see what’s wrong, open a terminal and run:',
    ),
  ).toBeVisible();
  expect(screen.getByText('claude doctor')).toBeVisible();
});

it('ready: the plan and Claude Code’s version, and Done', () => {
  const { onDone } = show(
    plan({ state: 'ready', plan: 'Claude Max', version: '2.1.282' }),
  );

  expect(screen.getByRole('status')).toHaveTextContent(
    'Ready Claude Max · Claude Code 2.1.282',
  );
  expect(
    screen.getByText('Claude models are in the model picker.'),
  ).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Check again' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Done' }));
  expect(onDone).toHaveBeenCalledOnce();
});

it('a profile the old Claude sign-in had set up is told once to set it up again', () => {
  show(plan({ set_up_again: true }));

  expect(
    screen.getByText('Claude now works through Claude Code. Set it up again.'),
  ).toBeVisible();
});

it('checking: Check again waits, and the page says it is busy', () => {
  show(plan(), { checking: true });

  expect(screen.getByRole('button', { name: 'Checking…' })).toBeDisabled();
});

it('before Claude Code answers: a quiet loading state', async () => {
  show(null);

  expect(await screen.findByText('Checking Claude Code')).toBeInTheDocument();
});

it('opens the person’s own terminal only in the desktop app', async () => {
  const openExternalTerminal = vi.fn(async () => ({
    status: 'ok' as const,
    value: null,
  }));
  show(plan(), { platform: { ...desktop(), openExternalTerminal } });

  fireEvent.click(
    await screen.findByRole('button', { name: 'Open a terminal' }),
  );
  expect(openExternalTerminal).toHaveBeenCalledWith(null);
  cleanup();

  show(plan());
  await waitFor(() =>
    expect(screen.getByText('claude auth login')).toBeVisible(),
  );
  expect(screen.queryByRole('button', { name: 'Open a terminal' })).toBeNull();
});

it('says so when the terminal app could not be opened', async () => {
  show(plan(), {
    platform: desktop({
      openExternalTerminal: { status: 'unavailable', reason: 'no_terminal' },
    }),
  });

  fireEvent.click(
    await screen.findByRole('button', { name: 'Open a terminal' }),
  );
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Row-Bot couldn’t open your terminal app. Open it yourself.',
  );
});
