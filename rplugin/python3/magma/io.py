from typing import Type, Optional, Dict, Any
import os

from pynvim.api import Buffer

from magma.utils import MagmaException, Span, DynamicPosition
from magma.options import MagmaOptions
from magma.outputchunks import OutputStatus, Output, to_outputchunk
from magma.outputbuffer import OutputBuffer
from magma.magmabuffer import MagmaBuffer


class MagmaIOError(Exception):
    @classmethod
    def assert_has_key(
        cls, data: Dict[str, Any], key: str, type_: Optional[Type[Any]] = None
    ) -> Any:
        if key not in data:
            raise cls(f"Missing key: {key}")
        value = data[key]
        if type_ is not None and not isinstance(value, type_):
            raise cls(
                f"Incorrect type for key '{key}': expected {type_.__name__}, \
                got {type(value).__name__}"
            )
        return value


def get_default_save_file(options: MagmaOptions, buffer: Buffer) -> str:
    # XXX: this is string containment checking. Beware.
    if "nofile" in buffer.options["buftype"]:
        raise MagmaException("Buffer does not correspond to a file")

    mangled_name = buffer.name.replace("%", "%%").replace("/", "%")

    return os.path.join(options.save_path, mangled_name + ".json")


def load(magmabuffer: MagmaBuffer, data: Dict[str, Any]) -> None:
    MagmaIOError.assert_has_key(data, "content_checksum", str)

    if magmabuffer._get_content_checksum() != data["content_checksum"]:
        raise MagmaIOError("Buffer contents' checksum does not match!")

    MagmaIOError.assert_has_key(data, "cells", list)
    for cell in data["cells"]:
        MagmaIOError.assert_has_key(cell, "span", dict)
        MagmaIOError.assert_has_key(cell["span"], "begin", dict)
        MagmaIOError.assert_has_key(cell["span"]["begin"], "lineno", int)
        MagmaIOError.assert_has_key(cell["span"]["begin"], "colno", int)
        MagmaIOError.assert_has_key(cell["span"], "end", dict)
        MagmaIOError.assert_has_key(cell["span"]["end"], "lineno", int)
        MagmaIOError.assert_has_key(cell["span"]["end"], "colno", int)
        begin_position = DynamicPosition(
            magmabuffer.nvim,
            magmabuffer.extmark_namespace,
            magmabuffer.buffer.number,
            cell["span"]["begin"]["lineno"],
            cell["span"]["begin"]["colno"],
        )
        end_position = DynamicPosition(
            magmabuffer.nvim,
            magmabuffer.extmark_namespace,
            magmabuffer.buffer.number,
            cell["span"]["end"]["lineno"],
            cell["span"]["end"]["colno"],
        )
        span = Span(begin_position, end_position)

        # XXX: do we really want to have the execution count here?
        #      what happens when the counts start to overlap?
        MagmaIOError.assert_has_key(cell, "execution_count", int)
        output = Output(cell["execution_count"])

        MagmaIOError.assert_has_key(cell, "status", int)
        output.status = OutputStatus(cell["status"])

        MagmaIOError.assert_has_key(cell, "success", bool)
        output.success = cell["success"]

        MagmaIOError.assert_has_key(cell, "chunks", list)
        for chunk in cell["chunks"]:
            MagmaIOError.assert_has_key(chunk, "data", dict)
            MagmaIOError.assert_has_key(chunk, "metadata", dict)
            output.chunks.append(
                to_outputchunk(
                    magmabuffer.runtime._alloc_file,
                    chunk["data"],
                    chunk["metadata"],
                )
            )

        output.old = True

        magmabuffer.outputs[span] = OutputBuffer(
            magmabuffer.nvim, magmabuffer.canvas, magmabuffer.options
        )


def save(magmabuffer: MagmaBuffer) -> Dict[str, Any]:
    return {
        "version": 1,
        "kernel": magmabuffer.runtime.kernel_name,
        "content_checksum": magmabuffer._get_content_checksum(),
        "cells": [
            {
                "span": {
                    "begin": {
                        "lineno": span.begin.lineno,
                        "colno": span.begin.colno,
                    },
                    "end": {
                        "lineno": span.end.lineno,
                        "colno": span.end.colno,
                    },
                },
                "execution_count": output.output.execution_count,
                "status": output.output.status.value,
                "success": output.output.success,
                "chunks": [
                    {
                        "data": chunk.jupyter_data,
                        "metadata": chunk.jupyter_metadata,
                    }
                    for chunk in output.output.chunks
                    if chunk.jupyter_data is not None
                    and chunk.jupyter_metadata is not None
                ],
            }
            for span, output in magmabuffer.outputs.items()
        ],
    }
