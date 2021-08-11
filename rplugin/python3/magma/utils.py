from typing import Union, List, Set, Dict

from pynvim import Nvim
import ueberzug.lib.v0 as ueberzug


class MagmaException(Exception):
    pass


def nvimui(func):
    def inner(self, *args, **kwargs):
        try:
            func(self, *args, **kwargs)
        except MagmaException as err:
            self.nvim.err_write("[Magma] " + str(err) + "\n")

    return inner


class Canvas:
    ueberzug_canvas: ueberzug.Canvas

    identifiers: Dict[str, ueberzug.Placement]

    _visible: Set[str]
    _to_make_visible: Set[str]
    _to_make_invisible: Set[str]

    def __init__(self):
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
        identifier += f"-{x}-{y}-{width}-{height}"

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


class Position:
    bufno: int
    lineno: int
    colno: int

    def __init__(self, bufno: int, lineno: int, colno: int):
        self.bufno = bufno
        self.lineno = lineno
        self.colno = colno

    def __lt__(self, other: 'Position') -> bool:
        return (self.lineno, self.colno) < (other.lineno, other.colno)

    def __le__(self, other: 'Position') -> bool:
        return (self.lineno, self.colno) <= (other.lineno, other.colno)


class DynamicPosition(Position):
    nvim: Nvim
    extmark_namespace: int
    bufno: int

    extmark_id: int

    def __init__(self, nvim: Nvim, extmark_namespace: int, bufno: int, lineno: int, colno: int):
        self.nvim = nvim
        self.extmark_namespace = extmark_namespace

        self.bufno = bufno
        self.extmark_id = self.nvim.funcs.nvim_buf_set_extmark(self.bufno, extmark_namespace, lineno, colno, {})

    def __del__(self):
        self.nvim.funcs.nvim_buf_del_extmark(self.bufno, self.extmark_namespace, self.extmark_id)

    def _get_pos(self) -> List[int]:
        return self.nvim.funcs.nvim_buf_get_extmark_by_id(self.bufno, self.extmark_namespace, self.extmark_id, {})

    @property
    def lineno(self) -> int:
        return self._get_pos()[0]

    @property
    def colno(self) -> int:
        return self._get_pos()[1]


class Span:
    begin: Union[Position, DynamicPosition]
    end:   Union[Position, DynamicPosition]

    def __init__(self, begin: Union[Position, DynamicPosition], end: Union[Position, DynamicPosition]):
        self.begin = begin
        self.end = end

    def __contains__(self, pos: Union[Position, DynamicPosition]) -> bool:
        return self.begin <= pos and pos < self.end

    def get_text(self, nvim: Nvim) -> str:
        assert self.begin.bufno == self.end.bufno
        bufno = self.begin.bufno

        lines = nvim.funcs.nvim_buf_get_lines(bufno, self.begin.lineno, self.end.lineno+1, True)

        if len(lines) == 1:
            return lines[0][self.begin.colno:self.end.colno]
        else:
            return '\n'.join(
                [lines[0][self.begin.colno:]] +
                lines[1:-1] +
                [lines[-1][:self.end.colno]]
            )
