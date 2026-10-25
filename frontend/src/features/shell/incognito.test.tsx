import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { ClientController } from '../../api/controller';
import { FixtureTransport } from '../../api/fixtures';
import { createFakePlatform } from '../../platform/fake';
import { RuntimeContext } from '../../runtime';
import { OverlayProvider } from '../../ui/overlays';
import ConversationHeader from './ConversationHeader';
import Navigation from './Navigation';

// Incognito chats (N11): New chat ▾ › New incognito chat, the ⋯ switch, and the icon where the chat shows.

const clients: ClientController[] = [];
afterEach(() => {
  clients.splice(0).forEach((controller) => controller.dispose());
  localStorage.removeItem('row-bot.sidebar-type.v1');
  vi.restoreAllMocks();
});

function CurrentRoute() {
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

async function setup(prepare?: (transport: FixtureTransport) => void) {
  const transport = new FixtureTransport({ conversationCount: 3 });
  prepare?.(transport);
  const controller = new ClientController(transport, () => 1);
  clients.push(controller);
  await controller.start();
  const onNewChat = vi.fn();
  const onNewIncognitoChat = vi.fn();
  render(
    <MemoryRouter>
      <CurrentRoute />
      <RuntimeContext.Provider
        value={{ controller, platform: createFakePlatform() }}
      >
        <OverlayProvider>
          <Navigation
            showBuddy={false}
            onNewChat={onNewChat}
            onNewIncognitoChat={onNewIncognitoChat}
          />
        </OverlayProvider>
      </RuntimeContext.Provider>
    </MemoryRouter>,
  );
  const nav = screen.getByRole('navigation', { name: 'Workspace navigation' });
  return { transport, nav, onNewChat, onNewIncognitoChat };
}

it('starts an incognito chat from New chat ▾, saying nothing in it is saved to memory', async () => {
  const user = userEvent.setup();
  const { nav, onNewChat, onNewIncognitoChat } = await setup();

  await user.click(
    within(nav).getByRole('button', { name: 'New chat options' }),
  );
  const item = await screen.findByRole('menuitem', {
    name: /^New incognito chat/,
  });
  expect(item).toHaveTextContent('Nothing in it is saved to memory');
  await user.click(item);

  expect(onNewIncognitoChat).toHaveBeenCalledOnce();
  expect(onNewChat).not.toHaveBeenCalled();
});

it('marks an incognito chat in the sidebar and switches it in its ⋯ menu', async () => {
  const user = userEvent.setup();
  const { transport, nav } = await setup((fixture) => {
    fixture.conversations[1].incognito = true;
  });
  const title = transport.conversations[1].title;

  const row = await within(nav).findByRole('button', {
    name: `${title} (incognito)`,
  });
  expect(
    row.querySelector('.conversation-glyph[title="Incognito"]'),
  ).not.toBeNull();

  await user.click(
    within(nav).getByRole('button', { name: `Actions for ${title}` }),
  );
  const on = await screen.findByRole('menuitem', { name: /^Incognito/ });
  expect(on).toHaveAttribute('aria-current', 'true');
  expect(on).toHaveTextContent('On: nothing here is saved to memory');
  await user.click(on);

  await waitFor(() => expect(transport.conversations[1].incognito).toBe(false));
  expect(
    await screen.findAllByText(
      'Incognito is off: memory reads this chat from now.',
    ),
  ).not.toHaveLength(0);
  expect(await within(nav).findByRole('button', { name: title })).toBeVisible();
});

it('turning incognito on for an existing chat says what stays and links to Knowledge', async () => {
  const user = userEvent.setup();
  const { transport, nav } = await setup();
  const title = transport.conversations[0].title;

  await user.click(
    within(nav).getByRole('button', { name: `Actions for ${title}` }),
  );
  const off = await screen.findByRole('menuitem', { name: /^Incognito/ });
  expect(off).not.toHaveAttribute('aria-current');
  expect(off).toHaveTextContent('Keep this chat out of memory');
  await user.click(off);

  await waitFor(() => expect(transport.conversations[0].incognito).toBe(true));
  const [message] = await screen.findAllByText(
    'Incognito is on: nothing from now is saved to memory. Memories already saved from this chat stay.',
  );
  const toast = within(message.closest<HTMLElement>('.toast')!);
  // A short action, its full name for screen readers and its tooltip.
  const review = toast.getByRole('button', { name: 'Review in Knowledge' });
  expect(review).toHaveTextContent(/^Review$/);
  // Radix toasts handle pointer capture, which jsdom lacks: a plain click.
  await act(async () => review.click());
  expect(screen.getByLabelText('Current route')).toHaveTextContent(
    '/settings/knowledge',
  );
});

it('shows the header’s incognito mark with its words, only in an incognito chat', async () => {
  const header = (incognito: boolean) => (
    <ConversationHeader
      title="Health questions"
      canRename={false}
      onRename={async () => undefined}
      incognito={incognito}
    />
  );
  const view = render(header(false));
  expect(screen.queryByRole('img', { name: /Incognito/ })).toBeNull();

  view.rerender(header(true));
  const mark = screen.getByRole('img', {
    name: 'Incognito: nothing here is saved to memory',
  });
  await userEvent.setup().tab();
  expect(mark).toHaveFocus();
  expect(
    (await screen.findAllByText('Incognito: nothing here is saved to memory'))
      .length,
  ).toBeGreaterThan(0);
});
