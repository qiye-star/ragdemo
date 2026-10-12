# infra/parse-engines/paddle/Dockerfile
# 构建上下文：infra/parse-engines（compose 里 context: ./parse-engines）。
# 版本与源按 T1 验收记录（docs/10.12-plan/验收记录/T1-前置探测.md §4）填写。
# 基础镜像按 digest 固定（2026-10-12 由 docker buildx imagetools inspect 解析的 OCI index digest）。
ARG BASE_IMAGE=python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
FROM ${BASE_IMAGE}
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PADDLE_VERSION=3.3.1
ARG PADDLEOCR_VERSION=3.7.0
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PYTHONUNBUFFERED=1 PADDLE_PDX_MODEL_SOURCE=BOS
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "paddlepaddle==${PADDLE_VERSION}" "paddleocr[doc-parser]==${PADDLEOCR_VERSION}"
WORKDIR /app
# 构建期预热：下载全部模型进镜像层。运行期在 internal 网络里，下载不了任何东西。
RUN python -c "from paddleocr import PPStructureV3; PPStructureV3(device='cpu')"
# 版本固定检查放在预热层之后（不使已缓存的 pip/模型层失效）：paddlex 由 paddleocr 传递依赖解析，
# 漂移会让构建失败而不是悄悄换版本。
ARG PADDLEX_VERSION=3.7.2
RUN python -c "import sys; from importlib.metadata import version as v; assert v('paddlex')=='${PADDLEX_VERSION}', v('paddlex'); assert v('paddlepaddle')=='${PADDLE_VERSION}'; assert v('paddleocr')=='${PADDLEOCR_VERSION}'"
COPY common/shim.py /app/shim.py
COPY paddle/server.py /app/server.py
# Paddle 3.x CPU 默认走 oneDNN(PIR) 执行器，版面检测模型在 3.3.1/3.7.0 上报
# NotImplementedError: ConvertPirAttribute2RuntimeAttribute（T5 冒烟实测）。关掉 oneDNN 默认路径。
ENV PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0
EXPOSE 8000
CMD ["python", "/app/server.py"]
