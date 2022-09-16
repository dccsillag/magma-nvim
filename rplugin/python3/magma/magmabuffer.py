from typing import Optional, Dict
from queue import Queue
import hashlib

import pynvim
from pynvim import Nvim
from pynvim.api import Buffer

from magma.options import MagmaOptions
from magma.images import Canvas
from magma.utils import MagmaException, Position, Span
from magma.outputbuffer import OutputBuffer
from magma.outputchunks import OutputStatus
from magma.runtime import JupyterRuntime


class MagmaBuffer:
    nvim: Nvim
    canvas: Canvas
    highlight_namespace: int
    extmark_namespace: int
    buffer: Buffer

    runtime: JupyterRuntime

    outputs: Dict[Span, OutputBuffer]
    current_output: Optional[Span]
    queued_outputs: "Queue[Span]"

    selected_cell: Optional[Span]
    should_open_display_window: bool
    updating_interface: bool

    options: MagmaOptions

    def __init__(
        self,
        nvim: Nvim,
        canvas: Canvas,
        highlight_namespace: int,
        extmark_namespace: int,
        buffer: Buffer,
        options: MagmaOptions,
        kernel_name: str,
    ):
        self.nvim = nvim
        self.canvas = canvas
        self.highlight_namespace = highlight_namespace
        self.extmark_namespace = extmark_namespace
        self.buffer = buffer

        self._doautocmd("MagmaInitPre")

        self.runtime = JupyterRuntime(kernel_name, options)

        self.outputs = {}
        self.current_output = None
        self.queued_outputs = Queue()

        self.selected_cell = None
        self.should_open_display_window = False
        self.updating_interface = False

        self.options = options

        self._doautocmd("MagmaInitPost")

    def _doautocmd(self, autocmd: str) -> None:
        assert " " not in autocmd
        self.nvim.command(f"doautocmd User {autocmd}")

    def deinit(self) -> None:
        self._doautocmd("MagmaDeinitPre")
        self.runtime.deinit()
        self._doautocmd("MagmaDeinitPost")

    def interrupt(self) -> None:
        self.runtime.interrupt()

    def restart(self, delete_outputs: bool = False) -> None:
        if delete_outputs:
            self.outputs = {}
            self.clear_interface()

        self.runtime.restart()

    def run_code(self, code: str, span: Span) -> None:
        self.runtime.run_code(code)
        if span in self.outputs:
            del self.outputs[span]
        self.outputs[span] = OutputBuffer(self.nvim, self.canvas, self.options)
        self.queued_outputs.put(span)

        self.selected_cell = span
        self.should_open_display_window = True
        self.update_interface()

        self._check_if_done_running()

    def reevaluate_cell(self) -> None:
        self.selected_cell = self._get_selected_span()
        if self.selected_cell is None:
            raise MagmaException("Not in a cell")

        code = self.selected_cell.get_text(self.nvim)

        self.run_code(code, self.selected_cell)

    def _check_if_done_running(self) -> None:
        # TODO: refactor
        is_idle = ( self.current_output is None
            or not self.current_output in self.outputs
        ) or ( self.current_output is not None
            and self.outputs[self.current_output].output.status
            == OutputStatus.DONE
        )
        if is_idle and not self.queued_outputs.empty():
            key = self.queued_outputs.get_nowait()
            self.current_output = key

    def tick(self) -> None:
        self._check_if_done_running()

        was_ready = self.runtime.is_ready()
        if self.current_output is None or not self.current_output in self.outputs:
            did_stuff = self.runtime.tick(None)
        else:
            did_stuff = self.runtime.tick(
                self.outputs[self.current_output].output
            )
        if did_stuff:
            self.update_interface()
        if not was_ready and self.runtime.is_ready():
            self.nvim.api.notify(
                "Kernel '%s' is ready." % self.runtime.kernel_name,
                pynvim.logging.INFO,
                {"title": "Magma"},
            )

    def enter_output(self) -> None:
        if self.selected_cell is not None:
            self.outputs[self.selected_cell].enter()

    def _get_cursor_position(self) -> Position:
        _, lineno, colno, _, _ = self.nvim.funcs.getcurpos()
        return Position(self.nvim.current.buffer.number, lineno - 1, colno - 1)

    def clear_interface(self) -> None:
        if self.updating_interface:
            return

        self.nvim.funcs.nvim_buf_clear_namespace(
            self.buffer.number,
            self.highlight_namespace,
            0,
            -1,
        )
        # and self.nvim.funcs.winbufnr(self.display_window) != -1:
        if self.selected_cell is not None and self.selected_cell in self.outputs:
            self.outputs[self.selected_cell].clear_interface()
        self.canvas.clear()

    def _get_selected_span(self) -> Optional[Span]:
        current_position = self._get_cursor_position()
        selected = None
        for span in reversed(self.outputs.keys()):
            if current_position in span:
                selected = span
                break

        return selected

    def delete_cell(self) -> None:
        self.selected_cell = self._get_selected_span()
        if self.selected_cell is None:
            return

        del self.outputs[self.selected_cell]

        self.update_interface()

    def update_interface(self) -> None:
        if self.buffer.number != self.nvim.current.buffer.number:
            return
        if self.buffer.number != self.nvim.current.window.buffer.number:
            return

        self.clear_interface()

        self.updating_interface = True

        selected_cell = self._get_selected_span()

        if self.options.automatically_open_output:
            self.should_open_display_window = True
        else:
            if self.selected_cell != selected_cell:
                self.should_open_display_window = False

        self.selected_cell = selected_cell

        if self.selected_cell is not None:
            self._show_selected(self.selected_cell)
        self.canvas.present()

        self.updating_interface = False

    def _show_selected(self, span: Span) -> None:
        if span.begin.lineno == span.end.lineno:
            self.nvim.funcs.nvim_buf_add_highlight(
                self.buffer.number,
                self.highlight_namespace,
                self.options.cell_highlight_group,
                span.begin.lineno,
                span.begin.colno,
                span.end.colno,
            )
        else:
            self.nvim.funcs.nvim_buf_add_highlight(
                self.buffer.number,
                self.highlight_namespace,
                self.options.cell_highlight_group,
                span.begin.lineno,
                span.begin.colno,
                -1,
            )
            for lineno in range(span.begin.lineno + 1, span.end.lineno):
                self.nvim.funcs.nvim_buf_add_highlight(
                    self.buffer.number,
                    self.highlight_namespace,
                    self.options.cell_highlight_group,
                    lineno,
                    0,
                    -1,
                )
            self.nvim.funcs.nvim_buf_add_highlight(
                self.buffer.number,
                self.highlight_namespace,
                self.options.cell_highlight_group,
                span.end.lineno,
                0,
                span.end.colno,
            )

        if self.should_open_display_window:
            self.outputs[span].show(span.end)

    def _get_content_checksum(self) -> str:
        return hashlib.md5(
            "\n".join(
                self.nvim.current.buffer.api.get_lines(0, -1, True)
            ).encode("utf-8")
        ).hexdigest()
