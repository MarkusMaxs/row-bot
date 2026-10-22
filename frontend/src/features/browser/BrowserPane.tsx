import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import {
  ArrowLeft,
  ArrowRight,
  Globe,
  Hand,
  KeyRound,
  Laptop,
  MoreHorizontal,
  PictureInPicture2,
  RotateCw,
  Square,
  UserRoundCheck,
  X,
} from 'lucide-react';
import type { BrowserPaneAction, BrowserPaneState } from '../../api/types';
import { clientError } from '../../api/errors';
import { useClientSelector, useRuntime } from '../../runtime';
import {
  Button,
  IconButton,
  Menu,
  Skeleton,
  StatusDot,
} from '../../ui/primitives';
import { useNotify, useOverlay } from '../../ui/overlays';
import { LocalAddresses } from './LocalAddresses';
import { BrowserSites } from './BrowserSites';
import {
  BUTTONS,
  createInputQueue,
  liveWidth,
  pageKey,
  pagePoint,
  useBrowserLive,
  type LiveFrame,
} from './live';

/** What the pane says Row-Bot is doing: "Clicking button "Next"" reads as "Clicking "Next"". */
export function actingWords(acting: string) {
  return acting.replace(
    /^(\w+ing(?: into)?) (?:button|link|textbox|searchbox|radio|checkbox|combobox|option|tab|menuitem|switch|control) /,
    '$1 ',
  );
}

const SIGN_IN: Record<
  Exclude<BrowserPaneState['sign_in'], ''>,
  { title: string; text: string; action: string }
> = {
  password: {
    title: 'Sign in here',
    text: "Row-Bot doesn't type passwords. Sign in on the page; Row-Bot carries on when you give it back.",
    action: 'Sign in',
  },
  code: {
    title: 'Enter the code here',
    text: "Row-Bot doesn't type one-time codes. Enter it on the page, then give it back.",
    action: 'Enter code',
  },
  card: {
    title: 'Enter your card details here',
    text: "Row-Bot doesn't type card numbers. Enter them on the page, then give it back.",
    action: 'Enter details',
  },
};

/** Where the pop-out opens: a client route showing only this conversation's browser. */
export function browserRoute(conversationId: string) {
  return `/app-v2/browser/${encodeURIComponent(conversationId)}`;
}

/**
 * Row-Bot's browser for one conversation (N12-A): the live page Row-Bot is
 * using, with tabs, an address bar, Back, Forward and Reload, Take control /
 * Give back and Stop. The person can click and type in the page directly:
 * doing so takes control, and Row-Bot waits until it's given back.
 */
export default function BrowserPane({
  conversationId,
  mode = 'docked',
  visible = true,
}: {
  conversationId: string;
  mode?: 'docked' | 'window';
  visible?: boolean;
}) {
  const { controller, platform } = useRuntime();
  const overlay = useOverlay();
  const notify = useNotify();
  const viewport = useRef<HTMLDivElement>(null);
  const picture = useRef<HTMLImageElement>(null);
  const keys = useRef<HTMLTextAreaElement>(null);
  const takeControlButton = useRef<HTMLButtonElement>(null);
  const [width, setWidth] = useState(1280);
  useEffect(() => {
    const element = viewport.current;
    if (!element || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => {
      const next = liveWidth(
        entry?.contentRect.width ?? 0,
        window.devicePixelRatio,
      );
      setWidth((current) => (current === next ? current : next));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const { state, frame, connection } = useBrowserLive({
    conversationId,
    view: 'pane',
    width,
    enabled: visible,
  });
  const generation = useClientSelector((value) =>
    value.conversation?.id === conversationId
      ? value.projection?.generation
      : undefined,
  );
  const revision = useClientSelector((value) =>
    value.conversation?.id === conversationId
      ? value.conversation.revision
      : undefined,
  );
  const running = Boolean(generation && !generation.quiesced);
  const [busy, setBusy] = useState(false);
  const [address, setAddress] = useState('');
  const [editing, setEditing] = useState(false);
  const shownUrl = state?.url ?? '';
  useEffect(() => {
    if (!editing) setAddress(shownUrl);
  }, [shownUrl, editing]);

  const act = useCallback(
    async (action: BrowserPaneAction) => {
      setBusy(true);
      try {
        await controller.browserAction(conversationId, action);
      } catch (cause) {
        notify(clientError(cause).message, 'warning');
      } finally {
        setBusy(false);
      }
    },
    [controller, conversationId, notify],
  );
  const stop = useCallback(async () => {
    if (!revision) return;
    try {
      await controller.intent(
        conversationId,
        'conversation.stop',
        {},
        revision,
      );
    } catch (cause) {
      notify(clientError(cause).message, 'warning');
    }
  }, [controller, conversationId, notify, revision]);
  const popOut = useCallback(async () => {
    const route = browserRoute(conversationId);
    const opened = await platform.managedWindow(route);
    if (opened.status !== 'ok')
      window.open(route, `row-bot-browser-${conversationId}`);
  }, [conversationId, platform]);

  const input = useMemo(
    () =>
      createInputQueue(async (events) => {
        const result = await controller.browserInput(conversationId, events);
        if (result.status === 'agent_acting') {
          notify(
            'Row-Bot is using the page. Take control to use it yourself.',
            'warning',
          );
          takeControlButton.current?.focus();
        }
      }),
    [controller, conversationId, notify],
  );

  const status = state?.status ?? 'stopped';
  const control = state?.control ?? 'agent';
  const acting = state?.acting ?? '';
  const waiting = state?.waiting ?? '';
  const signIn = state?.sign_in || '';
  const tabs = state?.tabs ?? [];
  const page = status === 'running' && tabs.length > 0;

  function point(
    event: { clientX: number; clientY: number },
    shown: LiveFrame,
  ) {
    const rect = picture.current?.getBoundingClientRect();
    return rect ? pagePoint(event.clientX, event.clientY, rect, shown) : null;
  }
  const touch = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  function onPointerDown(event: ReactPointerEvent) {
    if (!frame || !page) return;
    keys.current?.focus({ preventScroll: true });
    const at = point(event, frame);
    if (!at) return;
    if (event.pointerType === 'touch') {
      touch.current = { x: event.clientX, y: event.clientY, moved: false };
      return;
    }
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    input.push({
      type: 'mouse',
      action: 'down',
      ...at,
      button: BUTTONS[event.button] ?? 'left',
      clicks: Math.min(3, Math.max(1, event.detail || 1)),
    });
  }
  const lastMove = useRef(0);
  function onPointerMove(event: ReactPointerEvent) {
    if (!frame || !page) return;
    if (event.pointerType === 'touch' && touch.current) {
      const dy = touch.current.y - event.clientY;
      const dx = touch.current.x - event.clientX;
      if (Math.abs(dy) + Math.abs(dx) < 6) return;
      touch.current = { x: event.clientX, y: event.clientY, moved: true };
      const at = point(event, frame);
      if (at)
        input.push({
          type: 'mouse',
          action: 'wheel',
          ...at,
          dx: dx * 2,
          dy: dy * 2,
        });
      return;
    }
    const now = performance.now();
    if (now - lastMove.current < 33) return;
    lastMove.current = now;
    const at = point(event, frame);
    if (at) input.push({ type: 'mouse', action: 'move', ...at });
  }
  function onPointerUp(event: ReactPointerEvent) {
    if (!frame || !page) return;
    const at = point(event, frame);
    if (!at) return;
    if (event.pointerType === 'touch') {
      const tapped = touch.current && !touch.current.moved;
      touch.current = null;
      if (!tapped) return;
      input.push({
        type: 'mouse',
        action: 'down',
        ...at,
        button: 'left',
        clicks: 1,
      });
    }
    input.push({
      type: 'mouse',
      action: 'up',
      ...at,
      button: BUTTONS[event.button] ?? 'left',
      clicks: Math.min(3, Math.max(1, event.detail || 1)),
    });
  }
  function onWheel(event: React.WheelEvent) {
    if (!frame || !page) return;
    const at = point(event, frame);
    if (!at) return;
    const scale = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? 800 : 1;
    input.push({
      type: 'mouse',
      action: 'wheel',
      ...at,
      dx: event.deltaX * scale,
      dy: event.deltaY * scale,
    });
  }
  function onKey(
    event: ReactKeyboardEvent<HTMLTextAreaElement>,
    action: 'down' | 'up',
  ) {
    if (!page || event.nativeEvent.isComposing) return;
    const key = pageKey(event.key);
    if (!key) return;
    // The person's paste reaches the page as text (below), not as the keystroke.
    const paste =
      (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'v';
    if (paste) return;
    event.preventDefault();
    input.push({ type: 'key', action, key });
  }
  function onText(text: string) {
    if (page && text) input.push({ type: 'text', text });
  }

  const menu = [
    {
      label: 'Signed-in sites…',
      icon: <UserRoundCheck size={16} aria-hidden />,
      onSelect: () =>
        overlay.open({
          title: 'Signed-in sites',
          description:
            "Sites you're signed in to in Row-Bot's browser. Sign out of one, or all of them.",
          content: <BrowserSites />,
        }),
    },
    {
      label: 'Addresses on this computer…',
      icon: <Laptop size={16} aria-hidden />,
      onSelect: () =>
        overlay.open({
          title: 'Addresses on this computer',
          description:
            'Addresses like localhost:5173 that Row-Bot’s browser may open in this chat.',
          content: (
            <LocalAddresses
              conversationId={conversationId}
              load={controller.browserLocalAddresses}
              remove={controller.removeBrowserLocalAddress}
            />
          ),
        }),
    },
    {
      label: 'Restart browser',
      icon: <RotateCw size={16} aria-hidden />,
      separatorBefore: true,
      onSelect: () => void act({ action: 'restart' }),
    },
  ];

  const driving = control === 'agent' && Boolean(acting);
  // The toolbar offers Take control only while the status line has no control of its own.
  const quiet =
    control === 'agent' && !signIn && !acting && !(running && waiting);
  return (
    <section
      className="browser-pane"
      data-mode={mode}
      data-control={control}
      aria-label="Browser"
    >
      <div className="browser-chrome">
        <div className="browser-nav">
          <IconButton
            label="Back"
            size="sm"
            disabled={!page || busy}
            onClick={() => void act({ action: 'back' })}
          >
            <ArrowLeft aria-hidden />
          </IconButton>
          <IconButton
            label="Forward"
            size="sm"
            disabled={!page || busy}
            onClick={() => void act({ action: 'forward' })}
          >
            <ArrowRight aria-hidden />
          </IconButton>
          {state?.loading ? (
            <IconButton
              label="Stop loading"
              size="sm"
              onClick={() => void act({ action: 'stop_loading' })}
            >
              <X aria-hidden />
            </IconButton>
          ) : (
            <IconButton
              label="Reload"
              size="sm"
              disabled={!page || busy}
              onClick={() => void act({ action: 'reload' })}
            >
              <RotateCw aria-hidden />
            </IconButton>
          )}
        </div>
        <form
          className="browser-address"
          role="search"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            const target = address.trim();
            if (!target) return;
            setEditing(false);
            (document.activeElement as HTMLElement | null)?.blur();
            void act({ action: 'navigate', url: target });
          }}
        >
          <Globe size={14} aria-hidden className="browser-address-icon" />
          <input
            aria-label="Address"
            placeholder="Type an address"
            value={address}
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            inputMode="url"
            onFocus={(event) => {
              setEditing(true);
              event.currentTarget.select();
            }}
            onBlur={() => setEditing(false)}
            onChange={(event) => setAddress(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                setAddress(shownUrl);
                event.currentTarget.blur();
              }
            }}
          />
        </form>
        <div className="browser-tools">
          {quiet && (
            <IconButton
              ref={takeControlButton}
              label="Take control"
              size="sm"
              disabled={!page}
              onClick={() => {
                void act({ action: 'take_control' });
                keys.current?.focus({ preventScroll: true });
              }}
            >
              <Hand aria-hidden />
            </IconButton>
          )}
          {mode === 'docked' && (
            <IconButton label="Pop out" size="sm" onClick={() => void popOut()}>
              <PictureInPicture2 aria-hidden />
            </IconButton>
          )}
          <Menu
            label="More browser actions"
            iconOnly
            variant="ghost"
            actions={menu}
          >
            <MoreHorizontal size={16} aria-hidden />
          </Menu>
        </div>
      </div>
      {tabs.length > 1 && (
        <div className="browser-tabs" role="tablist" aria-label="Tabs">
          {tabs.map((tab) => (
            <div key={tab.id} className="browser-tab" data-active={tab.active}>
              <button
                type="button"
                role="tab"
                aria-selected={tab.active}
                className="browser-tab-title"
                onClick={() => void act({ action: 'switch_tab', tab: tab.id })}
                title={tab.url}
              >
                {tab.title || siteOf(tab.url) || 'New tab'}
              </button>
              <IconButton
                label={`Close ${tab.title || 'tab'}`}
                size="sm"
                onClick={() => void act({ action: 'close_tab', tab: tab.id })}
              >
                <X aria-hidden />
              </IconButton>
            </div>
          ))}
        </div>
      )}
      <PaneStatus
        status={status}
        control={control}
        acting={acting}
        waiting={running ? waiting : ''}
        signIn={signIn}
        running={running && Boolean(generation?.can_stop)}
        onTakeControl={() => {
          void act({ action: 'take_control' });
          keys.current?.focus({ preventScroll: true });
        }}
        onGiveBack={() => void act({ action: 'give_back' })}
        onStop={() => void stop()}
      />
      <div
        ref={viewport}
        className="browser-viewport"
        data-driving={
          driving ? 'agent' : control === 'person' ? 'person' : undefined
        }
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onWheel={onWheel}
        onContextMenu={(event) => page && event.preventDefault()}
      >
        {state?.loading && (
          <span
            className="browser-loading"
            role="progressbar"
            aria-label="Loading the page"
          />
        )}
        {status === 'crashed' ? (
          <div className="browser-state" role="alert">
            <strong>Row-Bot&apos;s browser stopped. Restart it?</strong>
            <p>
              Signed-in sites stay signed in. Row-Bot opens the page again when
              it carries on.
            </p>
            <Button
              variant="primary"
              disabled={busy}
              onClick={() => void act({ action: 'restart' })}
            >
              Restart
            </Button>
          </div>
        ) : status === 'starting' ? (
          <div className="browser-state">
            <Skeleton label="Starting Row-Bot's browser" />
            <p>Starting Row-Bot&apos;s browser…</p>
          </div>
        ) : !page || !frame ? (
          state && connection !== 'offline' ? (
            <div className="browser-state">
              <Globe size={28} aria-hidden className="browser-state-icon" />
              <strong>Nothing open yet</strong>
              <p>
                Row-Bot opens pages here as it works, and you stay signed in to
                sites. Type an address above to open one yourself.
              </p>
            </div>
          ) : (
            <div className="browser-state">
              <Skeleton label="Connecting to Row-Bot's browser" />
            </div>
          )
        ) : (
          <img
            ref={picture}
            className="browser-picture"
            src={frame.src}
            alt={`Live page: ${state?.title || state?.site || 'the page'}`}
            draggable={false}
          />
        )}
        {page && frame && connection === 'offline' && (
          <span className="browser-reconnecting" role="status">
            Reconnecting…
          </span>
        )}
        <textarea
          ref={keys}
          className="browser-keys"
          aria-label={`Type into the page${state?.title ? `: ${state.title}` : ''}`}
          tabIndex={page ? 0 : -1}
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          onKeyDown={(event) => onKey(event, 'down')}
          onKeyUp={(event) => onKey(event, 'up')}
          onPaste={(event) => {
            event.preventDefault();
            onText(event.clipboardData.getData('text/plain'));
          }}
          onInput={(event) => {
            const target = event.currentTarget;
            const native = event.nativeEvent as InputEvent;
            if (native.inputType === 'deleteContentBackward') {
              input.push({ type: 'key', action: 'down', key: 'Backspace' });
              input.push({ type: 'key', action: 'up', key: 'Backspace' });
            } else if (
              native.inputType?.startsWith('insert') &&
              !native.isComposing &&
              target.value
            ) {
              // Phone keyboards send text, not keys.
              onText(target.value);
            }
            target.value = '';
          }}
          onCompositionEnd={(event) => {
            onText(event.data);
            event.currentTarget.value = '';
          }}
        />
      </div>
    </section>
  );
}

function siteOf(url: string) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}

/** One line under the address bar: who's driving, and the one thing to do about it. */
function PaneStatus({
  status,
  control,
  acting,
  waiting,
  signIn,
  running,
  onTakeControl,
  onGiveBack,
  onStop,
}: {
  status: BrowserPaneState['status'];
  control: BrowserPaneState['control'];
  acting: string;
  waiting: string;
  signIn: BrowserPaneState['sign_in'];
  running: boolean;
  onTakeControl: () => void;
  onGiveBack: () => void;
  onStop: () => void;
}) {
  if (status !== 'running') return null;
  if (control === 'person')
    return (
      <div className="browser-status" data-tone="accent" role="status">
        <Hand size={16} aria-hidden className="browser-status-icon" />
        <span className="browser-status-text">
          <strong>You have control.</strong> Row-Bot waits until you give it
          back.
        </span>
        <Button variant="primary" onClick={onGiveBack}>
          Give back
        </Button>
      </div>
    );
  if (signIn)
    return (
      <div className="browser-status" data-tone="info" role="status">
        <KeyRound size={16} aria-hidden className="browser-status-icon" />
        <span className="browser-status-text">
          <strong>{SIGN_IN[signIn].title}</strong> {SIGN_IN[signIn].text}
        </span>
        <Button variant="primary" onClick={onTakeControl}>
          {SIGN_IN[signIn].action}
        </Button>
      </div>
    );
  if (acting || waiting)
    return (
      <div className="browser-status" role="status">
        <StatusDot
          tone="accent"
          pulse={Boolean(acting)}
          label="Row-Bot is using the browser"
        />
        <span className="browser-status-text">
          {acting ? `Row-Bot: ${actingWords(acting)}` : waiting}
        </span>
        <Button onClick={onTakeControl}>Take control</Button>
        {running && (
          <IconButton label="Stop" size="sm" variant="ghost" onClick={onStop}>
            <Square aria-hidden />
          </IconButton>
        )}
      </div>
    );
  return null;
}
