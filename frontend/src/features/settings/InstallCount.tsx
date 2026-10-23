import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import type {
  InstallCountDeletion,
  InstallCountSnapshot,
} from '../../api/types';
import { clientError } from '../../api/errors';
import { useRuntime } from '../../runtime';
import { Button, Disclosure, Toggle } from '../../ui/primitives';
import { useNotify, useOverlay } from '../../ui/overlays';
import { SettingsGroup, SettingsItem, StatusLine } from './anatomy';

const SYSTEMS: Record<InstallCountSnapshot['os'], string> = {
  windows: 'Windows',
  macos: 'macOS',
  linux: 'Linux',
  docker: 'Docker',
};

/**
 * What a count sends, when, where and for how long (N6). The card before the
 * first chat and Settings › System › Privacy say the same.
 */
export function InstallCountFacts({
  snapshot,
  place,
}: {
  snapshot: InstallCountSnapshot;
  place: 'card' | 'settings';
}) {
  return (
    <dl className="install-count-facts">
      <div>
        <dt>What is sent</dt>
        <dd>
          A random ID made for this count, the Row-Bot version (
          {snapshot.version}) and the system ({SYSTEMS[snapshot.os]}). Nothing
          else: no name, account, location, conversations, files or usage.
        </dd>
      </div>
      <div>
        <dt>When</dt>
        <dd>Once a day while Row-Bot is running.</dd>
      </div>
      <div>
        <dt>Where</dt>
        <dd>
          Row-Bot’s counting service, which runs on Cloudflare:{' '}
          <span className="install-count-service">{snapshot.service}</span>.
        </dd>
      </div>
      <div>
        <dt>How long it is kept</dt>
        <dd>
          Each day’s record is deleted after 35 days. Only totals without IDs
          are kept.
        </dd>
      </div>
      <div>
        <dt>Your IP address</dt>
        <dd>
          Cloudflare sees it, as it does for any connection. Row-Bot doesn’t
          store it.
        </dd>
      </div>
      {place === 'card' && (
        <div>
          <dt>Turning it off</dt>
          <dd>
            Here, or at any time in Settings › System › Privacy, where you can
            also delete what was counted.
          </dd>
        </div>
      )}
    </dl>
  );
}

function sentOn(day: string) {
  const date = new Date(`${day}T12:00:00Z`);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return 'Last sent today';
  return `Last sent on ${date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}`;
}

export type PrivacyIo = {
  load: (signal: AbortSignal) => Promise<InstallCountSnapshot>;
  choose: (enabled: boolean) => Promise<InstallCountSnapshot>;
  remove: () => Promise<InstallCountDeletion>;
};

/**
 * Settings › System › Privacy: the installation count's switch, "Delete
 * counted data", and what else Row-Bot contacts and why.
 */
export function Privacy({ io }: { io: PrivacyIo }) {
  const overlay = useOverlay();
  const notify = useNotify();
  const [snapshot, setSnapshot] = useState<InstallCountSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const abort = new AbortController();
    io.load(abort.signal).then(setSnapshot, (cause: unknown) => {
      if (!abort.signal.aborted) setError(clientError(cause).message);
    });
    return () => abort.abort();
  }, [io]);

  async function choose(enabled: boolean) {
    setBusy(true);
    setError('');
    try {
      setSnapshot(await io.choose(enabled));
      notify(enabled ? 'Counting turned on' : 'Counting turned off');
    } catch (cause) {
      setError(clientError(cause).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setError('');
    try {
      const result = await io.remove();
      setSnapshot(result.snapshot);
      if (result.outcome === 'deleted')
        notify('Counted data deleted. Counting is off.');
      else
        setError(
          'Counting is off, but Row-Bot couldn’t reach its counting service to delete what it holds. Try again later.',
        );
    } catch (cause) {
      setError(clientError(cause).message);
    } finally {
      setBusy(false);
    }
  }

  function confirmDelete(opener: HTMLElement) {
    overlay.open({
      kind: 'alert',
      returnFocusTo: opener,
      title: 'Delete counted data?',
      description:
        'Row-Bot asks its counting service to delete everything it holds for this installation, and turns counting off. Daily totals without IDs stay.',
      confirmLabel: 'Delete',
      onConfirm: () => {
        overlay.close();
        void remove();
      },
    });
  }

  const counting =
    snapshot !== null && (snapshot.available || snapshot.turned_off_here);
  return (
    <SettingsGroup title="Privacy" anchor="privacy">
      {snapshot && counting && (
        <SettingsItem
          anchor="install-count"
          label="Count this installation"
          help="Once a day: a random ID, the Row-Bot version and the system, so Row-Bot’s maker can count the installations reporting in the last 7 and 15 days."
          layout="inline"
          status={
            snapshot.turned_off_here ? (
              <StatusLine tone="neutral">
                Off: this installation’s set-up turns counting off
                (ROW_BOT_INSTALL_COUNT=0)
              </StatusLine>
            ) : snapshot.enabled ? (
              <StatusLine
                tone="success"
                more={
                  snapshot.last_sent_on ? [sentOn(snapshot.last_sent_on)] : []
                }
              >
                On
              </StatusLine>
            ) : (
              <StatusLine tone="neutral">Off</StatusLine>
            )
          }
          control={
            snapshot.turned_off_here ? undefined : (
              <Toggle
                label="Count this installation"
                checked={snapshot.enabled}
                disabled={busy}
                onChange={(event) => void choose(event.currentTarget.checked)}
              />
            )
          }
        >
          <Disclosure summary="What is sent">
            <InstallCountFacts snapshot={snapshot} place="settings" />
          </Disclosure>
        </SettingsItem>
      )}
      {snapshot?.can_delete && (
        <SettingsItem
          anchor="delete-counted-data"
          label="Delete counted data"
          help="Asks the counting service to delete what it holds for this installation."
          bind={false}
          control={
            <Button
              disabled={busy}
              onClick={(event) => confirmDelete(event.currentTarget)}
            >
              Delete…
            </Button>
          }
        />
      )}
      {error && (
        <p className="settings-help" role="alert">
          {error}
        </p>
      )}
      <SettingsItem
        anchor="contacts"
        label="What else Row-Bot contacts"
        help="Besides the models, tools, apps and channels you use:"
        layout="stacked"
      >
        <ul className="privacy-contacts">
          <li>
            <strong>Update checks</strong>
            <span>
              About once a day Row-Bot asks GitHub for a new version and, unless
              you turned background downloads off, downloads it from GitHub,
              with any new libraries from the Python Package Index and PyTorch.{' '}
              <Link to="/settings/updates">Settings › Updates</Link>
            </span>
          </li>
          <li>
            <strong>Connection checks</strong>
            <span>
              Every hour Row-Bot checks that it can reach the internet
              (Cloudflare’s 1.1.1.1), Ollama and the accounts you connected.{' '}
              <Link to="/?tab=monitor">Monitor</Link>
            </span>
          </li>
          <li>
            <strong>Catalogs</strong>
            <span>
              Model lists come from your providers when you connect one or
              choose Refresh, with context sizes from OpenRouter’s public list.
              App and skill catalogs update when you choose Update, or on a
              schedule you turn on.{' '}
              <Link to="/settings/apps?view=advanced#catalogs">
                Apps › Advanced
              </Link>
            </span>
          </li>
        </ul>
      </SettingsItem>
    </SettingsGroup>
  );
}

export default function ConnectedPrivacy() {
  const { controller } = useRuntime();
  const io = useRef<PrivacyIo>({
    load: (signal) => controller.installCount(signal),
    choose: (enabled) => controller.chooseInstallCount(enabled),
    remove: () => controller.deleteCountedData(),
  });
  return <Privacy io={io.current} />;
}
