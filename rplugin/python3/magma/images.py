import math
from typing import Set
from abc import ABC, abstractmethod

from pynvim import Nvim, logging

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
    def img_height(self, identifier: str) -> int:
        """
        Get the height of an image in terminal rows.
        """

    @abstractmethod
    def add_image(
        self,
        path: str,
        identifier: str,
        x: int,
        y: int,
        bufnr: int,
    ) -> str:
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
        - bufnr: int
          The buffer number for the buffer in which to draw the image.

        Returns:
        str the identifier for the image
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

    def img_height(self, _indentifier: str) -> int:
        return 0

    def add_image(
        self,
        _path: str,
        _identifier: str,
        _x: int,
        _y: int,
        _window: int,
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
        self.nvim.exec_lua("_image = require('load_image_nvim').image_api")
        self.nvim.exec_lua(
            "_image_utils = require('load_image_nvim').image_utils"
        )
        self.image_api = self.nvim.lua._image
        self.image_utils = self.nvim.lua._image_utils

    def deinit(self) -> None:
        self.image_api.clear_all()
        self.images.clear()

    def present(self) -> None:
        # images to both show and hide should be ignored
        to_work_on = self.to_make_visible.difference(
            self.to_make_visible.intersection(self.to_make_invisible)
        )
        self.to_make_invisible.difference_update(self.to_make_visible)
        for identifier in self.to_make_invisible:
            self.image_api.clear(identifier)

        for identifier in to_work_on:
            self.image_api.render(identifier)

        self.visible.update(self.to_make_visible)
        self.to_make_invisible.clear()
        self.to_make_visible.clear()

    def clear(self) -> None:
        for img in self.visible:
            self.image_api.clear(img)

    def img_height(self, identifier: str) -> int:
        img_size_px = self.image_api.image_size(identifier)
        cell_size_px = self.image_utils.cell_size()
        return math.ceil(img_size_px["height"] / cell_size_px["height"])

    def add_image(
        self,
        path: str,
        identifier: str,
        x: int,
        y: int,
        bufnr: int,
    ) -> str:
        if path not in self.images:
            img = self.image_api.from_file(
                path,
                {
                    "id": identifier,
                    "buffer": bufnr,
                    "with_virtual_padding": True,
                    "x": x,
                    "y": y,
                },
            )
            self.to_make_visible.add(img)
            return img
        return path


def get_canvas_given_provider(name: str, nvim: Nvim) -> Canvas:
    if name == "none":
        return NoCanvas()
    elif name == "image.nvim":
        return ImageNvimCanvas(nvim)
    else:
        nvim.api.notify(
            f"[Magma] unknown image provider: `{name}`",
            logging.ERROR,
            {"title": "Magma"},
        )
        return NoCanvas()
