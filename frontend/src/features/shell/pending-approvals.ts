import { useCallback, useSyncExternalStore } from 'react';
import type { PendingApproval, PendingApprovalPage } from '../../api/types';
import {
  fallbackDue,
  type LiveTopic,
  type LiveUpdates,
} from '../../api/live-updates';
import { useLive } from '../../live';

type Load<T> = (signal?: AbortSignal) => Promise<T>;

const READ_EVERY_MS = 15_000;
const ANNOUNCED_KEY = 'row-bot.approvals-announced.v1';

/**
 * One shared read of a value every view of it follows: the waiting approvals
 * (B255) or what needs the person (B383). Showing it in several places never
 * multiplies requests. It reads at once when one of its live topics changes
 * (B407), when the window is shown again and on `read()`; every `every` ms
 * while the live stream is down, else once a minute as a fallback.
 */
class SharedRead<T> {
  value: T | null = null;
  private listeners = new Set<() => void>();
  private timer = 0;
  private abort: AbortController | null = null;
  private readAt = 0;
  private unsubscribeLive: (() => void)[] = [];

  constructor(
    private load: Load<T>,
    private every: number,
    private live?: LiveUpdates,
    private topics: readonly LiveTopic[] = [],
  ) {}

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    if (this.listeners.size === 1) {
      this.read();
      this.timer = window.setInterval(this.tick, this.every);
      document.addEventListener('visibilitychange', this.read);
      this.unsubscribeLive = this.live
        ? this.topics.map((topic) => this.live!.subscribe(topic, this.read))
        : [];
    }
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size) return;
      window.clearInterval(this.timer);
      document.removeEventListener('visibilitychange', this.read);
      this.unsubscribeLive.forEach((stop) => stop());
      this.unsubscribeLive = [];
      this.abort?.abort();
      this.abort = null;
    };
  };

  private tick = () => {
    if (fallbackDue(this.live, this.readAt)) this.read();
  };

  read = () => {
    if (document.visibilityState === 'hidden') return;
    this.readAt = Date.now();
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

/**
 * A shared read of `load` (null before its first answer) and a re-read; it
 * reads again at once when one of `topics` changes on the server.
 */
export function useSharedRead<T>(
  load: Load<T> | undefined,
  every: number,
  topics: readonly LiveTopic[] = [],
): { value: T | null; refresh: () => void } {
  const live = useLive();
  let read = load
    ? (reads.get(load as Load<unknown>) as SharedRead<T> | undefined)
    : undefined;
  if (load && !read) {
    read = new SharedRead(load, every, live, topics);
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
  const { value, refresh } = useSharedRead(load, READ_EVERY_MS, ['approvals']);
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
