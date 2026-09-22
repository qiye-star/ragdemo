FROM mitmproxy/mitmproxy:11.0.0
# 基础镜像自带 ruamel.yaml（mitmproxy 自身配置用），但没有 addon.py 依赖的
# PyYAML（`import yaml`）。Phase 1B 首次实际启动这个容器时才发现——此前
# 只审过代码，从未跑起来过。
RUN pip install --no-cache-dir pyyaml==6.0.2
COPY policy.yaml /policy/policy.yaml
COPY addon.py /policy/addon.py
ENTRYPOINT ["mitmdump", "--set", "block_global=false", "-s", "/policy/addon.py"]
