import { useContext, useEffect, useState } from 'react';
import { ExternalLink, RefreshCw, SquareTerminal } from 'lucide-react';
import type { ClaudePlanStatus } from '../../api/types';
import { writeClipboardText } from '../../platform/clipboard';
import { RuntimeContext } from '../../runtime';
import {
  Button,
  CopyGlyph,
  IconButton,
  Skeleton,
  StatusDot,
  useCopyFeedback,
  type Tone,
} from '../../ui/primitives';

/** Anthropic's own page for installing Claude Code. */
export const CLAUDE_CODE_INSTALL_URL = 'https://code.claude.com/docs/en/setup';

/** The provider's name, and the one line that says what it does. */
export const CLAUDE_PLAN_TITLE = 'Claude (your Claude plan)';
export const CLAUDE_PLAN_SUMMARY =
  'Runs Claude Code on this computer; uses your Claude plan’s limits.';

type State = ClaudePlanStatus['state'];

/** Each state in words: the status pill, and the row's short detail. */
export const CLAUDE_PLAN_STATES: Record<
  State,
  { label: string; tone: Tone; detail: string }
> = {
  not_installed: {
    label: 'Not set up',
    tone: 'neutral',
    detail: 'Needs Claude Code on this computer',
  },
  signed_out: {
    label: 'Not signed in',
    tone: 'warning',
    detail: 'Sign in to Claude Code to use your plan',
  },
  too_old: {
    label: 'Update needed',
    tone: 'warning',
    detail: 'Claude Code needs an update',
  },
  ready: {
    label: 'Ready',
    tone: 'success',
    detail: 'Runs Claude Code on this computer',
  },
  error: {
    label: 'Couldn’t check',
    tone: 'warning',
    detail: 'Row-Bot couldn’t check Claude Code',
  },
};

/** The status pill and detail for a Claude plan row. */
export function claudePlanSummary(plan: ClaudePlanStatus) {
  const state = CLAUDE_PLAN_STATES[plan.state];
  if (plan.set_up_again)
    return {
      label: 'Set up again',
      tone: 'warning' as Tone,
      detail: 'Claude now works through Claude Code. Set it up again',
    };
  return state;
}

/** A command to run in a terminal, with Copy. */
function Command({ command }: { command: string }) {
  const [copied, setCopied] = useCopyFeedback();
  const writer = useContext(RuntimeContext)?.platform.writeClipboard;
  return (
    <div className="claude-plan-command">
      <code>{command}</code>
      <IconButton
        label={copied ? 'Copied' : 'Copy command'}
        size="sm"
        onClick={() => void writeClipboardText(command, writer).then(setCopied)}
      >
        <CopyGlyph copied={copied} />
      </IconButton>
    </div>
  );
}

/** "Open a terminal", only in the desktop app, which can open the person's own terminal app. */
function OpenTerminal() {
  const platform = useContext(RuntimeContext)?.platform;
  const [available, setAvailable] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    void platform
      ?.discover()
      .then((result) => {
        if (alive)
          setAvailable(
            result.status === 'ok' &&
              result.value.kind === 'pywebview' &&
              result.value.capabilities.includes('terminal_external'),
          );
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [platform]);
  if (!available || !platform) return null;
  return (
    <>
      <Button
        onClick={() =>
          void platform
            .openExternalTerminal(null)
            .then((result) => setFailed(result.status !== 'ok'))
        }
      >
        <SquareTerminal size={16} aria-hidden /> Open a terminal
      </Button>
      {failed && (
        <p className="settings-help" role="alert">
          Row-Bot couldn’t open your terminal app. Open it yourself.
        </p>
      )}
    </>
  );
}

/** What this computer's Claude Code says, and the one thing to do next. */
export default function ClaudePlanSetup({
  plan,
  checking,
  onCheck,
  onDone,
}: {
  plan: ClaudePlanStatus | null | undefined;
  checking: boolean;
  onCheck: () => void;
  /** Ready: close the dialog (Settings) or carry on (first run). */
  onDone?: () => void;
}) {
  if (!plan) return <Skeleton label="Checking Claude Code" />;
  const state = CLAUDE_PLAN_STATES[plan.state];
  const check = (
    <Button
      variant={plan.state === 'not_installed' ? 'secondary' : 'primary'}
      disabled={checking}
      onClick={onCheck}
    >
      <RefreshCw size={16} aria-hidden data-spinning={checking || undefined} />{' '}
      {checking ? 'Checking…' : 'Check again'}
    </Button>
  );
  return (
    <div className="stack claude-plan-setup" aria-busy={checking}>
      {plan.set_up_again && (
        <p className="settings-help" role="status">
          Claude now works through Claude Code. Set it up again.
        </p>
      )}
      <p className="claude-plan-state" role="status">
        <StatusDot tone={state.tone} label={state.label} showLabel />
        {plan.state === 'ready' && (
          <span>
            {plan.plan ? ` ${plan.plan}` : ' Your Claude plan'} · Claude Code{' '}
            {plan.version}
          </span>
        )}
      </p>
      {plan.state === 'not_installed' && (
        <>
          <p>Install Claude Code on this computer, then come back.</p>
          <div className="actions">
            <a
              className="button primary"
              href={CLAUDE_CODE_INSTALL_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              Install Claude Code <ExternalLink size={14} aria-hidden />
            </a>
            {check}
          </div>
        </>
      )}
      {plan.state === 'signed_out' && (
        <>
          <p>
            {plan.detail ? `${plan.detail} ` : ''}Open a terminal and run this,
            then sign in with your Claude plan:
          </p>
          <Command command="claude auth login" />
          <div className="actions">
            {check}
            <OpenTerminal />
          </div>
        </>
      )}
      {plan.state === 'too_old' && (
        <>
          <p>
            Claude Code {plan.version} is too old for Row-Bot; it needs{' '}
            {plan.minimum_version} or newer. Open a terminal and run:
          </p>
          <Command command="claude update" />
          <div className="actions">
            {check}
            <OpenTerminal />
          </div>
        </>
      )}
      {plan.state === 'error' && (
        <>
          <p>
            {plan.detail || 'Claude Code didn’t answer.'} To see what’s wrong,
            open a terminal and run:
          </p>
          <Command command="claude doctor" />
          <div className="actions">
            {check}
            <OpenTerminal />
          </div>
        </>
      )}
      {plan.state === 'ready' && (
        <>
          <p>Claude models are in the model picker.</p>
          {onDone && (
            <div className="actions">
              <Button variant="primary" onClick={onDone}>
                Done
              </Button>
            </div>
          )}
        </>
      )}
      <p className="settings-help claude-plan-note">
        Row-Bot never sees your Claude sign-in. It starts Claude Code with its
        non-essential traffic off: no telemetry, error reports or automatic
        updates from these runs.
      </p>
    </div>
  );
}
