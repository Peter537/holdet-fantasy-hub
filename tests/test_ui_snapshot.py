"""Browser-free coverage of snapshot tolerance and failure diagnostics."""

from io import BytesIO
from types import SimpleNamespace

from PIL import Image
import pytest

from tests.ui import snapshot


@pytest.mark.parametrize("difference_count", (1, 2))
def test_snapshot_counts_each_changed_pixel_once(tmp_path, monkeypatch, difference_count):
    baseline = tmp_path / "baselines"
    baseline.mkdir()
    artifacts = tmp_path / "artifacts"
    monkeypatch.setattr(snapshot, "BASELINES", baseline)
    monkeypatch.setattr(snapshot, "ARTIFACTS", artifacts)

    image = Image.new("RGB", (100, 10))
    image.save(baseline / "example.png")
    image.putpixel((0, 0), (10, 10, 10))
    image.putpixel((1, 0), (11, 11, 0))
    if difference_count == 2:
        image.putpixel((2, 0), (0, 0, 11))
    png = BytesIO()
    image.save(png, format="PNG")
    page = SimpleNamespace(screenshot=lambda **options: png.getvalue())
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda option: False))

    if difference_count == 1:
        snapshot.assert_ui_snapshot(page, "example", request)
        assert not artifacts.exists()
    else:
        with pytest.raises(pytest.fail.Exception, match=r"0\.200% pixels"):
            snapshot.assert_ui_snapshot(page, "example", request)
        assert (artifacts / "example-actual.png").is_file()
        with Image.open(artifacts / "example-diff.png") as diff:
            assert diff.mode == "RGB"
            assert [diff.getpixel((x, 0)) for x in range(3)] == [
                (0, 0, 0), (255, 255, 0), (0, 0, 255),
            ]
