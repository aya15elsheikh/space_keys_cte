"""HTTP client for the payload ESP32 access point."""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
from pathlib import Path
import urllib.error
import urllib.request

from PIL import Image


IMAGE_TRANSFER_TIMEOUT_SECONDS = 180.0


@dataclass(slots=True)
class WifiStatus:
    reachable: bool
    values: dict
    error: str = ""


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        value = "192.168.4.1"
    if not value.lower().startswith(("http://", "https://")):
        value = "http://" + value
    return value


class PayloadWifiClient:
    def __init__(self, base_url: str = "http://192.168.4.1", timeout: float = 4.0) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout

    def status(self) -> WifiStatus:
        try:
            with urllib.request.urlopen(self.base_url + "/status", timeout=self.timeout) as response:
                values = json.loads(response.read().decode("utf-8"))
            return WifiStatus(True, values)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            return WifiStatus(False, {}, str(exc))

    def image_bytes(self) -> bytes:
        request = urllib.request.Request(self.base_url + "/image", headers={"Cache-Control": "no-cache"})
        try:
            with urllib.request.urlopen(request, timeout=max(self.timeout, IMAGE_TRANSFER_TIMEOUT_SECONDS)) as response:
                content = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(
                    "The ESP32-CAM has no published image. Send CIMG, wait for ACK, then send TIMG."
                ) from exc
            raise RuntimeError(f"ESP32-CAM image request failed: HTTP {exc.code}") from exc
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
        except Exception as exc:
            raise RuntimeError("The ESP32-CAM response is not a valid JPEG image") from exc
        return content

    def download(self, path: Path) -> tuple[Path, Image.Image]:
        content = self.image_bytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        image = Image.open(io.BytesIO(content))
        image.load()
        return path, image
