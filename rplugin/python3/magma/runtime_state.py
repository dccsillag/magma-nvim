from enum import Enum


class RuntimeState(Enum):
    STARTING = 0
    IDLE = 1
    RUNNING = 2
