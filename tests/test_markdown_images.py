from app.render.markdown_text import extract_markdown_images, markdown_to_plain
from app.tools.amap import photos_to_markdown


def test_photos_to_markdown_empty_alt():
    md = photos_to_markdown(
        "都江堰",
        ["https://store.is.autonavi.com/showpic/abc", "https://aos-comment.amap.com/x_y_z.jpg"],
        limit=2,
    )
    assert md == (
        "![](https://store.is.autonavi.com/showpic/abc)\n"
        "![](https://aos-comment.amap.com/x_y_z.jpg)"
    )
    assert "都江堰" not in md


def test_markdown_to_plain_preserves_underscores_in_image_urls():
    url = (
        "https://aos-comment.amap.com/B001C7WE5S/comment/"
        "D9A52A3A_A680_4447_9647_0C0069242BF2_L0_001_1500_200_1767303057334_10874.jpg"
    )
    raw = f"先看熊猫\n![]({url})\n再去都江堰"
    out = markdown_to_plain(raw)
    assert url in out
    assert f"![]({url})" in out
    assert "_A680_" in out


def test_markdown_to_plain_still_strips_italic_outside_images():
    out = markdown_to_plain("这是_斜体_文字")
    assert "斜体" in out
    assert "_斜体_" not in out


def test_extract_empty_alt_image():
    items = extract_markdown_images("![](https://example.com/a.jpg)")
    assert len(items) == 1
    assert items[0]["url"] == "https://example.com/a.jpg"
