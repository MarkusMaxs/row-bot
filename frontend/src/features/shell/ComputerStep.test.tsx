import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import type {
  ComputerUsePreview,
  ComputerUseReceipt,
  ComputerUseSnapshot,
} from '../../api/types';
import { COMPUTER_POLL_MS, ComputerStep, useComputerUse } from './ComputerStep';

const A = 'a'.repeat(64);
const B = 'b'.repeat(64);

vi.mock('../../runtime', () => ({
  useRuntime: () => ({ controller: {} }),
  useClientSelector: () => false,
}));

function snapshot(
  patch: Partial<ComputerUseSnapshot> = {},
): ComputerUseSnapshot {
  return {
    schema_version: 1,
    conversation_id: 'conversation-a',
    revision: A,
    active: true,
    state: 'working',
    app: 'Microsoft Edge',
    window: 'HomeServe – Make a claim',
    doing: 'Clicked "Leak"',
    has_picture: true,
    problem: null,
    can_stop: true,
    ...patch,
  };
}

function preview(revision = A): ComputerUsePreview {
  return {
    schema_version: 1,
    conversation_id: 'conversation-a',
    revision,
    state: 'available',
    mime_type: 'image/jpeg',
    image_base64: `cGljdHVyZS0${revision[0]}`,
  };
}

function receipt(patch: Partial<ComputerUseReceipt> = {}): ComputerUseReceipt {
  return {
    schema_version: 1,
    command_id: crypto.randomUUID(),
    action: 'computer_use.stop',
    conversation_id: 'conversation-a',
    status: 'completed',
    code: null,
    computer_use: snapshot({ active: false, state: 'stopped' }),
    ...patch,
  };
}

function step(
  value: ComputerUseSnapshot | null,
  overrides: Partial<Parameters<typeof ComputerStep>[0]> = {},
) {
  const loadPreview = vi.fn(async (_c: string, revision: string) =>
    preview(revision),
  );
  const props = {
    conversationId: 'conversation-a',
    snapshot: value,
    stopped: false,
    loadPreview,
    send: vi.fn(async () => receipt()),
    onChange: vi.fn(),
    onStopped: vi.fn(),
    ...overrides,
  };
  return { props, loadPreview, ...render(<ComputerStep {...props} />) };
}

afterEach(() => vi.useRealTimers());

it('shows the app, its window, what Row-Bot is doing and the live picture', async () => {
  const { props, rerender } = step(snapshot());

  const region = screen.getByRole('region', { name: 'Using Microsoft Edge' });
  expect(region).toHaveTextContent('Using Microsoft Edge');
  expect(region).toHaveTextContent('HomeServe – Make a claim');
  expect(within(region).getByRole('status')).toHaveTextContent(
    'Clicked "Leak"',
  );
  expect(region).toHaveTextContent(
    'Press Esc on the computer to stop it at once.',
  );
  const picture = await screen.findByRole('img', {
    name: 'The Microsoft Edge window Row-Bot is using',
  });
  expect(picture).toHaveAttribute('src', 'data:image/jpeg;base64,cGljdHVyZS0a');

  // A new revision is a new picture.
  rerender(<ComputerStep {...props} snapshot={snapshot({ revision: B })} />);
  await screen.findByRole('img', { name: /Microsoft Edge window/ });
  expect(props.loadPreview).toHaveBeenLastCalledWith(
    'conversation-a',
    B,
    expect.any(AbortSignal),
  );
});

it('says when it waits for an OK, and hides the picture while the person has the computer', async () => {
  const { props, loadPreview, rerender } = step(
    snapshot({ state: 'waiting_approval', doing: '' }),
  );
  expect(screen.getByRole('status')).toHaveTextContent('Waiting for your OK');

  loadPreview.mockClear();
  rerender(
    <ComputerStep
      {...props}
      snapshot={snapshot({ state: 'paused', has_picture: false })}
    />,
  );
  expect(screen.getByRole('status')).toHaveTextContent(
    'Paused while you use it',
  );
  expect(screen.queryByRole('img')).toBeNull();
  expect(loadPreview).not.toHaveBeenCalled();
});

it('stops at once from the step and says so', async () => {
  const { props, rerender } = step(snapshot());
  await act(async () =>
    fireEvent.click(
      screen.getByRole('button', { name: 'Stop using the computer' }),
    ),
  );
  expect(props.send).toHaveBeenCalledWith(
    'conversation-a',
    'computer_use.stop',
  );
  expect(props.onStopped).toHaveBeenCalled();
  expect(props.onChange).toHaveBeenCalledWith(
    expect.objectContaining({ state: 'stopped' }),
  );

  rerender(
    <ComputerStep
      {...props}
      snapshot={snapshot({ active: false, state: 'stopped' })}
      stopped
    />,
  );
  expect(screen.getByRole('status')).toHaveTextContent(
    'Row-Bot no longer controls your computer.',
  );
});

it('says when Stop could not be confirmed', async () => {
  step(snapshot(), {
    send: vi.fn(async () =>
      receipt({ status: 'partial', code: 'computer_use_outcome_uncertain' }),
    ),
  });
  await act(async () =>
    fireEvent.click(
      screen.getByRole('button', { name: 'Stop using the computer' }),
    ),
  );
  expect(screen.getByRole('alert')).toHaveTextContent(
    "Row-Bot couldn't confirm it stopped.",
  );
});

it('says what keeps computer use from starting and opens its settings', () => {
  step(
    snapshot({
      active: false,
      state: 'stopped',
      has_picture: false,
      problem: {
        message: "Computer use isn't set up yet: Cua Driver isn't installed.",
        fix: 'Open Settings › System › Browser and computer use › Set up… › Install.',
      },
    }),
  );
  const alert = screen.getByRole('alert');
  expect(alert).toHaveTextContent(
    "Computer use isn't set up yet: Cua Driver isn't installed.",
  );
  expect(alert).toHaveTextContent('Set up… › Install.');
  expect(
    within(alert).getByRole('link', { name: 'Open computer use settings' }),
  ).toHaveAttribute('href', '/app-v2/settings/system#browser-computer-use');
});

function Follower(props: Parameters<typeof useComputerUse>[0]) {
  const followed = useComputerUse(props);
  return (
    <output>{followed.visible ? followed.snapshot?.state : 'hidden'}</output>
  );
}

it('follows a session about once a second while it runs, and a problem stays visible', async () => {
  vi.useFakeTimers();
  const load = vi
    .fn()
    .mockResolvedValueOnce(snapshot())
    .mockResolvedValueOnce(snapshot({ revision: B }))
    .mockResolvedValue(
      snapshot({
        revision: 'c'.repeat(64),
        active: false,
        state: 'stopped',
        problem: { message: 'Cua Driver stopped.', fix: 'Try again.' },
      }),
    );
  function Host() {
    const [watch] = useState(true);
    return (
      <Follower
        conversationId="conversation-a"
        watch={watch}
        generationId="run-a"
        turnKey="run-a:running"
        load={load}
      />
    );
  }
  render(<Host />);
  await act(async () => vi.advanceTimersByTimeAsync(0));
  expect(screen.getByRole('status')).toHaveTextContent('working');
  await act(async () => vi.advanceTimersByTimeAsync(COMPUTER_POLL_MS));
  await act(async () => vi.advanceTimersByTimeAsync(COMPUTER_POLL_MS));
  expect(load).toHaveBeenCalledTimes(3);
  expect(screen.getByRole('status')).toHaveTextContent('stopped');
});
