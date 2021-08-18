from typing import Set, Dict, List, Any
import os
from abc import ABC, abstractmethod
from pynvim import Nvim
import time
from magma.utils import MagmaException


class Canvas(ABC):
    @abstractmethod
    def init(self) -> None:
        """
        Initialize the canvas.

        This will be called before the canvas is ever used.
        """

    @abstractmethod
    def deinit(self) -> None:
        """
        Deinitialize the canvas.

        The canvas will not be used after this operation.
        """

    @abstractmethod
    def present(self) -> None:
        """
        Present the canvas.

        This is called only when a redraw is necessary -- so, if desired, it can
        be implemented so that `clear` and `add_image` only queue images as to
        be drawn, and `present` actually performs the operaetions, in order to
        reduce flickering.
        """

    @abstractmethod
    def clear(self) -> None:
        """
        Clear all images from the canvas.
        """

    @abstractmethod
    def add_image(self, path: str, identifier: str, x: int, y: int, width: int, height: int) -> None:
        """
        Add an image to the canvas.

        Parameters
        - path: str
          Path to the image we want to show
        - identifier: str
          A string which identifies this image (it is a checksum of of its
          data). It is given for convenience for methods which require
          identifiers, but can be safely ignored.
        - x: int
          Column number of where the image is supposed to be drawn at (top-left
          corner).
        - y: int
          Row number of where the image is supposed to be drawn at (top-right
          corner).
        - width: int
          The desired width for the image, in terminal columns.
        - height: int
          The desired height for the image, in terminal rows.
        """


class UeberzugCanvas(Canvas):
    ueberzug_canvas: 'ueberzug.Canvas' # type: ignore

    identifiers: Dict[str, 'ueberzug.Placement'] # type: ignore

    _visible: Set[str]
    _to_make_visible: Set[str]
    _to_make_invisible: Set[str]

    def __init__(self):
        import ueberzug.lib.v0 as ueberzug

        self.ueberzug_canvas = ueberzug.Canvas()
        self.identifiers = {}

        self._visible           = set()
        self._to_make_visible   = set()
        self._to_make_invisible = set()

    def init(self):
        return self.ueberzug_canvas.__enter__()

    def deinit(self):
        if len(self.identifiers) > 0:
            return self.ueberzug_canvas.__exit__()

    def present(self) -> None:
        import ueberzug.lib.v0 as ueberzug

        self._to_make_invisible.difference_update(self._to_make_visible)
        for identifier in self._to_make_invisible:
            self.identifiers[identifier].visibility = ueberzug.Visibility.INVISIBLE
        for identifier in self._to_make_visible:
            self.identifiers[identifier].visibility = ueberzug.Visibility.VISIBLE
            self._visible.add(identifier)
        self._to_make_invisible.clear()
        self._to_make_visible.clear()

    def clear(self):
        for identifier in self._visible:
            self._to_make_invisible.add(identifier)
        self._visible.clear()

    def add_image(self, path: str, identifier: str, x: int, y: int, width: int, height: int):
        import ueberzug.lib.v0 as ueberzug

        if width > 0 and height > 0:
            identifier += f"-{os.getpid()}-{x}-{y}-{width}-{height}"

            if identifier in self.identifiers:
                img = self.identifiers[identifier]
            else:
                img = self.ueberzug_canvas.create_placement(
                    identifier,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    scaler=ueberzug.ScalerOption.FIT_CONTAIN.value,
                )
                self.identifiers[identifier] = img
            img.path = path

            self._to_make_visible.add(identifier)


# see https://sw.kovidgoyal.net/kitty/graphics-protocol/
class KittyImage:
    def __init__(self, id: int, path: str, row: int, col: int, width: int, height: int, nvim: Nvim):
        self.id = id
        self.path = path
        self.row = row
        self.col = col
        self.width = width
        self.height = height
        self.nvim = nvim

    def serialize_gr_command(self, **cmd):
        payload = cmd.pop('payload', None)
        cmd = ','.join('{}={}'.format(k, v) for k, v in cmd.items())
        ans = []
        w = ans.append
        w(b'\033_G'), w(cmd.encode('ascii'))
        if payload:
            w(b';')
            w(payload)
        w(b'\033\\')
        return b''.join(ans)


    def write_chunked(self, **cmd):
        from base64 import standard_b64encode

        data = standard_b64encode(cmd.pop('data'))
        while data:
            chunk, data = data[:4096], data[4096:]
            m = 1 if data else 0
            # import sys
            # sys.stdout.buffer.write(
            self.nvim.lua.stdout.write(
                self.serialize_gr_command(
                    payload=chunk,
                    m=m,
                    **cmd
                )
            )
            # sys.stdout.flush()
            cmd.clear()


    def show(self):
        with open(self.path, 'rb') as f:
            self.write_chunked(
                a='T', # transmit directly to the terminal
                i=self.id,
                f=100, # for now, only png
                v=self.height,
                s=self.width,
                X=self.col,
                Y=self.row,
                C=1,
                z=10,
                data=f.read(),
            )

    def hide(self):
        self.nvim.lua.stdout.write(
            self.serialize_gr_command(
                i=self.id,
                a='d', # remove image
            )
        )


class Kitty(Canvas):
    nvim: Nvim
    images: Dict[str, KittyImage]
    visible: Set[KittyImage]
    to_show: Set[KittyImage]

    def __init__(self, nvim):
        self.nvim = nvim
        self.images = {}
        self.visible = set()
        self.to_show = set()
        nvim.exec_lua("""
            local fd = vim.loop.new_pipe(false)
            fd:open(1)
            local function write(data)
                    fd:write(data)
            end

            stdout = {write = write}
            """)

    def init(self):
        return self

    def deinit(self):
        return

    def present(self) -> None:
        for image in self.to_show:
            image.show()
            time.sleep(0.01)
        self.visible.update(self.to_show)
        self.to_show = set()

    def clear(self):
        for image in self.visible:
            image.hide()
        self.visible = set()

    def add_image(self, path: str, identifier: str, x: int, y: int, width: int, height: int):
        identifier += f"-{os.getpid()}-{x}-{y}-{width}-{height}"
        if identifier not in self.images:
            self.images[identifier] = KittyImage(
                id=len(self.images),
                path=path,
                row=y,
                col=x,
                width=width,
                height=height,
                nvim=self.nvim,
            )
        self.to_show.add(self.images[identifier])


def get_canvas_given_provider(name: str, nvim: Nvim) -> Canvas:
    if name == "ueberzug":
        return UeberzugCanvas()
    elif name == "kitty":
        return Kitty(nvim)
    else:
        raise MagmaException(f"Unknown image provider: '{name}'")
