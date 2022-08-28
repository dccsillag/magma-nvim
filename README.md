# Magma

Magma is a NeoVim plugin for running code interactively with Jupyter.

![](https://user-images.githubusercontent.com/15617291/128964224-f157022c-25cd-4a60-a0da-7d1462564ae4.gif)

## Requirements

- NeoVim 0.5+
- Python 3.8+
- Required Python packages:
    - [`pynvim`](https://github.com/neovim/pynvim) (for the Remote Plugin API)
    - [`jupyter_client`](https://github.com/jupyter/jupyter_client) (for interacting with Jupyter)
    - [`ueberzug`](https://github.com/seebye/ueberzug) (for displaying images. Not available on MacOS, but see [#15](https://github.com/dccsillag/magma-nvim/issues/15) for alternatives)
    - [`Pillow`](https://github.com/python-pillow/Pillow) (also for displaying images, should be installed with `ueberzug`)
    - [`cairosvg`](https://cairosvg.org/) (for displaying SVG images)
    - [`pnglatex`](https://pypi.org/project/pnglatex/) (for displaying TeX formulas)
    - `plotly` and `kaleido` (for displaying Plotly figures)

You can do a `:checkhealth` to see if you are ready to go.

**Note:** Python packages which are used only for the display of some specific kind of output are only imported when that output actually appears.

## Installation

Use your favourite package/plugin manager.

If you use `packer.nvim`,

```lua
use { 'dccsillag/magma-nvim', run = ':UpdateRemotePlugins' }
```

If you use `vim-plug`,

```vim
Plug 'dccsillag/magma-nvim', { 'do': ':UpdateRemotePlugins' }
```

Note that you will still need to configure keymappings -- see [Keybindings](#keybindings).

## Suggested settings

If you want a quickstart, these are the author's suggestions of mappings and options (beware of potential conflicts of these mappings with your own!):

```vim
nnoremap <silent><expr> <LocalLeader>r  :MagmaEvaluateOperator<CR>
nnoremap <silent>       <LocalLeader>rr :MagmaEvaluateLine<CR>
xnoremap <silent>       <LocalLeader>r  :<C-u>MagmaEvaluateVisual<CR>
nnoremap <silent>       <LocalLeader>rc :MagmaReevaluateCell<CR>
nnoremap <silent>       <LocalLeader>rd :MagmaDelete<CR>
nnoremap <silent>       <LocalLeader>ro :MagmaShowOutput<CR>

let g:magma_automatically_open_output = v:false
let g:magma_image_provider = "ueberzug"
```

**Note:** Key mappings are not defined by default because of potential conflicts -- the user should decide which keys they want to use (if at all).

**Note:** The options that are altered here don't have these as their default values in order to provide a simpler (albeit perhaps a bit more inconvenient) UI for someone who just added the plugin without properly reading the README.

## Usage

The plugin provides a bunch of commands to enable interaction. It is recommended to map most of them to keys, as explained in [Keybindings](#keybindings). However, this section will refer to the commands by their names (so as to not depend on some specific mappings).

### Interface

When you execute some code, it will create a *cell*. You can recognize a cell because it will be highlighted when your cursor is in it.

A cell is delimited using two extmarks (see `:help api-extended-marks`), so it will adjust to you editing the text within it.

When your cursor is in a cell (i.e., you have an *active cell*), a floating window may be shown below the cell, reporting output. This is the *display window*, or *output window*. (To see more about whether a window is shown or not, see `:MagmaShowOutput` and `g:magma_automatically_open_output`). When you cursor is not in any cell, no cell is active.

Also, the active cell is searched for from newest to oldest. That means that you can have a cell within another cell, and if the one within is newer, then that one will be selected. (Same goes for merely overlapping cells).

The output window has a header, containing the execution count and execution state (i.e., whether the cell is waiting to be run, running, has finished successfully or has finished with an error). Below the header are shown the outputs.

Jupyter provides a rich set of outputs. To see what we can currently handle, see [Output Chunks](#output-chunks).

### Commands

#### MagmaInit

This command initializes a runtime for the current buffer.

It can take a single argument, the Jupyter kernel's name. For example,

```vim
:MagmaInit python3
```

will initialize the current buffer with a `python3` kernel.

It can also be called with no arguments, as such:

```vim
:MagmaInit
```

This will prompt you for which kernel you want to launch (from the list of available kernels).

#### MagmaDeinit

This command deinitializes the current buffer's runtime and magma instance.

```vim
:MagmaDeinit
```

**Note** You don't need to run this, as deinitialization will happen automatically upon closing Vim or the buffer being unloaded. This command exists in case you just want to make Magma stop running.

#### MagmaEvaluateLine

Evaluate the current line.

Example usage:

```vim
:MagmaEvaluateLine
```

#### MagmaEvaluateVisual

Evaluate the selected text.

Example usage (after having selected some text):

```vim
:MagmaEvaluateVisual
```

#### MagmaEvaluateOperator

Evaluate the text given by some operator.

This won't do much outside of an `<expr>` mapping. Example usage:

```vim
nnoremap <expr> <LocalLeader>r nvim_exec('MagmaEvaluateOperator', v:true)
```

Upon using this mapping, you will enter operator mode, with which you will be able to select text you want to execute. You can, of course, hit ESC to cancel, as usual with operator mode.

#### MagmaReevaluateCell

Reevaluate the currently selected cell.

```vim
:MagmaReevaluateCell
```

#### MagmaDelete

Delete the currently selected cell. (If there is no selected cell, do nothing.)

Example usage:

```vim
:MagmaDelete
```

#### MagmaShowOutput

This only makes sense when you have `g:magma_automatically_open_output = v:false`. See [Customization](#customization).

Running this command with some active cell will open the output window.

Example usage:

```vim
:MagmaShowOutput
```

#### MagmaInterrupt

Send a keyboard interrupt to the kernel. Interrupts the currently running cell and does nothing if not
cell is running.

Example usage:

```vim
:MagmaInterrupt
```

#### MagmaRestart

Shuts down and restarts the current kernel.

Optionally deletes all output if used with a bang.

Example usage:

```vim
:MagmaRestart
```

Example usage (also deleting outputs):

```vim
:MagmaRestart!
```

#### MagmaSave

Save the current cells and evaluated outputs into a JSON file, which can then be loaded back with [`:MagmaLoad`](#magmaload).

It has two forms; first, receiving a parameter, specifying where to save to:

```vim
:MagmaSave file_to_save.json
```

If that parameter is omitted, then one will be automatically generated using the `g:magma_save_path` option.

```vim
:MagmaSave
```

#### MagmaLoad

Load the cells and evaluated outputs stored in a given JSON file, which should have been generated with [`:MagmaSave`](#magmasave).

Like `MagmaSave`, It has two forms; first, receiving a parameter, specifying where to save to:

```vim
:MagmaLoad file_to_load.json
```

If that parameter is omitted, then one will be automatically generated using the `g:magma_save_path` option.

#### MagmaEnterOutput

Enter the output window, if it is currently open. You must call this as follows:

```vim
:noautocmd MagmaEnterOutput
```

This is escpecially useful when you have a long output (or errors) and wish to inspect it.

## Keybindings

It is recommended to map all the evaluate commands to the same mapping (in different modes). For example, if we wanted to bind evaluation to `<LocalLeader>r`:

```vim
nnoremap <expr><silent> <LocalLeader>r  nvim_exec('MagmaEvaluateOperator', v:true)
nnoremap <silent>       <LocalLeader>rr :MagmaEvaluateLine<CR>
xnoremap <silent>       <LocalLeader>r  :<C-u>MagmaEvaluateVisual<CR>
nnoremap <silent>       <LocalLeader>rc :MagmaReevaluateCell<CR>
```

This way, `<LocalLeader>r` will behave just like standard keys such as `y` and `d`.

You can, of course, also map other commands:

```vim
nnoremap <silent> <LocalLeader>rd :MagmaDelete<CR>
nnoremap <silent> <LocalLeader>ro :MagmaShowOutput<CR>
nnoremap <silent> <LocalLeader>rq :noautocmd MagmaEnterOutput<CR>
```

## Customization

Customization is done via variables.

### `g:magma_image_provider`

Defaults to `"none"`.

This configures how to display images. The following options are available:

- `"none"` -- don't show images.
- `"ueberzug"` -- use [Ueberzug](https://github.com/seebye/ueberzug) to display images.
- `"kitty"` -- use the Kitty protocol to display images.

### `g:magma_automatically_open_output`

Defaults to `v:true`.

If this is true, then whenever you have an active cell its output window will be automatically shown.

If this is false, then the output window will only be automatically shown when you've just evaluated the code. So, if you take your cursor out of the cell, and then come back, the output window won't be opened (but the cell will be highlighted). This means that there will be nothing covering your code. You can then open the output window at will using [`:MagmaShowOutput`](#magma-show-output).

### `g:magma_wrap_output`

Defaults to `v:true`.

If this is true, then text output in the output window will be wrapped (akin to `set wrap`).

### `g:magma_output_window_borders`

Defaults to `v:true`.

If this is true, then the output window will have rounded borders. If it is false, it will have no borders.

### `g:magma_cell_highlight_group`

Defaults to `"CursorLine"`.

The highlight group to be used for highlighting cells.

### `g:magma_save_path`

Defaults to `stdpath("data") .. "/magma"`.

Where to save/load with [`:MagmaSave`](#magmasave) and [`:MagmaLoad`](#magmaload) (with no parameters).

The generated file is placed in this directory, with the filename itself being the buffer's name, with `%` replaced by `%%` and `/` replaced by `%`, and postfixed with the extension `.json`.

### [DEBUG] `g:magma_show_mimetype_debug`

Defaults to `v:false`.

If this is true, then before any non-iostream output chunk, Magma shows the mimetypes it received for it.

This is meant for debugging and adding new mimetypes.

## Autocommands

We provide some `User` autocommands (see `:help User`) for further customization. They are:

- `MagmaInitPre`: runs right before `MagmaInit` initialization happens for a buffer
- `MagmaInitPost`: runs right after `MagmaInit` initialization happens for a buffer
- `MagmaDeinitPre`: runs right before `MagmaDeinit` deinitialization happens for a buffer
- `MagmaDeinitPost`: runs right after `MagmaDeinit` deinitialization happens for a buffer

## Extras

### Output Chunks

In the Jupyter protocol, most output-related messages provide a dictionary of mimetypes which can be used to display the data. Theoretically, a `text/plain` field (i.e., plain text) is always present, so we (theoretically) always have that fallback.

Here is a list of the currently handled mimetypes:

- `text/plain`: Plain text. Shown as text in the output window's buffer.
- `image/png`: A PNG image. Shown according to `g:magma_image_provider`.
- `image/svg+xml`: A SVG image. Rendered into a PNG with [CairoSVG](https://cairosvg.org/) and shown with [Ueberzug](https://github.com/seebye/ueberzug).
- `application/vnd.plotly.v1+json`: A Plotly figure. Rendered into a PNG with [Plotly](https://plotly.com/python/) + [Kaleido](https://github.com/plotly/Kaleido) and shown with [Ueberzug](https://github.com/seebye/ueberzug).
- `text/latex`: A LaTeX formula. Rendered into a PNG with [pnglatex](https://pypi.org/project/pnglatex/) and shown with [Ueberzug](https://github.com/seebye/ueberzug).

This already provides quite a bit of basic functionality. As development continues, more mimetypes will be added.

### Notifications

We use the `vim.notify` API. This means that you can use plugins such as [rcarriga/nvim-notify](https://github.com/rcarriga/nvim-notify) for pretty notifications.
