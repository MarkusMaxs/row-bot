import { describe, expect, it, vi } from 'vitest';
import { MAX_STATE_BYTES, PageBridge, type PageHost } from './page-bridge';

function setup(overrides: Partial<PageHost> = {}) {
  const posted: Record<string, unknown>[] = [];
  const contentWindow = {
    postMessage: vi.fn((message: Record<string, unknown>) =>
      posted.push(message),
    ),
  };
  const frame = { contentWindow } as unknown as HTMLIFrameElement;
  const host: PageHost = {
    saveState: vi.fn(async () => undefined),
    decide: vi.fn(async () => undefined),
    onNavigated: vi.fn(),
    ...overrides,
  };
  const bridge = new PageBridge(frame, host);
  const send = (
    data: unknown,
    { source = contentWindow, origin = 'null' } = {},
  ) => bridge.handle({ data, source, origin } as unknown as MessageEvent);
  const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
  return { bridge, host, posted, send, flush };
}

describe('the page bridge', () => {
  it('reads only its own frame, from an opaque origin', async () => {
    const page = setup();
    const call = {
      rowbot: 1,
      id: 1,
      method: 'state.set',
      params: { state: { ticked: ['a'] } },
    };
    page.send(call, { source: { postMessage: vi.fn() } as never });
    page.send(call, { origin: 'http://localhost:8080' });
    await page.flush();
    expect(page.host.saveState).not.toHaveBeenCalled();
    expect(page.posted).toEqual([]);
    page.send(call);
    await page.flush();
    expect(page.host.saveState).toHaveBeenCalledWith({ ticked: ['a'] });
    expect(page.posted).toEqual([{ rowbot: 1, id: 1 }]);
  });

  it('saves only an object state within its size', async () => {
    const page = setup();
    page.send({ rowbot: 1, id: 1, method: 'state.set', params: { state: [] } });
    page.send({
      rowbot: 1,
      id: 2,
      method: 'state.set',
      params: { state: { notes: 'x'.repeat(MAX_STATE_BYTES) } },
    });
    await page.flush();
    expect(page.host.saveState).not.toHaveBeenCalled();
    expect(page.posted.map((message) => message.id)).toEqual([1, 2]);
    expect(
      page.posted.every((message) => typeof message.error === 'string'),
    ).toBe(true);
  });

  it('saves the latest state once a save in flight is done, never a queue of them', async () => {
    let finish = () => {};
    const saveState = vi.fn(
      () => new Promise<void>((resolve) => (finish = resolve)),
    );
    const page = setup({ saveState });
    for (let id = 1; id <= 3; id += 1)
      page.send({
        rowbot: 1,
        id,
        method: 'state.set',
        params: { state: { ticked: id } },
      });
    await page.flush();
    expect(saveState).toHaveBeenCalledTimes(1);
    finish();
    await page.flush();
    finish();
    await page.flush();
    expect(saveState.mock.calls).toEqual([[{ ticked: 1 }], [{ ticked: 3 }]]);
    expect(page.posted.map((message) => message.id).sort()).toEqual([1, 2, 3]);
  });

  it('records a checked decision and nothing else', async () => {
    const page = setup();
    page.send({
      rowbot: 1,
      id: 1,
      method: 'decide',
      params: {
        id: 'venue',
        choice: 'change',
        note: 'Smaller rooms',
        label: 'Book the venue',
      },
    });
    for (const [id, params] of [
      [2, { id: '../venue', choice: 'approve' }],
      [3, { id: 'venue', choice: 'x'.repeat(41) }],
      [4, { id: 'venue', choice: 'approve', note: 'x'.repeat(2001) }],
    ] as const)
      page.send({ rowbot: 1, id, method: 'decide', params });
    // Anything else a page might ask for is refused: it can't send, call a tool or approve.
    page.send({ rowbot: 1, id: 5, method: 'send', params: {} });
    page.send({ rowbot: 1, id: 6, method: 'tools/call', params: {} });
    await page.flush();
    expect(page.host.decide).toHaveBeenCalledTimes(1);
    expect(page.host.decide).toHaveBeenCalledWith({
      id: 'venue',
      choice: 'change',
      note: 'Smaller rooms',
      label: 'Book the venue',
    });
    const replies = Object.fromEntries(
      page.posted.map((message) => [message.id, message.error ?? 'saved']),
    );
    expect(replies).toEqual({
      1: 'saved',
      2: "That decision can't be saved.",
      3: "That decision can't be saved.",
      4: "That decision can't be saved.",
      5: "Pages can't do that.",
      6: "Pages can't do that.",
    });
  });

  it('tells the page nothing about a save cancelled because it is going away', async () => {
    const abort = () =>
      Promise.reject(new DOMException('Cancelled', 'AbortError'));
    const page = setup({ decide: vi.fn(abort), saveState: vi.fn(abort) });
    page.send({
      rowbot: 1,
      id: 1,
      method: 'decide',
      params: { id: 'a', choice: 'approve' },
    });
    page.send({
      rowbot: 1,
      id: 2,
      method: 'state.set',
      params: { state: { ticked: 1 } },
    });
    await page.flush();
    expect(page.host.decide).toHaveBeenCalledTimes(1);
    expect(page.host.saveState).toHaveBeenCalledTimes(1);
    expect(page.posted).toEqual([]);
  });

  it('is rate-limited', async () => {
    const page = setup();
    for (let id = 1; id <= 121; id += 1)
      page.send({
        rowbot: 1,
        id,
        method: 'decide',
        params: { id: 'a', choice: 'approve' },
      });
    await page.flush();
    expect(page.host.decide).toHaveBeenCalledTimes(120);
    expect(page.posted.find((message) => message.id === 121)).toEqual({
      rowbot: 1,
      id: 121,
      error: 'This page is saving too often. Wait a moment.',
    });
  });

  it('tells the page the theme and the decisions as sent, and ends when it navigates', async () => {
    const page = setup();
    page.bridge.theme('light');
    page.bridge.decisions([
      {
        id: 'venue',
        choice: 'change',
        note: 'Smaller',
        label: 'Venue',
        sent: true,
      },
    ]);
    expect(page.posted).toEqual([
      { rowbot: 1, event: 'theme', mode: 'light' },
      {
        rowbot: 1,
        event: 'decisions',
        decisions: {
          venue: {
            choice: 'change',
            note: 'Smaller',
            label: 'Venue',
            sent: true,
          },
        },
      },
    ]);
    page.bridge.navigated();
    expect(page.host.onNavigated).toHaveBeenCalled();
    page.send({
      rowbot: 1,
      id: 1,
      method: 'decide',
      params: { id: 'a', choice: 'approve' },
    });
    await page.flush();
    expect(page.host.decide).not.toHaveBeenCalled();
  });
});
