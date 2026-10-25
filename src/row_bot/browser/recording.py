"""Record a conversation's page in Row-Bot's managed browser to an MP4 (F2).

The browser's live picture (the same screencast the Browser pane shows) offers each new JPEG;
the recorder keeps only the latest, and an encoder thread writes that frame at a steady rate into
an H.264 MP4 through PyAV (installed with Row-Bot's
voice support; recording says plainly when it isn't there). Pages with a password, one-time
code or card field, and anything shown while the person has taken over the browser, are
recorded as a blank "hidden" frame, as the live preview hides them.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

FPS = 12
# Frames are scaled down so neither side is larger than this.
MAX_SIDE = 1280
# Fields whose contents a recording never shows (the same as the live preview's shield).
PROTECTED_FIELDS = (
    'input[type="password"], input[autocomplete="one-time-code"], '
    'input[autocomplete="cc-number"], input[autocomplete="cc-csc"]'
)
_HIDDEN_COLOUR = (38, 40, 46)


def encoder_problem() -> str:
    """Why this copy of Row-Bot can't encode a recording, in plain words; empty when it can."""

    try:
        import av

        av.codec.Codec("libx264", "w")
    except Exception:
        return (
            "Recording needs Row-Bot's video support, which isn't installed in this copy "
            "(it comes with the full install)."
        )
    return ""


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


class PageRecorder:
    """One recording of one conversation's page.

    The browser offers frames (``offer_frame``) and sets ``shielded``; the encoder runs on its
    own thread and calls ``on_end(reason, path)`` once when it stops.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_seconds: float,
        size_limit: int,
        on_end: Callable[[str, Path | None], None],
        clock: Callable[[], float] = time.monotonic,
        tick: float = 1.0 / FPS,
    ) -> None:
        self.path = Path(path)
        self._tick = float(tick)
        self.max_seconds = float(max_seconds)
        self.size_limit = int(size_limit)
        self._on_end = on_end
        self._clock = clock
        self._lock = threading.Lock()
        self._frame: bytes | None = None
        self._stop = threading.Event()
        self._reason = ""
        self.shielded = False
        # The browser's live-picture subscription that feeds this recording.
        self.watch: Any = None
        self.frames = 0
        self.bytes_written = 0
        self.started_at = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name="row-bot-browser-recording")

    # ---- Control ----------------------------------------------------------------------

    def start(self) -> None:
        self.started_at = self._clock()
        self._thread.start()

    def request_stop(self, reason: str) -> None:
        if not self._reason:
            self._reason = str(reason or "agent")
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def join(self, timeout: float | None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def offer_frame(self, data: bytes) -> None:
        """The page's latest picture (a JPEG)."""

        with self._lock:
            self._frame = bytes(data)

    # ---- Encoder thread ---------------------------------------------------------------

    def _run(self) -> None:
        reason, saved = "error", None
        try:
            reason, saved = self._encode()
        except Exception:
            logger.warning("The browser recording could not be encoded", exc_info=True)
            reason, saved = "error", None
        finally:
            try:
                self._on_end(reason, saved)
            except Exception:
                logger.warning("A finished browser recording could not be saved", exc_info=True)

    def _encode(self) -> tuple[str, Path | None]:
        import av
        from PIL import Image, ImageOps

        container = None
        stream = None
        size: tuple[int, int] | None = None
        last_data: bytes | None = None
        last_shielded = False
        last_frame = None
        index = 0
        reason = ""
        try:
            while True:
                if self._stop.is_set():
                    reason = self._reason or "agent"
                    break
                elapsed = self._clock() - self.started_at
                if elapsed >= self.max_seconds:
                    reason = "limit"
                    break
                if self.bytes_written >= self.size_limit:
                    reason = "size"
                    break
                with self._lock:
                    data = self._frame
                if data is not None:
                    if container is None:
                        first = Image.open(io.BytesIO(data))
                        scale = min(1.0, MAX_SIDE / max(first.size))
                        size = (_even(first.size[0] * scale), _even(first.size[1] * scale))
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        container = av.open(str(self.path), mode="w", format="mp4")
                        stream = container.add_stream("libx264", rate=FPS)
                        stream.width, stream.height = size
                        stream.pix_fmt = "yuv420p"
                        stream.options = {"preset": "veryfast", "crf": "28"}
                    shielded = self.shielded
                    if last_frame is None or data is not last_data or shielded != last_shielded:
                        if shielded:
                            image = Image.new("RGB", size, _HIDDEN_COLOUR)
                        else:
                            image = ImageOps.pad(Image.open(io.BytesIO(data)).convert("RGB"), size,
                                                 color=(0, 0, 0))
                        last_frame = av.VideoFrame.from_image(image)
                        last_data, last_shielded = data, shielded
                    # Keep real time: write the latest picture for every frame due so far.
                    due = int(elapsed * FPS)
                    caught_up = 0
                    while index <= due and caught_up < FPS * 2:
                        last_frame.pts = index
                        for packet in stream.encode(last_frame):
                            self.bytes_written += packet.size
                            container.mux(packet)
                        index += 1
                        caught_up += 1
                    self.frames = index
                self._stop.wait(self._tick)
        finally:
            if container is not None and stream is not None:
                for packet in stream.encode(None):
                    container.mux(packet)
                container.close()
        if container is None or index == 0:
            return reason or "agent", None
        return reason, self.path
