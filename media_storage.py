from __future__ import annotations

import base64
import os

import cloudinary
import cloudinary.uploader

_CONFIGURED = False


def _configure_cloudinary() -> bool:
    global _CONFIGURED
    if _CONFIGURED:
        return True

    if os.getenv("CLOUDINARY_URL"):
        cloudinary.config(secure=True)
        _CONFIGURED = True
        return True

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")
    if cloud_name and api_key and api_secret:
        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True,
        )
        _CONFIGURED = True
        return True

    return False


def _cloudinary_folder(name: str) -> str:
    base = (os.getenv("CLOUDINARY_FOLDER_BASE") or "").strip().strip("/")
    if base:
        return f"{base}/{name}"
    return name


def _ensure_configured() -> None:
    if not _configure_cloudinary():
        raise RuntimeError(
            "Cloudinary not configured. Set CLOUDINARY_URL or "
            "CLOUDINARY_CLOUD_NAME/CLOUDINARY_API_KEY/CLOUDINARY_API_SECRET."
        )


def upload_image_file(file_obj, folder: str) -> str:
    _ensure_configured()
    res = cloudinary.uploader.upload(
        file_obj,
        folder=_cloudinary_folder(folder),
        resource_type="image",
    )
    return res.get("secure_url") or res.get("url")


def upload_svg_text(svg_text: str, folder: str) -> str:
    _ensure_configured()
    encoded = base64.b64encode(svg_text.encode("utf-8")).decode("ascii")
    data_uri = f"data:image/svg+xml;base64,{encoded}"
    res = cloudinary.uploader.upload(
        data_uri,
        folder=_cloudinary_folder(folder),
        resource_type="image",
    )
    return res.get("secure_url") or res.get("url")
