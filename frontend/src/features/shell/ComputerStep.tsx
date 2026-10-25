import { useCallback, useEffect, useState } from 'react';
import { Monitor, Settings2, Square, TriangleAlert } from 'lucide-react';
import type {
  ComputerUseCommand,
  ComputerUsePreview,
  ComputerUseReceipt,
  ComputerUseSnapshot,
} from '../../api/types';
import { clientError } from '../../api/errors';
import { useClientSelector, useRuntime } from '../../runtime';
import { AppLink } from '../../ui/app-link';
import { IconButton, StatusDot } from '../../ui/primitives';

/**
 * Computer use in the conversation (N12-B): "Using Microsoft Edge", what
 * Row-Bot is doing, a live picture of the window and Stop, as the browser's
 * step shows its page. Only this computer's own desktop app sees it. The
 * picture lives only in memory: fetched when it changes, never stored, and
 * hidden while the person has the computer ("Carry on?" is asked below).
 */
export type ComputerUseAction = ComputerUseCommand['type'];

/** How often the step checks the session while there is one to follow. */
export const COMPUTER_POLL_MS = 1000;
const SETTINGS = '/settings/system#browser-computer-use';

type Followed = {
  conversation: string;
  snapshot: ComputerUseSnapshot | null;
  checked: boolean;
  stopped: boolean;
};

const nothing = (conversation: string): Followed => ({
  conversation,
  snapshot: null,
  checked: false,
  stopped: false,
});

function pageVisible() {
  return (
    typeof document === 'undefined' || document.visibilityState !== 'hidden'
  );
}

/**
 * Follows the open conversation's computer use. It reads once when the
 * conversation opens and when its turn changes, then about once a second
 * while there is something to follow (`watch`: the turn is using the
 * computer) and the page is visible. An empty `conversationId` turns it off
 * (another device, or a past conversation page).
 */
export function useComputerUse({
  conversationId,
  watch,
  generationId,
  turnKey,
  load,
}: {
  conversationId: string;
  watch: boolean;
  generationId: string;
  turnKey: string;
  load: (
    conversation: string,
    signal: AbortSignal,
  ) => Promise<ComputerUseSnapshot>;
}) {
  const [followed, setFollowed] = useState<Followed>(() => nothing(''));
  const [visible, setVisible] = useState(pageVisible);
  const current =
    followed.conversation === conversationId
      ? followed
      : nothing(conversationId);

  const read = useCallback(
    async (signal: AbortSignal) => {
      try {
        const value = await load(conversationId, signal);
        if (signal.aborted || value.conversation_id !== conversationId) return;
        setFollowed((prior) => {
          const same = prior.conversation === conversationId;
          if (
            same &&
            prior.checked &&
            prior.snapshot?.revision === value.revision
          )
            return prior;
          return {
            conversation: conversationId,
            snapshot: value,
            checked: true,
            // A session that starts again replaces the "Stopped" note.
            stopped: same && prior.stopped && !value.active,
          };
        });
      } catch {
        if (signal.aborted) return;
        setFollowed((prior) =>
          prior.conversation === conversationId
            ? prior.checked
              ? prior
              : { ...prior, checked: true }
            : { ...nothing(conversationId), checked: true },
        );
      }
    },
    [conversationId, load],
  );

  useEffect(() => {
    const update = () => setVisible(pageVisible());
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  }, []);

  useEffect(() => {
    if (!conversationId) return;
    const abort = new AbortController();
    void read(abort.signal);
    return () => abort.abort();
  }, [conversationId, read, turnKey]);

  const following =
    Boolean(conversationId) && (watch || Boolean(current.snapshot?.active));
  useEffect(() => {
    if (!following || !visible) return;
    const abort = new AbortController();
    let timer = 0;
    const next = () => {
      timer = window.setTimeout(() => {
        void read(abort.signal).then(() => {
          if (!abort.signal.aborted) next();
        });
      }, COMPUTER_POLL_MS);
    };
    next();
    return () => {
      abort.abort();
      window.clearTimeout(timer);
    };
  }, [following, visible, read]);

  // A new turn clears the "Stopped" note left by the last one.
  useEffect(() => {
    setFollowed((prior) =>
      prior.stopped ? { ...prior, stopped: false } : prior,
    );
  }, [generationId]);

  const apply = useCallback(
    (value: ComputerUseSnapshot) =>
      setFollowed((prior) =>
        value.conversation_id !== conversationId
          ? prior
          : {
              conversation: conversationId,
              snapshot: value,
              checked: true,
              stopped: prior.conversation === conversationId && prior.stopped,
            },
      ),
    [conversationId],
  );
  const markStopped = useCallback(
    () =>
      setFollowed((prior) =>
        prior.conversation === conversationId
          ? { ...prior, stopped: true }
          : { ...nothing(conversationId), checked: true, stopped: true },
      ),
    [conversationId],
  );
  return {
    snapshot: current.snapshot,
    /** False only until the first read of a followed conversation ends. */
    checked: !conversationId || current.checked,
    stopped: current.stopped,
    visible:
      Boolean(conversationId) &&
      (Boolean(current.snapshot?.active) ||
        Boolean(current.snapshot?.problem) ||
        current.stopped),
    apply,
    markStopped,
  };
}

/** The window's latest picture for the step's state; it changes as the state's revision does. */
export function useComputerPicture(
  conversationId: string,
  snapshot: ComputerUseSnapshot | null,
  loadPreview: (
    conversation: string,
    revision: string,
    signal: AbortSignal,
  ) => Promise<ComputerUsePreview>,
) {
  const [picture, setPicture] = useState<string | null>(null);
  const shown = Boolean(
    snapshot?.active && snapshot.has_picture && snapshot.state !== 'paused',
  );
  const revision = shown ? (snapshot?.revision ?? '') : '';
  useEffect(() => {
    // The picture is ephemeral: drop it as soon as it may not be shown.
    if (!shown) setPicture(null);
  }, [shown]);
  useEffect(() => {
    if (!revision) return;
    const abort = new AbortController();
    loadPreview(conversationId, revision, abort.signal)
      .then((value) => {
        if (abort.signal.aborted || value.conversation_id !== conversationId)
          return;
        if (
          value.state === 'available' &&
          value.image_base64 &&
          value.mime_type
        )
          setPicture(`data:${value.mime_type};base64,${value.image_base64}`);
        else if (value.state !== 'waiting') setPicture(null);
      })
      // A changed picture or a lost request: the next check tries again.
      .catch(() => undefined);
    return () => abort.abort();
  }, [conversationId, loadPreview, revision]);
  return picture;
}

function Picture({ src, label }: { src: string | null; label: string }) {
  return (
    <span className="browser-thumb computer-thumb">
      {src ? (
        <img src={src} alt={label} draggable={false} />
      ) : (
        <span className="browser-thumb-empty" aria-hidden>
          <Monitor size={20} />
        </span>
      )}
    </span>
  );
}

/** What the step says it's doing, in a few words. */
function doing(snapshot: ComputerUseSnapshot) {
  if (snapshot.state === 'waiting_approval') return 'Waiting for your OK';
  if (snapshot.state === 'paused') return 'Paused while you use it';
  return snapshot.doing || 'Looking at the window';
}

/**
 * The chat's step while a turn uses the computer: "Using Microsoft Edge ·
 * the window", what Row-Bot is doing, the live picture and Stop. When
 * computer use couldn't start (not set up, no permission), it says why and
 * how to fix it instead.
 */
export function ComputerStep({
  conversationId,
  snapshot,
  stopped,
  loadPreview,
  send,
  onChange,
  onStopped,
}: {
  conversationId: string;
  snapshot: ComputerUseSnapshot | null;
  stopped: boolean;
  loadPreview: (
    conversation: string,
    revision: string,
    signal: AbortSignal,
  ) => Promise<ComputerUsePreview>;
  send: (
    conversation: string,
    action: ComputerUseAction,
  ) => Promise<ComputerUseReceipt>;
  onChange: (snapshot: ComputerUseSnapshot) => void;
  onStopped: () => void;
}) {
  const picture = useComputerPicture(conversationId, snapshot, loadPreview);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function stop() {
    setBusy(true);
    setError('');
    try {
      const receipt = await send(conversationId, 'computer_use.stop');
      if (receipt.computer_use) onChange(receipt.computer_use);
      if (receipt.status === 'completed') onStopped();
      else
        setError(
          clientError({
            code: receipt.code ?? 'computer_use_outcome_uncertain',
          }).message,
        );
    } catch (cause) {
      setError(clientError(cause).message);
    } finally {
      setBusy(false);
    }
  }

  if (snapshot?.problem && !snapshot.active)
    return (
      <section className="browser-step computer-step" data-state="problem">
        <span className="browser-thumb computer-thumb" aria-hidden>
          <span className="browser-thumb-empty computer-problem-icon">
            <TriangleAlert size={20} />
          </span>
        </span>
        <div className="browser-step-words" role="alert">
          <span className="browser-step-title">{snapshot.problem.message}</span>
          <span className="computer-step-fix">{snapshot.problem.fix}</span>
          {snapshot.problem.fix.includes('Settings') && (
            <AppLink className="browser-step-open" to={SETTINGS}>
              <Settings2 size={14} aria-hidden />
              Open computer use settings
            </AppLink>
          )}
        </div>
      </section>
    );

  const active = Boolean(snapshot?.active);
  if (!active || !snapshot)
    return stopped ? (
      <section className="browser-step computer-step" data-state="stopped">
        <Picture src={null} label="" />
        <div className="browser-step-words" role="status">
          <span className="browser-step-title">Computer use stopped</span>
          <span className="browser-step-doing">
            Row-Bot no longer controls your computer.
          </span>
        </div>
      </section>
    ) : null;

  // Before a window is chosen (its first use waiting for an OK), the approval card says it all.
  if (!snapshot.app) return null;
  const app = snapshot.app;
  return (
    <section
      className="browser-step computer-step"
      data-state={snapshot.state}
      aria-label={`Using ${app}`}
      aria-busy={busy || undefined}
    >
      <Picture src={picture} label={`The ${app} window Row-Bot is using`} />
      <div className="browser-step-words">
        <span className="browser-step-title">
          <StatusDot
            tone={snapshot.state === 'working' ? 'accent' : 'warning'}
            pulse={snapshot.state === 'working'}
            label={snapshot.state === 'working' ? 'Live' : 'Waiting'}
          />
          Using {app}
        </span>
        {snapshot.window && snapshot.window !== app ? (
          <span className="browser-step-site computer-step-window">
            {snapshot.window}
          </span>
        ) : null}
        <span className="browser-step-doing" role="status">
          {doing(snapshot)}
        </span>
        <span className="computer-step-hint">
          Press Esc on the computer to stop it at once.
        </span>
        {error && (
          <span className="computer-step-error" role="alert">
            {error}
          </span>
        )}
      </div>
      {snapshot.can_stop && (
        // The composer's Stop: a round ink button with a filled square.
        <IconButton
          label="Stop using the computer"
          size="sm"
          variant="secondary"
          className="composer-stop computer-stop"
          disabled={busy}
          onClick={() => void stop()}
        >
          <Square className="composer-primary-stop" size={12} aria-hidden />
        </IconButton>
      )}
    </section>
  );
}

/**
 * The window's picture on an approval card (a ring marks where Row-Bot will
 * click), on this computer's desktop app only.
 */
export function ComputerThumbnail({
  conversationId,
}: {
  conversationId: string;
}) {
  const { controller } = useRuntime();
  const local = useClientSelector((state) =>
    Boolean(
      state.handshake?.application_capabilities?.includes(
        'computer:interactive',
      ),
    ),
  );
  const load = useCallback(
    (conversation: string, signal: AbortSignal) =>
      controller.computerUse(conversation, signal),
    [controller],
  );
  const loadPreview = useCallback(
    (conversation: string, revision: string, signal: AbortSignal) =>
      controller.computerUsePreview(conversation, revision, signal),
    [controller],
  );
  const { snapshot } = useComputerUse({
    conversationId: local ? conversationId : '',
    watch: true,
    generationId: '',
    turnKey: '',
    load,
  });
  const picture = useComputerPicture(conversationId, snapshot, loadPreview);
  if (!local || !picture) return null;
  return (
    <Picture
      src={picture}
      label={`The ${snapshot?.app || 'app'} window, with where Row-Bot will act`}
    />
  );
}
