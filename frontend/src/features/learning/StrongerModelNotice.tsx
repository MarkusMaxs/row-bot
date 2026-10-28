import { useState } from 'react';
import { clientError } from '../../api/errors';
import type { LearningStartingModel } from '../../api/types';
import { useOverlay } from '../../ui/overlays';
import { Button } from '../../ui/primitives';
import type { LearningIO } from './learning-state';
import { StartingModelSheet } from './StartingModelSheet';
import { sizeWords } from './words';

/**
 * O124: this build certifies a stronger starting model than the one in use, and this graphics card trains
 * it. Said once, plainly: Switch… asks first (its download), Dismiss hides it for good.
 */
export function StrongerModelNotice({
  io,
  model,
}: {
  io: LearningIO;
  model: LearningStartingModel;
}) {
  const overlay = useOverlay();
  const [open, setOpen] = useState(false);
  const dismiss = () =>
    void io.run('dismiss', { item_id: `stronger_model:${model.id}` }).then(
      () => undefined,
      (cause: unknown) => overlay.notify(clientError(cause).message, 'danger'),
    );
  return (
    <div
      className="learning-banner"
      role="status"
      aria-label="A stronger starting model"
    >
      <p>
        <strong>{`Your graphics card can now train ${model.name}, `}</strong>a
        stronger starting model. Your next versions would start from it; the
        ones you have stay as they are.
      </p>
      <span className="learning-actions-inline">
        <Button
          className="small"
          variant="primary"
          onClick={() => setOpen(true)}
        >
          {`Switch… (downloads ${sizeWords(model.download_bytes)})`}
        </Button>
        <Button
          className="small"
          variant="ghost"
          aria-label={`Don’t suggest ${model.name} again`}
          onClick={dismiss}
        >
          Dismiss
        </Button>
      </span>
      <StartingModelSheet
        open={open}
        onOpenChange={setOpen}
        io={io}
        offer={model}
      />
    </div>
  );
}
