/** What the person chose, in the words the chat message uses ("approve" → "Approved"). */
const WORDS: Record<string, string> = {
  approve: 'Approved',
  approved: 'Approved',
  accept: 'Approved',
  accepted: 'Approved',
  yes: 'Yes',
  change: 'Change',
  changes: 'Change',
  edit: 'Change',
  revise: 'Change',
  skip: 'Skipped',
  skipped: 'Skipped',
  no: 'No',
  reject: 'Declined',
  rejected: 'Declined',
  decline: 'Declined',
  declined: 'Declined',
  done: 'Done',
  pick: 'Picked',
  picked: 'Picked',
  choose: 'Picked',
  chosen: 'Picked',
  submit: 'Filled in',
  refresh: 'Refresh',
};

export function choiceWords(choice: string): string {
  return (
    WORDS[choice.toLowerCase()] ??
    choice.charAt(0).toUpperCase() + choice.slice(1)
  );
}

export function decisionCount(count: number): string {
  return `${count} decision${count === 1 ? '' : 's'}`;
}

/** "Launch plan" → "Launch-plan.html", as Row-Bot names the download. */
export function pageFileName(title: string): string {
  const stem = title
    .replace(/[^A-Za-z0-9 _-]+/g, '')
    .trim()
    .replaceAll(' ', '-')
    .slice(0, 80);
  return `${stem || 'page'}.html`;
}
