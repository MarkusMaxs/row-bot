import { useEffect, useId, useState, type ReactNode } from 'react';
import { GraduationCap } from 'lucide-react';
import { clientError } from '../../api/errors';
import type { LearningChangeView, LearningSnapshot } from '../../api/types';
import {
  Button,
  EntityList,
  EntityRow,
  InlineEmpty,
  Segmented,
} from '../../ui/primitives';
import { ModalTask, useNotify } from '../../ui/overlays';
import { relativeTime } from '../../ui/format';
import type { LearningIO } from './learning-state';
import ReviewSheet, { type ReviewChange } from './ReviewSheet';
import { usePhone } from './use-phone';
import {
  CHANGE_NAMES,
  CHANGE_STATE_WORDS,
  changeKindWords,
  kindLabel,
  NOT_USED_WORDS,
  plural,
  testDetailWords,
  VERDICT_LINES,
  whyWords,
} from './words';
import './learning.css';

export type ChangeFilter = 'ready' | 'testing' | 'in_use' | 'not_used';

const FILTER_OF: Record<LearningChangeView['state'], ChangeFilter> = {
  ready: 'ready',
  waiting_to_test: 'testing',
  testing: 'testing',
  edited: 'testing',
  in_use: 'in_use',
  not_used: 'not_used',
};

const LIST_NAMES: Record<ChangeFilter, string> = {
  ready: 'Changes ready to review',
  testing: 'Changes being tested',
  in_use: 'Changes in use',
  not_used: 'Changes not used',
};

const CHANGE_EMPTY: Record<ChangeFilter, string> = {
  ready: 'No changes are ready to review.',
  testing: 'Nothing is waiting to be tested.',
  in_use: 'No changes are in use. Changes you use show here, with Undo.',
  not_used: 'No changes were turned down or tested worse.',
};

/** The first filter worth opening: Ready, else Testing, else In use. */
export function firstFilter(changes: LearningChangeView[]): ChangeFilter {
  for (const filter of ['ready', 'testing', 'in_use'] as ChangeFilter[])
    if (changes.some((item) => FILTER_OF[item.state] === filter)) return filter;
  return 'ready';
}

function statusOf(change: LearningChangeView): {
  tone: 'neutral' | 'success' | 'info' | 'warning';
  label: string;
} {
  if (change.state === 'in_use')
    return {
      tone: 'success',
      label: change.used_by === 'automatic' ? 'In use · by itself' : 'In use',
    };
  if (change.state === 'ready') return { tone: 'info', label: 'Ready' };
  if (change.state === 'not_used')
    return {
      tone: 'neutral',
      label: NOT_USED_WORDS[change.not_used ?? 'not_better'],
    };
  return { tone: 'neutral', label: CHANGE_STATE_WORDS[change.state] };
}

/** Use, Undo, Not now, Don't suggest again, Edit and Test again, with their toasts. */
export function useChangeActions(io: LearningIO) {
  const notify = useNotify();
  async function run(
    action:
      | 'use_change'
      | 'undo_change'
      | 'not_now_change'
      | 'dont_suggest_change'
      | 'edit_change'
      | 'test_change',
    change: LearningChangeView,
    text?: string,
  ): Promise<boolean> {
    try {
      const receipt = await io.run(action, {
        change_id: change.id,
        ...(text === undefined ? {} : { text }),
      });
      notify(
        receipt.summary,
        undefined,
        action === 'use_change'
          ? {
              label: 'Undo',
              onAction: () => void run('undo_change', change),
            }
          : undefined,
      );
      return true;
    } catch (cause) {
      notify(clientError(cause).message, 'danger');
      return false;
    }
  }
  return run;
}

function EditChange({
  open,
  onOpenChange,
  change,
  onSave,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  change: LearningChangeView;
  onSave: (text: string) => Promise<boolean>;
}) {
  const phone = usePhone();
  const id = useId();
  const [text, setText] = useState(change.text);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) setText(change.text);
  }, [open, change.text]);
  const limit = change.change === 'way_of_working' ? 400 : 1200;
  return (
    <ModalTask
      open={open}
      onOpenChange={onOpenChange}
      title={`Edit ${CHANGE_NAMES[change.change].toLowerCase()}`}
      description="Row-Bot tests it again before you can use it."
      kind={phone ? 'sheet' : 'dialog'}
      className="learning-teach"
    >
      <form
        className="learning-sheet"
        onSubmit={(event) => {
          event.preventDefault();
          if (!text.trim() || busy) return;
          setBusy(true);
          void onSave(text.trim()).then((done) => {
            setBusy(false);
            if (done) onOpenChange(false);
          });
        }}
      >
        <label className="learning-field" htmlFor={id}>
          <span className="learning-label">
            {change.change === 'way_of_working'
              ? 'The way of working'
              : 'The lesson'}
          </span>
          <textarea
            id={id}
            className="input"
            maxLength={limit}
            value={text}
            onChange={(event) => setText(event.target.value)}
            data-initial-focus
          />
        </label>
        <div className="learning-sheet-actions">
          <Button onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            disabled={busy || !text.trim() || text.trim() === change.text}
          >
            {busy ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </form>
    </ModalTask>
  );
}

/** The review sheet for one change (designs/learning-ux.md §4), whatever its state. */
export function ChangeReview({
  io,
  change,
  open,
  onOpenChange,
}: {
  io: LearningIO;
  change: LearningChangeView;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const run = useChangeActions(io);
  const [details, setDetails] = useState(false);
  const [editing, setEditing] = useState(false);
  const close = () => onOpenChange(false);
  const act =
    (
      action:
        | 'use_change'
        | 'undo_change'
        | 'not_now_change'
        | 'dont_suggest_change'
        | 'test_change',
    ) =>
    () =>
      void run(action, change).then((done) => done && close());
  const test = change.test;
  const review: ReviewChange = {
    title: change.title,
    kind: changeKindWords(change),
    why:
      (change.agent_saved
        ? 'Row-Bot saved this lesson for itself during a chat. Nothing had tested it.'
        : whyWords(change)) +
      (change.never_automatic
        ? ' It rests only on what tools or web pages said, so it’s never used by itself.'
        : ''),
    change:
      change.change === 'skill' && change.diff ? change.diff : change.text,
    edited: change.state === 'edited',
    results: {
      rows: (test?.rows ?? []).map((row) => ({
        label: row.label,
        total: row.total,
        without: row.without,
        with: row.with_change,
      })),
      note: test?.note ?? '',
    },
  };
  const editable =
    change.change !== 'skill' &&
    ['ready', 'waiting_to_test', 'edited', 'not_used'].includes(change.state);
  // Ready with a better test: the sheet's own table and "Use this change" (or Test again once edited).
  const standard =
    (change.state === 'ready' || change.state === 'edited') && Boolean(test);
  let results: ReactNode | undefined;
  let primary: { label: string; onSelect: () => void } | undefined;
  if (!standard) {
    results = test ? (
      <>
        <ResultsTable change={change} />
        <p className="learning-muted">
          {VERDICT_LINES[test.verdict]} {test.note}
        </p>
      </>
    ) : (
      <p className="learning-muted">
        {change.state === 'testing'
          ? 'Testing it now on work it hasn’t seen.'
          : 'Not tested yet. Row-Bot tests each change on work it hasn’t seen during quiet hours, before offering it.'}
      </p>
    );
    if (change.state === 'in_use')
      primary = { label: 'Undo', onSelect: act('undo_change') };
    else if (change.state === 'waiting_to_test' || change.state === 'edited')
      primary = { label: 'Test now', onSelect: act('test_change') };
  }
  return (
    <>
      <ReviewSheet
        open={open}
        onOpenChange={onOpenChange}
        change={review}
        onUse={act('use_change')}
        onTestAgain={act('test_change')}
        onNotNow={
          change.state === 'ready' ? act('not_now_change') : () => close()
        }
        notNowLabel={change.state === 'ready' ? 'Not now' : 'Close'}
        onEdit={editable ? () => setEditing(true) : undefined}
        onDontSuggest={
          change.state === 'in_use' || change.state === 'not_used'
            ? undefined
            : act('dont_suggest_change')
        }
        onDetails={() => setDetails((value) => !value)}
        detailsOpen={details}
        details={<ChangeDetails change={change} />}
        results={standard ? undefined : results}
        primary={primary}
      />
      <EditChange
        open={editing}
        onOpenChange={setEditing}
        change={change}
        onSave={(text) => run('edit_change', change, text)}
      />
    </>
  );
}

function ResultsTable({ change }: { change: LearningChangeView }) {
  const rows = change.test?.rows ?? [];
  if (!rows.length) return null;
  return (
    <table className="learning-results">
      <caption className="visually-hidden">
        Tasks passed without and with this change
      </caption>
      <thead>
        <tr>
          <th scope="col">Tasks</th>
          <th scope="col">Without</th>
          <th scope="col">With</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.label}>
            <th scope="row">
              {row.label} ({row.total})
            </th>
            <td data-label="Without">{row.without}</td>
            <td data-label="With">{row.with_change} passed</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Experiment details (read-only): how it was tested, in numbers. */
function ChangeDetails({ change }: { change: LearningChangeView }) {
  const test = change.test;
  return (
    <dl className="learning-facts">
      <div>
        <dt>Kind of change</dt>
        <dd>
          {CHANGE_NAMES[change.change]}
          {change.setting ? ` · ${change.setting}` : ''}
        </dd>
      </div>
      <div>
        <dt>Drafted</dt>
        <dd>{change.created_at ? relativeTime(change.created_at) : '—'}</dd>
      </div>
      <div>
        <dt>Test</dt>
        <dd>
          {test
            ? testDetailWords(test)
            : 'Not tested yet. Each test runs the same tasks with and without the change, at the same budget.'}
        </dd>
      </div>
      {change.never_automatic && (
        <div>
          <dt>Automatic use</dt>
          <dd>
            Never by itself: it rests only on what tools or web pages said.
          </dd>
        </div>
      )}
    </dl>
  );
}

/** Changes (designs §7): Ready · Testing · In use · Not used, each opening its review. */
export function ChangesView({
  io,
  snapshot,
  onOpen,
}: {
  io: LearningIO;
  snapshot: LearningSnapshot;
  onOpen: (id: string) => void;
}) {
  const changes = snapshot.changes ?? [];
  const [filter, setFilter] = useState<ChangeFilter>(() =>
    firstFilter(changes),
  );
  const run = useChangeActions(io);
  const shown = changes.filter((item) => FILTER_OF[item.state] === filter);
  const testing = snapshot.testing_now;
  return (
    <div className="learning-section-list">
      <div className="learning-filters">
        <Segmented
          label="Show changes"
          size="sm"
          value={filter}
          onChange={setFilter}
          options={[
            { value: 'ready', label: 'Ready' },
            { value: 'testing', label: 'Testing' },
            { value: 'in_use', label: 'In use' },
            { value: 'not_used', label: 'Not used' },
          ]}
        />
      </div>
      {testing && filter === 'testing' && (
        <div className="learning-progress" role="status">
          <p className="home-caption">
            Testing “{testing.title}” on {plural(testing.total, 'task')} ·{' '}
            {testing.done.toLocaleString()} done
          </p>
          <progress
            max={Math.max(1, testing.total)}
            value={testing.done}
            aria-label={`Testing “${testing.title}”`}
          />
        </div>
      )}
      {shown.length ? (
        <EntityList label={LIST_NAMES[filter]}>
          {shown.map((change) => (
            <EntityRow
              key={change.id}
              className="learning-change-row"
              title={change.title}
              icon={<GraduationCap size={16} />}
              status={statusOf(change)}
              meta={`${CHANGE_NAMES[change.change]} · ${kindLabel(change.kind)}${
                change.created_at ? ` · ${relativeTime(change.created_at)}` : ''
              }`}
              action={
                change.state === 'in_use' ? (
                  <Button
                    className="small"
                    aria-label={`Undo ${change.title}`}
                    onClick={() => void run('undo_change', change)}
                  >
                    Undo
                  </Button>
                ) : (
                  <Button
                    className="small"
                    variant={change.state === 'ready' ? 'primary' : undefined}
                    aria-label={`Review ${change.title}`}
                    onClick={() => onOpen(change.id)}
                  >
                    Review
                  </Button>
                )
              }
              menu={
                change.state === 'in_use'
                  ? [{ label: 'Review', onSelect: () => onOpen(change.id) }]
                  : undefined
              }
              menuLabel={`More for ${change.title}`}
            />
          ))}
        </EntityList>
      ) : (
        <InlineEmpty icon={<GraduationCap size={15} />}>
          {CHANGE_EMPTY[filter]} Row-Bot proposes a change only after testing it
          on work it hasn’t seen.
        </InlineEmpty>
      )}
    </div>
  );
}

/** Overview's Needs you lines for changes that are ready (not put off with Not now). */
export function ChangeNeeds({
  changes,
  onReview,
}: {
  changes: LearningChangeView[];
  onReview: (id: string) => void;
}) {
  const ready = changes.filter(
    (item) => item.state === 'ready' && !item.snoozed,
  );
  if (!ready.length) return null;
  return (
    <EntityList label="Changes ready to review">
      {ready.map((change) => (
        <EntityRow
          key={change.id}
          title={change.title}
          icon={<GraduationCap size={16} />}
          status={{ tone: 'info', label: 'Ready' }}
          meta={changeKindWords(change)}
          action={
            <Button
              className="small"
              variant="primary"
              aria-label={`Review ${change.title}`}
              onClick={() => onReview(change.id)}
            >
              Review
            </Button>
          }
        />
      ))}
    </EntityList>
  );
}

/** Changes automatic mode put into use by itself in the last day: Overnight lists each with Undo. */
export function UsedByItself({
  io,
  changes,
}: {
  io: LearningIO;
  changes: LearningChangeView[];
}) {
  const run = useChangeActions(io);
  const since = Date.now() - 36 * 3600 * 1000;
  const used = changes.filter(
    (item) =>
      item.state === 'in_use' &&
      item.used_by === 'automatic' &&
      item.used_at &&
      Date.parse(item.used_at) >= since,
  );
  if (!used.length) return null;
  return (
    <ul className="learning-overnight learning-used">
      {used.map((change) => (
        <li key={change.id}>
          <span>
            Put “{change.title}” into use by itself (
            {CHANGE_NAMES[change.change].toLowerCase()})
          </span>
          <Button
            variant="ghost"
            className="small"
            aria-label={`Undo ${change.title}`}
            onClick={() => void run('undo_change', change)}
          >
            Undo
          </Button>
        </li>
      ))}
    </ul>
  );
}
