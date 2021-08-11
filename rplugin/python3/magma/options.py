from pynvim import Nvim


class MagmaOptions:
    automatically_open_output: bool
    show_mimetype_debug: bool
    cell_highlight_group: str

    def __init__(self, nvim: Nvim):
        self.automatically_open_output = nvim.vars.get("magma_automatically_open_output", True)
        self.show_mimetype_debug = nvim.vars.get("magma_show_mimetype_debug", False)
        self.cell_highlight_group = nvim.vars.get("magma_cell_highlight_group", "CursorLine")
