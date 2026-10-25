import { useCallback, useContext, useEffect, useState } from 'react';
import { LogOut, UserRoundCheck } from 'lucide-react';
import type { BrowserSite } from '../../api/types';
import { clientError } from '../../api/errors';
import { RuntimeContext } from '../../runtime';
import {
  Button,
  EntityList,
  EntityRow,
  IconButton,
  InlineEmpty,
  Skeleton,
} from '../../ui/primitives';

/**
 * The sites the person is signed in to in Row-Bot's browser (cookies kept in
 * its own profile), with Sign out for one and Sign out of all. Settings ›
 * Browser and the Browser pane's menu both show it.
 */
export function BrowserSites() {
  // Settings can show without a connected runtime (then there's nothing to list).
  const controller = useContext(RuntimeContext)?.controller;
  const [items, setItems] = useState<BrowserSite[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [confirmAll, setConfirmAll] = useState(false);
  useEffect(() => {
    if (!controller) return;
    const abort = new AbortController();
    controller
      .browserSites(abort.signal)
      .then((page) => setItems(page.items))
      .catch((cause) => {
        if (!abort.signal.aborted) setError(clientError(cause).message);
      });
    return () => abort.abort();
  }, [controller]);
  const clear = useCallback(
    async (site: string | null) => {
      if (!controller) return;
      setBusy(site ?? '*');
      setError('');
      try {
        const result = await controller.clearBrowserSites(site);
        setItems(result.items);
        setConfirmAll(false);
      } catch (cause) {
        setError(clientError(cause).message);
      } finally {
        setBusy('');
      }
    },
    [controller],
  );
  if (!controller) return null;
  if (!items && !error) return <Skeleton label="Reading signed-in sites" />;
  return (
    <div className="browser-sites">
      {error && (
        <p className="browser-sites-error" role="alert">
          {error}
        </p>
      )}
      {items && !items.length ? (
        <InlineEmpty icon={<UserRoundCheck size={16} />}>
          Not signed in anywhere yet. Sites you sign in to in Row-Bot&apos;s
          browser show here.
        </InlineEmpty>
      ) : (
        <EntityList label="Signed-in sites">
          {(items ?? []).map((item) => (
            <EntityRow
              key={item.site}
              title={item.site}
              action={
                <IconButton
                  label={`Sign out of ${item.site}`}
                  size="sm"
                  disabled={Boolean(busy)}
                  onClick={() => void clear(item.site)}
                >
                  <LogOut aria-hidden />
                </IconButton>
              }
            />
          ))}
        </EntityList>
      )}
      {Boolean(items?.length) &&
        (confirmAll ? (
          <div
            className="browser-sites-confirm"
            role="group"
            aria-label="Sign out of all sites"
          >
            <span>Sign out of every site in Row-Bot&apos;s browser?</span>
            <Button onClick={() => setConfirmAll(false)}>Cancel</Button>
            <Button
              variant="danger"
              disabled={Boolean(busy)}
              onClick={() => void clear(null)}
            >
              Sign out of all
            </Button>
          </div>
        ) : (
          <Button
            className="browser-sites-all"
            onClick={() => setConfirmAll(true)}
          >
            Sign out of all…
          </Button>
        ))}
    </div>
  );
}
