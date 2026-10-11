import { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { humanizeToken } from '../../ui/format';
import { IconButton, Input } from '../../ui/primitives';

export type ChecklistOption = { id: string; label: string; detail?: string };

/**
 * A profile's tools or skills, picked from a searchable list instead of typed
 * IDs (F14). The choices show as removable chips; an ID the list doesn't hold
 * (a tool turned off, a skill since removed) stays visible by its ID.
 */
export function ProfileChecklist({
  label,
  selected,
  onChange,
  search,
  empty,
  disabled = false,
}: {
  label: string;
  selected: readonly string[];
  onChange: (next: string[]) => void;
  search: (query: string, signal: AbortSignal) => Promise<ChecklistOption[]>;
  /** What choosing nothing means, e.g. "Uses every tool turned on in Settings". */
  empty: string;
  disabled?: boolean;
}) {
  const [query, setQuery] = useState('');
  const [options, setOptions] = useState<ChecklistOption[] | null>(null);
  const [names, setNames] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const abort = new AbortController();
    const timer = window.setTimeout(() => {
      search(query.trim(), abort.signal)
        .then((items) => {
          if (abort.signal.aborted) return;
          setOptions(items);
          setFailed(false);
          setNames((known) => ({
            ...known,
            ...Object.fromEntries(items.map((item) => [item.id, item.label])),
          }));
        })
        .catch(() => {
          if (!abort.signal.aborted) setFailed(true);
        });
    }, 200);
    return () => {
      window.clearTimeout(timer);
      abort.abort();
    };
  }, [query, search]);
  const name = (id: string) => names[id] ?? humanizeToken(id);
  const toggle = (id: string) =>
    onChange(
      selected.includes(id)
        ? selected.filter((item) => item !== id)
        : [...selected, id],
    );
  const noun = label.toLowerCase();
  return (
    <fieldset className="profile-checklist" disabled={disabled}>
      <legend>{label}</legend>
      {selected.length ? (
        <ul className="profile-checklist-chosen" aria-label={`Chosen ${noun}`}>
          {selected.map((id) => (
            <li key={id}>
              <span>{name(id)}</span>
              <IconButton
                size="sm"
                label={`Take out ${name(id)}`}
                onClick={() => toggle(id)}
              >
                <X size={12} aria-hidden />
              </IconButton>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">{empty}</p>
      )}
      <Input
        type="search"
        aria-label={`Find ${noun}`}
        placeholder={`Find ${noun}`}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {failed && <p role="alert">The {noun} could not be listed. Try again.</p>}
      {options && (
        <ul
          className="profile-checklist-options"
          aria-label={`${label} to choose from`}
        >
          {options.length === 0 && (
            <li className="muted">Nothing matches “{query.trim()}”.</li>
          )}
          {options.map((option) => (
            <li key={option.id}>
              <label>
                <input
                  type="checkbox"
                  checked={selected.includes(option.id)}
                  onChange={() => toggle(option.id)}
                />
                <span>{option.label}</span>
                {option.detail && <small>{option.detail}</small>}
              </label>
            </li>
          ))}
        </ul>
      )}
    </fieldset>
  );
}
