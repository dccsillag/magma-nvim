from typing import Type, Optional, Tuple, Dict, List
from queue import Queue
import json
import hashlib

import pynvim
from pynvim.api import Buffer
from pynvim import Nvim

from magma.options      import MagmaOptions
from magma.utils        import MagmaException, nvimui, Canvas, Position, DynamicPosition, Span
from magma.outputchunks import Output, OutputStatus, to_outputchunk
from magma.runtime      import JupyterRuntime, get_available_kernels


class MagmaIOError(Exception):
    @classmethod
    def assert_has_key(cls, data: dict, key: str, type_: Optional[Type] = None) -> None:
        if key not in data:
            raise cls(f"Missing key: {key}")
        value = data[key]
        if type_ is not None and not isinstance(value, type_):
            raise cls(f"Incorrect type for key '{key}': expected {type_.__name__}, got {type(value).__name__}")
        return value


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

    def deinit(self):
        self.runtime.deinit()

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
                chunktext = chunk.place(lineno, shape, self.canvas)
                lines += chunktext
                lineno += chunktext.count("\n")
            lines = lines.rstrip().split("\n")
        self.display_buffer[0] = self._get_header_text(output)
        self.display_buffer.append(lines)

        # Open output window
        assert self.display_window is None
        self.display_window = self.nvim.funcs.nvim_open_win(
            self.display_buffer.number,
            False,
            {
                'relative': 'win',
                'col': 0,
                'row': win_row,
                'width': win_width,
                'height': min(win_height - win_row, len(lines)+1),
                'anchor': 'NW',
                'style': 'minimal',
                'focusable': False,
            }
        )

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


    def load(self, data: dict) -> None:
        MagmaIOError.assert_has_key(data, 'content_checksum', str)

        if self._get_content_checksum() != data['content_checksum']:
            raise MagmaIOError("Buffer contents' checksum does not match!")

        MagmaIOError.assert_has_key(data, 'cells', list)
        for cell in data['cells']:
            MagmaIOError.assert_has_key(cell, 'span', dict)
            MagmaIOError.assert_has_key(cell['span'], 'begin', dict)
            MagmaIOError.assert_has_key(cell['span']['begin'], 'lineno', int)
            MagmaIOError.assert_has_key(cell['span']['begin'], 'colno', int)
            MagmaIOError.assert_has_key(cell['span'], 'end', dict)
            MagmaIOError.assert_has_key(cell['span']['end'], 'lineno', int)
            MagmaIOError.assert_has_key(cell['span']['end'], 'colno', int)
            begin_position = DynamicPosition(
                self.nvim,
                self.extmark_namespace,
                self.buffer.number,
                cell['span']['begin']['lineno'],
                cell['span']['begin']['colno']
            )
            end_position = DynamicPosition(
                self.nvim,
                self.extmark_namespace,
                self.buffer.number,
                cell['span']['end']['lineno'],
                cell['span']['end']['colno']
            )
            span = Span(begin_position, end_position)

            # XXX: do we really want to have the execution count here?
            #      what happens when the counts start to overlap?
            MagmaIOError.assert_has_key(cell, "execution_count", int)
            output = Output(cell['execution_count'])

            MagmaIOError.assert_has_key(cell, "status", int)
            output.status = OutputStatus(cell['status'])

            MagmaIOError.assert_has_key(cell, "success", bool)
            output.success = cell['success']

            MagmaIOError.assert_has_key(cell, 'chunks', list)
            for chunk in cell['chunks']:
                MagmaIOError.assert_has_key(chunk, 'data', dict)
                MagmaIOError.assert_has_key(chunk, 'metadata', dict)
                output.chunks.append(to_outputchunk(self.runtime._alloc_file, chunk['data'], chunk['metadata']))

            output.old = True

            self.outputs[span] = output

    def save(self) -> dict:
        return {
            "version": 1,
            "kernel": self.runtime.kernel_name,
            "content_checksum": self._get_content_checksum(),
            "cells": [
                {
                    "span": {
                        "begin": {"lineno": span.begin.lineno, "colno": span.begin.colno},
                        "end": {"lineno": span.end.lineno, "colno": span.end.colno},
                    },
                    "execution_count": output.execution_count,
                    "status": output.status.value,
                    "success": output.success,
                    "chunks": [
                        {
                            "data": chunk.jupyter_data,
                            "metadata": chunk.jupyter_metadata,
                        }
                        for chunk in output.chunks
                        if chunk.jupyter_data is not None and chunk.jupyter_metadata is not None
                    ],
                }
                for span, output in self.outputs.items()
            ],
        }


@pynvim.plugin
class Magma:
    nvim: Nvim
    canvas: Optional[Canvas]
    initialized: bool

    highlight_namespace: int
    extmark_namespace: int

    buffers: Dict[int, MagmaBuffer]

    timer: Optional[int]

    def __init__(self, nvim):
        self.nvim = nvim
        self.initialized = False

        self.canvas = None
        self.buffers = {}
        self.timer = None

    def _initialize(self) -> None:
        assert not self.initialized

        self.canvas = Canvas()
        self.canvas.__enter__()

        self.highlight_namespace = self.nvim.funcs.nvim_create_namespace("magma-highlights")
        self.extmark_namespace   = self.nvim.funcs.nvim_create_namespace("magma-extmarks")

        self.timer = self.nvim.eval("timer_start(500, 'MagmaTick', {'repeat': -1})") # type: ignore

        self._set_autocommands()

        self.initialized = True

    def _set_autocommands(self) -> None:
        self.nvim.command("augroup magma")
        self.nvim.command("  autocmd CursorMoved  * call MagmaUpdateInterface()")
        self.nvim.command("  autocmd CursorMovedI * call MagmaUpdateInterface()")
        self.nvim.command("  autocmd WinScrolled  * call MagmaUpdateInterface()")
        self.nvim.command("  autocmd BufEnter     * call MagmaUpdateInterface()")
        self.nvim.command("  autocmd BufLeave     * call MagmaClearInterface()")
        self.nvim.command("  autocmd BufUnload    * call MagmaOnBufferUnload()")
        self.nvim.command("  autocmd ExitPre      * call MagmaOnExitPre()")
        self.nvim.command("augroup END")

    def _deinitialize(self) -> None:
        for magma in self.buffers.values():
            magma.deinit()
        if self.canvas is not None:
            self.canvas.__exit__()
        if self.timer is not None:
            self.nvim.funcs.timer_stop(self.timer)

    def _initialize_if_necessary(self) -> None:
        if not self.initialized:
            self._initialize()

    def _get_magma(self, requires_instance: bool) -> Optional[MagmaBuffer]:
        maybe_magma = self.buffers.get(self.nvim.current.buffer.number)
        if requires_instance and maybe_magma is None:
            raise MagmaException("Magma is not initialized; run `:MagmaInit <kernel_name>` to initialize.")
        return maybe_magma

    def _clear_interface(self) -> None:
        if not self.initialized:
            return

        for magma in self.buffers.values():
            magma.clear_interface()
        self.canvas.present()

    def _update_interface(self) -> None:
        if not self.initialized:
            return

        magma = self._get_magma(False)
        if magma is None:
            return

        magma.update_interface()

    def _ask_for_choice(self, preface: str, options: List[str]) -> str:
        index = self.nvim.funcs.inputlist(
            [preface]
            + [f"{i+1}. {option}" for i, option in enumerate(options)]
        )
        return options[index-1]

    def _ask_for_kernel(self) -> str:
        return self._ask_for_choice(
            "Select the kernel to launch:",
            get_available_kernels(), # type: ignore
        )

    def _initialize_buffer(self, kernel_name: str) -> MagmaBuffer:
        assert self.canvas is not None
        magma = MagmaBuffer(
            self.nvim,
            self.canvas,
            self.highlight_namespace,
            self.extmark_namespace,
            self.nvim.current.buffer,
            MagmaOptions(self.nvim),
            kernel_name,
        )

        self.buffers[self.nvim.current.buffer.number] = magma

        return magma

    @pynvim.command("MagmaInit", nargs='?', sync=True)
    @nvimui
    def command_init(self, args: List[str]) -> None:
        self._initialize_if_necessary()

        if args:
            kernel_name = args[0]
        else:
            kernel_name = self._ask_for_kernel()

        self._initialize_buffer(kernel_name)

    def _deinit_buffer(self, magma: MagmaBuffer) -> None:
        magma.deinit()
        del self.buffers[magma.buffer.number]

    @pynvim.command("MagmaDeinit", nargs=0, sync=True)
    @nvimui
    def command_deinit(self) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(True)
        assert magma is not None

        self._clear_interface()

        self._deinit_buffer(magma)

    def _do_evaluate(self, pos: Tuple[Tuple[int, int], Tuple[int, int]]) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(True)
        assert magma is not None

        bufno = self.nvim.current.buffer.number
        span = Span(DynamicPosition(self.nvim, self.extmark_namespace, bufno, *pos[0]),
                    DynamicPosition(self.nvim, self.extmark_namespace, bufno, *pos[1]))

        code = span.get_text(self.nvim)

        magma.run_code(code, span)

    @pynvim.command("MagmaEvaluateVisual", sync=True)
    @nvimui
    def command_evaluate_visual(self) -> None:
        _, lineno_begin, colno_begin, _ = self.nvim.funcs.getpos("'<")
        _, lineno_end,   colno_end,   _ = self.nvim.funcs.getpos("'>")
        span = ((lineno_begin-1, min(colno_begin, len(self.nvim.funcs.getline(lineno_begin)))-1),
                (lineno_end-1,   min(colno_end,   len(self.nvim.funcs.getline(  lineno_end)))))

        self._do_evaluate(span)

    @pynvim.command("MagmaEvaluateOperator", sync=True)
    @nvimui
    def command_evaluate_operator(self) -> None:
        self._initialize_if_necessary()

        self.nvim.options['operatorfunc'] = 'MagmaOperatorfunc'
        self.nvim.out_write("g@\n")

    @pynvim.command("MagmaEvaluateLine", nargs=0, sync=True)
    @nvimui
    def command_evaluate_line(self) -> None:
        _, lineno, _, _, _ = self.nvim.funcs.getcurpos()
        lineno -= 1

        span = ((lineno, 0), (lineno, -1))

        self._do_evaluate(span)

    @pynvim.command("MagmaReevaluateCell", nargs=0, sync=True)
    @nvimui
    def command_evaluate_cell(self) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(True)
        assert magma is not None

        magma.reevaluate_cell()

    @pynvim.command("MagmaDelete", nargs=0, sync=True)
    @nvimui
    def command_delete(self) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(True)
        assert magma is not None

        magma.delete_cell()

    @pynvim.command("MagmaShowOutput", nargs=0, sync=True)
    @nvimui
    def command_show_output(self) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(True)
        assert magma is not None

        magma.should_open_display_window = True
        self._update_interface()

    @pynvim.command("MagmaSave", nargs=1, sync=True)
    @nvimui
    def command_save(self, args: List[str]) -> None:
        path = args[0]

        self._initialize_if_necessary()

        magma = self._get_magma(True)
        assert magma is not None

        with open(path, 'w') as file:
            json.dump(magma.save(), file)

    @pynvim.command("MagmaLoad", nargs=1, sync=True)
    @nvimui
    def command_load(self, args: List[str]) -> None:
        path = args[0]

        self._initialize_if_necessary()

        if self.nvim.current.buffer.number in self.buffers:
            raise MagmaException("Magma is already initialized; MagmaLoad initializes Magma.")

        with open(path) as file:
            data = json.load(file)

        magma = None

        try:
            MagmaIOError.assert_has_key(data, 'version', int)
            if (version := data['version']) != 1:
                raise MagmaIOError(f"Bad version: {version}")

            MagmaIOError.assert_has_key(data, 'kernel', str)
            kernel_name = data['kernel']

            magma = self._initialize_buffer(kernel_name)

            magma.load(data)

            self._update_interface()
        except MagmaIOError as err:
            if magma is not None:
                self._deinit_buffer(magma)

            raise MagmaException("Error while doing Magma IO: " + str(err))

    # Internal functions which are exposed to VimScript

    @pynvim.function("MagmaClearInterface", sync=True)
    @nvimui
    def function_clear_interface(self, _) -> None:
        self._clear_interface()

    @pynvim.function("MagmaOnBufferUnload", sync=True)
    @nvimui
    def function_on_buffer_unload(self, _) -> None:
        abuf_str = self.nvim.funcs.expand('<abuf>')
        if not abuf_str:
            return

        magma = self.buffers.get(int(abuf_str))
        if magma is None:
            return

        self._deinit_buffer(magma)

    @pynvim.function("MagmaOnExitPre", sync=True)
    @nvimui
    def function_on_exit_pre(self, _) -> None:
        self._deinitialize()

    @pynvim.function("MagmaTick", sync=True)
    @nvimui
    def function_magma_tick(self, _) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(False)
        if magma is None:
            return

        magma.tick()

    @pynvim.function("MagmaUpdateInterface", sync=True)
    @nvimui
    def function_update_interface(self, _) -> None:
        self._update_interface()

    @pynvim.function("MagmaOperatorfunc", sync=True)
    @nvimui
    def function_magma_operatorfunc(self, args) -> None:
        if not args:
            return

        kind = args[0]

        _, lineno_begin, colno_begin, _ = self.nvim.funcs.getpos("'[")
        _, lineno_end,   colno_end,   _ = self.nvim.funcs.getpos("']")

        if kind == 'line':
            colno_begin = 1
            colno_end = -1
        elif kind == 'char':
            pass
        else:
            raise MagmaException(f"this kind of selection is not supported: '{kind}'")

        span = ((lineno_begin-1, min(colno_begin, len(self.nvim.funcs.getline(lineno_begin)))-1),
                (lineno_end-1,   min(colno_end,   len(self.nvim.funcs.getline(  lineno_end)))))

        self._do_evaluate(span)
