"""跨模块共享的诊断接口常量。

单独成一个模块（而不是塞进 app.py）是为了避免循环 import：
app.py 需要 include_router(routers.meta.router)，routers/meta.py
的响应体又需要引用同一个横幅字符串——两边都从这里读，谁也不 import 谁。
"""

from __future__ import annotations

# ADR-0010 后果 2：内部工具一旦好用就会被拿给外人看，页面顶部固定这行字
# 作为提醒。这个字符串本身不是由数据生成的论断，是写死的标签（约束 7）。
BANNER = "内部诊断工具，非产品界面"
