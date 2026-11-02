"""基础设施适配层。"""

from tax_service.infra.cache import (
    Cache,
    InMemoryCache,
    RedisCache,
    build_cache,
)

__all__ = ["Cache", "InMemoryCache", "RedisCache", "build_cache"]
