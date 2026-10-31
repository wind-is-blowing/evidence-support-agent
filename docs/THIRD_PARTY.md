# 第三方归属与数据使用

本实现使用依赖包，不复制上游 Agent 框架源代码；包版本及依赖哈希记录在 `uv.lock` 和 `web/package-lock.json`。发行这些依赖时保留对应包的许可证与版权文件。

主要组件：FastAPI / Starlette、SQLAlchemy / Alembic、psycopg、pgvector、Redis Python客户端、argon2-cffi、Pillow、OpenAI Python SDK（连接百炼兼容API）、cryptography、React、TypeScript、Vite、Playwright、Caddy。可选离线资料/向量组件为Docling、Transformers、Sentence Transformers和Beautiful Soup。

PostgreSQL 与 pgvector 使用各自上游发行包。Compose 的Redis版本为7.4系列，适用RSALv2或SSPLv1双许可证；Redis8新增的许可选项不自动适用于7.4。参见 [Redis官方许可说明](https://redis.io/legal/licenses/)。

Qwen3-Embedding-0.6B 权重不包含在源码包中，须单独按 [官方模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) 准备并固定修订；托管模型服务另外遵循服务商账号条款，源码包不包含任何账号密钥。

安克、eufy、soundcore产品名和官方资料归各权利人。仓库仅包含来源元数据与短改述；不重新分发未经许可的完整手册、官网原图、真实用户材料或模型权重。具体来源和使用条件逐条保存在 `data/sources.jsonl`，人工审查前的真实步骤不可自动执行。

`data/demo.jsonl` 为项目自建虚拟设备测试材料，不代表安克产品、维修方法或质保承诺。仓库原有内容没有在本次实现中被重新指定开源许可证；本文不替原权利人授予额外许可。
