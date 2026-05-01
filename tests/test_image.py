"""Tests for image attachment support (#27)."""


def test_image_extension_detection():
    from clauded.bot import _IMAGE_EXTENSIONS

    assert ".png" in _IMAGE_EXTENSIONS
    assert ".jpg" in _IMAGE_EXTENSIONS
    assert ".jpeg" in _IMAGE_EXTENSIONS
    assert ".gif" in _IMAGE_EXTENSIONS
    assert ".webp" in _IMAGE_EXTENSIONS
    assert ".bmp" in _IMAGE_EXTENSIONS
    assert ".svg" in _IMAGE_EXTENSIONS
    assert ".txt" not in _IMAGE_EXTENSIONS
    assert ".py" not in _IMAGE_EXTENSIONS
