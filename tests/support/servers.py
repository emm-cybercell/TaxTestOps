"""在后台线程里跑一个真实的 uvicorn 服务。

为什么需要它
------------
接口层测试用 ``TestClient`` 在进程内直连，最快也最稳。但有两类测试**必须**有真实
监听端口：

- **UI 测试**：浏览器只能访问真实 URL；
- **性能测试**：Locust 需要真实网络栈与并发连接，``TestClient`` 测不出网络开销。

本模块把「起服务 → 等就绪 → 提供 base_url → 关服务」封装成上下文管理器，
端口由操作系统分配（``port=0``），避免与开发机上已占用的端口冲突。
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from types import TracebackType

import uvicorn
from fastapi import FastAPI

STARTUP_TIMEOUT_SECONDS = 20.0
POLL_INTERVAL_SECONDS = 0.05


def find_free_port() -> int:
    """向操作系统申请一个空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class LiveServer:
    """运行中的本地服务。"""

    base_url: str
    port: int
    app: FastAPI
    _server: uvicorn.Server = field(repr=False)
    _thread: threading.Thread = field(repr=False)

    def stop(self) -> None:
        """请求停止并等待线程退出。"""
        self._server.should_exit = True
        self._thread.join(timeout=STARTUP_TIMEOUT_SECONDS)


def serve_app(
    app: FastAPI,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    log_level: str = "warning",
) -> LiveServer:
    """在后台线程启动服务并等待就绪。

    :raises TimeoutError: 超过 ``STARTUP_TIMEOUT_SECONDS`` 仍未监听成功。
    """
    chosen_port = port or find_free_port()
    config = uvicorn.Config(
        app,
        host=host,
        port=chosen_port,
        log_level=log_level,
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="tax-testops-server", daemon=True)
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if server.started:
            break
        if not thread.is_alive():  # pragma: no cover - 启动即崩溃
            raise RuntimeError("uvicorn 线程意外退出，服务启动失败")
        time.sleep(POLL_INTERVAL_SECONDS)
    else:  # pragma: no cover - 启动超时
        raise TimeoutError(f"服务在 {STARTUP_TIMEOUT_SECONDS}s 内未就绪")

    return LiveServer(
        base_url=f"http://{host}:{chosen_port}",
        port=chosen_port,
        app=app,
        _server=server,
        _thread=thread,
    )


class LiveServerContext:
    """``with serve(app) as server:`` 形式的便捷封装。"""

    def __init__(self, app: FastAPI, **kwargs) -> None:
        self._app = app
        self._kwargs = kwargs
        self.server: LiveServer | None = None

    def __enter__(self) -> LiveServer:
        self.server = serve_app(self._app, **self._kwargs)
        return self.server

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.server is not None:
            self.server.stop()
