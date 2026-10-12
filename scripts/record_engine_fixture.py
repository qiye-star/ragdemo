"""录制引擎薄壳对 tests/fixtures/pdf/table_sample.pdf 第 1 页的真实响应，作为契约测试回放夹具。

用法：uv run --no-sync python scripts/record_engine_fixture.py paddle http://127.0.0.1:8091"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx

from ragdemo.parse.tiered.pdfpages import single_page_pdf

FIXTURE = Path("tests/fixtures/pdf/table_sample.pdf")


def main(name: str, base_url: str) -> None:
    page = single_page_pdf(FIXTURE.read_bytes(), 1)
    with httpx.Client(base_url=base_url, timeout=600) as client:
        health = client.get("/v1/health").json()
        body = {"pdf_base64": base64.b64encode(page).decode("ascii"), "page_no": 1}
        parse = client.post("/v1/parse-page", json=body).json()
    out = Path(f"tests/fixtures/parse_engines/{name}_table_sample.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"health": health, "parse": parse}, ensure_ascii=False, indent=2)
    out.write_text(payload, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
