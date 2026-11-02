"""外部依赖适配层。"""

from tax_service.external.policy_client import (
    PolicyParameters,
    PolicyServiceClient,
    PolicyServiceError,
)

__all__ = ["PolicyParameters", "PolicyServiceClient", "PolicyServiceError"]
