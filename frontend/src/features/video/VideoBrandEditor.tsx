import { useEffect, useState } from 'react';
import { clientError } from '../../api/errors';
import type { MediaItem, VideoBrand } from '../../api/types';
import { useRuntime } from '../../runtime';
import { Button, Field, Select, Skeleton } from '../../ui/primitives';

const CAPTIONS: [VideoBrand['captions'], string][] = [
  ['box', 'In a box'],
  ['bold', 'Bold, outlined'],
  ['title', 'Large, centred'],
];
const CORNERS: [VideoBrand['logo_corner'], string][] = [
  ['top-left', 'Top left'],
  ['top-right', 'Top right'],
  ['bottom-left', 'Bottom left'],
  ['bottom-right', 'Bottom right'],
];
const MOODS: VideoBrand['music_mood'][] = ['calm', 'upbeat', 'focused', 'warm'];

/**
 * The brand kit (5.3.1 B3): one look for every video. Its colour fills title
 * cards and badges, its caption style sets how lines show, its logo (a picture
 * from Library › Media) sits in a corner, its intro and outro are cards before
 * and after, and its music mood is what Row-Bot picks. A version can turn it
 * off; changing the kit applies the next time a video is made.
 */
export function VideoBrandEditor({ onSaved }: { onSaved: () => void }) {
  const { controller } = useRuntime();
  const [brand, setBrand] = useState<VideoBrand | null>(null);
  const [saved, setSaved] = useState<VideoBrand | null>(null);
  const [pictures, setPictures] = useState<MediaItem[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const abort = new AbortController();
    controller.videoBrand(abort.signal).then(
      (value) => {
        if (!value) return;
        setBrand(value);
        setSaved(value);
      },
      (cause) => {
        if (!abort.signal.aborted) setError(clientError(cause).message);
      },
    );
    controller.media({ kind: 'image' }, abort.signal).then(
      (page) => setPictures(page?.items ?? []),
      () => undefined,
    );
    return () => abort.abort();
  }, [controller]);

  if (!brand)
    return error ? (
      <p role="alert" className="video-panel-error">
        {error}
      </p>
    ) : (
      <Skeleton label="Opening the brand kit" />
    );
  const set = (patch: Partial<VideoBrand>) => setBrand({ ...brand, ...patch });
  const changed = JSON.stringify(brand) !== JSON.stringify(saved);

  async function save() {
    if (!brand) return;
    setBusy(true);
    setError('');
    try {
      const next = await controller.saveVideoBrand(brand);
      if (next) {
        setBrand(next);
        setSaved(next);
        onSaved();
      }
    } catch (cause) {
      setError(clientError(cause).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="video-brand" role="group" aria-label="Brand kit">
      <div className="video-brand-grid">
        <Field label="Colour" layout="row">
          <input
            type="color"
            className="video-brand-colour"
            aria-label="Colour"
            value={brand.colour}
            onChange={(event) => set({ colour: event.target.value })}
          />
        </Field>
        <Field label="Caption style" layout="row">
          <Select
            aria-label="Caption style"
            value={brand.captions}
            onChange={(event) =>
              set({ captions: event.target.value as VideoBrand['captions'] })
            }
          >
            {CAPTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Logo" layout="row">
          <Select
            aria-label="Logo"
            value={brand.logo}
            onChange={(event) => set({ logo: event.target.value })}
          >
            <option value="">None</option>
            {pictures.map((item) => (
              <option key={item.ref} value={item.ref}>
                {item.name}
              </option>
            ))}
          </Select>
        </Field>
        {brand.logo && (
          <Field label="Logo corner" layout="row">
            <Select
              aria-label="Logo corner"
              value={brand.logo_corner}
              onChange={(event) =>
                set({
                  logo_corner: event.target.value as VideoBrand['logo_corner'],
                })
              }
            >
              {CORNERS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </Select>
          </Field>
        )}
        <Field label="Intro" layout="row">
          <input
            className="input"
            aria-label="Intro"
            placeholder="A card before the first scene"
            maxLength={120}
            value={brand.intro}
            onChange={(event) => set({ intro: event.target.value })}
          />
        </Field>
        <Field label="Outro" layout="row">
          <input
            className="input"
            aria-label="Outro"
            placeholder="A card after the last, e.g. row-bot.ai"
            maxLength={120}
            value={brand.outro}
            onChange={(event) => set({ outro: event.target.value })}
          />
        </Field>
        <Field label="Music mood" layout="row">
          <Select
            aria-label="Music mood"
            value={brand.music_mood}
            onChange={(event) =>
              set({
                music_mood: event.target.value as VideoBrand['music_mood'],
              })
            }
          >
            {MOODS.map((mood) => (
              <option key={mood} value={mood}>
                {mood[0].toUpperCase() + mood.slice(1)}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      {error && (
        <p role="alert" className="video-panel-error">
          {error}
        </p>
      )}
      <div className="video-brand-actions">
        <span>Used for every video from the next one made.</span>
        <Button
          variant="primary"
          disabled={!changed || busy}
          onClick={() => void save()}
        >
          Save brand kit
        </Button>
      </div>
    </div>
  );
}
