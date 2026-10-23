/**
 * The host side of a page's bridge (N5): postMessage with one sandboxed
 * frame. The frame runs with `sandbox="allow-scripts"` only, so its origin is
 * opaque ("null"): it never reaches Row-Bot's origin, cookies, storage or
 * API. Only messages from that frame's own window are read, each one's shape
 * and size is checked, and they are rate-limited. A page can save its state
 * and record decisions, nothing else: it can't send a message, call a tool
 * or approve anything. Row-Bot checks every write again.
 */
import type { PageDecisionView } from '../../api/types';

export const MAX_STATE_BYTES = 256 * 1024;
const MAX_MESSAGE_BYTES = MAX_STATE_BYTES + 16 * 1024;
const CALLS_PER_MINUTE = 120;
const DECISION_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/;

export type PageDecision = {
  id: string;
  choice: string | null;
  note: string;
  label: string;
};

export type PageHost = {
  /** Save the page's whole state (an object). */
  saveState: (state: Record<string, unknown>) => Promise<void>;
  /** Record (or, with a null choice, clear) one decision. */
  decide: (decision: PageDecision) => Promise<void>;
  /** The page loaded another document: it is closed. */
  onNavigated: () => void;
};

type Call = {
  rowbot: 1;
  id: number;
  method: 'state.set' | 'decide';
  params: Record<string, unknown>;
};

function byteLength(value: unknown): number {
  try {
    return new TextEncoder().encode(JSON.stringify(value)).length;
  } catch {
    return Infinity;
  }
}

function plainObject(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === 'object' &&
    value !== null &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

function text(value: unknown, maximum: number): string | null {
  if (value === undefined || value === null || value === '') return '';
  return typeof value === 'string' && value.length <= maximum ? value : null;
}

/** A decision the page asked to record, or null when it isn't one. */
export function readDecision(params: unknown): PageDecision | null {
  if (!plainObject(params)) return null;
  const id = params.id;
  if (typeof id !== 'string' || !DECISION_ID.test(id)) return null;
  const choice = params.choice;
  if (
    choice !== null &&
    (typeof choice !== 'string' || !choice.trim() || choice.length > 40)
  )
    return null;
  const note = text(params.note, 2000);
  const label = text(params.label, 200);
  if (note === null || label === null) return null;
  return { id, choice: choice === null ? null : choice.trim(), note, label };
}

export class PageBridge {
  private frame: HTMLIFrameElement;
  private host: PageHost;
  private calls: number[] = [];
  private ended = false;
  // State saves coalesce: one in flight, and only the latest of those waiting
  // behind it is saved (a form saving on every key sends one at a time).
  private saving = false;
  private nextState: { state: Record<string, unknown>; ids: number[] } | null =
    null;

  constructor(frame: HTMLIFrameElement, host: PageHost) {
    this.frame = frame;
    this.host = host;
  }

  /** A message event from the window: anything not from this frame's own window is ignored. */
  handle(event: MessageEvent) {
    if (this.ended || event.source !== this.frame.contentWindow) return;
    if (event.origin !== 'null') return; // The sandbox gives the page no origin of its own.
    const message = event.data as Partial<Call> | null;
    if (
      !message ||
      typeof message !== 'object' ||
      message.rowbot !== 1 ||
      typeof message.id !== 'number' ||
      !Number.isInteger(message.id) ||
      message.id < 1 ||
      byteLength(message) > MAX_MESSAGE_BYTES
    )
      return;
    const id = message.id;
    const now = Date.now();
    this.calls = this.calls.filter((at) => now - at < 60_000);
    if (this.calls.length >= CALLS_PER_MINUTE)
      return this.reply(id, 'This page is saving too often. Wait a moment.');
    this.calls.push(now);
    if (message.method === 'state.set') {
      const state = plainObject(message.params)
        ? message.params.state
        : undefined;
      if (!plainObject(state))
        return this.reply(id, "A page's state must be an object.");
      if (byteLength(state) > MAX_STATE_BYTES)
        return this.reply(id, "This page's state is over 256 KB.");
      this.nextState = {
        state,
        ids: [...(this.nextState?.ids ?? []), id],
      };
      return void this.saveStates();
    }
    if (message.method === 'decide') {
      const decision = readDecision(message.params);
      if (!decision) return this.reply(id, "That decision can't be saved.");
      return void this.run(id, () => this.host.decide(decision));
    }
    this.reply(id, "Pages can't do that.");
  }

  /** The app's theme changed: the page follows it without loading again. */
  theme(mode: 'light' | 'dark') {
    this.post({ rowbot: 1, event: 'theme', mode });
  }

  /** The decisions as Row-Bot has them now (after the person sent them). */
  decisions(items: readonly PageDecisionView[]) {
    const decided: Record<string, Omit<PageDecisionView, 'id'>> = {};
    for (const { id, ...rest } of items) decided[id] = rest;
    this.post({ rowbot: 1, event: 'decisions', decisions: decided });
  }

  /** The frame loaded again: a page that navigates away is closed. */
  navigated() {
    this.end();
    this.host.onNavigated();
  }

  end() {
    this.ended = true;
  }

  private async saveStates() {
    if (this.saving) return;
    this.saving = true;
    try {
      while (this.nextState && !this.ended) {
        const { state, ids } = this.nextState;
        this.nextState = null;
        let error: string | undefined;
        try {
          await this.host.saveState(state);
        } catch (cause) {
          error =
            cause instanceof Error && cause.message
              ? cause.message
              : "That couldn't be saved.";
        }
        for (const id of ids) this.reply(id, error);
      }
    } finally {
      this.saving = false;
    }
  }

  private async run(id: number, operation: () => Promise<void>) {
    try {
      await operation();
      this.reply(id);
    } catch (cause) {
      this.reply(
        id,
        cause instanceof Error && cause.message
          ? cause.message
          : "That couldn't be saved.",
      );
    }
  }

  private reply(id: number, error?: string) {
    this.post(
      error === undefined ? { rowbot: 1, id } : { rowbot: 1, id, error },
    );
  }

  private post(message: Record<string, unknown>) {
    if (this.ended) return;
    // An opaque origin can't be named; the frame window itself is the only recipient.
    this.frame.contentWindow?.postMessage(message, '*');
  }
}
