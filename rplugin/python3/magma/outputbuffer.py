from typing import Optional

from pynvim import Nvim
from pynvim.api import Buffer

from magma.images import Canvas
from magma.outputchunks import Output, OutputStatus
from magma.options import MagmaOptions
from magma.utils import Position


class OutputBuffer:
    nvim: Nvim
    canvas: Canvas

    output: Output

    display_buffer: Buffer
    display_window: Optional[int]

    options: MagmaOptions

    def __init__(self, nvim: Nvim, canvas: Canvas, options: MagmaOptions):
        self.nvim = nvim
        self.canvas = canvas

        self.output = Output(None)

        self.display_buffer = self.nvim.buffers[
            self.nvim.funcs.nvim_create_buf(False, True)
        ]
        self.display_window = None

        self.options = options

    def _buffer_to_window_lineno(self, lineno: int) -> int:
        win_top = self.nvim.funcs.line("w0")
        assert isinstance(win_top, int)
        return lineno - win_top + 1

    def _get_header_text(self, output: Output) -> str:
        if output.execution_count is None:
            execution_count = "..."
        else:
            execution_count = str(output.execution_count)

        if output.status == OutputStatus.HOLD:
            status = "* On Hold"
        elif output.status == OutputStatus.DONE:
            if output.success:
                status = "✓ Done"
            else:
                status = "✗ Failed"
        elif output.status == OutputStatus.RUNNING:
            status = "... Running"
        else:
            raise ValueError("bad output.status: %s" % output.status)

        if output.old:
            old = "[OLD] "
        else:
            old = ""

        return f"{old}Out[{execution_count}]: {status}"

    def enter(self) -> None:
        if self.display_window is not None:  # TODO open window if is None?
            self.nvim.funcs.nvim_set_current_win(self.display_window)

    def clear_interface(self) -> None:
        if self.display_window is not None:
            self.nvim.funcs.nvim_win_close(self.display_window, True)
            self.display_window = None

    def show(self, anchor: Position) -> None:  # XXX .show_outputs(_, anchor)
        # FIXME use `anchor.buffer`, Not `self.nvim.current.window`

        # Get width&height, etc
        win_col = self.nvim.current.window.col
        win_row = self._buffer_to_window_lineno(anchor.lineno + 1)
        win_width = self.nvim.current.window.width
        win_height = self.nvim.current.window.height
        if self.options.output_window_borders:
            win_height -= 2

        # Clear buffer:
        self.nvim.funcs.deletebufline(self.display_buffer.number, 1, "$")
        # Add output chunks to buffer
        lines_str = ""
        lineno = 0
        shape = (win_col, win_row, win_width, win_height)
        if len(self.output.chunks) > 0:
            for chunk in self.output.chunks:
                chunktext = chunk.place(
                    self.options, lineno, shape, self.canvas
                )
                lines_str += chunktext
                lineno += chunktext.count("\n")
            lines = lines_str.rstrip().split("\n")
        else:
            lines = [lines_str]
        self.display_buffer[0] = self._get_header_text(self.output)  # TODO
        self.display_buffer.append(lines)

        # Open output window
        assert self.display_window is None
        if win_row < win_height:
            self.display_window = self.nvim.funcs.nvim_open_win(
                self.display_buffer.number,
                False,
                {
                    "relative": "win",
                    "col": 0,
                    "row": win_row,
                    "width": win_width,
                    "height": min(win_height - win_row, lineno + 1),
                    "anchor": "NW",
                    "style": None
                    if self.options.output_window_borders
                    else "minimal",
                    "border": "rounded"
                    if self.options.output_window_borders
                    else "none",
                    "focusable": False,
                },
            )
            # self.nvim.funcs.nvim_win_set_option(
            #     self.display_window, "wrap", True
            # )
