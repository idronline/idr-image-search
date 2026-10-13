"""
sscd_model.py — SSCD embedding model wrapper.

Downloads the pretrained SSCD TorchScript model from GitHub on first run
and caches it locally. No SSCD library code needed — just the weights file.

Model: sscd_disc_mixup (ResNet50) — recommended default
  - 512-dim L2-normalised embeddings
  - Robust to: crops, flips, compression, colour shifts, watermarks
  - Size: ~100MB
  - CPU inference: ~200-400ms per image on Xeon 8259CL
"""

import io
import os

import numpy as np
import requests
import torch
from PIL import Image

MODEL_URL = (
    "https://dl.fbaipublicfiles.com/sscd-copy-detection/"
    "sscd_disc_mixup.torchscript.pt"
)
MODEL_DIR  = os.path.join(os.path.dirname(__file__), "models")
MODEL_PATH = os.path.join(MODEL_DIR, "sscd_disc_mixup.torchscript.pt")

# Image preprocessing — SSCD was trained with these exact values
IMAGE_SIZE   = 288   # SSCD uses 288×288 not 224×224
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD  = [0.229, 0.224, 0.225]


def _download_model():
    """
    Download model weights using requests with streaming.
    urllib.request.urlretrieve can silently produce corrupt files on
    large downloads — requests streaming is more reliable.
    """
    os.makedirs(MODEL_DIR, exist_ok=True)
    tmp_path = MODEL_PATH + ".tmp"
    print(f"[sscd] Downloading model (~100MB) to {MODEL_PATH}...")
    try:
        resp = requests.get(MODEL_URL, stream=True, timeout=120)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        print(f"\r[sscd] {pct:.1f}% ({downloaded//1024//1024}MB / {total//1024//1024}MB)",
                              end="", flush=True)
        print()
        # Verify file size is reasonable (model should be > 50MB)
        size = os.path.getsize(tmp_path)
        if size < 50 * 1024 * 1024:
            raise RuntimeError(f"Downloaded file too small ({size} bytes) — likely corrupt")
        os.rename(tmp_path, MODEL_PATH)
        print(f"[sscd] Download complete ({size // 1024 // 1024}MB).")
    except Exception as e:
        # Clean up partial download
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise RuntimeError(f"Model download failed: {e}") from e


def _preprocess(img: Image.Image) -> torch.Tensor:
    """
    Resize, centre-crop to 288×288, normalise with ImageNet stats.
    Returns a (1, 3, 288, 288) float32 tensor.
    """
    # Resize short edge to IMAGE_SIZE keeping aspect ratio
    w, h = img.size
    scale = IMAGE_SIZE / min(w, h)
    new_w, new_h = int(round(w * scale)), int(round(h * scale))
    img = img.resize((new_w, new_h), Image.BILINEAR)

    # Centre crop
    left  = (new_w - IMAGE_SIZE) // 2
    top   = (new_h - IMAGE_SIZE) // 2
    img   = img.crop((left, top, left + IMAGE_SIZE, top + IMAGE_SIZE))

    # To float tensor [0,1]
    arr = np.array(img, dtype=np.float32) / 255.0

    # Normalise
    mean = np.array(IMAGENET_MEAN, dtype=np.float32)
    std  = np.array(IMAGENET_STD,  dtype=np.float32)
    arr  = (arr - mean) / std

    # HWC → CHW → NCHW
    tensor = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)
    return tensor


class SSCDModel:
    """
    Wrapper around the SSCD TorchScript model.
    Load once, reuse for all embeddings.

    Usage:
        model = SSCDModel()
        embedding = model.embed(pil_image)   # np.ndarray shape (512,)
    """

    def __init__(self):
        if not os.path.exists(MODEL_PATH):
            _download_model()
        else:
            # Verify existing file is not corrupt (must be > 50MB)
            size = os.path.getsize(MODEL_PATH)
            if size < 50 * 1024 * 1024:
                print(f"[sscd] Cached model appears corrupt ({size} bytes) — re-downloading...")
                os.remove(MODEL_PATH)
                _download_model()

        print(f"[sscd] Loading model from {MODEL_PATH}...")
        self._model = torch.jit.load(MODEL_PATH, map_location="cpu")
        self._model.eval()
        # Warm up (first inference is slower due to JIT compilation)
        dummy = torch.zeros(1, 3, IMAGE_SIZE, IMAGE_SIZE)
        with torch.no_grad():
            self._model(dummy)
        print("[sscd] Model ready.")

    def embed(self, img: Image.Image) -> np.ndarray | None:
        """
        Compute SSCD embedding for a PIL Image.
        Returns float32 numpy array of shape (512,), L2-normalised.
        Returns None on error.
        """
        try:
            img = img.convert("RGB")
            tensor = _preprocess(img)
            with torch.no_grad():
                output = self._model(tensor)
            vec = output.squeeze().numpy().astype(np.float32)
            # L2 normalise
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            return vec
        except Exception as e:
            print(f"[sscd] Embed error: {e}")
            return None

    def embed_bytes(self, img_bytes: bytes) -> np.ndarray | None:
        """Convenience method — embed from raw image bytes."""
        try:
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            return self.embed(img)
        except Exception as e:
            print(f"[sscd] embed_bytes error: {e}")
            return None


# Module-level singleton — loaded once when app.py imports this module
_instance: SSCDModel | None = None

def get_model() -> SSCDModel:
    global _instance
    if _instance is None:
        _instance = SSCDModel()
    return _instance


if __name__ == "__main__":
    # Quick smoke test
    import sys
    model = get_model()
    print(f"Model loaded. Testing with a blank image...")
    test_img = Image.new("RGB", (400, 300), color=(128, 64, 200))
    vec = model.embed(test_img)
    print(f"Embedding shape: {vec.shape}")
    print(f"Embedding norm:  {np.linalg.norm(vec):.6f}  (should be ~1.0)")
    print(f"First 5 values:  {vec[:5]}")
    print("SSCD model OK.")
