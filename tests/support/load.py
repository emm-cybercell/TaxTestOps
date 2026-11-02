"""负载测试工具：并发执行、延迟采集、SLO 判定。

从 ``tests/perf/conftest.py`` 抽出来，让性能用例可以像其他层一样
``from support.load import ...``，而不必跨目录导入 ``tests.perf.conftest``
（那种写法依赖 pytest 的 sys.path 插入行为，很脆弱）。
"""

from __future__ import annotations

import os
import statistics
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_REQUESTS = int(os.getenv("TAX_PERF_REQUESTS", "120"))
"""单轮负载的请求总数。默认值刻意很小，让性能用例能在几秒内跑完。"""

DEFAULT_WORKERS = int(os.getenv("TAX_PERF_WORKERS", "8"))
"""并发线程数。"""

DEFAULT_TOKEN_POOL_SIZE = int(os.getenv("TAX_PERF_USERS", "4"))
"""令牌池大小。

单用户并发会集中命中同一行的唯一索引与同一批数据页，不能代表真实多用户负载。
"""


@dataclass
class LoadResult:
    """一次负载测试的原始数据。"""

    latencies_ms: list[float] = field(default_factory=list)
    status_codes: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    wall_seconds: float = 0.0

    @property
    def total(self) -> int:
        return len(self.status_codes)

    @property
    def failures(self) -> int:
        return sum(1 for code in self.status_codes if code >= 400) + len(self.errors)

    @property
    def error_rate(self) -> float:
        return self.failures / self.total if self.total else 1.0

    @property
    def throughput(self) -> float:
        return self.total / self.wall_seconds if self.wall_seconds else 0.0

    def percentile(self, value: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, int(len(ordered) * value))
        return ordered[index]

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def p95(self) -> float:
        return self.percentile(0.95)

    @property
    def p99(self) -> float:
        return self.percentile(0.99)

    def summary(self) -> str:
        return (
            f"总请求 {self.total}｜失败 {self.failures}（{self.error_rate:.1%}）｜"
            f"吞吐 {self.throughput:.1f} req/s｜"
            f"P50 {self.p50:.1f}ms｜P95 {self.p95:.1f}ms｜P99 {self.p99:.1f}ms｜"
            f"总耗时 {self.wall_seconds:.2f}s"
        )


def run_load(
    *,
    base_url: str,
    request_factory: Callable[[httpx.Client, int], Any],
    requests_count: int = DEFAULT_REQUESTS,
    workers: int = DEFAULT_WORKERS,
    tokens: list[str] | None = None,
) -> LoadResult:
    """以固定并发跑一轮负载，采集延迟与状态码。

    :param base_url: 被测服务地址。
    :param request_factory: ``(client, index) -> response``，决定每个请求打什么接口。
    :param tokens: 可选的令牌池，按**工作线程**分配，模拟多用户并发。
    """
    result = LoadResult()
    lock = threading.Lock()

    def worker(worker_index: int) -> None:
        headers: dict[str, str] = {}
        if tokens:
            headers["Authorization"] = f"Bearer {tokens[worker_index % len(tokens)]}"

        with httpx.Client(base_url=base_url, timeout=30.0, headers=headers) as client:
            for index in range(worker_index, requests_count, workers):
                began = time.perf_counter()
                try:
                    response = request_factory(client, index)
                    elapsed = (time.perf_counter() - began) * 1000
                    with lock:
                        result.latencies_ms.append(elapsed)
                        result.status_codes.append(response.status_code)
                except Exception as exc:  # noqa: BLE001 - 采集后统一断言
                    with lock:
                        result.errors.append(f"#{index}: {exc!r}")

    began = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, range(workers)))
    result.wall_seconds = time.perf_counter() - began

    return result
