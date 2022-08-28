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

    def __init__(self, nvim: Nvim):
        self.automatically_open_output = nvim.vars.get(
            "magma_automatically_open_output", True
        )
        self.wrap_output = nvim.vars.get("magma_wrap_output", True)
        self.output_window_borders = nvim.vars.get(
            "magma_output_window_borders", True
        )
        self.show_mimetype_debug = nvim.vars.get(
            "magma_show_mimetype_debug", False
        )
        self.cell_highlight_group = nvim.vars.get(
            "magma_cell_highlight_group", "CursorLine"
        )
        self.save_path = nvim.vars.get(
            "magma_save_cell",
            os.path.join(nvim.funcs.stdpath("data"), "magma"),
        )
        self.image_provider = nvim.vars.get("magma_image_provider", "none")
