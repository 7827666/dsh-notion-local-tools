# 可选 notion2api 服务端模块

此目录供维护已有 notion2api 的运维者使用。朋友只需安装 DSH 插件并在本机填写服务地址和 API key。这里没有服务器地址、配置文件、账户、密钥或真实会话。

`app/agent_bridge.py`、`app/api/agent.py` 与 `app/agent_protocol.py` 提供单账号单在途、持久异步任务、v2 同线程续聊和严格的本轮结果判定。它们依赖现有项目的 `app.notion_client.NotionUpstreamError`、`app.logger`、`app.conversation.build_standard_transcript`、`app.model_registry.is_supported_model`，以及已配置并通过认证的 account pool；不是可独立运行的服务器，也不是上游任意版本的一键补丁。

集成前先对照接口、备份源文件与状态目录，确认没有 queued/pending 任务，保留现有鉴权与路由前缀，再合并这三个模块。chat completions 处理器在选择 agent 协议时委托：

```python
from app.api.agent import handle_agent_request
return await handle_agent_request(request, req_body)
```

同一个受鉴权保护的路由器还需要注册：

```python
@router.get('/agent/jobs/{job_id}')
async def get_agent_job_status(request: Request, job_id: str):
    from app.api.agent import get_agent_job
    return await get_agent_job(request, job_id)
```

两条路径必须经过相同的 API key 认证。不得将状态查询注册成公开路径。使用一个 ASGI worker；进程内锁不是多实例分布式锁。状态目录默认是应用根目录的 `data/agent-jobs`，可由 `NOTION_AGENT_STATE_DIR` 指定，运行账户必须可读写；文件权限为 0600，目录为 0700。不要用 root 探测生成并留下服务账户不可写的状态文件。

客户端的 `x-notion-async: 1` 启用短连接任务查询。`x-notion-local-protocol: dsh-local-tools-v2` 才启用新的本地工具判定与续聊；保留普通 Notion 云端通道。模型名称仍须由已有模型注册表映射成当前账号可用的上游名称。

服务端先持久化 queued 任务再返回 202；提交生成前持久化 pending。pending 在重启、断线或超时后只查询原线程，绝不盲目重发 create/send。已完成结果独立缓存 24 小时，未知任务不因 TTL 自动重建。默认后台等待 30 分钟、同步兼容请求最多 240 秒；DSH 客户端默认总等待 31 分钟。

同线程复用要求账号、DSH 会话标识、模型、工具/系统定义及历史前缀一致。持久上下文只记录哈希和线程水位；queued 状态暂存尚未提交的请求文本，提交后移除。任务结果和工具参数仍可能包含用户内容，因此整个状态目录必须保持私有。

轮询补齐前后分页，核对本轮 committed user message、turn ID、end_turn、idle 与 pending=false。需要云端确认、被中断、被其他消息覆盖、协议不合法和未知状态分别报告；未支持的 transcript 操作保持保守失败。这里没有完整 NDJSON 增量 reducer、原生 MCP 或图片上传实现。取消客户端等待也不会取消上游生成。

部署后用独立文件验证实际 DSH read → write → read，并检查 journal 的 `agent_bridge_create` / `agent_bridge_continue`，确认同一工具循环是否复用了线程。再验证重启恢复、GET 临时失败、原有云端通道、服务资源与现有代理；只通过语法检查不能宣称生产可用。反向代理监听、TLS 和防火墙不属于本模块的安装步骤。

已有 Python 环境安装 requests 和 FastAPI 后，可从仓库根目录运行 `python -m unittest discover -s server/test`。这些合成账户/网络回归不访问 Notion，不包含真实凭据，也不替代生产验收。
