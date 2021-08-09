from typing import Union, Optional, Tuple, Dict, List
from abc import ABC, abstractmethod
from enum import Enum
from queue import Empty as EmptyQueueException

import pynvim
from pynvim.api import Buffer
from pynvim import Nvim
import jupyter_client


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
            return lines[0][self.begin.colno:self.end.colno+1]
        else:
            return '\n'.join(
                [lines[0][self.begin.colno:]] +
                lines[1:-1] +
                [lines[1][:self.end.colno+1]]
            )


class OutputChunk(ABC):
    @abstractmethod
    def to_text(self) -> str:
        pass


class TextOutputChunk(OutputChunk):
    text: str

    def __init__(self, text: str):
        self.text = text

    def to_text(self) -> str:
        return self.text


class InterruptOutputChunk(OutputChunk):
    def to_text(self) -> str:
        return "<Interrupted>."


class Output:
    execution_count: Optional[int]
    chunks: List[OutputChunk]
    done: bool

    def __init__(self, execution_count: Optional[int]):
        self.execution_count = execution_count
        self.done = False
        self.chunks = []


class RuntimeState(Enum):
    IDLE = 1
    RUNNING = 2


class JupyterRuntime:
    state: RuntimeState
    kernel_name: str

    kernel_manager: jupyter_client.KernelManager
    kernel_client: jupyter_client.KernelClient

    counter: int

    def __init__(self, kernel_name: str):
        self.state = RuntimeState.IDLE
        self.kernel_name = kernel_name

        self.kernel_manager, self.kernel_client = \
            jupyter_client.manager.start_new_kernel(kernel_name=kernel_name)

    def run_code(self, code: str) -> Output:
        self.kernel_client.execute(code)

        return Output(None)

    def tick(self, output: Output) -> None:
        try:
            assert isinstance(self.kernel_client, jupyter_client.blocking.client.BlockingKernelClient)
            message = self.kernel_client.get_iopub_msg(timeout=0)

            if 'content' not in message or 'msg_type' not in message:
                return

            message_type = message['msg_type']
            content = message['content']

            if self.state == RuntimeState.IDLE:
                if message_type == 'execute_input':
                    self.state = RuntimeState.RUNNING
                    output.execution_count = content['execution_count']
            elif self.state == RuntimeState.RUNNING:
                if message_type == 'status':
                    if content['execution_state'] == 'idle':
                        self.state = RuntimeState.IDLE
                elif message_type == 'execute_reply':
                    if content['status'] == 'ok':
                        output.chunks.append(TextOutputChunk(content['status']))
                    elif content['status'] == 'error':
                        # TODO improve error formatting (in particular, use content['traceback'])
                        output.chunks.append(TextOutputChunk(
                            f"[Error] {content['ename']}: {content['evalue']}"
                        ))
                    elif content['status'] == 'abort':
                        # TODO improve error formatting
                        output.chunks.append(TextOutputChunk(
                            f"<Kernel aborted with no error message>."
                        ))
                elif message_type == 'execute_result':
                    if (text := content['data'].get('text/plain')) is not None:
                        output.chunks.append(TextOutputChunk(text))
                elif message_type == 'error':
                    output.chunks.append(TextOutputChunk(
                        f"[Error] {content['ename']}: {content['evalue']}"
                    ))
                elif message_type == 'stream':
                    output.chunks.append(TextOutputChunk(content['text']))
            else:
                raise RuntimeError(f"bad RuntimeState: {self.state}")
        except EmptyQueueException:
            return


class MagmaBuffer:
    nvim: Nvim
    highlight_namespace: int
    extmark_namespace: int
    buffer: Buffer

    runtime: JupyterRuntime

    outputs: Dict[Span, Output]
    current_output: Optional[Output]

    display_buffer: Buffer
    display_window: Optional[int]

    def __init__(self,
                 nvim: Nvim,
                 highlight_namespace: int,
                 extmark_namespace: int,
                 buffer: Buffer,
                 kernel_name: str):
        self.nvim                = nvim
        self.highlight_namespace = highlight_namespace
        self.extmark_namespace   = extmark_namespace
        self.buffer = buffer

        self.runtime        = JupyterRuntime(kernel_name)

        self.outputs = {}
        self.current_output = None

        self.display_buffer = self.nvim.buffers[self.nvim.funcs.nvim_create_buf(False, True)]
        self.display_window = None

    def run_code(self, code: str, span: Optional[Span]):
        self.current_output = self.runtime.run_code(code)
        if span is not None:
            assert self.current_output is not None
            self.outputs[span] = self.current_output

        if span is not None:
            self._show_selected(span)

    def tick(self):
        if self.current_output:
            self.runtime.tick(self.current_output)

    def _show_outputs(self, output: Output):
        # Clear buffer:
        self.nvim.funcs.deletebufline(self.display_buffer.number, 1, '$')
        # Add output chunks to buffer
        lines = "\n\n".join(chunk.to_text() for chunk in output.chunks).strip().split("\n")
        self.display_buffer.append(lines, index=0)

        cur_lineno, cur_colno = self.nvim.funcs.nvim_win_get_cursor(0)
        assert self.display_window is None
        self.display_window = self.nvim.funcs.nvim_open_win(
            self.display_buffer.number,
            False,
            {
                'relative': 'win', # TODO use anchor
                'col': cur_colno,
                'row': cur_lineno,
                'width': 30, # TODO consider the terminal width
                'height': min(30, len(lines)), # TODO consider the terminal height
                'anchor': 'NW',
                'style': 'minimal',
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
            self.display_window = None

    def on_cursor_move(self) -> None:
        self._clear_interface()

        current_position = self._get_cursor_position()
        selected = None
        for span in reversed(self.outputs.keys()):
            if current_position in span:
                selected = span
                break

        if selected is not None:
            self._show_selected(selected)

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

        if self.current_output is not None:
            self._show_outputs(self.current_output)


@pynvim.plugin
class Magma:
    nvim: Nvim
    initialized: bool

    highlight_namespace: int
    extmark_namespace: int

    buffers: Dict[int, MagmaBuffer]

    timer: int

    def __init__(self, nvim):
        self.nvim = nvim
        self.initialized = False

    def _initialize(self) -> None:
        assert not self.initialized

        self.highlight_namespace = self.nvim.funcs.nvim_create_namespace("magma-highlights")
        self.extmark_namespace   = self.nvim.funcs.nvim_create_namespace("magma-extmarks")

        self.buffers = {}

        self.timer = self.nvim.eval("timer_start(500, {-> nvim_command('MagmaTick')}, {'repeat': -1})") # type: ignore
        self.nvim.command("""
            function! g:MagmaOperatorfunc(type) abort
                exec 'MagmaEvaluateFromOperator ' .. a:type
            endfunction
        """)

        self.initialized = True

    def _initialize_if_necessary(self) -> None:
        if not self.initialized:
            self._initialize()

    def _get_magma(self) -> Optional[MagmaBuffer]:
        return self.buffers.get(self.nvim.current.buffer.number)

    @pynvim.command('MagmaTick', sync=True)
    def tick(self) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma()
        if magma is None:
            return

        magma.tick()

    def _on_cursor_moved(self) -> None:
        if not self.initialized:
            return

        magma = self._get_magma()
        if magma is None:
            return

        magma.on_cursor_move()

    @pynvim.command("MagmaInit", nargs=1, sync=True)
    def command_init(self, args: List[str]) -> None:
        self._initialize_if_necessary()

        magma = MagmaBuffer(
            self.nvim,
            self.highlight_namespace,
            self.extmark_namespace,
            self.nvim.current.buffer,
            args[0]
        )

        self.buffers[self.nvim.current.buffer.number] = magma

    def _do_evaluate(self, pos: Tuple[Tuple[int, int], Tuple[int, int]]) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma()
        if magma is None:
            return # TODO show an error

        bufno = self.nvim.current.buffer.number
        span = Span(DynamicPosition(self.nvim, self.extmark_namespace, bufno, *pos[0]),
                    DynamicPosition(self.nvim, self.extmark_namespace, bufno, *pos[1]))

        code = span.get_text(self.nvim)

        magma.run_code(code, span)

    @pynvim.command("MagmaEvaluateVisual")
    def command_evaluate_visual(self) -> None:
        _, lineno_begin, colno_begin, _ = self.nvim.funcs.getpos("'<")
        _, lineno_end,   colno_end,   _ = self.nvim.funcs.getpos("'>")
        span = ((lineno_begin-1, min(colno_begin, len(self.nvim.funcs.getline(lineno_begin)))-1),
                (lineno_end-1,   min(colno_end,   len(self.nvim.funcs.getline(  lineno_end)))))

        self._do_evaluate(span)

    @pynvim.command("MagmaEvaluateFromOperator", nargs=1, sync=True)
    def command_evaluate_from_marks(self, kind) -> None:
        kind = kind[0]

        if kind == 'line':
            self.nvim.feedkeys(self.nvim.replace_termcodes(r"'[V']:<C-u>MagmaEvaluateVisual<CR>"))
        elif kind == 'char':
            self.nvim.feedkeys(self.nvim.replace_termcodes(r"`[v`]:<C-u>MagmaEvaluateVisual<CR>"))
        elif kind == 'block':
            self.nvim.feedkeys(self.nvim.replace_termcodes(r"`[\<C-v>`]:<C-u>MagmaEvaluateVisual<CR>"))
        else:
            raise ValueError(f"bad type for MagmaEvaluateFromOperator: {kind}")

    @pynvim.command("MagmaEvaluateOperator")
    def command_evaluate_operator(self) -> None:
        self.nvim.options['operatorfunc'] = 'g:MagmaOperatorfunc'
        self.nvim.feedkeys('g@')

    @pynvim.command("MagmaEvaluateLine", nargs=0, sync=True)
    def command_evaluate_line(self) -> None:
        _, lineno, _, _, _ = self.nvim.funcs.getcurpos()
        lineno -= 1

        span = ((lineno, 0), (lineno, -1))

        self._do_evaluate(span)

    @pynvim.autocmd('CursorMoved', sync=True)
    def autocmd_cursormoved(self):
        self._on_cursor_moved()

    @pynvim.autocmd('CursorMovedI', sync=True)
    def autocmd_cursormovedi(self):
        self._on_cursor_moved()
