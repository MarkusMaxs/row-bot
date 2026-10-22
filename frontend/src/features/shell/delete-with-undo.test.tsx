import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import {
  ACTION_NOTICE_MS,
  OverlayProvider,
  useOverlay,
} from '../../ui/overlays';
import { deleteWithUndo, useDeletingConversations } from './delete-with-undo';

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

function owner(
  command: (target: string) => Promise<unknown> = async () => ({
    status: 'DeleteCompleted',
  }),
) {
  return {
    getSnapshot: () =>
      ({ handshake: { client_session_id: 'session-a' } }) as never,
    command: vi.fn(command) as never as ReturnType<typeof vi.fn>,
    forgetConversation: vi.fn(),
    loadMoreConversations: vi.fn(async () => {}),
  };
}

const TIDES = { id: 'conversation-a', title: 'Tides', revision: '3' };
const KELP = { id: 'conversation-b', title: 'Kelp', revision: '5' };

function Harness({ controller }: { controller: ReturnType<typeof owner> }) {
  const { notify } = useOverlay();
  const deleting = useDeletingConversations();
  return (
    <>
      <output aria-label="Leaving the lists">{[...deleting].join(' ')}</output>
      {[TIDES, KELP].map((row) => (
        <button
          key={row.id}
          onClick={() => deleteWithUndo(controller as never, notify, row)}
        >
          {`Delete ${row.title}`}
        </button>
      ))}
    </>
  );
}

function setup(controller = owner()) {
  render(
    <OverlayProvider>
      <Harness controller={controller} />
    </OverlayProvider>,
  );
  return controller;
}

const leaving = () => screen.getByRole('status', { name: 'Leaving the lists' });
const notice = (text: string) =>
  within(screen.getByText(text).closest<HTMLElement>('.toast')!);
/** Let the sent delete's promise chain finish. */
const settle = () =>
  act(async () => {
    for (let tick = 0; tick < 10; tick += 1) await Promise.resolve();
  });

type Sent = {
  type: string;
  expected_revision: string;
  payload: unknown;
};
const sent = (controller: ReturnType<typeof owner>) =>
  (controller.command.mock.calls as [string, Sent][]).map(
    ([target, command]) => [
      target,
      command.type,
      command.expected_revision,
      command.payload,
    ],
  );

it('deletes a confirmed conversation at once, so a page closed during Undo cannot bring it back (B343)', async () => {
  const controller = setup();
  fireEvent.click(screen.getByRole('button', { name: 'Delete Tides' }));
  expect(leaving()).toHaveTextContent('conversation-a');
  expect(controller.forgetConversation).toHaveBeenCalledWith('conversation-a');
  await settle();
  // Sent before the notice ends: its own id and confirmed revision, held for Undo.
  expect(sent(controller)).toEqual([
    ['conversation-a', 'conversation.delete', '3', { undo: true }],
  ]);
  expect(
    notice("Deleted 'Tides'.").getByRole('button', { name: 'Undo' }),
  ).toBeVisible();
  await act(async () => {
    vi.advanceTimersByTime(ACTION_NOTICE_MS + 100);
  });
  await settle();
  expect(sent(controller)).toHaveLength(1);
  expect(leaving()).toHaveTextContent('');
  expect(controller.loadMoreConversations).toHaveBeenCalledWith(true);
  expect(screen.queryByText("Deleted 'Tides'.")).toBeNull();
});

it('Undo asks the server to restore the conversation and shows it again', async () => {
  const controller = setup();
  fireEvent.click(screen.getByRole('button', { name: 'Delete Tides' }));
  await settle();
  await act(async () =>
    fireEvent.click(
      notice("Deleted 'Tides'.").getByRole('button', { name: 'Undo' }),
    ),
  );
  await settle();
  expect(sent(controller)).toEqual([
    ['conversation-a', 'conversation.delete', '3', { undo: true }],
    ['conversation-a', 'conversation.restore', '3', {}],
  ]);
  expect(leaving()).toHaveTextContent('');
  expect(controller.loadMoreConversations).toHaveBeenCalledWith(true);
});

it('says so when Undo comes too late', async () => {
  const controller = setup(
    owner(async () => {
      if (controller.command.mock.calls.length > 1)
        throw { code: 'restore_expired' };
      return { status: 'DeleteCompleted' };
    }),
  );
  fireEvent.click(screen.getByRole('button', { name: 'Delete Tides' }));
  await settle();
  await act(async () =>
    fireEvent.click(
      notice("Deleted 'Tides'.").getByRole('button', { name: 'Undo' }),
    ),
  );
  await settle();
  expect(
    screen.getByText("It's too late to undo that delete."),
  ).toBeInTheDocument();
  expect(leaving()).toHaveTextContent('');
});

it('sends a second delete while the first offers Undo, each on its own', async () => {
  const controller = setup();
  fireEvent.click(screen.getByRole('button', { name: 'Delete Tides' }));
  fireEvent.click(screen.getByRole('button', { name: 'Delete Kelp' }));
  expect(leaving()).toHaveTextContent('conversation-a conversation-b');
  await settle();
  expect(sent(controller)).toEqual([
    ['conversation-a', 'conversation.delete', '3', { undo: true }],
    ['conversation-b', 'conversation.delete', '5', { undo: true }],
  ]);
  // Dismissing Kelp's notice ends only Kelp's Undo.
  await act(async () =>
    fireEvent.click(
      notice("Deleted 'Kelp'.").getByRole('button', {
        name: 'Dismiss notification',
      }),
    ),
  );
  expect(leaving()).toHaveTextContent('conversation-a');
  await act(async () => {
    vi.advanceTimersByTime(ACTION_NOTICE_MS + 100);
  });
  expect(leaving()).toHaveTextContent('');
  expect(sent(controller)).toHaveLength(2);
});

it('shows the conversation again and says why when the delete is refused', async () => {
  const controller = setup(
    owner(async () => {
      throw { code: 'revision_conflict' };
    }),
  );
  fireEvent.click(screen.getByRole('button', { name: 'Delete Tides' }));
  await settle();
  expect(controller.command).toHaveBeenCalledTimes(1);
  expect(leaving()).toHaveTextContent('');
  expect(controller.loadMoreConversations).toHaveBeenCalledWith(true);
  expect(screen.queryByText("Deleted 'Tides'.")).toBeNull();
  expect(
    screen.getByText(
      'This changed somewhere else. Load it again, then make your change.',
    ),
  ).toBeInTheDocument();
});
