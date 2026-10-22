import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import type { ClientController } from '../../api/controller';
import type {
  BrowserFrame,
  BrowserInputEvent,
  BrowserLiveSize,
  BrowserPaneState,
} from '../../api/types';
import type { ClientPlatform } from '../../platform';
import { RuntimeContext } from '../../runtime';
import { OverlayProvider } from '../../ui/overlays';
import BrowserPane, { actingWords } from './BrowserPane';
import { BrowserStep } from './BrowserThumbnail';

const PAGE: BrowserPaneState = {
  schema_version: 1,
  conversation_id: 'conversation-a',
  status: 'running',
  control: 'agent',
  acting: '',
  waiting: '',
  url: 'https://www.homeserve.co.uk/claim/1',
  title: 'Make a claim',
  site: 'homeserve.co.uk',
  loading: false,
  sign_in: '',
  recording: false,
  tabs: [
    {
      id: 't1',
      title: 'Make a claim',
      url: 'https://www.homeserve.co.uk/claim/1',
      active: true,
    },
  ],
  revision: 3,
};
const FRAME: BrowserFrame = {
  seq: 1,
  css_width: 1280,
  css_height: 800,
  image_base64: '/9j/4AAQSkZJRg==',
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function setup(
  state: Partial<BrowserPaneState> | null,
  {
    frame = FRAME,
    running = false,
  }: { frame?: BrowserFrame | null; running?: boolean } = {},
) {
  const sent: BrowserInputEvent[][] = [];
  const snapshot = {
    status: 'ready',
    conversation: { id: 'conversation-a', revision: '7', title: 'Plumber' },
    projection: running
      ? { generation: { quiesced: false, can_stop: true, status: 'running' } }
      : undefined,
  };
  const controller = {
    subscribe: () => () => {},
    getSnapshot: () => snapshot,
    browserLive: vi.fn(async function* (
      _conversation: string,
      _view: string,
      _size: BrowserLiveSize,
      signal: AbortSignal,
    ) {
      if (state) yield { state: { ...PAGE, ...state } };
      if (frame) yield { frame };
      await new Promise((resolve) => signal.addEventListener('abort', resolve));
    }),
    browserAction: vi.fn(async () => ({ ...PAGE, ...state })),
    browserInput: vi.fn(async (_c: string, events: BrowserInputEvent[]) => {
      sent.push(events);
      return { status: 'ok', control: 'person' };
    }),
    intent: vi.fn(async () => ({})),
    browserLocalAddresses: vi.fn(),
    removeBrowserLocalAddress: vi.fn(),
  } as unknown as ClientController & {
    browserAction: ReturnType<typeof vi.fn>;
    browserInput: ReturnType<typeof vi.fn>;
    intent: ReturnType<typeof vi.fn>;
    browserLive: ReturnType<typeof vi.fn>;
  };
  const platform = {
    managedWindow: vi.fn(async () => ({ status: 'ok' })),
  } as unknown as ClientPlatform & { managedWindow: ReturnType<typeof vi.fn> };
  render(
    <RuntimeContext.Provider value={{ controller, platform }}>
      <OverlayProvider>
        <BrowserPane conversationId="conversation-a" />
      </OverlayProvider>
    </RuntimeContext.Provider>,
  );
  return { controller, platform, sent };
}

it('shows the live page with the address, and plain words for what Row-Bot is doing', async () => {
  const { controller } = setup(
    { acting: 'Clicking button "Next"' },
    { running: true },
  );
  expect(
    await screen.findByRole('img', { name: 'Live page: Make a claim' }),
  ).toBeVisible();
  expect(screen.getByRole('textbox', { name: 'Address' })).toHaveValue(
    'https://www.homeserve.co.uk/claim/1',
  );
  expect(await screen.findByText('Row-Bot: Clicking "Next"')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: 'Stop' }));
  expect(controller.intent).toHaveBeenCalledWith(
    'conversation-a',
    'conversation.stop',
    {},
    '7',
  );
  await userEvent.click(screen.getByRole('button', { name: 'Take control' }));
  expect(controller.browserAction).toHaveBeenCalledWith('conversation-a', {
    action: 'take_control',
  });
});

it('gives the page the pane’s size, and the new size once a resize settles', async () => {
  let resized: ResizeObserverCallback = () => {};
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(callback: ResizeObserverCallback) {
        resized = callback;
      }
      observe() {}
      disconnect() {}
    },
  );
  let area = { width: 420, height: 700 };
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(
    function (this: HTMLElement) {
      return (
        this.classList.contains('browser-viewport')
          ? { ...area, top: 0, left: 0, right: area.width, bottom: area.height }
          : { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 }
      ) as DOMRect;
    },
  );
  vi.stubGlobal('devicePixelRatio', 2);
  const { controller } = setup({});
  await screen.findByRole('img', { name: 'Live page: Make a claim' });
  // Measured before it first connects: one connection, at the pane's size.
  expect(controller.browserLive).toHaveBeenCalledTimes(1);
  expect(controller.browserLive.mock.calls[0][2]).toEqual({
    width: 420,
    height: 700,
    scale: 2,
  });
  // Docked → phone (or a drag): the page follows once the size settles.
  area = { width: 390, height: 780 };
  act(() => resized([], {} as ResizeObserver));
  await waitFor(() =>
    expect(controller.browserLive).toHaveBeenLastCalledWith(
      'conversation-a',
      'pane',
      { width: 390, height: 780, scale: 2 },
      expect.any(AbortSignal),
    ),
  );
  expect(controller.browserLive).toHaveBeenCalledTimes(2);
});

it('says plainly that the person signs in, with one primary action', async () => {
  const { controller } = setup({ sign_in: 'password' });
  expect(await screen.findByText('Sign in here')).toBeVisible();
  expect(screen.getByText(/Row-Bot doesn't type passwords/)).toBeVisible();
  // One primary action, and no second Take control beside it.
  expect(screen.queryByRole('button', { name: 'Take control' })).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
  expect(controller.browserAction).toHaveBeenCalledWith('conversation-a', {
    action: 'take_control',
  });
});

it('while the person has control, Give back hands the page to Row-Bot', async () => {
  const { controller } = setup({ control: 'person' });
  expect(await screen.findByText('You have control.')).toBeVisible();
  expect(
    screen.getByText(/Row-Bot waits until you give it back/),
  ).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: 'Give back' }));
  expect(controller.browserAction).toHaveBeenCalledWith('conversation-a', {
    action: 'give_back',
  });
});

it('a crash says so and restarts on one click', async () => {
  const { controller } = setup(
    { status: 'crashed', tabs: [] },
    { frame: null },
  );
  expect(await screen.findByRole('alert')).toHaveTextContent(
    "Row-Bot's browser stopped. Restart it?",
  );
  await userEvent.click(screen.getByRole('button', { name: 'Restart' }));
  expect(controller.browserAction).toHaveBeenCalledWith('conversation-a', {
    action: 'restart',
  });
});

it('with nothing open it says what will show here and the address bar opens a page', async () => {
  const { controller } = setup(
    { status: 'stopped', tabs: [], url: '', title: '', site: '' },
    { frame: null },
  );
  expect(await screen.findByText('Nothing open yet')).toBeVisible();
  const address = screen.getByRole('textbox', { name: 'Address' });
  await userEvent.type(address, 'homeserve.co.uk{Enter}');
  expect(controller.browserAction).toHaveBeenCalledWith('conversation-a', {
    action: 'navigate',
    url: 'homeserve.co.uk',
  });
});

it('the person clicks and types into the page in its own CSS pixels', async () => {
  const { sent } = setup({});
  const picture = await screen.findByRole('img', {
    name: 'Live page: Make a claim',
  });
  vi.spyOn(picture, 'getBoundingClientRect').mockReturnValue({
    left: 100,
    top: 50,
    width: 640,
    height: 400,
    right: 740,
    bottom: 450,
    x: 100,
    y: 50,
    toJSON: () => ({}),
  });
  const viewport = picture.parentElement!;
  fireEvent.pointerDown(viewport, {
    clientX: 420,
    clientY: 250,
    button: 0,
    pointerType: 'mouse',
    detail: 1,
  });
  fireEvent.pointerUp(viewport, {
    clientX: 420,
    clientY: 250,
    button: 0,
    pointerType: 'mouse',
    detail: 1,
  });
  const keys = screen.getByRole('textbox', { name: /Type into the page/ });
  fireEvent.keyDown(keys, { key: 'a' });
  fireEvent.keyUp(keys, { key: 'a' });
  await waitFor(() =>
    expect(sent.flat()).toEqual([
      {
        type: 'mouse',
        action: 'down',
        x: 640,
        y: 400,
        button: 'left',
        clicks: 1,
      },
      {
        type: 'mouse',
        action: 'up',
        x: 640,
        y: 400,
        button: 'left',
        clicks: 1,
      },
      { type: 'key', action: 'down', key: 'a' },
      { type: 'key', action: 'up', key: 'a' },
    ]),
  );
});

it('pops out to its own window in the desktop app', async () => {
  const { platform } = setup({});
  await userEvent.click(await screen.findByRole('button', { name: 'Pop out' }));
  expect(platform.managedWindow).toHaveBeenCalledWith(
    '/app-v2/browser/conversation-a',
  );
});

it('shows the conversation’s tabs when there are several', async () => {
  const { controller } = setup({
    tabs: [
      {
        id: 't1',
        title: 'Make a claim',
        url: 'https://a.example/',
        active: false,
      },
      {
        id: 't2',
        title: 'Your engineer',
        url: 'https://a.example/e',
        active: true,
      },
    ],
  });
  const tab = await screen.findByRole('tab', { name: 'Make a claim' });
  expect(screen.getByRole('tab', { name: 'Your engineer' })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await userEvent.click(tab);
  expect(controller.browserAction).toHaveBeenCalledWith('conversation-a', {
    action: 'switch_tab',
    tab: 't1',
  });
});

it('the chat’s live step names the site and opens the pane', async () => {
  const onOpen = vi.fn();
  const controller = {
    subscribe: () => () => {},
    getSnapshot: () => ({ status: 'ready' }),
    browserLive: async function* (
      _c: string,
      _v: string,
      _s: BrowserLiveSize,
      signal: AbortSignal,
    ) {
      yield { state: { ...PAGE, acting: 'Typing into textbox "Details"' } };
      yield { frame: FRAME };
      await new Promise((resolve) => signal.addEventListener('abort', resolve));
    },
  } as unknown as ClientController;
  await act(async () => {
    render(
      <RuntimeContext.Provider
        value={{ controller, platform: {} as ClientPlatform }}
      >
        <BrowserStep conversationId="conversation-a" onOpen={onOpen} />
      </RuntimeContext.Provider>,
    );
  });
  expect(await screen.findByText(/Using the browser/)).toHaveTextContent(
    'Using the browser · homeserve.co.uk',
  );
  expect(screen.getByText('Typing into "Details"')).toBeVisible();
  await userEvent.click(
    screen.getByRole('button', { name: 'Open the browser: homeserve.co.uk' }),
  );
  expect(onOpen).toHaveBeenCalled();
});

it('reads Row-Bot’s action words without control roles', () => {
  expect(actingWords('Clicking button "Confirm and pay £60"')).toBe(
    'Clicking "Confirm and pay £60"',
  );
  expect(actingWords('Opening https://example.com')).toBe(
    'Opening https://example.com',
  );
});
