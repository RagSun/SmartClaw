"""真实 HTTP 验证用最小服务：仅挂载租户管理路由，跑真实 uvicorn。

不引入 server.py 的重型 lifespan，但用的是**生产同一个 router 与注册表实现**，
因此能真实验证 HTTP 层 CRUD。注册表落在隔离的 HOME（临时目录）下，不污染真实数据。

用法：python scripts/_verify_tenant_api_server.py <port>
"""

import sys

import uvicorn
from fastapi import FastAPI

from smartclaw.tenancy.api import router

app = FastAPI(title="tenant-admin-verify")
app.include_router(router)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8077
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
