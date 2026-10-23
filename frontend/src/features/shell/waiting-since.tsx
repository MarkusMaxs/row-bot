import { absoluteTime, parseTimestamp } from '../../ui/format';

/** "Waiting since 9:00 AM", "… since yesterday, 9:00 AM", or with the date. */
export function waitingSince(value: string, now = new Date()): string {
  const date = parseTimestamp(value);
  if (!date) return '';
  const time = new Intl.DateTimeFormat(undefined, {
    timeStyle: 'short',
  }).format(date);
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === now.toDateString())
    return `Waiting since ${time}`;
  if (date.toDateString() === yesterday.toDateString())
    return `Waiting since yesterday, ${time}`;
  return `Waiting since ${absoluteTime(date)}`;
}

export function WaitingSince({ value }: { value: string | null | undefined }) {
  const words = value ? waitingSince(value) : '';
  if (!words) return null;
  return (
    <time dateTime={value!} title={absoluteTime(value)}>
      {words}
    </time>
  );
}
