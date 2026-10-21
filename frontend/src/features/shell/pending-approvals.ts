import { useCallback, useSyncExternalStore } from 'react';
import type { PendingApproval, PendingApprovalPage } from '../../api/types';

type Load<T> = (signal?: AbortSignal) => Promise<T>;

const READ_EVERY_MS = 15_000;
const ANNOUNCED_KEY = 'row-bot.approvals-announced.v1';

/**
 * One shared read of a value every view of it follows: the waiting approvals
 * (B255) or what needs the person (B383). Showing it in several places never
 * multiplies requests. It reads every `every` ms while the window is visible,
 * when the window is shown again, and at once on `read()`.
 */
class SharedRead<T> {
  value: T | null = null;
  private listeners = new Set<() => void>();
  private timer = 0;
  private abort: AbortController | null = null;

  constructor(
    private load: Load<T>,
    private every: number,
  ) {}

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    if (this.listeners.size === 1) {
      this.read();
      this.timer = window.setInterval(this.read, this.every);
      document.addEventListener('visibilitychange', this.read);
    }
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size) return;
      window.clearInterval(this.timer);
      document.removeEventListener('visibilitychange', this.read);
      this.abort?.abort();
      this.abort = null;
    };
  };

  read = () => {
    if (document.visibilityState === 'hidden') return;
    this.abort?.abort();
    const current = new AbortController();
    this.abort = current;
    this.load(current.signal).then(
      (value) => {
        if (current.signal.aborted) return;
        this.value = value;
        this.listeners.forEach((listener) => listener());
      },
      () => undefined,
    );
  };
}

const reads = new WeakMap<Load<unknown>, SharedRead<unknown>>();
const noLoad = () => () => undefined;

/** A shared read of `load` (null before its first answer) and a re-read. */
export function useSharedRead<T>(
  load: Load<T> | undefined,
  every: number,
): { value: T | null; refresh: () => void } {
  let read = load
    ? (reads.get(load as Load<unknown>) as SharedRead<T> | undefined)
    : undefined;
  if (load && !read) {
    read = new SharedRead(load, every);
    reads.set(load as Load<unknown>, read as SharedRead<unknown>);
  }
  const value = useSyncExternalStore(
    read?.subscribe ?? noLoad,
    () => read?.value ?? null,
  );
  const refresh = useCallback(() => read?.read(), [read]);
  return { value, refresh };
}

/** The waiting approvals (null before the first read) and a re-read. */
export function usePendingApprovals(load?: Load<PendingApprovalPage>): {
  page: PendingApprovalPage | null;
  refresh: () => void;
} {
  const { value, refresh } = useSharedRead(load, READ_EVERY_MS);
  return { page: value, refresh };
}

/**
 * Read the waiting approvals again now, for every view of them, without
 * subscribing: after an answer in a chat the sidebar no longer keeps
 * "1 approval is waiting" until its next 15 s read (B302).
 */
export function readPendingApprovalsNow(load: Load<PendingApprovalPage>) {
  reads.get(load as Load<unknown>)?.read();
}

/** Announced this page, for windows whose storage is refused. */
const announcedHere = new Set<string>();

/**
 * The waiting approvals this device has not announced yet, remembered as
 * announced from now on: a notice says each one once per device. The memory
 * keeps only approvals that still wait, so it stays as small as the list.
 */
export function unannounced(
  items: readonly PendingApproval[],
): PendingApproval[] {
  let saved: string[] = [];
  try {
    const value: unknown = JSON.parse(
      localStorage.getItem(ANNOUNCED_KEY) ?? '[]',
    );
    if (Array.isArray(value))
      saved = value.filter((id): id is string => typeof id === 'string');
  } catch {
    // Unreadable or refused storage: this page's memory still applies.
  }
  const known = new Set([...saved, ...announcedHere]);
  const fresh = items.filter((item) => !known.has(item.id));
  if (!fresh.length) return fresh;
  fresh.forEach((item) => announcedHere.add(item.id));
  const waiting = new Set(items.map((item) => item.id));
  try {
    localStorage.setItem(
      ANNOUNCED_KEY,
      JSON.stringify(
        [...known]
          .filter((id) => waiting.has(id))
          .concat(fresh.map((item) => item.id)),
      ),
    );
  } catch {
    // Private windows may refuse storage; the memory then lasts this page.
  }
  return fresh;
}
