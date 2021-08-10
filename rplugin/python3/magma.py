from typing import Union, Optional, Tuple, Dict, List, Set
from abc import ABC, abstractmethod
from enum import Enum
from contextlib import contextmanager
from math import floor
from queue import Empty as EmptyQueueException, Queue
import base64
import hashlib
import re
import io
import os
import tempfile
import termios
import fcntl
import struct

import pynvim
from pynvim.api import Buffer
from pynvim import Nvim
import jupyter_client
import ueberzug.lib.v0 as ueberzug
# FIXME: This is not really in Ueberzug's public API.
#        We should move this function into this codebase.
from ueberzug.process import get_pty_slave
from PIL import Image


class MagmaException(Exception):
    pass


class MagmaOptions:
    automatically_open_output: bool

    def __init__(self, nvim: Nvim):
        self.automatically_open_output = nvim.vars.get("magma_automatically_open_output", True)


# Adapted from [https://stackoverflow.com/a/14693789/4803382]:
ANSI_CODE_REGEX = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
def remove_ansi_codes(text: str) -> str:
    return ANSI_CODE_REGEX.sub('', text)


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


class OutputChunk(ABC):
    @abstractmethod
    def place(self, lineno: int, shape: Tuple[int, int, int, int], canvas: Canvas) -> str:
        pass


class TextOutputChunk(OutputChunk):
    text: str

    def __init__(self, text: str):
        self.text = text

    def place(self, *_) -> str:
        return remove_ansi_codes(self.text)


class TextLnOutputChunk(TextOutputChunk):
    def __init__(self, text: str):
        self.text = text + "\n"


class ErrorOutputChunk(TextLnOutputChunk):
    def __init__(self, name: str, message: str, traceback: List[str]):
        self.text = "\n".join(
            [
                f"[Error] {name}: {message}",
                f"Traceback:",
            ]
            + traceback
        )


class AbortedOutputChunk(TextLnOutputChunk):
    def __init__(self):
        self.text = "<Kernel aborted with no error message.>"


class ImageOutputChunk(OutputChunk):
    def __init__(self, img_path: str, img_checksum: str, img_shape: Tuple[int, int]):
        self.img_path = img_path
        self.img_checksum = img_checksum
        self.img_width, self.img_height = img_shape

    def _get_char_pixelsize(self) -> Tuple[int, int]:
        pty = get_pty_slave(os.getppid())
        assert pty is not None
        with open(pty) as fd_pty:
            farg = struct.pack("HHHH", 0, 0, 0, 0)
            fretint = fcntl.ioctl(fd_pty, termios.TIOCGWINSZ, farg)
            rows, cols, xpixels, ypixels = struct.unpack("HHHH", fretint)

            return xpixels/cols, ypixels/rows

    def place(self, lineno: int, shape: Tuple[int, int, int, int], canvas: Canvas) -> str:
        x, y, w, h = shape

        xpixels, ypixels = self._get_char_pixelsize()

        max_nlines = (h-y)-lineno - 3
        if ((self.img_width/xpixels)/(self.img_height/ypixels))*max_nlines <= w:
            nlines = max_nlines
        else:
            nlines = floor(((self.img_height/ypixels)/(self.img_width/xpixels))*w)

        canvas.add_image(
            self.img_path,
            self.img_checksum,
            x=x,
            y=y + lineno + 1, # TODO: consider scroll in the display window
            width=w,
            height=nlines,
        )
        return "-\n"*nlines


class OutputStatus(Enum):
    HOLD         = 0
    RUNNING      = 1
    DONE = 2


class Output:
    execution_count: Optional[int]
    chunks: List[OutputChunk]
    status: OutputStatus
    success: bool

    _should_clear: bool

    def __init__(self, execution_count: Optional[int]):
        self.execution_count = execution_count
        self.status = OutputStatus.HOLD
        self.chunks = []
        self.success = True

        self._should_clear = False


class RuntimeState(Enum):
    STARTING = 0
    IDLE     = 1
    RUNNING  = 2


class JupyterRuntime:
    state: RuntimeState
    kernel_name: str

    kernel_manager: jupyter_client.KernelManager
    kernel_client: jupyter_client.KernelClient

    counter: int

    allocated_files: List[str]

    def __init__(self, kernel_name: str):
        self.state = RuntimeState.STARTING
        self.kernel_name = kernel_name

        self.kernel_manager = jupyter_client.manager.KernelManager(kernel_name=kernel_name)
        self.kernel_manager.start_kernel()
        self.kernel_client = self.kernel_manager.client()
        assert isinstance(self.kernel_client, jupyter_client.blocking.client.BlockingKernelClient)
        self.kernel_client.start_channels()

        self.allocated_files = []

    def is_ready(self) -> bool:
        return self.state.value > RuntimeState.STARTING.value

    def deinit(self):
        for path in self.allocated_files:
            os.remove(path)

    def run_code(self, code: str) -> Output:
        self.kernel_client.execute(code)

        return Output(None)

    @contextmanager
    def _alloc_file(self, extension, mode):
        with tempfile.NamedTemporaryFile(suffix="."+extension, mode=mode, delete=False) as file:
            path = file.name
            yield path, file
        self.allocated_files.append(path)

    def _to_outputchunk(self, data: dict, _: dict) -> OutputChunk:
        if (imgdata := data.get('image/png')) is not None:
            with self._alloc_file('png', 'wb') as (path, file):
                img = base64.b64decode(str(imgdata))
                file.write(img)  # type: ignore
                pil_image = Image.open(io.BytesIO(img))
                return ImageOutputChunk(
                    path,
                    hashlib.md5(imgdata.encode('ascii')).hexdigest(),
                    pil_image.size,
                )
        elif (text := data.get('text/plain')) is not None:
            return TextLnOutputChunk(text)
        else:
            # TODO make this a special OutputChunk
            raise RuntimeError("no usable mimetype available in output chunk")

    def _tick_one(self, output: Output, message_type: str, content: dict) -> bool:
        if output._should_clear:
            output.chunks.clear()
            output._should_clear = False

        if message_type == 'execute_input':
            output.execution_count = content['execution_count']
            assert output.status != OutputStatus.DONE
            if output.status == OutputStatus.HOLD:
                output.status = OutputStatus.RUNNING
            elif output.status == OutputStatus.RUNNING:
                output.status = OutputStatus.DONE
            else:
                raise ValueError("bad value for output.status: %r" % output.status)
            return True
        elif message_type == 'status':
            execution_state = content['execution_state']
            assert execution_state != 'starting'
            if execution_state == 'idle':
                self.state = RuntimeState.IDLE
                output.status = OutputStatus.DONE
                return True
            elif execution_state == 'busy':
                self.state = RuntimeState.RUNNING
                return True
            else:
                return False
        elif message_type == 'execute_reply':
            # This doesn't really give us any relevant information.
            return False
        elif message_type == 'execute_result':
            output.chunks.append(self._to_outputchunk(content['data'], content['metadata']))
            return True
        elif message_type == 'error':
            output.chunks.append(ErrorOutputChunk(
                content['ename'],
                content['evalue'],
                content['traceback']
            ))
            output.success = False
            return True
        elif message_type == 'stream':
            output.chunks.append(TextOutputChunk(content['text']))
            return True
        elif message_type == 'display_data':
            # XXX: consider content['transient'], if we end up saving execution outputs.
            output.chunks.append(self._to_outputchunk(content['data'], content['metadata']))
            return True
        elif message_type == 'update_display_data':
            # We don't really want to bother with this type of message.
            return False
        elif message_type == 'clear_output':
            if content['wait']:
                output._should_clear = True
            else:
                output.chunks.clear()
            return True
        # TODO: message_type == 'debug'?
        else:
            return False

    def tick(self, output: Optional[Output]) -> bool:
        did_stuff = False

        assert isinstance(self.kernel_client, jupyter_client.blocking.client.BlockingKernelClient)

        if not self.is_ready():
            try:
                self.kernel_client.wait_for_ready(timeout=0)
                self.state = RuntimeState.IDLE
                did_stuff = True
            except RuntimeError:
                return False

        if output is None:
            return did_stuff

        while True:
            try:
                message = self.kernel_client.get_iopub_msg(timeout=0)

                if 'content' not in message or 'msg_type' not in message:
                    continue

                did_stuff_now = self._tick_one(output, message['msg_type'], message['content'])
                did_stuff = did_stuff or did_stuff_now

                if output.status == OutputStatus.DONE:
                    break
            except EmptyQueueException:
                break

        return did_stuff


class MagmaBuffer:
    nvim: Nvim
    canvas: Canvas
    highlight_namespace: int
    extmark_namespace: int
    buffer: Buffer

    runtime: JupyterRuntime

    outputs: Dict[Span, Output]
    current_output: Optional[Output]
    queued_outputs: Queue[Output]

    display_buffer: Buffer
    display_window: Optional[int]
    should_open_display_window: bool

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

        self.runtime = JupyterRuntime(kernel_name)

        self.outputs = {}
        self.current_output = None
        self.queued_outputs = Queue()

        self.display_buffer = self.nvim.buffers[self.nvim.funcs.nvim_create_buf(False, True)]
        self.display_window = None

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

        self.should_open_display_window = True
        self.update_interface()

        self._check_if_done_running()

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

        return f"Out[{execution_count}]: {status}"

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

    def _clear_interface(self) -> None:
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
        selected = self._get_selected_span()
        if selected is None:
            return

        del self.outputs[selected]

        self.update_interface()

    def update_interface(self) -> None:
        self._clear_interface()

        selected = self._get_selected_span()

        if self.options.automatically_open_output:
            self.should_open_display_window = True

        if selected is not None:
            self._show_selected(selected)
        else:
            self.should_open_display_window = False
        self.canvas.present()

    def _show_selected(self, span: Span) -> None:
        # TODO: get a better highlight group
        HIGHLIGHT_GROUP = 'Visual'
        if span.begin.lineno == span.end.lineno:
            self.nvim.funcs.nvim_buf_add_highlight(
                self.buffer.number,
                self.highlight_namespace,
                HIGHLIGHT_GROUP,
                span.begin.lineno,
                span.begin.colno,
                span.end.colno,
            )
        else:
            self.nvim.funcs.nvim_buf_add_highlight(
                self.buffer.number,
                self.highlight_namespace,
                HIGHLIGHT_GROUP,
                span.begin.lineno,
                span.begin.colno,
                -1,
            )
            for lineno in range(span.begin.lineno+1, span.end.lineno):
                self.nvim.funcs.nvim_buf_add_highlight(
                    self.buffer.number,
                    self.highlight_namespace,
                    HIGHLIGHT_GROUP,
                    lineno,
                    0,
                    -1,
                )
            self.nvim.funcs.nvim_buf_add_highlight(
                self.buffer.number,
                self.highlight_namespace,
                HIGHLIGHT_GROUP,
                span.end.lineno,
                0,
                span.end.colno,
            )

        if self.current_output is not None and self.should_open_display_window:
            self._show_outputs(self.outputs[span], span.end)


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

        self.timer = self.nvim.eval("timer_start(500, {-> nvim_command('MagmaTick')}, {'repeat': -1})") # type: ignore
        self.nvim.command("""
            function! g:MagmaOperatorfunc(type) abort
                exec 'MagmaEvaluateFromOperator ' .. a:type
            endfunction
        """)

        self.initialized = True

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

    @pynvim.command('MagmaTick', sync=True)
    @nvimui
    def tick(self) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(False)
        if magma is None:
            return

        magma.tick()

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
            list(jupyter_client.kernelspec.find_kernel_specs().keys()),
        )

    @pynvim.command("MagmaInit", nargs='?', sync=True)
    @nvimui
    def command_init(self, args: List[str]) -> None:
        self._initialize_if_necessary()

        if args:
            kernel_name = args[0]
        else:
            kernel_name = self._ask_for_kernel()

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

    @pynvim.command("MagmaEvaluateFromOperator", nargs=1, sync=True)
    @nvimui
    def command_evaluate_from_marks(self, kind) -> None:
        kind = kind[0]

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

    @pynvim.command("MagmaEvaluateOperator", sync=True)
    @nvimui
    def command_evaluate_operator(self) -> None:
        self._initialize_if_necessary()

        self.nvim.options['operatorfunc'] = 'g:MagmaOperatorfunc'
        self.nvim.out_write("g@\n")

    @pynvim.command("MagmaEvaluateLine", nargs=0, sync=True)
    @nvimui
    def command_evaluate_line(self) -> None:
        _, lineno, _, _, _ = self.nvim.funcs.getcurpos()
        lineno -= 1

        span = ((lineno, 0), (lineno, -1))

        self._do_evaluate(span)

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

    @pynvim.autocmd('CursorMoved', sync=True)
    @nvimui
    def autocmd_cursormoved(self):
        self._update_interface()

    @pynvim.autocmd('CursorMovedI', sync=True)
    @nvimui
    def autocmd_cursormovedi(self):
        self._update_interface()

    @pynvim.autocmd('WinScrolled', sync=True)
    @nvimui
    def autocmd_winscrolled(self):
        self._update_interface()

    @pynvim.autocmd('ExitPre')
    def autocmd_exitpre(self):
        self._deinitialize()
