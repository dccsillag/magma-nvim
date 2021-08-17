from typing import Set, Dict
import os
from abc import ABC, abstractmethod

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


def get_canvas_given_provider(name: str) -> Canvas:
    """
    Return a canvas object given its provider's name.
    """

    if name == "ueberzug":
        return UeberzugCanvas()
    else:
        raise MagmaException(f"Unknown image provider: '{name}'")
