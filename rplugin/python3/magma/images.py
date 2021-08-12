from typing import Set, Dict
import os
from abc import ABC, abstractmethod

from magma.utils import MagmaException


class Canvas(ABC):
    @abstractmethod
    def __enter__(self, *_):
        pass

    @abstractmethod
    def __exit__(self, *_):
        pass

    @abstractmethod
    def present(self) -> None:
        pass

    @abstractmethod
    def clear(self) -> None:
        pass

    @abstractmethod
    def add_image(self, path: str, identifier: str, x: int, y: int, width: int, height: int) -> None:
        pass


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

    def __enter__(self, *args):
        return self.ueberzug_canvas.__enter__(*args)

    def __exit__(self, *args):
        if len(self.identifiers) > 0:
            return self.ueberzug_canvas.__exit__(*args)

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
    if name == "ueberzug":
        return UeberzugCanvas()
    else:
        raise MagmaException(f"Unknown image provider: '{name}'")
