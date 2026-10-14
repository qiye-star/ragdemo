# tests/contracts/ — 外部适配器的契约测试（CLAUDE.md §1.5 / §3）

## 这个目录里有什么

| 文件 | 用途 |
|---|---|
| `adapter_contract.py`、`announcement_contract.py`、`dataapi_contract.py`、`embedder_contract.py`、`reranker_contract.py`、`mcp_gateway_contract.py`、`page_engine_contract.py`、`html_renderer_contract.py` | 契约基类：定义某类适配器必须满足的行为（时区、幂等、顺序、哈希稳定、`known_at` 回填等） |
| `test_mock_*.py` | 把 Mock 实现挂到对应契约上，免费、离线、必须常绿 |
| `test_pg_dataapi.py`、`test_wind_contract.py` | 真实实现也要过同一套契约：PgDataApi 种进临时库跑；Wind 适配器接一个「按问句现场作答」的假网关（不出网） |
| `test_paddle_page_engine.py` | 同时有 Mock 版与 `@pytest.mark.live_engine` 版（要 `PARSE_PADDLE_BASE_URL`） |
| `test_edge_html_renderer.py` | 本机 Edge 渲染，`RAGDEMO_RUN_LOCAL_EDGE=1` 才跑 |

## 硬规则

- 新增外部适配器 = 新增 Mock + 契约基类的一个子类；业务代码不得 import 供应商 SDK。
- 契约基类只写「行为约定」，不写供应商细节；真实/录制与 Mock 必须过**同一套**断言，否则 Mock 会漂离真实。
- 契约测试不得出网、不得带密钥；真实响应用 `tests/fixtures/` 里脱敏后的录制文件或测试内的假网关（可用 `scripts/record_engine_fixture.py` 录引擎夹具）。

## 常见坑

- `live_engine` / `local_edge` 用例在默认环境里会 skip，别把「skip」当作「通过」写进验收记录。
