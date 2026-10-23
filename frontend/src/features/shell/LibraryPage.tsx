import { useSearchParams } from 'react-router-dom';
import { LayoutTemplate, MessagesSquare } from 'lucide-react';
import { Segmented } from '../../ui/primitives';
import LibraryPages from '../pages/LibraryPages';
import ConversationLibrary from './ConversationLibrary';

type Shown = 'conversations' | 'pages';

/**
 * Every conversation in one place: one search for titles and messages, a
 * type filter, and bulk actions. The sidebar and ⌘K cover quick switching.
 * Pages lists every page Row-Bot made, with its chat (N5).
 */
export default function LibraryPage() {
  const [params, setParams] = useSearchParams();
  const shown: Shown =
    params.get('show') === 'pages' ? 'pages' : 'conversations';
  return (
    <div className="library-page">
      <header className="library-header">
        <h1>Library</h1>
        <Segmented
          size="sm"
          label="Show in the library"
          value={shown}
          onChange={(value) =>
            setParams(value === 'pages' ? { show: 'pages' } : {}, {
              replace: true,
            })
          }
          options={[
            {
              value: 'conversations',
              label: 'Conversations',
              icon: <MessagesSquare size={15} aria-hidden />,
            },
            {
              value: 'pages',
              label: 'Pages',
              icon: <LayoutTemplate size={15} aria-hidden />,
            },
          ]}
        />
      </header>
      {shown === 'pages' ? <LibraryPages /> : <ConversationLibrary />}
    </div>
  );
}
