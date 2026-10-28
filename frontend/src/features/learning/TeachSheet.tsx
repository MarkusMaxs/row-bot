import { useEffect, useId, useState } from 'react';
import { clientError } from '../../api/errors';
import { Button, Segmented } from '../../ui/primitives';
import { ModalTask } from '../../ui/overlays';
import { TEACH_CATEGORIES, type TeachCategory } from './words';
import { usePhone } from './use-phone';
import './learning.css';

export type TeachAnswer = {
  category: TeachCategory | null;
  note: string;
  /** "What should it have done?" (optional, a reply only). */
  answer: string;
};

/**
 * Teach Row-Bot (designs/learning-ux.md §2): what went wrong (optional) and
 * what it should do next time. A small dialog, a bottom sheet on a phone.
 */
export default function TeachSheet({
  open,
  onOpenChange,
  step,
  onSend,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Teaching about a failed step rather than the reply. */
  step?: boolean;
  onSend: (answer: TeachAnswer) => Promise<void>;
}) {
  const phone = usePhone();
  const noteId = useId();
  const answerId = useId();
  const [category, setCategory] = useState<TeachCategory | ''>(
    step ? 'wrong_tool_or_step' : '',
  );
  const [note, setNote] = useState('');
  const [answer, setAnswer] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    setCategory(step ? 'wrong_tool_or_step' : '');
    setNote('');
    setAnswer('');
    setError('');
  }, [open, step]);
  const empty = !category && !note.trim() && !answer.trim();
  async function send() {
    if (empty || busy) return;
    setBusy(true);
    setError('');
    try {
      await onSend({
        category: category || null,
        note: note.trim(),
        answer: step ? '' : answer.trim(),
      });
    } catch (cause) {
      setError(clientError(cause).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <ModalTask
      open={open}
      onOpenChange={onOpenChange}
      title="Teach Row-Bot"
      description={
        step
          ? 'Say what went wrong in this step. Row-Bot learns from it later; nothing changes right away.'
          : 'Say what went wrong. Row-Bot learns from it later; nothing changes right away.'
      }
      kind={phone ? 'sheet' : 'dialog'}
      className="learning-teach"
    >
      <form
        className="learning-sheet"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <div className="learning-field">
          <span className="learning-label" id={`${noteId}-what`}>
            What went wrong? <span className="learning-muted">(optional)</span>
          </span>
          <Segmented
            label="What went wrong?"
            value={category}
            onChange={(next) => setCategory(next === category ? '' : next)}
            options={TEACH_CATEGORIES}
          />
        </div>
        {!step && (
          <label className="learning-field" htmlFor={answerId}>
            <span className="learning-label">
              What should it have done?{' '}
              <span className="learning-muted">(optional)</span>
            </span>
            <input
              id={answerId}
              className="input"
              maxLength={2000}
              placeholder="e.g. 85 EUR, from the 2026 travel policy"
              value={answer}
              onChange={(event) => setAnswer(event.target.value)}
            />
          </label>
        )}
        <label className="learning-field" htmlFor={noteId}>
          <span className="learning-label">What should it do next time?</span>
          <textarea
            id={noteId}
            className="input"
            maxLength={2000}
            placeholder="e.g. Use the document’s ID, not its title"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            data-initial-focus
          />
        </label>
        {error && (
          <p role="alert" className="learning-error">
            {error}
          </p>
        )}
        <div className="learning-sheet-actions">
          <Button onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={busy || empty}>
            {busy ? 'Sending…' : 'Send'}
          </Button>
        </div>
      </form>
    </ModalTask>
  );
}
