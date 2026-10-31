# 数据范围与来源

2026-10-02 赛事增补清单位于 `contest.jsonl`；2026-10-08 两份官方增补位于 `official_expansion_2026-10-08.jsonl` 与 `official_products_2026-10-08.jsonl`。与基础清单合计为 **10 个真实产品、27 条来源记录（26 个不同来源地址）、19 篇技术短改述、24 个技术检索片段**。新增 A1638、A1695、eufyCam S3 Pro，并补充 E73、A1336、T2351 等主题。`codex-source-comparison-*` 表示项目 Agent 来源比对，不是品牌认证或人工专家审核。

`sources.jsonl` 当前含 **7 个实际产品目录、18 个官方来源记录、10 个已发布短改述文档和 11 个检索片段**。首批资料于 2026-09-15 核对，扩充资料于 2026-09-28 通过官方 Sitemap 发现并核对；2026-10-01 另核实了两个同名 S1 Pro 产品的官方身份页。仓库未复制手册全文、页面正文或图片，目录和文章范围不代表全产品覆盖。语言或页面地区导航不用于推断销售地区。

| 产品 | 目录 | 可检索资料 | 可执行操作卡 | 政策 |
|---|---|---|---|---|
| soundcore Liberty 4 NC / A3947 | 有 | 重置、断连、声音中断、充电与耗电 | 待维护者审查 | 无 |
| eufy S1 Pro / T2080 | 有 | 吸力、Wi-Fi、精选错误码和升级材料 | 吸力异常的非侵入式外观检查 | 无 |
| eufy Wearable Breast Pump S1 Pro / T8D04 | 有 | 仅核实产品身份与型号 | 无（未审阅指南附件，不发布排障步骤） | 无 |
| Anker 737 Charger / A2148 | 有 | 手册入口、特定组合的功率解释 | 无 | 无 |
| soundcore Space One / A3035 | 有 | 蓝牙重连线索 | 无 | 无 |
| eufy X10 Pro Omni / T2351 | 有 | Wi-Fi、地图丢失、滚刷缠毛与技术转接材料 | 无 | 无 |
| Anker Prime Power Bank / A1336 | 有 | 端口/标称输出与输出限制 | 无 | 无 |
| Anker Nano Power Bank / A1638 | 有 | 容量、端口、组合输出与小电流模式 | 无 | 无 |
| Anker Power Bank 25K 165W / A1695 | 有 | 多口输出、边充边放与温控暂缓回充 | 无 | 无 |
| eufyCam S3 Pro / T8162（SKU T81621W1） | 有 | 画质、智能家居、太阳能与雷达/PIR 检测 | 无 | 无 |

所有官方文章权利归原权利人；本仓库仅分发短改述和引用信息。`content_hash` 是导入后改述与片段的校验值，不冒充官网全文的哈希；`source-checked-*` 是本项目核对版本，不是官方版本号或政策生效日期。公开退货和质保页面没有可独立验证的生效日期，因此只登记为来源候选，未发布为可执行政策。

`demo.jsonl` 是完全自建的虚拟清洁器，仅允许模拟配置导入。品牌 `DEMO`、产品 `demo-cleaner` 和 `demo-inspect` 操作均不对应真实设备。演示点击反馈和模拟工单可检验流程，不能证明设备修复效果。

`store_demo.jsonl` 是“安克创新官方直营综合店（赛事模拟）”的自建政策数据，配合 `seed-demo` 创建 6 笔模拟订单及直营、安心数码两个虚构渠道记录。它只用于检验订单选择、商品匹配、渠道过滤和质保日期计算，不代表安克或真实店铺政策。

## 官方来源采集

`official_collection.json` 维护明确的官方页面白名单。采集器先读取各站 `robots.txt` 和官方 Sitemap，再访问白名单页面；输出仅保存 URL、抓取日期、HTTP 状态、正文大小和 SHA-256，不保存页面正文。

```bash
python -m kefu.cli collect-official --config data/official_collection.json --output data/collected/official-support-audit.jsonl
```

2026-10-08 实际扫描 14,439 个 Sitemap 地址并核验 14 个选定页面。采集结果不能直接发布到知识库，仍需维护者撰写短改述、核对型号和适用范围后写入独立版本清单。

## 离线导入

维护者整理为 JSONL `product` 与 `document` 记录后逐个执行 `python -m kefu.cli import-knowledge --manifest <清单>`。`seed-demo` 会依次导入基础、赛事、两份 2026-10-08 官方增补、两份回复手册和模拟清单。来源元数据随清单保留。文档需具备产品范围、来源、版本、位置、使用条件与审核者；政策另外要求地区和明确生效日期。发布后同一 ID/版本不可覆盖，修订使用新版本；撤回后不再返回步骤。

可选 embedding 支持固定本地目录的 `SentenceTransformersEmbedding`，也支持固定 manifest 摘要、仅 loopback 的 Ollama `qwen3-embedding:0.6b`。没有模型时使用错误码、标题/关键词和正文三个词法通道做 RRF，并标明 `lexical_rrf`；不会生成随机向量冒充语义检索。真实向量已实测；纯向量新增证据使用 0.48 门槛后，v3 的 Recall@5 从词法 0.6818 提到 0.8636，拒答从 0.8333 降到 0.7500，仍需按业务偏好评估权衡。PDF 与复杂 HTML 需由离线环境用 Docling/HybridChunker 转成带页码和父章节的 JSONL，当前导入器不自动猜测版式或填补警告。

`response_manual.jsonl` 是项目自编、非官方表达手册。其 6 个片段只按意图、情绪和优先级提供回复结构，不是技术事实或政策证据；导入校验禁止操作步骤、事实 claim 和明确结果承诺。

### 本地文件解析

安装 `ingestion` 可选依赖，并准备好本地模型资源后执行：

```bash
python -m kefu.cli prepare-knowledge --source /local/source.pdf --metadata /local/metadata.json --output /local/candidate.jsonl
```

元数据 JSON 的 `document` 对象必须包含 `id`、`version`、`source_url`、`product_ids`、`kind`、`license`、`reviewed_by`。`reviewed_by` 表示对来源元数据负责的维护者，解析结果仍需审查。可提供 `source_sha256`，防止审查后的原始文件被替换。新产品完整目录放在顶层 `products` 数组；已有产品可以省略该数组。

`parser` 对象需要绝对路径 `tokenizer_path` 和真实固定修订 `tokenizer_revision`（40–64 位十六进制）；PDF 额外需要本地 `artifacts_path`。HTML 需要唯一正文 `article_selector`，例如 `main article`；不接受整个 `body`。`section_scopes` 可按章节标题指定更窄的产品、地区、渠道、固件及前置条件。解析始终输出 `candidate`、空步骤列表以及 `.docling.json` 定位副本；核对警告、单位、图表和适用范围后才能发布，不应直接把解析状态改为已审核操作。

生产解析和 embedding 不会自动下载模型。当前自动检查使用固定解析替身验证元数据与失败边界；真实复杂 PDF 质量仍待测量，Qwen 向量效果与限制见 `docs/RAG_RETRIEVAL_EVALUATION.md`。
