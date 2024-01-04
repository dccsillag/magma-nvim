function! s:python_has_module(module) abort
    python3 import importlib
    return py3eval("importlib.util.find_spec(vim.eval('a:module')) is not None")
endfunction

function! s:python_module_check(module, pip_package) abort
    if s:python_has_module(a:module)
        call v:lua.vim.health.ok('Python package ' .. a:pip_package .. ' found')
    else
        call v:lua.vim.health.error('Python package ' .. a:pip_package .. ' not found',
                    \ ['pip install ' .. a:pip_package])
    endif
endfunction

function! health#magma#check() abort
    call v:lua.vim.health.start('requirements')

    if has("nvim-0.5")
        call v:lua.vim.health.ok('NeoVim >=0.5')
    else
        call v:lua.vim.health.error('magma-nvim requires NeoVim >=0.5')
    endif

    if !has("python3")
        call v:lua.vim.health.error('magma-nvim requires a Python provider to be configured!')
        return
    endif

    python3 import sys
    if py3eval("sys.version_info.major == 3 and sys.version_info.minor >= 8")
        call v:lua.vim.health.ok('Python >=3.8')
    else
        call v:lua.vim.health.error('magma-nvim requires Python >=3.8')
    endif

    call s:python_module_check("pynvim", "pynvim")
    call s:python_module_check("jupyter_client", "jupyter-client")
    call s:python_module_check("ueberzug", "ueberzug")
    call s:python_module_check("PIL", "Pillow")
    call s:python_module_check("cairosvg", "cairosvg")
    call s:python_module_check("pnglatex", "pnglatex")
    call s:python_module_check("plotly", "plotly")
    call s:python_module_check("kaleido", "kaleido")
endfunction
