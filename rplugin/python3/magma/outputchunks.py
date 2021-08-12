from typing import Optional, Tuple, List
from enum import Enum
from abc import ABC, abstractmethod
from math import floor
import re

from magma.images import Canvas


class OutputChunk(ABC):
    jupyter_data: Optional[dict] = None
    jupyter_metadata: Optional[dict] = None

    @abstractmethod
    def place(self, lineno: int, shape: Tuple[int, int, int, int], canvas: Canvas) -> str:
        pass


class TextOutputChunk(OutputChunk):
    text: str

    # Adapted from [https://stackoverflow.com/a/14693789/4803382]:
    ANSI_CODE_REGEX = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

    def __init__(self, text: str):
        self.text = text

    def _cleanup_text(self, text: str) -> str:
        # Adapted from [https://stackoverflow.com/a/14693789/4803382]:
        text = self.ANSI_CODE_REGEX.sub('', text)
        text = text.replace("\r\n", "\n")
        return text

    def place(self, *_) -> str:
        return self._cleanup_text(self.text)


class TextLnOutputChunk(TextOutputChunk):
    def __init__(self, text: str):
        super().__init__(text + "\n")


class BadOutputChunk(TextLnOutputChunk):
    def __init__(self, mimetypes: List[str]):
        super().__init__("<No usable MIMEtype! Received mimetypes %r>" % mimetypes)


class MimetypesOutputChunk(TextLnOutputChunk):
    def __init__(self, mimetypes: List[str]):
        super().__init__("[DEBUG] Received mimetypes: %r" % mimetypes)


class ErrorOutputChunk(TextLnOutputChunk):
    def __init__(self, name: str, message: str, traceback: List[str]):
        super().__init__("\n".join(
            [
                f"[Error] {name}: {message}",
                f"Traceback:",
            ]
                + traceback
        ))


class AbortedOutputChunk(TextLnOutputChunk):
    def __init__(self):
        super().__init__("<Kernel aborted with no error message.>")


class ImageOutputChunk(OutputChunk):
    def __init__(self, img_path: str, img_checksum: str, img_shape: Tuple[int, int]):
        self.img_path = img_path
        self.img_checksum = img_checksum
        self.img_width, self.img_height = img_shape

    def _get_char_pixelsize(self) -> Tuple[int, int]:
        import termios
        import fcntl
        import struct
        import os

        # FIXME: This is not really in Ueberzug's public API.
        #        We should move this function into this codebase.
        from ueberzug.process import get_pty_slave

        pty = get_pty_slave(os.getppid())
        assert pty is not None
        with open(pty) as fd_pty:
            farg = struct.pack("HHHH", 0, 0, 0, 0)
            fretint = fcntl.ioctl(fd_pty, termios.TIOCGWINSZ, farg)
            rows, cols, xpixels, ypixels = struct.unpack("HHHH", fretint)

            return max(1, xpixels//cols), max(1, ypixels//rows)

    def place(self, lineno: int, shape: Tuple[int, int, int, int], canvas: Canvas) -> str:
        x, y, w, h = shape

        xpixels, ypixels = self._get_char_pixelsize()

        max_nlines = max(0, (h-y)-lineno - 1)
        if ((self.img_width/xpixels)/(self.img_height/ypixels))*max_nlines <= w:
            nlines = max_nlines
        else:
            nlines = floor(((self.img_height/ypixels)/(self.img_width/xpixels))*w)
        nlines = min(nlines, self.img_height//ypixels)

        canvas.add_image(
            self.img_path,
            self.img_checksum,
            x=x,
            y=y + lineno + 1, # TODO: consider scroll in the display window
            width=w,
            height=nlines,
        )
        return "-\n"*nlines


class OutputStatus(Enum):
    HOLD         = 0
    RUNNING      = 1
    DONE = 2


class Output:
    execution_count: Optional[int]
    chunks: List[OutputChunk]
    status: OutputStatus
    success: bool
    old: bool

    _should_clear: bool

    def __init__(self, execution_count: Optional[int]):
        self.execution_count = execution_count
        self.status = OutputStatus.HOLD
        self.chunks = []
        self.success = True
        self.old = False

        self._should_clear = False


def to_outputchunk(alloc_file, data: dict, metadata: dict) -> OutputChunk:
    def _to_image_chunk(path: str) -> OutputChunk:
        import hashlib
        from PIL import Image
        import ueberzug as _

        pil_image = Image.open(path)
        return ImageOutputChunk(
            path,
            hashlib.md5(pil_image.tobytes()).hexdigest(),
            pil_image.size,
        )

    # Output chunk functions:
    def _from_image_png(imgdata) -> OutputChunk:
        import base64

        with alloc_file('png', 'wb') as (path, file):
            file.write(base64.b64decode(str(imgdata)))  # type: ignore
        return _to_image_chunk(path)

    def _from_image_svgxml(svg) -> OutputChunk:
        import cairosvg

        with alloc_file('png', 'wb') as (path, file):
            cairosvg.svg2png(svg, write_to=file)
        return _to_image_chunk(path)

    def _from_application_plotly(figure_json) -> OutputChunk:
        from plotly.io import from_json
        import json

        figure = from_json(json.dumps(figure_json)) # type: ignore

        with alloc_file('png', 'wb') as (path, file):
            figure.write_image(file, engine="kaleido")
        return _to_image_chunk(path)

    def _from_latex(tex) -> OutputChunk:
        from pnglatex import pnglatex

        with alloc_file('png', 'w') as (path, _):
            pass
        pnglatex(tex, path)
        return _to_image_chunk(path)

    def _from_plaintext(text) -> OutputChunk:
        return TextLnOutputChunk(text)

    OUTPUT_CHUNKS = {
        'image/png': _from_image_png,
        'image/svg+xml': _from_image_svgxml,
        'application/vnd.plotly.v1+json': _from_application_plotly,
        'text/latex': _from_latex,
        'text/plain': _from_plaintext,
    }

    chunk = None
    for mimetype, process_func in OUTPUT_CHUNKS.items():
        try:
            maybe_data = data.get(mimetype)
            if maybe_data is not None:
                chunk = process_func(maybe_data)
                break
        except ImportError:
            continue

    if chunk is None:
        chunk = BadOutputChunk(list(data.keys()))

    chunk.jupyter_data = data
    chunk.jupyter_metadata = metadata

    return chunk
