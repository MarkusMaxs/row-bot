import type { PageTheme } from '../../api/types';
import { TOKENS, type Accent } from '../../ui/theme-model';

/** Row-Bot's colour tokens a page receives (as --rb-<name>); Row-Bot checks them again. */
const NAMES = [
  'canvas',
  'surface',
  'surface-raised',
  'surface-sunken',
  'surface-hover',
  'surface-pressed',
  'text-primary',
  'text-secondary',
  'text-muted',
  'text-inverse',
  'border-subtle',
  'border-control',
  'border-strong',
  'shadow-color',
  'status-info-text',
  'status-info-background',
  'status-success-text',
  'status-success-background',
  'status-warning-text',
  'status-warning-background',
  'status-danger-text',
  'status-danger-background',
  'chart-series-1',
  'chart-series-2',
  'chart-series-3',
  'chart-series-4',
  'chart-series-5',
  'chart-series-6',
  'chart-grid',
  'chart-axis',
] as const;

export type PageMode = 'light' | 'dark';

/** The theme the app shows now. */
export function pageMode(root: HTMLElement = document.documentElement) {
  return (root.dataset.theme === 'light' ? 'light' : 'dark') as PageMode;
}

/**
 * Both themes' colours with the person's accent, and the one in use: a page
 * follows the app when the person switches theme, without loading again.
 */
export function pageTheme(
  root: HTMLElement = document.documentElement,
): PageTheme {
  const accent = (
    root.dataset.accent && root.dataset.accent in TOKENS.accents
      ? root.dataset.accent
      : 'blue'
  ) as Accent;
  const palette = (mode: PageMode) => {
    const values: Record<string, string> = {};
    for (const name of NAMES) values[name] = TOKENS[mode][name];
    values['accent-solid'] = TOKENS.accents[accent][mode];
    values['focus-ring'] = TOKENS.accents[accent][mode];
    values['accent-on-solid'] = TOKENS[mode]['text-inverse'];
    return values;
  };
  return {
    mode: pageMode(root),
    light: palette('light'),
    dark: palette('dark'),
  };
}
