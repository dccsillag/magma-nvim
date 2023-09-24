-- loads the image.nvim plugin and exposes methods to the python remote plugin
local ok, image = pcall(require, "image")

if not ok then
  vim.api.nvim_err_writeln("[magma.nvim] `image.nvim` not found")
  return
end

print('image.nvim loaded')

local image_api = {}
local images = {}

image_api.from_file = function(path)
  images[path] = image.from_file(path)
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

return image_api
