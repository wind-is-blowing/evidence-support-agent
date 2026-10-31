# 知维 ServiceRelay · 多产品售后 Agent

面向 Anker/eufy 多产品线的本地演示系统，强调产品消歧、适用知识检索、逐步排障与经用户确认的人工接续。
本包保留当前 FastAPI、React、PostgreSQL/pgvector、Redis、Worker 与 Agent 工具链；Python 包名仍为 `kefu`。
本项目不是品牌官方服务。订单、渠道、质保演示规则和工单均为模拟数据。

## 重点设计

- 同名 S1 Pro 先确认具体产品，避免扫地机器人与穿戴式吸奶器资料混用。
- 产品/地区/固件等适用范围过滤，错误码、标题、正文和可选 Qwen 向量检索，以 RRF 合并排名。
- 弱匹配规则和向量证据门槛控制返回；相关性不等于证据充分性。
- 表达策略与技术依据分离；已尝试步骤、用户反馈和待补材料进入人工接续草稿。
- 状态与确认规则由程序执行，用户确认后才创建模拟工单。

## 一键启动本地演示

需要 Python 3.12+、uv、Node.js 22+，以及已启动的 Docker Desktop/Engine。首次运行会安装依赖、拉取数据库/Redis 镜像并构建前端。
`demo` 是离线模拟模型，不需要大模型服务；依赖和镜像首次安装需要网络。

```powershell
uv sync --frozen
uv run --frozen python scripts/demo.py start --model demo
```

访问 `http://localhost:8020`。启动器自动创建独立容器、生成本机凭证并迁移数据库；不要复制别人的 `.env` 或虚拟环境。
新凭证只保存在被忽略的 `.runtime/contest/`；演示脚本只用于本地模拟环境。

真实本地模型（先确保 Ollama 已启动）：

```powershell
ollama pull qwen3.5:4b
uv run --frozen python scripts/demo.py stop
uv run --frozen python scripts/demo.py start --model qwen3.5:4b
```

`stop` 停止本项目进程并保留演示数据；没有替用户删除数据库。文本/视觉能力以实际模型为准。
可选 embedding 需额外部署并配置固定模型标识；未启用时明确使用词法检索，不声称已启用向量。

## 最小检查

```powershell
uv run --frozen python -m unittest discover -s tests -v
npm --prefix web ci
npm --prefix web run build
```

打包自检不依赖真实模型或数据库，只验证模块、知识导入、产品消歧和检索边界。完整运行仍需要 Docker/PostgreSQL/Redis。

## 60 秒演示

1. 新建任务，输入“S1 Pro 不吸了，明天派对要用”。
2. 确认扫地机器人 T2080；未确认时不应给出具体产品步骤。
3. 根据界面执行已审核的非侵入式检查，反馈“无变化”。
4. 查看接续草稿中的已尝试步骤、反馈与材料，确认后创建模拟工单。
5. 另建任务选择 T8D04，验证不会复用扫地机器人步骤。

## 源码与数据

- `src/kefu/runtime/`：模型决策循环与预算。
- `src/kefu/plugins/knowledge.py`：导入、产品识别、范围检查、词法/向量检索。
- `src/kefu/services.py`、`plugins/tools.py`：业务工具和执行前提。
- `src/kefu/storage/`、`migrations/`：状态持久化、数据结构和权限。
- `web/`：消费者与人工界面；`skills/`：三个流程指导。
- `data/`：产品资料短改述、来源记录和明确标注的模拟业务数据。

线上默认链路没有全面替换为 Haystack 或 CrossEncoder；相关模块的离线实验与产品能力分开陈述。
详见 [评测说明](docs/EVALUATION.md) 与 [第三方说明](docs/THIRD_PARTY.md)。
完整评测集、模型、真实凭证、用户上传、运行状态及浏览器测试产物未包含。
本包未替作者新增开源许可证；知识源 URL 和第三方授权说明保留，不附官网全文或第三方客户数据。
