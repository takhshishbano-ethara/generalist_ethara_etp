"""SAM3 image segmentation wrapper.

Mirrors WildClawBench's task_1_sam3_inference / task_2_sam3_debug usage pattern.

Public API:
    segment_image(env, attachment, *, weights_path=None, prompts=None) -> dict
        Runs SAM3 on the attachment image. Returns dict with mask_count + s3_key.
        Lazy-imports torch + sam3 (segment_anything package) so this module
        doesn't break the addon at install time if those deps are missing.

    download_sam3_weights(env, *, repo_id='facebook/sam3', filename='sam3.pt') -> Path
        Convenience wrapper around prep_runner.download_hf_hub for SAM3 weights.
"""

import io
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, List

_logger = logging.getLogger(__name__)


def download_sam3_weights(env, *, repo_id: str = "facebook/sam3", filename: str = "sam3.pt"):
    from . import prep_runner
    return prep_runner.download_hf_hub(env, repo_id=repo_id, filename=filename, register=True)


def segment_image(env, attachment, *, weights_path: Optional[str] = None,
                  prompts: Optional[List[dict]] = None) -> dict:
    try:
        import torch  # noqa: F401
        from PIL import Image
    except ImportError:
        _logger.warning("torch/PIL not installed; SAM3 inference unavailable")
        return {"mask_count": 0, "error": "torch_not_installed"}

    try:
        from segment_anything import sam_model_registry, SamPredictor
    except ImportError:
        _logger.warning("segment_anything not installed; pip install segment_anything")
        return {"mask_count": 0, "error": "segment_anything_not_installed"}

    if not weights_path:
        weights_path = env["ir.config_parameter"].sudo().get_param("wildclaw.sam3_weights_path", "")
    if not weights_path or not Path(weights_path).exists():
        _logger.info("SAM3 weights not found; auto-downloading via prep_runner.download_hf_hub")
        try:
            rec = download_sam3_weights(env)
            weights_path = env["ir.config_parameter"].sudo().get_param("wildclaw.prep_dir",
                                                                        "/tmp/wildclaw_prep") + "/weights/facebook_sam3/sam3.pt"
        except Exception as exc:
            return {"mask_count": 0, "error": f"weights_download_failed: {exc}"}

    bucket = env["ir.config_parameter"].sudo().get_param("wildclaw.s3_bucket", "")
    if not bucket or not attachment.s3_key:
        return {"mask_count": 0, "error": "no_s3_image"}

    from . import media_processor
    client = media_processor._s3_client(env)
    obj = client.get_object(Bucket=bucket, Key=attachment.s3_key)
    img_bytes = obj["Body"].read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

    try:
        sam = sam_model_registry["vit_h"](checkpoint=weights_path)
        predictor = SamPredictor(sam)
        import numpy as np
        predictor.set_image(np.array(img))
        if prompts:
            points = [(p["x"], p["y"]) for p in prompts if p.get("type") == "point"]
            labels = [p.get("label", 1) for p in prompts if p.get("type") == "point"]
            if points:
                masks, scores, _ = predictor.predict(
                    point_coords=np.array(points),
                    point_labels=np.array(labels),
                    multimask_output=True,
                )
            else:
                masks = []
        else:
            masks = []
        mask_count = len(masks) if hasattr(masks, "__len__") else 0
        masks_s3_key = ""
        if mask_count and bucket:
            import numpy as np
            payload = {"masks_shape": list(np.array(masks).shape),
                       "scores": scores.tolist() if hasattr(scores, "tolist") else list(scores)}
            key = f"{media_processor._s3_prefix(env)}/sam3/{attachment.sha256_hex}_masks.json"
            client.put_object(Bucket=bucket, Key=key,
                              Body=json.dumps(payload).encode("utf-8"),
                              ContentType="application/json")
            masks_s3_key = key
        attachment.write({"sam3_mask_count": mask_count, "sam3_masks_s3_key": masks_s3_key})
        return {"mask_count": mask_count, "masks_s3_key": masks_s3_key}
    except Exception as exc:
        _logger.exception("SAM3 inference failed for attachment %s: %s", attachment.id, exc)
        return {"mask_count": 0, "error": str(exc)}
