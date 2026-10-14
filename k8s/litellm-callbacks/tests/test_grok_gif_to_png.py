"""Regression tests for ``grok_gif_to_png.py``.

x.ai rejects GIF images (400 invalid_image); the callback converts the first GIF frame to PNG
for sub2api deployments. Pillow is only used here, as the reference decoder — the pod has none.

Run
---

    cd k8s/litellm-callbacks/tests
    python3 -m unittest test_grok_gif_to_png -v
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


def _install_litellm_stubs() -> None:
    s = types.ModuleType("litellm")
    sys.modules["litellm"] = s
    integ = types.ModuleType("litellm.integrations")
    cl = types.ModuleType("litellm.integrations.custom_logger")

    class _CL:
        def __init__(self) -> None: ...

    cl.CustomLogger = _CL
    sys.modules["litellm.integrations"] = integ
    sys.modules["litellm.integrations.custom_logger"] = cl


_install_litellm_stubs()
_HOOK_PATH = Path(__file__).resolve().parent.parent / "grok_gif_to_png.py"
_spec = importlib.util.spec_from_file_location("grok_gif_to_png", _HOOK_PATH)
M = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(M)

SUB2API = "http://sub2api.litellm-dev.svc.cluster.local:8080/v1"
OTHER = "https://openrouter.ai/api/v1"


def _gif(size=(64, 48), interlace=False, transparent=False, frames=1, palette_colors=200) -> bytes:
    imgs = []
    for f in range(frames):
        im = Image.new("RGB", size)
        px = im.load()
        for y in range(size[1]):
            for x in range(size[0]):
                px[x, y] = ((x * 7 + f * 40) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
        imgs.append(im.convert("P", palette=Image.Palette.ADAPTIVE, colors=palette_colors))
    buf = io.BytesIO()
    kw = {"format": "GIF", "interlace": interlace}
    if transparent:
        kw["transparency"] = 0
    if frames > 1:
        kw.update(save_all=True, append_images=imgs[1:], duration=100, loop=0)
    imgs[0].save(buf, **kw)
    return buf.getvalue()


def _ref_rgba(gif: bytes):
    im = Image.open(io.BytesIO(gif))
    im.seek(0)
    return im.convert("RGBA")


@unittest.skipIf(Image is None, "Pillow needed as reference decoder")
class Decoder(unittest.TestCase):
    def _check(self, gif: bytes):
        png = M.gif_to_png(gif)
        got = Image.open(io.BytesIO(png))
        self.assertEqual(got.format, "PNG")
        ref = _ref_rgba(gif)
        self.assertEqual(got.size, ref.size)
        self.assertEqual(list(got.convert("RGBA").getdata()), list(ref.getdata()))

    def test_plain(self):
        self._check(_gif())

    def test_interlaced(self):
        self._check(_gif(interlace=True))

    def test_transparent(self):
        self._check(_gif(transparent=True))

    def test_animated_takes_first_frame(self):
        self._check(_gif(frames=3))

    def test_small_palette(self):
        self._check(_gif(palette_colors=2))

    def test_large_image_forces_12bit_codes(self):
        self._check(_gif(size=(400, 300)))

    def test_not_gif_raises(self):
        with self.assertRaises(ValueError):
            M.gif_to_png(b"\x89PNG\r\n\x1a\n....")


def _run(kwargs, call_type="anthropic_messages"):
    return asyncio.run(M.grok_gif_to_png.async_pre_call_deployment_hook(kwargs, call_type))


@unittest.skipIf(Image is None, "Pillow needed to build fixtures")
class Rewrite(unittest.TestCase):
    def setUp(self):
        self.b64 = base64.b64encode(_gif()).decode()

    def test_anthropic_image_nested_in_tool_result(self):
        kw = {"api_base": SUB2API, "messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [
            {"type": "text", "text": "shot"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/gif", "data": self.b64}}]}]}]}
        out = _run(kw)
        img = out["messages"][0]["content"][0]["content"][1]
        self.assertEqual(img["source"]["media_type"], "image/png")
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(img["source"]["data"]))).format, "PNG")

    def test_responses_input_image(self):
        kw = {"api_base": SUB2API, "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/gif;base64," + self.b64}]}]}
        out = _run(kw, "aresponses")
        self.assertTrue(out["input"][0]["content"][0]["image_url"].startswith("data:image/png;base64,"))

    def test_chat_image_url_dict(self):
        kw = {"api_base": SUB2API, "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/gif;base64," + self.b64, "detail": "high"}}]}]}
        out = _run(kw, "acompletion")
        part = out["messages"][0]["content"][0]
        self.assertTrue(part["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(part["image_url"]["detail"], "high")

    def test_broken_gif_becomes_text_note(self):
        kw = {"api_base": SUB2API, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/gif", "data": base64.b64encode(b"GIF89a-garbage").decode()}}]}]}
        out = _run(kw)
        self.assertEqual(out["messages"][0]["content"][0], {"type": "text", "text": M._OMITTED})

    def test_png_untouched(self):
        png = base64.b64encode(M.gif_to_png(_gif())).decode()
        part = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}
        out = _run({"api_base": SUB2API, "messages": [{"role": "user", "content": [dict(part)]}]})
        self.assertEqual(out["messages"][0]["content"][0], part)

    def test_non_sub2api_untouched(self):
        kw = {"api_base": OTHER, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/gif", "data": self.b64}}]}]}
        out = _run(kw)
        self.assertEqual(out["messages"][0]["content"][0]["source"]["media_type"], "image/gif")

    def test_garbage_never_raises(self):
        for kw in ({"api_base": SUB2API, "messages": [None, 3, {"content": "x"}]}, {"api_base": SUB2API}, "notadict"):
            _run(kw)


if __name__ == "__main__":
    unittest.main()
