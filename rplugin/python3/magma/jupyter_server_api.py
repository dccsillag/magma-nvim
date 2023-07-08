import json
import uuid
import re
from queue import Empty as EmptyQueueException
from typing import Any, Dict
from threading import Thread
from queue import Queue
from urllib.parse import urlparse

import requests
import websocket

from magma.runtime_state import RuntimeState


class JupyterAPIClient:
    def __init__(self,
                 url: str,
                 kernel_info: Dict[str, Any],
                 headers: Dict[str, str]):
        self._base_url = url
        self._kernel_info = kernel_info
        self._headers = headers

        self._recv_queue: Queue[Dict[str, Any]] = Queue()

    def wait_for_ready(self, timeout=0):
        pass

    def start_channels(self) -> None:
        parsed_url = urlparse(self._base_url)
        self._socket = websocket.create_connection(f"ws://{parsed_url.hostname}:{parsed_url.port}"
                                                   f"/api/kernels/{self._kernel_info['id']}/channels",
                                                   header=self._headers,
                                                   )
        self._kernel_api_base = f"{self._base_url}/api/kernels/{self._kernel_info['id']}"

        self._iopub_recv_thread = Thread(target=self._recv_message)
        self._iopub_recv_thread.start()

    def _recv_message(self) -> None:
        while True:
            response = json.loads(self._socket.recv())
            self._recv_queue.put(response)

    def get_iopub_msg(self, **kwargs):
        if self._recv_queue.empty():
            raise EmptyQueueException

        response = self._recv_queue.get()

        return response

    def execute(self, code: str):
        header = {
            'msg_type': 'execute_request',
            'msg_id': uuid.uuid1().hex,
            'session': uuid.uuid1().hex
        }

        message = json.dumps({
            'header': header,
            'parent_header': header,
            'metadata': {},
            'content': {
                'code': code,
                'silent': False
            }
        })
        self._socket.send(message)

    def shutdown(self):
        requests.delete(self._kernel_api_base,
                        headers=self._headers)
        self._socket.close()


class JupyterAPIManager:
    def __init__(self,
                 url: str,
                 ):
        parsed_url = urlparse(url)
        self._base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

        token_part = re.search(r"token=(.*)", parsed_url.query)

        if token_part:
            token = token_part.groups()[0]
            self._headers = {'Authorization': 'token ' + token}
        else:
            # Run notebook with --NotebookApp.disable_check_xsrf="True".
            self._headers = {}

    def start_kernel(self) -> None:
        url = f"{self._base_url}/api/kernels"
        response = requests.post(url,
                                 headers=self._headers)
        self._kernel_info = json.loads(response.text)
        assert "id" in self._kernel_info, "Could not connect to Jupyter Server API. The URL specified may be incorrect."
        self._kernel_api_base = f"{url}/{self._kernel_info['id']}"

    def client(self) -> JupyterAPIClient:
        return JupyterAPIClient(url=self._base_url,
                                kernel_info=self._kernel_info,
                                headers=self._headers)

    def interrupt_kernel(self) -> None:
        requests.post(f"{self._kernel_api_base}/interrupt",
                      headers=self._headers)

    def restart_kernel(self) -> None:
        self.state = RuntimeState.STARTING
        requests.post(f"{self._kernel_api_base}/restart",
                      headers=self._headers)
