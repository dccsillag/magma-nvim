from typing import Set, Dict
import os
from abc import ABC, abstractmethod
import time

from pynvim import Nvim

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

        This is called only when a redraw is necessary -- so, if desired, it
        can be implemented so that `clear` and `add_image` only queue images as
        to be drawn, and `present` actually performs the operaetions, in order
        to reduce flickering.
        """

    @abstractmethod
    def clear(self) -> None:
        """
        Clear all images from the canvas.
        """

    @abstractmethod
    def add_image(
        self,
        path: str,
        identifier: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
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


class NoCanvas(Canvas):
    def __init__(self) -> None:
        pass

    def init(self) -> None:
        pass

    def deinit(self) -> None:
        pass

    def present(self) -> None:
        pass

    def clear(self) -> None:
        pass

    def add_image(
        self,
        path: str,
        identifier: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        pass


class UeberzugCanvas(Canvas):
    ueberzug_canvas: "ueberzug.Canvas"  # type: ignore

    identifiers: Dict[str, "ueberzug.Placement"]  # type: ignore

    _visible: Set[str]
    _to_make_visible: Set[str]
    _to_make_invisible: Set[str]

    def __init__(self) -> None:
        import ueberzug.lib.v0 as ueberzug

        self.ueberzug_canvas = ueberzug.Canvas()
        self.identifiers = {}

        self._visible = set()
        self._to_make_visible = set()
        self._to_make_invisible = set()

    def init(self) -> None:
        self.ueberzug_canvas.__enter__()

    def deinit(self) -> None:
        if len(self.identifiers) > 0:
            self.ueberzug_canvas.__exit__()

    def present(self) -> None:
        import ueberzug.lib.v0 as ueberzug

        self._to_make_invisible.difference_update(self._to_make_visible)
        for identifier in self._to_make_invisible:
            self.identifiers[
                identifier
            ].visibility = ueberzug.Visibility.INVISIBLE
        for identifier in self._to_make_visible:
            self.identifiers[
                identifier
            ].visibility = ueberzug.Visibility.VISIBLE
            self._visible.add(identifier)
        self._to_make_invisible.clear()
        self._to_make_visible.clear()

    def clear(self) -> None:
        for identifier in self._visible:
            self._to_make_invisible.add(identifier)
        self._visible.clear()

    def add_image(
        self,
        path: str,
        identifier: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
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


class KittyImage:
    # Adapted from https://sw.kovidgoyal.net/kitty/graphics-protocol/

    def __init__(
        self,
        id: int,
        path: str,
        row: int,
        col: int,
        width: int,
        height: int,
        nvim: Nvim,
    ):
        self.id = id
        self.path = path
        self.row = row
        self.col = col
        self.width = width
        self.height = height
        self.nvim = nvim

    def serialize_gr_command(self, **cmd):  # type: ignore
        payload = cmd.pop("payload", None)
        cmd = ",".join("{}={}".format(k, v) for k, v in cmd.items())  # type: ignore
        ans = []  # type: ignore
        w = ans.append
        w(b"\033_G"), w(cmd.encode("ascii"))  # type: ignore
        if payload:
            w(b";")
            w(payload)
        w(b"\033\\")
        ans = b"".join(ans)  # type: ignore
        if "tmux" in os.environ["TERM"]:
            ans = b"\033Ptmux;" + ans.replace(b"\033", b"\033\033") + b"\033\\"  # type: ignore
        return ans

    def write_chunked(self, **cmd):  # type: ignore
        from base64 import standard_b64encode

        data = standard_b64encode(cmd.pop("data"))
        while data:
            chunk, data = data[:4096], data[4096:]
            m = 1 if data else 0
            self.nvim.lua.stdout.write(
                self.serialize_gr_command(payload=chunk, m=m, **cmd)  # type: ignore
            )
            cmd.clear()

    def show(self) -> None:
        with open(self.path, "rb") as f:
            self.write_chunked(  # type: ignore
                a="T",  # transmit directly to the terminal
                i=self.id,
                f=100,  # for now, only png
                v=self.height,
                s=self.width,
                C=1,
                z=10,
                q=2,
                data=f.read(),
            )

    def hide(self) -> None:
        self.nvim.lua.stdout.write(
            self.serialize_gr_command(  # type: ignore
                i=self.id,
                a="d",  # remove image
                q=2,
            )
        )


class Kitty(Canvas):
    nvim: Nvim
    images: Dict[str, KittyImage]
    to_make_visible: Set[str]
    to_make_invisible: Set[str]
    visible: Set[str]
    next_id: int

    def __init__(self, nvim: Nvim):
        self.nvim = nvim
        self.images = {}
        self.visible = set()
        self.to_make_visible = set()
        self.to_make_invisible = set()
        self.next_id = 0
        nvim.exec_lua(
            """
            local fd = vim.loop.new_pipe(false)
            fd:open(1)
            local function write(data)
                    fd:write(data)
            end

            stdout = {write = write}
        """
        )

    def init(self) -> None:
        pass

    def deinit(self) -> None:
        pass

    def present(self) -> None:
        # images to both show and hide should be ignored
        to_work_on = self.to_make_visible.difference(
            self.to_make_visible.intersection(self.to_make_invisible)
        )
        self.to_make_invisible.difference_update(self.to_make_visible)
        for identifier in self.to_make_invisible:

            def hide_fn(image: KittyImage) -> None:
                image.hide()
                # we need the sleep here, otherwise the escape codes might
                # `spill` over into the buffer when doing
                # several rapid operations consectively
                time.sleep(0.01)

            self.nvim.async_call(hide_fn, self.images[identifier])
        for identifier in to_work_on:
            image = self.images[identifier]

            def fn(nvim: Nvim, image: KittyImage) -> None:
                eventignore_save = nvim.options["eventignore"]
                nvim.options["eventignore"] = "all"

                org_position = nvim.current.window.cursor
                # We need to move the cursor to the place where we want to
                # place the image.
                # We need to make sure we are still in the buffer.
                nvim.current.window.cursor = (
                    min(image.row + 1, len(nvim.current.buffer)),
                    image.col,
                )
                image.show()
                time.sleep(0.01)

                nvim.current.window.cursor = org_position
                nvim.options["eventignore"] = eventignore_save

            self.nvim.async_call(fn, self.nvim, image)
        self.visible.update(self.to_make_visible)
        self.to_make_invisible.clear()
        self.to_make_visible.clear()

    def clear(self) -> None:
        for identifier in self.visible:
            self.to_make_invisible.add(identifier)
        self.visible.clear()

    def add_image(
        self,
        path: str,
        identifier: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        if identifier not in self.images:
            self.images[identifier] = KittyImage(
                id=self.next_id,
                path=path,
                row=y,
                col=x,
                width=width,
                height=height,
                nvim=self.nvim,
            )
            self.next_id += 1
        else:
            self.images[identifier].path = path
        self.to_make_visible.add(identifier)


def get_canvas_given_provider(name: str, nvim: Nvim) -> Canvas:
    if name == "none":
        return NoCanvas()
    elif name == "ueberzug":
        return UeberzugCanvas()
    elif name == "kitty":
        return Kitty(nvim)
    else:
        raise MagmaException(f"Unknown image provider: '{name}'")
