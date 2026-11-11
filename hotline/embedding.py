"""已授权候选范围内的语义召回；复用本地 OpenAI-compatible 嵌入服务。

不使用上游 vectorize() 的异常分支：锁定 KAG 版本会记录完整输入文本。
缓存只含向量，以模型配置及内容散列寻址，不生成图事实或绕过发布过滤。
"""

import hashlib
import json
import math
import os
import tempfile
import threading
from pathlib import Path

import httpx
from openai import OpenAI

from hotline.settings import ServiceError


class SemanticRetriever:
    def __init__(self, settings):
        self.settings = settings
        self.client = None
        self.lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.settings.embedding_base_url and self.settings.embedding_model)

    def vectors(self, texts):
        s = self.settings
        if not self.enabled:
            raise ServiceError("语义检索未配置", 503)
        root = Path(s.data_dir) / "embeddings"
        root.mkdir(parents=True, exist_ok=True)
        vectors, missing = {}, {}
        with self.lock:
            for text in texts:
                digest = hashlib.sha256(
                    json.dumps([s.embedding_base_url, s.embedding_model, text], ensure_ascii=False).encode()
                ).hexdigest()
                path = root / (digest + ".json")
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    self.validate(value)
                    vectors[text] = value
                except (OSError, ValueError, ServiceError):
                    missing[text] = path
            if missing:
                if self.client is None:
                    self.client = OpenAI(
                        base_url=s.embedding_base_url,
                        api_key=s.embedding_api_key.get_secret_value() or "local-no-key",
                        timeout=s.embedding_timeout,
                        max_retries=0,
                        http_client=httpx.Client(trust_env=s.http_trust_env),
                    )
                values = list(missing)
                for offset in range(0, len(values), 32):
                    batch = values[offset : offset + 32]
                    try:
                        response = self.client.embeddings.create(
                            input=batch, model=s.embedding_model, encoding_format="float"
                        )
                    except Exception as exc:
                        raise ServiceError("本地嵌入服务调用失败，已保留图谱与关键词检索", 503) from exc
                    rows = sorted(response.data, key=lambda x: x.index)
                    if [row.index for row in rows] != list(range(len(batch))):
                        raise ServiceError("嵌入结果数量或索引不一致", 502)
                    for text, row in zip(batch, rows):
                        value = row.embedding
                        self.validate(value)
                        vectors[text] = value
                        with tempfile.NamedTemporaryFile(
                            mode="w", encoding="utf-8", dir=root, delete=False
                        ) as out:
                            json.dump(value, out)
                            temporary = out.name
                        os.replace(temporary, missing[text])
        return [vectors[t] for t in texts]

    @staticmethod
    def validate(vector):
        if (
            not isinstance(vector, list)
            or not 1 <= len(vector) <= 8192
            or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in vector)
            or sum(v * v for v in vector) <= 0
        ):
            raise ServiceError("嵌入服务返回无效向量", 502)

    def rank(self, question, texts):
        if len(texts) > self.settings.embedding_max_chunks:
            raise ServiceError("候选超出语义检索上限，本轮使用图谱与关键词检索", 503)
        # 长文本仅为索引建立重叠窗口，返回原文及生成上下文仍使用完整语义块。
        windows, owners = [], []
        for index, text in enumerate(texts):
            for start in range(0, len(text), 700):
                windows.append(text[start : start + 800])
                owners.append(index)
        if len(windows) > 2048:
            raise ServiceError("语义索引窗口超限，本轮使用图谱与关键词检索", 503)
        query, *vectors = self.vectors([question, *windows])
        query_norm = math.sqrt(sum(v * v for v in query))
        scores = {}
        for owner, vector in zip(owners, vectors):
            if len(vector) != len(query):
                raise ServiceError("嵌入维度不一致，请核对模型配置", 502)
            score = sum(a * b for a, b in zip(query, vector)) / (
                query_norm * math.sqrt(sum(v * v for v in vector))
            )
            scores[owner] = max(scores.get(owner, -1), score)
        return sorted(scores.items(), key=lambda x: (-x[1], x[0]))
