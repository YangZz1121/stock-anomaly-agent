FROM python:3.11-slim

# 以非 root 用户运行，并预先把工作目录的属主交给它——
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

# 端口由托管平台通过 PORT 注入（Render 默认 10000），这里只给一个本地默认值。
# 不写死端口是为了让本地、Render 和其他容器平台共用同一个镜像。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000

# 密钥一律通过运行时环境变量注入，镜像里不包含任何密钥，
# Dockerfile 也不引用任何构建参数——否则密钥会被烘进镜像层。
# 不配置密钥时产品会自动降级到内置构造数据集并在页面上声明。
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
