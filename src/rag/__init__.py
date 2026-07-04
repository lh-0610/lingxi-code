"""RAG 知识库检索子系统。

对一个本地 md 资料库做语义检索：
  chunk.py     —— markdown 按标题分层切块（保留标题路径做引用）
  embed.py     —— DashScope /embeddings（OpenAI 兼容）批量 embedding，带重试
  store.py     —— numpy 持久化向量库（暴力 cosine + 按 text-hash 缓存复用未变块）
  index.py     —— 摄取 kb_dir 下所有 .md → 切块 → embed → 建索引（块级增量）
  retriever.py —— query → embed → 检索 top-k（+ 阈值）→ 拼成带引用的上下文

对外主要入口：index.reindex(...) 建索引；retriever.retrieve(...) 检索；
tools_rag.search_knowledge 是给 agent 的工具。
"""
