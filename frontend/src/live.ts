import { useEffect, useEffectEvent } from 'react';
import type { LiveTopic, LiveUpdates } from './api/live-updates';

let current: LiveUpdates | undefined;

/** This window's app-wide live updates (B407): its controller's `live`. */
export function provideLive(live: LiveUpdates): void {
  current = live;
}

/**
 * This window's live updates. Outside the app (a component on its own)
 * there are none, and nothing is heard.
 */
export function useLive(): LiveUpdates | undefined {
  return current;
}

/**
 * Run `onChange` as soon as any of `topics` changes on the server (B407):
 * read that state again there. See `api/live-updates.ts` for the topics.
 */
export function useLiveUpdate(
  topics: readonly LiveTopic[],
  onChange: () => void,
): void {
  const live = useLive();
  const changed = useEffectEvent(onChange);
  const key = topics.join(' ');
  useEffect(() => {
    if (!live || !key) return;
    const unsubscribe = key
      .split(' ')
      .map((topic) => live.subscribe(topic as LiveTopic, () => changed()));
    return () => unsubscribe.forEach((stop) => stop());
  }, [live, key]);
}
