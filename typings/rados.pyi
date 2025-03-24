from typing import Callable

class Rados:
    def __init__(
        self,
        rados_id: str | None = None,
        name: str | None = None,
        clustername: str | None = None,
        conf_defaults: dict[str, str] | None = None,
        conffile: str | int | None = -1,
        conf: dict[str, str] | None = None,
        flags: int = 0,
        context: object = None,
    ) -> None: ...
    def connect(self, timeout: int = 0) -> None: ...
    def get_fsid(self) -> str: ...
    def osd_command(
        self, osdid: int, cmd: str, inbuf: bytes, timeout: int = 0
    ) -> tuple[int, bytes, str]: ...
    def mgr_command(
        self, cmd: str, inbuf: bytes, timeout: int = 0, target: str | None = None
    ) -> tuple[int, str, bytes]: ...
    def mon_command(
        self, cmd: str, inbuf: bytes, timeout: int = 0, target: str | int | None = None
    ) -> tuple[int, bytes, str]: ...
    def monitor_log2(
        self,
        level: str,
        callback: Callable[
            [object, str, bytes, bytes, str, int, int, int, str, bytes], None
        ]
        | None = None,
        arg: object | None = None,
    ) -> None: ...
