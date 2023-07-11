from typing import Optional, Tuple, List, Dict, Generator, IO, Any, Union
from contextlib import contextmanager
from queue import Empty as EmptyQueueException
import os
import tempfile
import json

import jupyter_client

from magma.runtime_state import RuntimeState
from magma.options import MagmaOptions
from magma.outputchunks import (
    Output,
    MimetypesOutputChunk,
    ErrorOutputChunk,
    TextOutputChunk,
    OutputStatus,
    to_outputchunk,
    clean_up_text
)
from magma.jupyter_server_api import JupyterAPIClient, JupyterAPIManager


class JupyterRuntime:
    state: RuntimeState
    kernel_name: str

    kernel_manager: Union[jupyter_client.KernelManager, JupyterAPIManager]
    kernel_client: Union[jupyter_client.KernelClient, JupyterAPIClient]

    allocated_files: List[str]

    options: MagmaOptions

    def __init__(self, kernel_name: str, options: MagmaOptions):
        self.state = RuntimeState.STARTING
        self.kernel_name = kernel_name

        if kernel_name.startswith("http://") or kernel_name.startswith("https://"):
            self.external_kernel = True
            self.kernel_manager = JupyterAPIManager(kernel_name)
            self.kernel_manager.start_kernel()
            self.kernel_client = self.kernel_manager.client()
            self.kernel_client.start_channels()

            self.allocated_files = []

            self.options = options

        elif ".json" not in self.kernel_name:

            self.external_kernel = True
            self.kernel_manager = jupyter_client.manager.KernelManager(
                kernel_name=kernel_name
            )
            self.kernel_manager.start_kernel()
            self.kernel_client = self.kernel_manager.client()
            assert isinstance(
                self.kernel_client,
                jupyter_client.blocking.client.BlockingKernelClient,
            )
            self.kernel_client.start_channels()

            self.allocated_files = []

            self.options = options

        else:
            kernel_file = kernel_name
            self.external_kernel = True
            # Opening JSON file
            kernel_json = json.load(open(kernel_file))
            # we have a kernel json
            self.kernel_manager = jupyter_client.manager.KernelManager(
                    kernel_name=kernel_json["kernel_name"]
                    )
            self.kernel_client = self.kernel_manager.client()

            self.kernel_client.load_connection_file(connection_file=kernel_file)

            self.allocated_files = []

            self.options = options

    def is_ready(self) -> bool:
        return self.state.value > RuntimeState.STARTING.value

    def deinit(self) -> None:
        for path in self.allocated_files:
            if os.path.exists(path):
                os.remove(path)

        if self.external_kernel is False:
            self.kernel_client.shutdown()

    def interrupt(self) -> None:
        self.kernel_manager.interrupt_kernel()

    def restart(self) -> None:
        self.state = RuntimeState.STARTING
        self.kernel_manager.restart_kernel()

    def run_code(self, code: str) -> None:
        self.kernel_client.execute(code)

    @contextmanager
    def _alloc_file(
        self, extension: str, mode: str
    ) -> Generator[Tuple[str, IO[bytes]], None, None]:
        with tempfile.NamedTemporaryFile(
            suffix="." + extension, mode=mode, delete=False
        ) as file:
            path = file.name
            yield path, file
        self.allocated_files.append(path)

    def _append_chunk(
        self, output: Output, data: Dict[str, Any], metadata: Dict[str, Any]
    ) -> None:
        if self.options.show_mimetype_debug:
            output.chunks.append(MimetypesOutputChunk(list(data.keys())))

        output.chunks.append(to_outputchunk(self._alloc_file, data, metadata))

    def _tick_one(
        self, output: Output, message_type: str, content: Dict[str, Any]
    ) -> bool:

        def copy_on_demand(content_ctor):
            if self.options.copy_output:
                import pyperclip
                if type(content_ctor) is str:
                    pyperclip.copy(content_ctor)
                else:
                    pyperclip.copy(content_ctor())

        if output._should_clear:
            output.chunks.clear()
            output._should_clear = False

        if message_type == "execute_input":
            output.execution_count = content["execution_count"]
            if self.external_kernel is False:
                assert output.status != OutputStatus.DONE
                if output.status == OutputStatus.HOLD:
                    output.status = OutputStatus.RUNNING
                elif output.status == OutputStatus.RUNNING:
                    output.status = OutputStatus.DONE
                else:
                    raise ValueError(
                        "bad value for output.status: %r" % output.status
                    )
            return True
        elif message_type == "status":
            execution_state = content["execution_state"]
            assert execution_state != "starting"
            if execution_state == "idle":
                self.state = RuntimeState.IDLE
                output.status = OutputStatus.DONE
                return True
            elif execution_state == "busy":
                self.state = RuntimeState.RUNNING
                return True
            else:
                return False
        elif message_type == "execute_reply":
            # This doesn't really give us any relevant information.
            return False
        elif message_type == "execute_result":
            self._append_chunk(output, content["data"], content["metadata"])
            if 'text/plain' in content['data']:
                copy_on_demand(content["data"]['text/plain'])
            return True
        elif message_type == "error":
            output.chunks.append(
                ErrorOutputChunk(
                    content["ename"], content["evalue"], content["traceback"]
                )
            )
            copy_on_demand(lambda: "\n\n".join(map(clean_up_text, content["traceback"])))
            output.success = False
            return True
        elif message_type == "stream":
            copy_on_demand(content["text"])
            output.chunks.append(TextOutputChunk(content["text"]))
            return True
        elif message_type == "display_data":
            # XXX: consider content['transient'], if we end up saving execution
            # outputs.
            self._append_chunk(output, content["data"], content["metadata"])
            return True
        elif message_type == "update_display_data":
            # We don't really want to bother with this type of message.
            return False
        elif message_type == "clear_output":
            if content["wait"]:
                output._should_clear = True
            else:
                output.chunks.clear()
            return True
        # TODO: message_type == 'debug'?
        else:
            return False

    def tick(self, output: Optional[Output]) -> bool:
        did_stuff = False

        assert isinstance(
            self.kernel_client,
            jupyter_client.blocking.client.BlockingKernelClient,
        ) or isinstance(
            self.kernel_client, JupyterAPIClient)
        if not self.is_ready():
            try:
                self.kernel_client.wait_for_ready(timeout=0)
                self.state = RuntimeState.IDLE
                did_stuff = True
            except RuntimeError:
                return False

        if output is None:
            return did_stuff

        while True:
            try:
                message = self.kernel_client.get_iopub_msg(timeout=0)

                if "content" not in message or "msg_type" not in message:
                    continue

                did_stuff_now = self._tick_one(
                    output, message["msg_type"], message["content"]
                )
                did_stuff = did_stuff or did_stuff_now

                if output.status == OutputStatus.DONE:
                    break
            except EmptyQueueException:
                break

        return did_stuff


def get_available_kernels() -> List[str]:
    return list(jupyter_client.kernelspec.find_kernel_specs().keys())
