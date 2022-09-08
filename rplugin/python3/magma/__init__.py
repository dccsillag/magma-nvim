from typing import Optional, Tuple, Dict, List, Any
import json
import os

import pynvim
from pynvim import Nvim

from magma.options import MagmaOptions
from magma.utils import MagmaException, nvimui, DynamicPosition, Span
from magma.images import Canvas, get_canvas_given_provider
from magma.runtime import get_available_kernels
from magma.magmabuffer import MagmaBuffer
from magma.io import MagmaIOError, save, load, get_default_save_file


@pynvim.plugin
class Magma:
    nvim: Nvim
    canvas: Optional[Canvas]
    initialized: bool

    highlight_namespace: int
    extmark_namespace: int

    buffers: Dict[int, MagmaBuffer]

    timer: Optional[int]

    options: MagmaOptions

    def __init__(self, nvim: Nvim):
        self.nvim = nvim
        self.initialized = False

        self.canvas = None
        self.buffers = {}
        self.timer = None

    def _initialize(self) -> None:
        assert not self.initialized

        self.options = MagmaOptions(self.nvim)

        self.canvas = get_canvas_given_provider(
            self.options.image_provider, self.nvim
        )
        self.canvas.init()

        self.highlight_namespace = self.nvim.funcs.nvim_create_namespace(
            "magma-highlights"
        )
        self.extmark_namespace = self.nvim.funcs.nvim_create_namespace(
            "magma-extmarks"
        )

        self.timer = self.nvim.eval(
            "timer_start(500, 'MagmaTick', {'repeat': -1})"
        )

        self._set_autocommands()

        self.initialized = True

    def _set_autocommands(self) -> None:
        self.nvim.command("augroup magma")
        self.nvim.command(
            "  autocmd CursorMoved  * call MagmaUpdateInterface()"
        )
        self.nvim.command(
            "  autocmd CursorMovedI * call MagmaUpdateInterface()"
        )
        self.nvim.command(
            "  autocmd WinScrolled  * call MagmaUpdateInterface()"
        )
        self.nvim.command(
            "  autocmd BufEnter     * call MagmaUpdateInterface()"
        )
        self.nvim.command(
            "  autocmd BufLeave     * call MagmaClearInterface()"
        )
        self.nvim.command(
            "  autocmd BufUnload    * call MagmaOnBufferUnload()"
        )
        self.nvim.command("  autocmd ExitPre      * call MagmaOnExitPre()")
        self.nvim.command("augroup END")

    def _deinitialize(self) -> None:
        for magma in self.buffers.values():
            magma.deinit()
        if self.canvas is not None:
            self.canvas.deinit()
        if self.timer is not None:
            self.nvim.funcs.timer_stop(self.timer)

    def _initialize_if_necessary(self) -> None:
        if not self.initialized:
            self._initialize()

    def _get_magma(self, requires_instance: bool) -> Optional[MagmaBuffer]:
        maybe_magma = self.buffers.get(self.nvim.current.buffer.number)
        if requires_instance and maybe_magma is None:
            raise MagmaException(
                "Magma is not initialized; run `:MagmaInit <kernel_name>` to \
                initialize."
            )
        return maybe_magma

    def _clear_interface(self) -> None:
        if not self.initialized:
            return

        for magma in self.buffers.values():
            magma.clear_interface()
        assert self.canvas is not None
        self.canvas.present()

    def _update_interface(self) -> None:
        if not self.initialized:
            return

        magma = self._get_magma(False)
        if magma is None:
            return

        magma.update_interface()

    def _ask_for_choice(
        self, preface: str, options: List[str]
    ) -> Optional[str]:
        index: int = self.nvim.funcs.inputlist(
            [preface]
            + [f"{i+1}. {option}" for i, option in enumerate(options)]
        )
        if index == 0:
            return None
        else:
            return options[index - 1]

    def _initialize_buffer(self, kernel_name: str) -> MagmaBuffer:
        assert self.canvas is not None
        magma = MagmaBuffer(
            self.nvim,
            self.canvas,
            self.highlight_namespace,
            self.extmark_namespace,
            self.nvim.current.buffer,
            self.options,
            kernel_name,
        )

        self.buffers[self.nvim.current.buffer.number] = magma

        return magma

    @pynvim.command("MagmaInit", nargs="?", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_init(self, args: List[str]) -> None:
        self._initialize_if_necessary()

        if args:
            kernel_name = args[0]
            self._initialize_buffer(kernel_name)
        else:
            PROMPT = "Select the kernel to launch:"
            available_kernels = get_available_kernels()
            if self.nvim.exec_lua("return vim.ui.select ~= nil"):
                self.nvim.exec_lua(
                    """
                        vim.ui.select(
                            {%s},
                            {prompt = "%s"},
                            function(choice)
                                if choice ~= nil then
                                    vim.cmd("MagmaInit " .. choice)
                                end
                            end
                        )
                    """
                    % (
                        ", ".join(repr(x) for x in available_kernels),
                        PROMPT,
                    )
                )
            else:
                kernel_name = self._ask_for_choice(
                    PROMPT,
                    available_kernels,  # type: ignore
                )
                if kernel_name is not None:
                    self.command_init([kernel_name])

    def _deinit_buffer(self, magma: MagmaBuffer) -> None:
        magma.deinit()
        del self.buffers[magma.buffer.number]

    @pynvim.command("MagmaDeinit", nargs=0, sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_deinit(self) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(True)
        assert magma is not None

        self._clear_interface()

        self._deinit_buffer(magma)

    def _do_evaluate(
        self, pos: Tuple[Tuple[int, int], Tuple[int, int]]
    ) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(True)
        assert magma is not None

        bufno = self.nvim.current.buffer.number
        span = Span(
            DynamicPosition(self.nvim, self.extmark_namespace, bufno, *pos[0]),
            DynamicPosition(self.nvim, self.extmark_namespace, bufno, *pos[1]),
        )

        code = span.get_text(self.nvim)

        magma.run_code(code, span)

    @pynvim.command("MagmaEnterOutput", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_enter_output_window(self) -> None:
        magma = self._get_magma(True)
        assert magma is not None
        magma.enter_output()

    @pynvim.command("MagmaEvaluateVisual", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_evaluate_visual(self) -> None:
        _, lineno_begin, colno_begin, _ = self.nvim.funcs.getpos("'<")
        _, lineno_end, colno_end, _ = self.nvim.funcs.getpos("'>")
        span = (
            (
                lineno_begin - 1,
                min(colno_begin, len(self.nvim.funcs.getline(lineno_begin)))
                - 1,
            ),
            (
                lineno_end - 1,
                min(colno_end, len(self.nvim.funcs.getline(lineno_end))),
            ),
        )

        self._do_evaluate(span)

    @pynvim.command("MagmaEvaluateOperator", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_evaluate_operator(self) -> None:
        self._initialize_if_necessary()

        self.nvim.options["operatorfunc"] = "MagmaOperatorfunc"
        self.nvim.out_write("g@\n")

    @pynvim.command("MagmaEvaluateLine", nargs=0, sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_evaluate_line(self) -> None:
        _, lineno, _, _, _ = self.nvim.funcs.getcurpos()
        lineno -= 1

        span = ((lineno, 0), (lineno, -1))

        self._do_evaluate(span)

    @pynvim.command("MagmaReevaluateCell", nargs=0, sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_evaluate_cell(self) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(True)
        assert magma is not None

        magma.reevaluate_cell()

    @pynvim.command("MagmaInterrupt", nargs=0, sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_interrupt(self) -> None:
        magma = self._get_magma(True)
        assert magma is not None

        magma.interrupt()

    @pynvim.command("MagmaRestart", nargs=0, sync=True, bang=True)  # type: ignore # noqa
    @nvimui  # type: ignore
    def command_restart(self, bang: bool) -> None:
        magma = self._get_magma(True)
        assert magma is not None

        magma.restart(delete_outputs=bang)

    @pynvim.command("MagmaDelete", nargs=0, sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_delete(self) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(True)
        assert magma is not None

        magma.delete_cell()

    @pynvim.command("MagmaShowOutput", nargs=0, sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_show_output(self) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(True)
        assert magma is not None

        magma.should_open_display_window = True
        self._update_interface()

    @pynvim.command("MagmaSave", nargs="?", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_save(self, args: List[str]) -> None:
        self._initialize_if_necessary()

        if args:
            path = args[0]
        else:
            path = get_default_save_file(
                self.options, self.nvim.current.buffer
            )

        dirname = os.path.dirname(path)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        magma = self._get_magma(True)
        assert magma is not None

        with open(path, "w") as file:
            json.dump(save(magma), file)

    @pynvim.command("MagmaLoad", nargs="?", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_load(self, args: List[str]) -> None:
        self._initialize_if_necessary()

        if args:
            path = args[0]
        else:
            path = get_default_save_file(
                self.options, self.nvim.current.buffer
            )

        if self.nvim.current.buffer.number in self.buffers:
            raise MagmaException(
                "Magma is already initialized; MagmaLoad initializes Magma."
            )

        with open(path) as file:
            data = json.load(file)

        magma = None

        try:
            MagmaIOError.assert_has_key(data, "version", int)
            if (version := data["version"]) != 1:
                raise MagmaIOError(f"Bad version: {version}")

            MagmaIOError.assert_has_key(data, "kernel", str)
            kernel_name = data["kernel"]

            magma = self._initialize_buffer(kernel_name)

            load(magma, data)

            self._update_interface()
        except MagmaIOError as err:
            if magma is not None:
                self._deinit_buffer(magma)

            raise MagmaException("Error while doing Magma IO: " + str(err))

    # Internal functions which are exposed to VimScript

    @pynvim.function("MagmaClearInterface", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def function_clear_interface(self, _: Any) -> None:
        self._clear_interface()

    @pynvim.function("MagmaOnBufferUnload", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def function_on_buffer_unload(self, _: Any) -> None:
        abuf_str = self.nvim.funcs.expand("<abuf>")
        if not abuf_str:
            return

        magma = self.buffers.get(int(abuf_str))
        if magma is None:
            return

        self._deinit_buffer(magma)

    @pynvim.function("MagmaOnExitPre", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def function_on_exit_pre(self, _: Any) -> None:
        self._deinitialize()

    @pynvim.function("MagmaTick", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def function_magma_tick(self, _: Any) -> None:
        self._initialize_if_necessary()

        magma = self._get_magma(False)
        if magma is None:
            return

        magma.tick()

    @pynvim.function("MagmaUpdateInterface", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def function_update_interface(self, _: Any) -> None:
        self._update_interface()

    @pynvim.function("MagmaOperatorfunc", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def function_magma_operatorfunc(self, args: List[str]) -> None:
        if not args:
            return

        kind = args[0]

        _, lineno_begin, colno_begin, _ = self.nvim.funcs.getpos("'[")
        _, lineno_end, colno_end, _ = self.nvim.funcs.getpos("']")

        if kind == "line":
            colno_begin = 1
            colno_end = -1
        elif kind == "char":
            pass
        else:
            raise MagmaException(
                f"this kind of selection is not supported: '{kind}'"
            )

        span = (
            (
                lineno_begin - 1,
                min(colno_begin, len(self.nvim.funcs.getline(lineno_begin)))
                - 1,
            ),
            (
                lineno_end - 1,
                min(colno_end, len(self.nvim.funcs.getline(lineno_end))),
            ),
        )

        self._do_evaluate(span)
