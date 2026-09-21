FROM mitmproxy/mitmproxy:11.0.0
COPY policy.yaml /policy/policy.yaml
COPY addon.py /policy/addon.py
ENTRYPOINT ["mitmdump", "--set", "block_global=false", "-s", "/policy/addon.py"]
