-- loads the image.nvim plugin and exposes methods to the python remote plugin
local ok, image = pcall(require, "image")

if not ok then
  vim.api.nvim_err_writeln("[magma.nvim] `image.nvim` not found")
  return
end

print('image.nvim loaded')

local image_api = {}
local images = {}
local utils = {}

image_api.from_file = function(path, opts)
  images[path] = image.from_file(path, opts or {})
  return path
end

image_api.render = function(identifier, geometry)
  geometry = geometry or {}
  images[identifier]:render(geometry)
end

image_api.clear = function(identifier)
  images[identifier]:clear()
end

image_api.clear_all = function()
  for _, img in pairs(images) do
    img:clear()
  end
end

image_api.move = function(identifier, x, y)
  images[identifier]:move(x, y)
end

image_api.update_window = function(identifier, window)
  local img = images[identifier]
  img.window = window
  img:clear()
end

image_api.image_size = function(identifier)
  local img = images[identifier]
  return { width = img.image_width, height = img.image_height }
end

------ utils --------

utils.cell_size = function()
  local size = require("image.utils.term").get_size()
  return { width = size.cell_width, height = size.cell_height }
end

return { image_api = image_api, image_utils = utils }
