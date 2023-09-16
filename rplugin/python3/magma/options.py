import os

from pynvim import Nvim


class MagmaOptions:
    automatically_open_output: bool
    wrap_output: bool
    output_window_borders: bool
    show_mimetype_debug: bool
    cell_highlight_group: str
    save_path: str
    image_provider: str
    copy_output: bool
    enter_output_behavior: str

    def __init__(self, nvim: Nvim):
        # fmt: off
        CONFIG_VARS = [
            ("magma_automatically_open_output", True),
            ("magma_wrap_output", False),
            ("magma_output_window_borders", True),
            ("magma_show_mimetype_debug", False),
            ("magma_cell_highlight_group", "CursorLine"),
            ("magma_save_cell", os.path.join(nvim.funcs.stdpath("data"), "magma")),
            ("magma_image_provider", "none"),
            ("magma_copy_output", False),
            ("magma_enter_output_behavior", "open_then_enter") # "open_then_enter", "open_and_enter", or "no_open"
        ]
        # fmt: on

        for name, default in CONFIG_VARS:
            setattr(self, name[6:], nvim.vars.get(name, default))
