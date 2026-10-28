import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import type {
  AttentionSnapshot,
  ConversationView,
  PendingApproval,
  PendingApprovalPage,
} from '../../api/types';
import { OverlayProvider } from '../../ui/overlays';
import AttentionIndicator, { remindLaterAbout } from './AttentionIndicator';

const approval = vi.fn();
const intent = vi.fn();
vi.mock('../../runtime', () => ({
  useRuntime: () => ({ controller: { approval, intent } }),
}));

function Where() {
  const location = useLocation();
  return (
    <output aria-label="Location">{location.pathname + location.search}</output>
  );
}

function show(
  snapshot: AttentionSnapshot,
  conversations: ConversationView[] = [],
) {
  const load = vi.fn(async () => snapshot);
  render(
    <MemoryRouter initialEntries={['/c/synthetic']}>
      <AttentionIndicator load={load} conversations={conversations} />
      <Routes>
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
  return load;
}

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

it('stays quiet while everything is healthy', async () => {
  const load = show({ schema_version: 1, problems: [], update: null });
  await act(async () => undefined);
  expect(load).toHaveBeenCalledOnce();
  expect(screen.queryByRole('link')).toBeNull();
});

it('counts problems and conversations waiting on you, and opens Home where each has its fix', async () => {
  show(
    {
      schema_version: 1,
      problems: [
        {
          id: 'channel:telegram',
          title: 'Telegram stopped',
          detail: 'It is set to start with Row-Bot but isn’t running.',
          place: 'channels',
        },
        {
          id: 'plugin:rss-reader',
          title: 'The plugin rss-reader didn’t load',
          detail: 'Open it in Settings › Plugins.',
          place: 'plugins',
        },
      ],
      // A problem comes first; the update waits.
      update: { version: '9.1.0' },
    },
    [
      {
        id: 'chat-goal',
        revision: 'r',
        title: 'Autumn launch',
        pinned: false,
        activity_state: 'attention',
        activity_phase: 'goal_needs_you',
      },
      { id: 'chat-idle', revision: 'r', title: 'Notes', pinned: false },
    ],
  );
  const link = await screen.findByRole('link', {
    name: '3 things need you. Open Home',
  });
  fireEvent.click(link);
  expect(screen.getByLabelText('Location')).toHaveTextContent(/^\/$/);
});

it('offers an update, opens Updates, and "Remind me later" hides it for a day', async () => {
  show({ schema_version: 1, problems: [], update: { version: '9.1.0' } });
  const link = await screen.findByRole('link', {
    name: 'Update to 9.1.0 available. Open Updates',
  });
  fireEvent.click(link);
  expect(screen.getByLabelText('Location')).toHaveTextContent(
    '/settings/updates',
  );
  act(() => remindLaterAbout('9.1.0'));
  expect(screen.queryByRole('link')).toBeNull();
  // A reminder about another version doesn't hide this one.
  act(() => remindLaterAbout('9.0.9'));
  expect(
    screen.getByRole('link', {
      name: 'Update to 9.1.0 available. Open Updates',
    }),
  ).toBeInTheDocument();
});

function waiting(id: string, title: string): PendingApproval {
  return {
    id,
    source: 'workflow',
    title,
    what: `${title}: continue?`,
    requested_at: '2026-09-30T09:00:00',
    expires_at: null,
    conversation_id: null,
    task_id: `task-${id}`,
  };
}

it('counts waiting approvals and opens each on its card (B255)', async () => {
  const pages: PendingApprovalPage[] = [
    {
      schema_version: 1,
      items: [
        waiting('news', 'Daily News'),
        waiting('report', 'Weekly report'),
      ],
      total: 2,
    },
    {
      schema_version: 1,
      items: [waiting('report', 'Weekly report')],
      total: 1,
    },
  ];
  const loadApprovals = vi.fn(async () => pages[0]);
  approval.mockResolvedValue({ id: 'news', revision: '0', nonce: 'nonce' });
  intent.mockResolvedValue({ status: 'completed' });
  render(
    <MemoryRouter>
      <OverlayProvider>
        <AttentionIndicator
          load={async () => ({
            schema_version: 1,
            problems: [
              {
                id: 'channel:telegram',
                title: 'Telegram stopped',
                detail: 'It isn’t running.',
                place: 'channels',
              },
            ],
            update: null,
          })}
          loadApprovals={loadApprovals}
        />
      </OverlayProvider>
    </MemoryRouter>,
  );
  fireEvent.click(
    await screen.findByRole('button', {
      name: '3 things need you. Review',
    }),
  );
  const list = screen.getByRole('dialog', {
    name: 'Waiting for your approval',
  });
  // The rest stay one step away, on Home.
  expect(
    within(list).getByRole('link', { name: /1 more needs you/ }),
  ).toHaveAttribute('href', '/');
  const news = within(list).getByRole('listitem', {
    name: 'Daily News needs your approval',
  });
  // Each opens on its own card: what exactly will happen, then Approve or Deny there (F31).
  expect(within(news).queryByRole('button', { name: 'Approve' })).toBeNull();
  await act(async () =>
    fireEvent.click(
      within(news).getByRole('button', { name: 'Review: Daily News' }),
    ),
  );
  expect(
    screen.getByRole('dialog', { name: 'Review and approve' }),
  ).toBeInTheDocument();
  expect(intent).not.toHaveBeenCalled();
});

it('says "Restart to update" once an update is downloaded and ready', async () => {
  show({
    schema_version: 1,
    problems: [],
    update: { version: '9.1.0', ready: true },
  });
  const link = await screen.findByRole('link', {
    name: 'Restart to update to 9.1.0. Open Updates',
  });
  expect(link).toHaveTextContent('Restart to update');
  fireEvent.click(link);
  expect(screen.getByLabelText('Location')).toHaveTextContent(
    '/settings/updates',
  );
});

it('keeps "Restart to update" beside a problem, each opening its own page (B364)', async () => {
  show({
    schema_version: 1,
    problems: [
      {
        id: 'mcp',
        title: "An app isn't connected",
        detail: '1 of 2 apps that are on are connected.',
        place: 'mcp',
      },
    ],
    update: { version: '9.1.0', ready: true },
  });
  const problem = await screen.findByRole('link', {
    name: '1 thing needs you. Open Home',
  });
  const update = screen.getByRole('link', {
    name: 'Restart to update to 9.1.0. Open Updates',
  });
  fireEvent.click(update);
  expect(screen.getByLabelText('Location')).toHaveTextContent(
    '/settings/updates',
  );
  fireEvent.click(problem);
  expect(screen.getByLabelText('Location')).toHaveTextContent(/^\/$/);
});

it('says only approvals are waiting when nothing else needs you', async () => {
  render(
    <MemoryRouter>
      <AttentionIndicator
        load={async () => ({ schema_version: 1, problems: [], update: null })}
        loadApprovals={async () => ({
          schema_version: 1,
          items: [waiting('news', 'Daily News')],
          total: 1,
        })}
      />
    </MemoryRouter>,
  );
  expect(
    await screen.findByRole('button', {
      name: '1 approval is waiting. Review',
    }),
  ).toBeVisible();
});

it('says Row-Bot has a question and counts it as one thing that needs you (N4)', async () => {
  render(
    <OverlayProvider>
      <MemoryRouter>
        <AttentionIndicator
          load={async () => ({ schema_version: 1, problems: [], update: null })}
          loadApprovals={async () => ({
            schema_version: 1,
            items: [{ ...waiting('beta', 'Plan the beta'), kind: 'question' }],
            total: 1,
          })}
        />
      </MemoryRouter>
    </OverlayProvider>,
  );
  const indicator = await screen.findByRole('button', {
    name: 'Row-Bot has a question. Review',
  });
  fireEvent.click(indicator);
  const list = await screen.findByRole('list', {
    name: 'Waiting for you',
  });
  // Answered on its card, never with Approve or Deny.
  expect(
    within(list).getByRole('listitem', {
      name: 'Plan the beta has a question',
    }),
  ).toBeVisible();
  expect(
    within(list).getByRole('button', { name: 'Answer: Plan the beta' }),
  ).toBeVisible();
  expect(within(list).queryByRole('button', { name: 'Approve' })).toBeNull();
});
