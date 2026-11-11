"""本地 CPU OCR 适配器：仅接收内存图片，禁止按文档内容访问 URL。"""

import importlib.util
import threading
from pathlib import Path

from hotline.settings import ServiceError

MODEL_FILES = {
    "Det": "PP-OCRv6_det_small.onnx",
    "Rec": "PP-OCRv6_rec_small.onnx",
    "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
}


class LocalOCR:
    def __init__(self, settings):
        self.settings = settings
        self.engine = None
        self.lock = threading.Lock()

    def model_paths(self):
        spec = importlib.util.find_spec("rapidocr")
        if spec is None or not spec.origin:
            raise ServiceError("缺少本地 OCR 依赖，请按部署说明安装 requirements.lock")
        root = self.settings.ocr_model_dir or Path(spec.origin).parent / "models"
        paths = {key: root / name for key, name in MODEL_FILES.items()}
        if any(not path.is_file() for path in paths.values()):
            raise ServiceError(
                "本地 OCR 模型不完整，请安装固定版本 RapidOCR 或配置 OCR_MODEL_DIR；不会自动联网下载"
            )
        return paths

    def status(self):
        if not self.settings.ocr_enabled:
            return {"enabled": False, "ready": False, "engine": "RapidOCR / PP-OCRv6", "message": "已关闭"}
        try:
            self.model_paths()
            return {
                "enabled": True,
                "ready": True,
                "engine": "RapidOCR / PP-OCRv6",
                "message": "本地 CPU，模型已就绪",
            }
        except ServiceError as exc:
            return {"enabled": True, "ready": False, "message": str(exc)}

    def recognize(self, image):
        if not self.settings.ocr_enabled:
            raise ServiceError("此文件需要 OCR，但本地 OCR 已关闭")
        with self.lock:
            try:
                if self.engine is None:
                    from rapidocr import RapidOCR

                    paths = self.model_paths()
                    params = {
                        "Global.log_level": "error",
                        # 保留低置信文字用于审查，不静默丢弃可能影响资格的短句。
                        "Global.text_score": 0.0,
                        "Global.max_side_len": self.settings.ocr_render_side,
                        "EngineConfig.onnxruntime.intra_op_num_threads": self.settings.ocr_threads,
                        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                    }
                    params.update({f"{key}.model_path": str(path) for key, path in paths.items()})
                    self.engine = RapidOCR(params=params)
                result = self.engine(image)
                lines, cursor = [], 0
                if result.txts is not None:
                    for text, score, box in zip(result.txts, result.scores, result.boxes):
                        if not text.strip():
                            continue
                        points = [
                            [round(float(x) / image.width, 6), round(float(y) / image.height, 6)]
                            for x, y in box
                        ]
                        lines.append(
                            {
                                "text": text,
                                "confidence": round(float(score), 4),
                                "box": points,
                                "start": cursor,
                                "end": cursor + len(text),
                            }
                        )
                        cursor += len(text) + 1
                text = "\n".join(line["text"] for line in lines)
                low = sum(line["confidence"] < self.settings.ocr_review_threshold for line in lines)
                return text, {
                    "method": "ocr",
                    "engine": "RapidOCR 3.10.0 / PP-OCRv6 small / ONNX CPU",
                    "width": image.width,
                    "height": image.height,
                    "lines": lines,
                    "confidence": round(sum(line["confidence"] for line in lines) / len(lines), 4)
                    if lines
                    else None,
                    "low_confidence_lines": low,
                    "warnings": ([f"有 {low} 行识别置信度低于阈值，请核对原件"] if low else [])
                    + (["未识别到文字，请检查空白页、方向或图像清晰度"] if not lines else []),
                }
            except ServiceError:
                raise
            except Exception as exc:
                raise ServiceError("本地 OCR 执行失败，请检查模型与运行依赖") from exc
