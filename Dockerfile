# Multi-stage Dockerfile for TaxTestOps
# 生产多阶段构建：构建层编译与安装依赖，运行层精简体积与权限隔离

FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# -----------------------------------------------------------------------------
# 运行时镜像
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

WORKDIR /app

# 创建非 root 运行账号
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/var /app/artifacts && \
    chown -R appuser:appuser /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY src/ ./src/
COPY web/ ./web/
COPY pyproject.toml ./

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TAX_DATABASE_URL=sqlite:////app/var/tax_testops.db

EXPOSE 8000 8100

# 默认启动被测服务；启动平台可通过 command 参数覆盖
CMD ["uvicorn", "tax_service.api.app:app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
