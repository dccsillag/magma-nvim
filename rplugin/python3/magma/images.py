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
        to be drawn, and `present` actually performs the operations, in order
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

# I think this class will end up being calls to equivalent lua functions in some lua file
# somewhere
class ImageNvimCanvas(Canvas):
    nvim: Nvim
    to_make_visible: Set[str]
    to_make_invisible: Set[str]
    visible: Set[str]

    def __init__(self, nvim: Nvim):
        self.nvim = nvim
        self.images = {}
        self.visible = set()
        self.to_make_visible = set()
        self.to_make_invisible = set()
        self.next_id = 0

    def init(self) -> None:
        # TODO:
        # there's a better way to call lua functions, I think that I have to put those lua functions
        # in a different spot tho, like a special folder
        self.nvim.exec_lua("""
            local ok, image pcall(require, 'image')
            Image = {}
            if not ok then
                nvim.err_writeln('[magme-nvim]: image.nvim was not found')
            else
                Image.api = image
                Image.images = {}
            end
        """)

    def deinit(self) -> None:
        self.nvim.exec_lua("""
            for key, img in ipairs(Image.images) do
                img:clear()
            end
        """)

    def present(self) -> None:
        # images to both show and hide should be ignored
        to_work_on = self.to_make_visible.difference(
            self.to_make_visible.intersection(self.to_make_invisible)
        )
        self.to_make_invisible.difference_update(self.to_make_visible)
        for identifier in self.to_make_invisible:
            # TODO: hide image
            # self.nvim.async_call(hide_fn, self.images[identifier])
            pass

        for identifier in to_work_on:
            # TODO: move or show
            # image = self.images[identifier]
            # self.nvim.async_call(fn, self.nvim, image)
            pass

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

        self.nvim.exec_lua(f"""
        Image.images[{identifier}]= api.from_file("https://gist.ro/s/remote.png", {{
          id = "{identifier}", -- optional, defaults to a random string
          window = 1000, -- optional, binds image to a window and its bounds
          buffer = 1000, -- optional, binds image to a buffer (paired with window binding)
          with_virtual_padding = true, -- optional, pads vertically with extmarks
        }})
        """)
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
    elif name == "image.nvim":
        return ImageNvimCanvas(nvim)
    else:
        raise MagmaException(f"Unknown image provider: '{name}'")
