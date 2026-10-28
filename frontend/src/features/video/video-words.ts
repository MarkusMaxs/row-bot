import type {
  VideoProjectView,
  VideoSceneWrite,
  VideoVersionView,
} from '../../api/types';
import { shortTime } from '../shell/conversation-groups';

/** The speeds a scene offers; 2× and up show a badge on the video. */
export const SPEEDS = [0.5, 1, 1.5, 2, 3, 4] as const;

/** The scenes as the panel edits them, from a version. */
export function draftOf(view: VideoProjectView): {
  scenes: VideoSceneWrite[];
  captions: boolean;
} {
  return {
    captions: view.captions,
    scenes: view.scenes.map((scene) => ({
      scene_id: scene.scene_id,
      source: scene.source,
      start: scene.start,
      end: scene.end,
      speed: scene.speed,
      zoom: scene.zoom,
      card: scene.card,
      line: scene.line,
    })),
  };
}

export function isDirty(
  view: VideoProjectView,
  draft: { scenes: VideoSceneWrite[]; captions: boolean },
): boolean {
  return JSON.stringify(draftOf(view)) !== JSON.stringify(draft);
}

/** "4.2 s", "1:05": how long a scene lasts on the video. */
export function lengthWords(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

/** "Version 3 · you · Edited in the panel". */
export function versionWords(item: VideoVersionView): string {
  return `Version ${item.number} · ${item.by === 'you' ? 'you' : 'Row-Bot'} · ${item.note}`;
}

export function statusWords(view: VideoProjectView): {
  label: string;
  detail: string;
  tone: 'neutral' | 'info' | 'success' | 'danger';
} {
  switch (view.status) {
    case 'voicing':
      return {
        label: 'Voicing',
        detail: 'Voicing the lines…',
        tone: 'info',
      };
    case 'rendering':
      return {
        label: 'Making',
        detail:
          view.edit?.status === 'getting_ffmpeg'
            ? 'Getting ffmpeg…'
            : 'Making the video…',
        tone: 'info',
      };
    case 'made':
      return {
        label: view.checks_passed === false ? 'Needs a look' : 'Made',
        detail: '',
        tone: view.checks_passed === false ? 'danger' : 'success',
      };
    case 'failed':
      return {
        label: 'Not made',
        detail: 'This version wasn’t made.',
        tone: 'danger',
      };
    case 'stopped':
      return {
        label: 'Stopped',
        detail: 'Making this version was stopped.',
        tone: 'neutral',
      };
    default:
      return {
        label: 'Not made yet',
        detail: 'Save a change, or ask Row-Bot, to make this version.',
        tone: 'neutral',
      };
  }
}

/** When a version was made, for its card: "Version 2 · 10:42". */
export function madeWords(view: VideoProjectView): string {
  const item = view.versions.find((entry) => entry.number === view.number);
  return item
    ? `Version ${item.number} of ${view.latest} · ${shortTime(item.at)}`
    : `Version ${view.number}`;
}
