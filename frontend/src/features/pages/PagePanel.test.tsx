import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import type { ClientController } from '../../api/controller';
import type { PageDecisionView, PageView } from '../../api/types';
import type { ClientPlatform } from '../../platform';
import { RuntimeContext } from '../../runtime';
import { OverlayProvider } from '../../ui/overlays';
import { PageCard } from './PageCard';
import PagePanel from './PagePanel';
import { onPageRequest, type PageRequest } from './open-page';

const DECISIONS: PageDecisionView[] = [
  {
    id: 'announce',
    choice: 'approve',
    note: '',
    label: 'Draft the announcement',
    sent: false,
  },
  {
    id: 'venue',
    choice: 'change',
    note: 'Smaller rooms near the station',
    label: 'Book the venue',
    sent: false,
  },
];

function view(patch: Partial<PageView> = {}): PageView {
  return {
    page_id: 'page-1a2b3c4d',
    conversation_id: 'conversation-a',
    conversation_title: 'Beta launch',
    title: 'Launch plan',
    version: 1,
    libraries: ['rowbot-style'],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    state_revision: 0,
    pending: 2,
    decisions: DECISIONS,
    ...patch,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

function setup(
  shown: PageView = view(),
  { running = false }: { running?: boolean } = {},
) {
  const snapshot = {
    status: 'ready',
    conversation: { id: 'conversation-a', revision: '7', title: 'Beta launch' },
    projection: running
      ? { generation: { quiesced: false, can_stop: true, status: 'running' } }
      : undefined,
  };
  let current = shown;
  const controller = {
    subscribe: () => () => {},
    getSnapshot: () => snapshot,
    pageFrame: vi.fn(async () => ({
      frame_url: '/page-frames/' + 'a'.repeat(32),
      page: current,
    })),
    page: vi.fn(async () => current),
    savePageState: vi.fn(async () => current),
    savePageDecision: vi.fn(async () => current),
    controlsSettled: vi.fn(async () => undefined),
    workspaceFor: vi.fn(async () => ({
      revision: '7',
      controls: {
        model_selection: { provider_id: 'fixture', model_ref: 'fixture::m' },
      },
    })),
    intent: vi.fn(async () => {
      current = view({
        pending: 0,
        decisions: DECISIONS.map((item) => ({ ...item, sent: true })),
      });
      return { status: 'accepted' };
    }),
    showLatest: vi.fn(),
  } as unknown as ClientController & Record<string, ReturnType<typeof vi.fn>>;
  const platform = {} as ClientPlatform;
  const utils = render(
    <RuntimeContext.Provider value={{ controller, platform }}>
      <OverlayProvider>
        <PagePanel
          conversationId="conversation-a"
          pageId="page-1a2b3c4d"
          visible
        />
      </OverlayProvider>
    </RuntimeContext.Provider>,
  );
  return { controller, ...utils };
}

it('shows the page in a frame that can only run its own scripts', async () => {
  const { container } = setup();
  const region = await screen.findByRole('region', {
    name: 'Launch plan page',
  });
  const frame = await waitFor(() => {
    const found = container.querySelector('iframe');
    expect(found).not.toBeNull();
    return found!;
  });
  expect(region).toContainElement(frame);
  expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
  expect(frame.getAttribute('src')).toBe('/page-frames/' + 'a'.repeat(32));
  expect(frame).toHaveAttribute('title', 'Launch plan');
});

it('gathers decisions under the page and lists them in words', async () => {
  setup();
  const footer = await screen.findByRole('region', { name: 'Decisions' });
  const count = within(footer).getByRole('button', { name: '2 decisions' });
  expect(count).toHaveAttribute('aria-expanded', 'false');
  expect(
    within(footer).getByRole('button', { name: 'Send to Row-Bot' }),
  ).toBeEnabled();
  await userEvent.click(count);
  const list = within(footer).getByRole('list');
  expect(list).toHaveTextContent('Approved: Draft the announcement');
  expect(list).toHaveTextContent(
    'Change: Book the venue “Smaller rooms near the station”',
  );
});

it("sends the decisions only on the person's click, as one message from the page", async () => {
  const { controller } = setup();
  await userEvent.click(
    await screen.findByRole('button', { name: 'Send to Row-Bot' }),
  );
  await waitFor(() => expect(controller.intent).toHaveBeenCalledTimes(1));
  expect(controller.intent).toHaveBeenCalledWith(
    'conversation-a',
    'conversation.submit',
    expect.objectContaining({
      page_id: 'page-1a2b3c4d',
      attachment_refs: [],
      model_selection: { provider_id: 'fixture', model_ref: 'fixture::m' },
    }),
    '7',
  );
  expect(await screen.findByText('Sent to Row-Bot.')).toHaveAttribute(
    'role',
    'status',
  );
  expect(
    screen.queryByRole('button', { name: 'Send to Row-Bot' }),
  ).not.toBeInTheDocument();
});

it('waits for the reply before sending more', async () => {
  setup(view(), { running: true });
  expect(
    await screen.findByRole('button', { name: 'Send to Row-Bot' }),
  ).toBeDisabled();
  expect(screen.getByText('Send when Row-Bot has replied.')).toBeVisible();
});

it('has no footer while there is nothing to send', async () => {
  setup(view({ pending: 0, decisions: [] }));
  await screen.findByRole('region', { name: 'Launch plan page' });
  expect(
    screen.queryByRole('region', { name: 'Decisions' }),
  ).not.toBeInTheDocument();
});

it("the chat's card names the page and opens it beside the chat", async () => {
  const opened: PageRequest[] = [];
  const stop = onPageRequest((request) => {
    opened.push(request);
    return true;
  });
  const controller = {
    subscribe: () => () => {},
    getSnapshot: () => ({
      conversation: { id: 'conversation-a', revision: '7' },
    }),
    page: vi.fn(async () => view()),
  } as unknown as ClientController;
  render(
    <RuntimeContext.Provider
      value={{ controller, platform: {} as ClientPlatform }}
    >
      <PageCard pageId="page-1a2b3c4d" conversationId="conversation-a" />
    </RuntimeContext.Provider>,
  );
  const card = await screen.findByRole('group', { name: 'Launch plan · page' });
  expect(card).toHaveTextContent('2 decisions not sent yet');
  await userEvent.click(within(card).getByRole('button', { name: 'Open' }));
  expect(opened).toEqual([
    {
      conversationId: 'conversation-a',
      pageId: 'page-1a2b3c4d',
      title: 'Launch plan',
    },
  ]);
  stop();
});
