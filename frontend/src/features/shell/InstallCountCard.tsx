import { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import type { InstallCountSnapshot } from '../../api/types';
import { clientError } from '../../api/errors';
import { useClientState, useRuntime } from '../../runtime';
import { Button, Toggle } from '../../ui/primitives';
import { ModalTask } from '../../ui/overlays';
import { InstallCountFacts } from '../settings/InstallCount';
import { setupDeferred } from './FirstRun';

export type InstallCountCardIo = {
  /** The count's state; null while Setup comes first (no model chosen yet). */
  load: (signal: AbortSignal) => Promise<InstallCountSnapshot | null>;
  choose: (enabled: boolean) => Promise<InstallCountSnapshot>;
};

/**
 * "Count this installation" (N6): shown before the first chat, and once to an
 * installation that updated, on whichever signed-in device opens Row-Bot
 * next. Nothing is counted until the person answers; closing it asks again
 * the next time Row-Bot opens.
 */
export function InstallCountCard({
  io,
  ready,
}: {
  io: InstallCountCardIo;
  /** Connected, and not in Setup: the card waits for both. */
  ready: string | null;
}) {
  const [snapshot, setSnapshot] = useState<InstallCountSnapshot | null>(null);
  const [later, setLater] = useState(false);
  const [on, setOn] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!ready) return;
    const abort = new AbortController();
    io.load(abort.signal).then(
      (value) => {
        if (!abort.signal.aborted) setSnapshot(value);
      },
      () => undefined, // Asked again the next time Row-Bot opens.
    );
    return () => abort.abort();
  }, [io, ready]);

  async function answer() {
    setBusy(true);
    setError('');
    try {
      setSnapshot(await io.choose(on));
    } catch (cause) {
      setError(clientError(cause).message);
    } finally {
      setBusy(false);
    }
  }

  if (!snapshot?.needs_disclosure) return null;
  return (
    <ModalTask
      open={Boolean(ready) && !later}
      onOpenChange={(open) => {
        if (!open) setLater(true);
      }}
      title="Count this installation"
      description="Row-Bot can send one small message a day, so its maker can count the installations reporting in the last 7 and 15 days."
      className="install-count-card"
    >
      <div className="stack">
        <InstallCountFacts snapshot={snapshot} place="card" />
        <label className="install-count-choice">
          <span>Count this installation</span>
          <Toggle
            label="Count this installation"
            checked={on}
            disabled={busy}
            onChange={(event) => setOn(event.currentTarget.checked)}
          />
        </label>
        {error && <p role="alert">{error}</p>}
        <div className="actions install-count-actions">
          <Button
            variant="primary"
            data-initial-focus
            disabled={busy}
            onClick={() => void answer()}
          >
            Continue
          </Button>
        </div>
      </div>
    </ModalTask>
  );
}

/** The card over the workspace, once Row-Bot is connected and Setup is done. */
export default function ConnectedInstallCountCard() {
  const { controller } = useRuntime();
  const state = useClientState();
  const { pathname } = useLocation();
  const identity =
    state.status === 'ready' && state.handshake
      ? `${state.handshake.instance_id}:${state.handshake.client_session_id}`
      : null;
  const [io] = useState<InstallCountCardIo>(() => ({
    load: async (signal) => {
      const [count, setup] = await Promise.all([
        controller.installCount(signal),
        controller.onboarding(signal),
      ]);
      // Setup comes first: the card follows once a model is chosen (or Setup is put off).
      return setup?.needs_model && !setupDeferred() ? null : count;
    },
    choose: (enabled) => controller.chooseInstallCount(enabled),
  }));
  // Read again on leaving Setup: the first run's card follows it.
  const ready = identity && pathname !== '/setup' ? `${identity}` : null;
  return <InstallCountCard io={io} ready={ready} />;
}
