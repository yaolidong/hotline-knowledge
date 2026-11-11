"""本地真实 ONNX 嵌入模型；容器内及本机回环端口均使用令牌鉴权。"""

import os
import secrets
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from fastembed import TextEmbedding
from pydantic import BaseModel, Field

MODEL_NAME = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
API_KEY = os.environ["EMBEDDING_API_KEY"]
if len(API_KEY) < 24:
    raise RuntimeError("嵌入服务令牌至少需要 24 位")


@asynccontextmanager
async def lifespan(app):
    app.state.model = TextEmbedding(model_name=MODEL_NAME, cache_dir="/models", threads=2)
    yield


app = FastAPI(lifespan=lifespan)


class EmbeddingsRequest(BaseModel):
    model: str
    input: str | list[str]
    encoding_format: Literal["float", "base64"] = "float"
    dimensions: int | None = Field(None, ge=1)


@app.get("/health")
def health():
    return {"ready": True, "model": MODEL_NAME}


@app.post("/v1/embeddings")
def embed(body: EmbeddingsRequest, authorization: str = Header("")):
    if not secrets.compare_digest(authorization, "Bearer " + API_KEY):
        raise HTTPException(401, "未授权")
    texts = [body.input] if isinstance(body.input, str) else body.input
    if body.model != MODEL_NAME or not 1 <= len(texts) <= 64:
        raise HTTPException(400, "模型或批大小不合法")
    if any(not text.strip() or len(text) > 4000 for text in texts):
        raise HTTPException(400, "输入为空或超过长度限制")
    vectors = list(app.state.model.embed(texts))
    if body.dimensions is not None and body.dimensions != len(vectors[0]):
        raise HTTPException(400, "本地模型不支持改变向量维度")
    if body.encoding_format == "base64":
        import base64

        values = [base64.b64encode(vector.astype("<f4").tobytes()).decode("ascii") for vector in vectors]
    else:
        values = [vector.tolist() for vector in vectors]
    return {
        "object": "list",
        "model": MODEL_NAME,
        "data": [{"object": "embedding", "index": i, "embedding": value} for i, value in enumerate(values)],
    }
