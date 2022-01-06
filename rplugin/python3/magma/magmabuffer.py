from typing import Optional, Dict
from queue import Queue
import hashlib

import pynvim
from pynvim import Nvim
from pynvim.api import Buffer

from magma.options      import MagmaOptions
from magma.images       import Canvas
from magma.utils        import MagmaException, Position, Span
from magma.outputchunks import Output, OutputStatus
from magma.runtime      import JupyterRuntime


class MagmaBuffer:
    nvim: Nvim
    canvas: Canvas
    highlight_namespace: int
    extmark_namespace: int
    buffer: Buffer

    runtime: JupyterRuntime

    outputs: Dict[Span, Output]
    current_output: Optional[Output]
    queued_outputs: 'Queue[Output]'

    display_buffer: Buffer
    display_window: Optional[int]
    selected_cell: Optional[Span]
    should_open_display_window: bool
    updating_interface: bool

    options: MagmaOptions

    def __init__(self,
                 nvim: Nvim,
                 canvas: Canvas,
                 highlight_namespace: int,
                 extmark_namespace: int,
                 buffer: Buffer,
                 options: MagmaOptions,
                 kernel_name: str):
        self.nvim = nvim
        self.canvas = canvas
        self.highlight_namespace = highlight_namespace
        self.extmark_namespace = extmark_namespace
        self.buffer = buffer

        self._doautocmd('MagmaInitPre')

        self.runtime = JupyterRuntime(kernel_name, options)

        self.outputs = {}
        self.current_output = None
        self.queued_outputs = Queue()

        self.display_buffer = self.nvim.buffers[self.nvim.funcs.nvim_create_buf(False, True)]
        self.display_window = None
        self.selected_cell = None
        self.should_open_display_window = False
        self.updating_interface = False

        self.options = options

        self._doautocmd('MagmaInitPost')

    def _doautocmd(self, autocmd: str) -> None:
        assert ' ' not in autocmd
        self.nvim.command(f"doautocmd User {autocmd}")

    def deinit(self):
        self._doautocmd('MagmaDeinitPre')
        self.runtime.deinit()
        self._doautocmd('MagmaDeinitPost')

    def interrupt(self) -> None:
        self.runtime.interrupt()

    def restart(self, delete_outputs: bool=False) -> None:
        self.runtime.restart()
        if delete_outputs:
            self.outputs = {}

    def _buffer_to_window_lineno(self, lineno: int) -> int:
        win_top = self.nvim.funcs.line('w0')
        return lineno - win_top + 1

    def run_code(self, code: str, span: Span) -> None:
        new_output = self.runtime.run_code(code)
        if span in self.outputs:
            del self.outputs[span]
        self.outputs[span] = new_output

        self.queued_outputs.put(new_output)

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
        is_idle = self.current_output is None or \
            (self.current_output is not None and self.current_output.status == OutputStatus.DONE)
        if is_idle and not self.queued_outputs.empty():
            output = self.queued_outputs.get_nowait()
            self.current_output = output

    def tick(self):
        self._check_if_done_running()

        was_ready = self.runtime.is_ready()
        did_stuff = self.runtime.tick(self.current_output)
        if did_stuff:
            self.update_interface()
        if not was_ready and self.runtime.is_ready():
            self.nvim.api.notify(
                "Kernel '%s' is ready." % self.runtime.kernel_name,
                pynvim.logging.INFO,
                {'title': "Magma"},
            )

    def _get_header_text(self, output: Output) -> str:
        if output.execution_count is None:
            execution_count = '...'
        else:
            execution_count = str(output.execution_count)

        if output.status == OutputStatus.HOLD:
            status = '* On Hold'
        elif output.status == OutputStatus.DONE:
            if output.success:
                status = '✓ Done'
            else:
                status = '✗ Failed'
        elif output.status == OutputStatus.RUNNING:
            status = '... Running'
        else:
            raise ValueError('bad output.status: %s' % output.status)

        if output.old:
            old = "[OLD] "
        else:
            old = ""

        return f"{old}Out[{execution_count}]: {status}"

    def _show_outputs(self, output: Output, anchor: Position):
        # Get width&height, etc
        win_col = self.nvim.current.window.col
        win_row = self._buffer_to_window_lineno(anchor.lineno+1)
        win_width  = self.nvim.current.window.width
        win_height = self.nvim.current.window.height

        # Clear buffer:
        self.nvim.funcs.deletebufline(self.display_buffer.number, 1, '$')
        # Add output chunks to buffer
        lines = ""
        lineno = 0
        shape = (win_col, win_row, win_width, win_height)
        if len(output.chunks) > 0:
            for chunk in output.chunks:
                chunktext = chunk.place(self.options, lineno, shape, self.canvas)
                lines += chunktext
                lineno += chunktext.count("\n")
            lines = lines.rstrip().split("\n")
        self.display_buffer[0] = self._get_header_text(output)
        self.display_buffer.append(lines)

        # Open output window
        assert self.display_window is None
        if win_row < win_height:
            self.display_window = self.nvim.funcs.nvim_open_win(
                self.display_buffer.number,
                False,
                {
                    'relative': 'win',
                    'col': 0,
                    'row': win_row,
                    'width': win_width,
                    'height': min(win_height - win_row, lineno+1),
                    'anchor': 'NW',
                    'style': 'minimal',
                    'focusable': False,
                }
            )
            # self.nvim.funcs.nvim_win_set_option(self.display_window, "wrap", True)

    def _get_cursor_position(self) -> Position:
        _, lineno, colno, _, _ = self.nvim.funcs.getcurpos()
        return Position(self.nvim.current.buffer.number, lineno-1, colno-1)

    def clear_interface(self) -> None:
        if self.updating_interface:
            return

        self.nvim.funcs.nvim_buf_clear_namespace(
            self.buffer.number,
            self.highlight_namespace,
            0,
            -1,
        )
        if self.display_window is not None: # and self.nvim.funcs.winbufnr(self.display_window) != -1:
            self.nvim.funcs.nvim_win_close(self.display_window, True)
            self.canvas.clear()
            self.display_window = None

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
            for lineno in range(span.begin.lineno+1, span.end.lineno):
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
            self._show_outputs(self.outputs[span], span.end)

    def _get_content_checksum(self) -> str:
        return hashlib.md5(
            "\n".join(self.nvim.current.buffer.api.get_lines(0, -1, True))
            .encode("utf-8")
        ).hexdigest()
