# RAG 知识库检索

## 功能概览
灵犀能对一个本地 Markdown 资料库做语义检索(RAG),让 AI"据实回答并标注来源"。整条链路:Markdown 切块 → embedding 向量化 → numpy 向量库存储 → 向量粗召回 → 交叉编码器精排(可选)→ 拼成带编号引用的上下文喂给模型。代码在 `src/rag/`,工具入口是 `search_knowledge`(`src/tools_rag.py`)。

## 切块(`src/rag/chunk.py`)
- 先按 Markdown 标题分层,保留"标题路径"(如 `文档标题 › 二级 › 三级`),检索时能显示出处、也拼进 embedding 文本提升召回。
- 代码围栏 ``` 内的 `#` 不当作标题(避免把代码注释误切)。
- 再按大小切块(默认 chunk_size=800、overlap=120)。
- **关键设计:任意相邻块都保留 overlap 字符的上下文**。短段落打包成块后,每关闭一个块就用它尾部 overlap 字符作为下一块的开头;超长段落用滑窗步进 `size-overlap` 硬切。这样命中块边界的信息不会被截断。(早期实现只在硬切超长单段时才有重叠,短段拼块前后完全无重叠——是个已修的 bug。)

## 向量库(`src/rag/store.py`)
- 为什么不用 chromadb/faiss:本机 Python 3.14 太新,重型向量库的二进制依赖多半没有 3.14 wheel;而单用户知识库(几百~几千块)下,numpy 暴力 cosine 亚毫秒级,零重依赖、保证能跑。store 抽象了后端,以后可换。
- **事务性(generation 方案)**:每次重建写一代带版本号的数据文件,最后**原子替换 manifest** 作为提交点。load 只认 manifest 指向的那一代,所以进程中途崩溃最多留下没被引用的新代文件,绝不会出现"新向量 + 旧元数据"的混搭。
- 落盘:`manifest.json`(锚:generation/kb_dir/embed_model/embed_base_url/chunk_size/chunk_overlap/chunks/dim)、`embeddings.<gen>.npy`(float32,已 L2 归一化)、`meta.<gen>.jsonl`、`vec_cache.<gen>.jsonl`(按 text-hash 缓存,重建时复用未变块免重复 embed)。

## 检索与两阶段 rerank(`src/rag/retriever.py`)
- `retrieve()`:query → embed → 向量 top-k(+ 相似度阈值 min_score)。
- 开 rerank 时走两阶段:向量先粗召回 `rerank_top_n` 条,再用 cross-encoder(DashScope gte-rerank)精排成 top_k;rerank 调用失败则回退向量顺序,保证"总能出结果"。
- `format_context()`:拼成带 `[1][2]` 编号 + 出处 + 相关度的上下文,便于模型据实回答并标注来源。

## 索引锚一致性校验(fail-closed)
换了知识库目录 / embedding 模型/端点 / 切块参数但没重建,旧索引就不该被拿来答题。`anchor_mismatch()` 是 `index_status`(UI 状态)和 `retrieve`(实际检索)**共用的单一判据**,且**缺字段一律 fail-closed**(旧版索引缺 kb_dir/embed/切块元数据都要求重建)。这样绕过 GUI 直接调工具也不会用错索引。会话层还有 `rag_kb_dir` 锚点:历史会话锚定 A、配置切到 B 后,发送和工具层都强制校验,不静默检索到 B。

## Embedding(`src/rag/embed.py`)
默认复用通义千问(DashScope OpenAI 兼容端点的 `/embeddings`),模型 `text-embedding-v3`。HTTP 200 但 data 为空/数量不符按失败处理(不静默返回空,防向量错位)。

## 稳健性(多轮 review 收敛的成果)
- 坏缓存向量(nan/inf/维度不符)加载时丢弃 → 该块重新 embedding,绝不复用毒向量。
- 缓存文件语法损坏只丢缓存,不连累主索引(vec+meta 照常可用)。
- rebuild 提交前校验整块矩阵(二维 + 全有限),坏矩阵拒绝提交、保留旧代——防"重建报成功、检索却 0 块"。
- 极端切块参数保护:chunk_size 下限 100(防海量切片);单文件/总字节上限;切片总数上限,且在 embedding 前增量检查(防内存/账单爆炸)。
- 加载校验向量必须二维、维度与 manifest 一致(防"一维 npy 恰好长度==meta 数"蒙混过关,检索时崩)。

## 配置(config.json 的 rag 段)
`kb_dir` / `embed_model` / `embed_base_url` / `embed_api_key` / `top_k` / `chunk_size` / `chunk_overlap` / `min_score` / `rerank` / `rerank_model` / `rerank_top_n` / `rerank_url`。布尔用严格解析(`"false"` 不会因 `bool("false")==True` 被误开)。设置弹窗有「知识库」tab 可视化编辑;kb_dir 在侧栏「知识库」卡片选并「重建索引」。
