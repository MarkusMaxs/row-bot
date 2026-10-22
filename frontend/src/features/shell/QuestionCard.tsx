import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { Check, MessageCircleQuestion } from 'lucide-react';
import type { ApprovalAnswer, ApprovalQuestion } from '../../api/types';
import { Button, Input } from '../../ui/primitives';

/** Whether a keystroke belongs to a text field rather than the card. */
function typing(target: EventTarget | null) {
  if (!(target instanceof Element)) return false;
  if (target instanceof HTMLElement && target.isContentEditable) return true;
  return Boolean(
    target.closest(
      'input:not([type="radio"]):not([type="checkbox"]), textarea, select, [contenteditable]:not([contenteditable="false"])',
    ),
  );
}

function dialogOpen() {
  return Boolean(
    document.querySelector('[role="dialog"], [role="alertdialog"]'),
  );
}

type Draft = { choices: number[]; other: string; otherOpen: boolean };

const EMPTY: Draft = { choices: [], other: '', otherOpen: false };

/** A step is answered by an option (or, when several are allowed, any), or the person's own words. */
function complete(question: ApprovalQuestion, draft: Draft) {
  const other = draft.otherOpen && draft.other.trim().length > 0;
  if (question.multi_select) return draft.choices.length > 0 || other;
  return draft.choices.length === 1 || other;
}

function answerOf(draft: Draft): ApprovalAnswer {
  return {
    choices: [...draft.choices].sort((a, b) => a - b),
    other: draft.otherOpen ? draft.other.trim() : '',
  };
}

/**
 * Row-Bot asks the person to choose (N4): header chips, the question in
 * plain words and its options as large rows (radio, or checkbox when several
 * may be picked), each with its one-line description and the recommended one
 * tagged. "Other…" opens a text field. Several questions are steps in one
 * card, with Answer at the end. Keyboard: 1–4 picks, Enter answers. Answer is
 * the one primary action; Skip lets Row-Bot go on with its recommendation.
 *
 * An answer is the person's input, never an approval of anything.
 */
export default function QuestionCard({
  questions,
  ready,
  resolution,
  error,
  notice = false,
  onSubmit,
}: {
  questions: ApprovalQuestion[];
  /** The question was read and is still waiting. */
  ready: boolean;
  resolution: string;
  error: string;
  /** In a parent chat or a dialog: keys work only with focus in the card. */
  notice?: boolean;
  /** The answers, one per question; null skips. */
  onSubmit: (answers: ApprovalAnswer[] | null) => Promise<void>;
}) {
  const [step, setStep] = useState(0);
  const [drafts, setDrafts] = useState<Draft[]>(() =>
    questions.map(() => EMPTY),
  );
  const card = useRef<HTMLElement>(null);
  const otherField = useRef<HTMLInputElement>(null);
  const focusOther = useRef(false);
  const id = useId();
  const question = questions[Math.min(step, questions.length - 1)];
  const draft = drafts[step] ?? EMPTY;
  const last = step >= questions.length - 1;
  const idle = ready && !resolution;
  const canGo = idle && Boolean(question) && complete(question, draft);

  useEffect(() => {
    if (focusOther.current && draft.otherOpen) {
      focusOther.current = false;
      otherField.current?.focus();
    }
  });

  function update(next: Partial<Draft>) {
    setDrafts((all) =>
      all.map((item, index) => (index === step ? { ...item, ...next } : item)),
    );
  }
  function pick(index: number) {
    if (!idle || !question || index >= question.options.length) return;
    if (question.multi_select) {
      const chosen = draft.choices.includes(index)
        ? draft.choices.filter((item) => item !== index)
        : [...draft.choices, index];
      update({ choices: chosen });
    } else update({ choices: [index], otherOpen: false });
  }
  function openOther(open: boolean) {
    if (!idle) return;
    focusOther.current = open;
    update(
      question?.multi_select
        ? { otherOpen: open }
        : { otherOpen: open, choices: open ? [] : draft.choices },
    );
  }
  function go() {
    if (!canGo) return;
    if (!last) {
      setStep(step + 1);
      return;
    }
    void onSubmit(drafts.map(answerOf));
  }
  function key(event: KeyboardEvent | globalThis.KeyboardEvent) {
    if (
      event.defaultPrevented ||
      ('nativeEvent' in event
        ? event.nativeEvent.isComposing
        : event.isComposing) ||
      typing(event.target)
    )
      return false;
    if (event.metaKey || event.ctrlKey || event.altKey) return false;
    if (/^[1-4]$/.test(event.key) && question) {
      const index = Number(event.key) - 1;
      if (index >= question.options.length) return false;
      event.preventDefault();
      pick(index);
      return true;
    }
    // Enter on a focused button (Skip, Back) is that button's own.
    const onButton =
      event.target instanceof Element &&
      Boolean(event.target.closest('button, a[href]'));
    if (event.key === 'Enter' && canGo && !onButton) {
      event.preventDefault();
      go();
      return true;
    }
    return false;
  }
  useEffect(() => {
    if (notice || !idle) return;
    const listener = (event: globalThis.KeyboardEvent) => {
      // Inside the card its own handler answers; elsewhere only with nothing else focused.
      if (card.current?.contains(event.target as Node) || dialogOpen()) return;
      key(event);
    };
    document.addEventListener('keydown', listener);
    return () => document.removeEventListener('keydown', listener);
  });

  if (!question) return null;
  const several = questions.length > 1;
  const kind = question.multi_select ? 'checkbox' : 'radio';
  const name = `${id}-q${step}`;
  return (
    <aside
      ref={card}
      className="approval-card question-card"
      data-kind="question"
      aria-label="Row-Bot has a question"
      onKeyDown={(event) => {
        key(event);
      }}
    >
      <span className="approval-card-icon" aria-hidden>
        <MessageCircleQuestion />
      </span>
      <div className="approval-card-context">
        <ol
          className="question-card-chips"
          aria-label={
            several ? `Question ${step + 1} of ${questions.length}` : 'Topic'
          }
        >
          {questions.map((item, index) => (
            <li
              key={`${index}:${item.header}`}
              className="question-card-chip"
              data-state={
                index === step ? 'current' : index < step ? 'done' : 'next'
              }
              aria-current={index === step ? 'step' : undefined}
            >
              {index < step && <Check aria-hidden size={12} />}
              {item.header}
            </li>
          ))}
        </ol>
      </div>
      <fieldset className="question-card-options" disabled={!idle}>
        <legend className="question-card-question">{question.question}</legend>
        {question.options.map((option, index) => {
          const checked = draft.choices.includes(index);
          const described = option.description
            ? `${name}-o${index}-d`
            : undefined;
          return (
            <label
              key={`${step}:${option.label}`}
              className="question-option"
              data-checked={checked || undefined}
            >
              <input
                type={kind}
                name={name}
                checked={checked}
                aria-describedby={described}
                onChange={() => pick(index)}
              />
              <span className="question-option-text">
                <span className="question-option-label">
                  {option.label}
                  {option.recommended && (
                    <span className="question-option-tag">Recommended</span>
                  )}
                </span>
                {option.description && (
                  <span id={described} className="question-option-description">
                    {option.description}
                  </span>
                )}
              </span>
              {index < 4 && (
                <kbd className="question-option-key" aria-hidden>
                  {index + 1}
                </kbd>
              )}
            </label>
          );
        })}
        <label
          className="question-option question-option-other"
          data-checked={draft.otherOpen || undefined}
        >
          <input
            type={kind}
            name={name}
            checked={draft.otherOpen}
            onChange={() =>
              openOther(!draft.otherOpen || !question.multi_select)
            }
          />
          <span className="question-option-text">
            <span className="question-option-label">Other…</span>
          </span>
        </label>
        {draft.otherOpen && (
          <Input
            ref={otherField}
            className="question-card-other"
            aria-label="Your own answer"
            placeholder="Write your answer"
            maxLength={500}
            value={draft.other}
            onChange={(event) => update({ other: event.target.value })}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                event.preventDefault();
                go();
              }
            }}
          />
        )}
      </fieldset>
      <div className="approval-card-actions">
        {step > 0 && (
          <Button
            variant="ghost"
            className="question-card-back"
            disabled={!idle}
            onClick={() => setStep(step - 1)}
          >
            Back
          </Button>
        )}
        <Button disabled={!idle} onClick={() => void onSubmit(null)}>
          Skip
        </Button>
        <Button
          variant="primary"
          aria-keyshortcuts={notice ? undefined : 'Enter'}
          disabled={!canGo}
          onClick={go}
        >
          {last ? 'Answer' : 'Next'}
        </Button>
      </div>
      {resolution && (
        <p role="status" className="approval-card-status">
          {resolution}
        </p>
      )}
      {error && (
        <p role="alert" className="approval-card-status">
          {error}
        </p>
      )}
    </aside>
  );
}
