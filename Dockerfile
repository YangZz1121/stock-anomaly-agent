FROM python:3.11-slim

# Hugging Face Spaces 以非 root 用户运行容器，用户目录必须可写，
# 否则行业映射缓存（.cache/）写不进去。
RUN useradd -m -u 1000 appuser \
    && mkdir -p /home/appuser/app/.cache \
    && chown -R appuser:appuser /home/appuser/app
WORKDIR /home/appuser/app

COPY --chown=appuser:appuser requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser fixtures/ ./fixtures/
COPY --chown=appuser:appuser scripts/ ./scripts/

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860

EXPOSE 7860

# 密钥一律通过环境变量注入（HF Spaces 用 Space Secrets），镜像里不包含任何密钥。
# 不配置密钥时产品会自动降级到内置构造数据集并在页面上声明。
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
