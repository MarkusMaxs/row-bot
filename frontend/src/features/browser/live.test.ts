import { expect, it, vi } from 'vitest';
import type { BrowserInputEvent } from '../../api/types';
import { createInputQueue, liveWidth, pageKey, pagePoint } from './live';

it('maps a point on the shown picture to the page’s CSS pixels, whatever its size', () => {
  const rect = { left: 10, top: 20, width: 320, height: 200 };
  const page = { cssWidth: 1280, cssHeight: 800 };
  expect(pagePoint(170, 120, rect, page)).toEqual({ x: 640, y: 400 });
  expect(pagePoint(0, 0, rect, page)).toEqual({ x: 0, y: 0 }); // Outside the picture: its edge.
  expect(pagePoint(999, 999, rect, page)).toEqual({ x: 1280, y: 800 });
});

it('asks for pictures as wide as the pane in device pixels, in steps, within bounds', () => {
  expect(liveWidth(390, 3)).toBe(960); // A phone: 2× at most, in 320 px steps.
  expect(liveWidth(600, 1)).toBe(640);
  expect(liveWidth(1400, 2)).toBe(1600);
  expect(liveWidth(10, 1)).toBe(320);
});

it('sends only keys a page understands', () => {
  expect(pageKey('a')).toBe('a');
  expect(pageKey('Enter')).toBe('Enter');
  expect(pageKey('ArrowLeft')).toBe('ArrowLeft');
  expect(pageKey('Unidentified')).toBe('');
  expect(pageKey('Process')).toBe('');
});

it('sends input in order and lets a slow connection skip stale pointer moves', async () => {
  const batches: BrowserInputEvent[][] = [];
  let release!: () => void;
  const send = vi.fn(
    (events: BrowserInputEvent[]) =>
      new Promise<void>((resolve) => {
        batches.push(events);
        release = resolve;
      }),
  );
  const queue = createInputQueue(send);
  queue.push({
    type: 'mouse',
    action: 'down',
    x: 1,
    y: 1,
  } as BrowserInputEvent);
  queue.push({
    type: 'mouse',
    action: 'move',
    x: 2,
    y: 2,
  } as BrowserInputEvent);
  queue.push({
    type: 'mouse',
    action: 'move',
    x: 3,
    y: 3,
  } as BrowserInputEvent);
  queue.push({ type: 'mouse', action: 'up', x: 3, y: 3 } as BrowserInputEvent);
  release();
  await vi.waitFor(() => expect(batches).toHaveLength(2));
  release();
  expect(batches[0]).toEqual([{ type: 'mouse', action: 'down', x: 1, y: 1 }]);
  expect(batches[1]).toEqual([
    { type: 'mouse', action: 'move', x: 3, y: 3 },
    { type: 'mouse', action: 'up', x: 3, y: 3 },
  ]);
});
