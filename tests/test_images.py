import base64
import copy
import os
import struct
import time

import pytest
from conftest import request, tool_use, user_text

from lightfoot import images, prune
from lightfoot.adapters import ANTHROPIC
from lightfoot.maintenance import cleanup
from lightfoot.profiles import CLAUDE_CODE
from lightfoot.store import Launch
from lightfoot.transform import plan_request, session_key
from lightfoot.validate import ValidationError, validate


def png(width, height, salt=b""):
    """Bytes with a real PNG signature and IHDR chunk; enough for size detection and saving."""
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00" + salt + b"\x00" * 64


def image_block(data: bytes, media_type="image/png"):
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()},
    }


def screenshot_result(tid, data, text="Screenshot taken"):
    content = ([{"type": "text", "text": text}] if text else []) + [image_block(data)]
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]}


# --- sizes ------------------------------------------------------------------------


def test_dimensions_from_png_gif_jpeg_and_webp_headers():
    assert images.dimensions(png(1280, 800)) == (1280, 800)
    assert images.dimensions(b"GIF89a" + struct.pack("<HH", 320, 200) + b"\x00" * 8) == (320, 200)
    jpeg = (
        b"\xff\xd8"
        + b"\xff\xe0"
        + struct.pack(">H", 16)
        + b"\x00" * 14  # APP0 segment, skipped
        + b"\xff\xc0"
        + struct.pack(">HB", 17, 8)
        + struct.pack(">HH", 600, 900)
        + b"\x00" * 12
    )
    assert images.dimensions(jpeg) == (900, 600)
    vp8x = (
        b"RIFF"
        + b"\x00" * 4
        + b"WEBP"
        + b"VP8X"
        + b"\x00" * 8
        + (1023).to_bytes(3, "little")
        + (767).to_bytes(3, "little")
    )
    assert images.dimensions(vp8x) == (1024, 768)
    assert images.dimensions(b"not an image") is None


def test_image_tokens_use_pixels_not_base64_length():
    block = image_block(png(1280, 800) + os.urandom(150_000))
    assert len(block["source"]["data"]) > 200_000
    assert images.image_tokens(block) == 1280 * 800 // 750
    assert (
        images.image_tokens({"type": "image", "source": {"type": "url", "url": "https://x"}}) == images.FALLBACK_TOKENS
    )


def test_unit_tokens_count_images_at_their_real_cost():
    unit = screenshot_result("t1", png(1920, 1080) + os.urandom(200_000))
    assert ANTHROPIC.unit_tokens(0, unit, {}) == pytest.approx(1920 * 1080 // 750, abs=10)
    assert prune.message_tokens(unit) == pytest.approx(1920 * 1080 // 750, abs=10)


# --- adapter ----------------------------------------------------------------------


def test_image_results_get_their_own_shape_and_a_hash_of_the_image_data():
    body = request(
        [
            user_text("look"),
            tool_use("a", "mcp__browser__screenshot", {}),
            screenshot_result("a", png(800, 600, b"one"), text=""),
            tool_use("b", "mcp__browser__screenshot", {}),
            screenshot_result("b", png(800, 600, b"two"), text=""),
        ]
    )
    idx = ANTHROPIC.index(body, CLAUDE_CODE)
    first, second = idx.results
    assert first.shape == second.shape == "image" and first.orig_text == ""
    assert len(first.media) == 1 and first.rewritable
    assert first.orig_hash != second.orig_hash  # same (empty) text, different images


def test_url_images_and_documents_stay_non_rewritable():
    url_image = {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}
    block = {"type": "tool_result", "tool_use_id": "x", "content": [url_image]}
    assert ANTHROPIC.extract(block) == (None, None)


# --- the images rule --------------------------------------------------------------


def screenshot_session(n=6):
    msgs = [user_text("check the page layout")]
    for i in range(n):
        msgs += [
            tool_use(f"s{i}", "mcp__browser__screenshot", {"page": i}),
            screenshot_result(f"s{i}", png(1920, 1080, bytes([i])), text=f"Screenshot {i} of the page"),
        ]
        msgs += [user_text(f"now step {i}")]
    msgs += [
        tool_use("z", "Bash", {"command": "ls"}),
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "z", "content": "ok"}]},
    ]
    return msgs


def plan_for(store, make_ctx, msgs, **kw):
    body = request(copy.deepcopy(msgs))
    session = store.get_or_create_session(session_key(body), None, "live", time.time())
    session.last_request = None  # cold cache: an epoch is free
    plan = plan_request(body, make_ctx(mode="live", session=session, **kw))
    store.put_rewrites(plan.new_rewrites)
    return body, plan


def test_older_images_become_stubs_and_are_saved(store, make_ctx, tmp_path):
    msgs = screenshot_session(6)
    _body, plan = plan_for(store, make_ctx, msgs)
    assert plan.rules.get("images") == 3  # 6 image results, the newest 3 kept
    stubbed = [r for r in plan.new_rewrites if r.rule == "images"]
    for rw in stubbed:
        assert rw.text.startswith("Screenshot ") and "1920x1080 png" in rw.text
        assert os.path.exists(rw.path)
    kept = plan.body_out["messages"][-4]["content"][0]["content"]  # the newest screenshot result
    assert any(b.get("type") == "image" for b in kept)  # a recent screenshot is untouched
    assert plan.saved_tokens >= 3 * (1920 * 1080 // 750) - 300


def test_image_stubs_replay_byte_identically(store, make_ctx):
    from lightfoot.server import dumps

    msgs = screenshot_session(6)
    _, first = plan_for(store, make_ctx, msgs)
    _, second = plan_for(store, make_ctx, msgs)
    assert second.new_rewrites == [] and dumps(first.body_out) == dumps(second.body_out)


def test_images_rule_can_be_turned_off(store, make_ctx, cfg):
    cfg["rules"]["images"] = False
    _, plan = plan_for(store, make_ctx, screenshot_session(6), config=cfg)
    assert "images" not in plan.rules


def test_validator_rejects_an_images_rewrite_that_drops_text():
    msgs = [user_text("x"), tool_use("a", "mcp__browser__screenshot", {}), screenshot_result("a", png(10, 10))]
    body = request(msgs)
    out = copy.deepcopy(body)
    path = ("messages", 2, "content", 0, "content")
    out["messages"][2]["content"][0]["content"] = [{"type": "text", "text": "[lightfoot: image removed]"}]
    meta = {path: ("images", "new", 2, "Screenshot taken", "[lightfoot: image removed]")}
    with pytest.raises(ValidationError, match="dropped a text block"):
        validate(body, out, meta, 3, ANTHROPIC)


# --- history files and cleanup -----------------------------------------------------


def test_history_file_saves_pruned_images(tmp_path):
    msgs = [user_text("a"), *screenshot_session(1)]
    history = tmp_path / "proj" / ".lightfoot" / "history" / "s-3.md"
    prune.write_history(history, msgs, len(msgs), "s")
    text = history.read_text(encoding="utf-8")
    assert "1920x1080 png: saved to" in text
    assert len(list((tmp_path / "proj" / ".lightfoot" / "images").glob("*.png"))) == 1


def test_cleanup_keeps_images_a_live_stub_names(store, make_ctx, cfg, tmp_path):
    msgs = screenshot_session(6)
    _, plan = plan_for(store, make_ctx, msgs)
    folder = tmp_path / "proj" / ".lightfoot" / "images"
    store.put_launch(Launch("l1", str(tmp_path / "proj"), "live", None, None, None))
    orphan = folder / "0000000000000000.png"
    orphan.write_bytes(b"x")
    old = time.time() - 40 * 86400
    for f in folder.iterdir():
        os.utime(f, (old, old))
    cleanup(store, cfg)
    assert not orphan.exists()
    assert all(os.path.exists(rw.path) for rw in plan.new_rewrites if rw.rule == "images")
