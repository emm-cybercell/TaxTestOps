"""认证与密码学工具。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

BCRYPT_MAX_BYTES = 72
"""bcrypt 只取密码前 72 字节，超长会报错，故显式截断。"""

DEFAULT_BCRYPT_ROUNDS = 12
"""生产环境哈希轮数。

每增加 1 轮，计算耗时翻倍。12 轮在现代硬件上约 250ms，是「抗暴力破解」与
「登录延迟可接受」之间的常用折中。

测试环境可降到 4 轮（约 15ms），把接口测试套件从 20 秒级压到秒级——bcrypt 的
设计目标就是慢，所以它往往是测试套件里最贵的单点。
"""


def hash_password(password: str, *, rounds: int = DEFAULT_BCRYPT_ROUNDS) -> str:
    """生成密码哈希（bcrypt，自带随机盐）。

    :param rounds: 计算轮数，轮数越高越慢越安全。
    """
    payload = password.encode("utf-8")[:BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(payload, bcrypt.gensalt(rounds)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码。

    使用 :func:`bcrypt.checkpw` 做**恒定时间比较**，避免时序侧信道。
    """
    payload = password.encode("utf-8")[:BCRYPT_MAX_BYTES]
    try:
        return bcrypt.checkpw(payload, password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    subject: str,
    *,
    secret: str,
    algorithm: str = "HS256",
    expires_minutes: int = 60,
    issuer: str = "tax-testops",
    now: datetime | None = None,
) -> tuple[str, int]:
    """签发访问令牌，返回 ``(token, 有效期秒数)``。"""
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=expires_minutes)
    payload = {
        "sub": subject,
        "iss": issuer,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm=algorithm)
    return token, expires_minutes * 60


def decode_access_token(
    token: str,
    *,
    secret: str,
    algorithm: str = "HS256",
    issuer: str = "tax-testops",
) -> dict:
    """解析并校验访问令牌。

    :raises jwt.InvalidTokenError: 签名错误、已过期或签发方不符。
    """
    return jwt.decode(token, secret, algorithms=[algorithm], issuer=issuer)
