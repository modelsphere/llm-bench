"""The bench path default must resolve from any layout: a source checkout,
and the flattened container image, where counting parents raised IndexError
at import time and every process of the platform died before reading its
settings (found by the first cluster deploy of the public image)."""

from pathlib import Path

from app.core.config import Settings, find_bench_dir


def test_source_checkout_resolves_the_repo_bench_dir():
    found = find_bench_dir(Path(__file__))
    assert (found / "modules").is_dir()
    assert Settings().BENCH_MODULES_PATH == str(found)


def test_a_shallow_image_path_falls_back_instead_of_raising(tmp_path):
    # /app/app/core/config.py has three ancestors and no bench/ beside any of
    # them: the answer is /app/bench, the image's copy.
    app = tmp_path / "app" / "app" / "core"
    app.mkdir(parents=True)
    assert find_bench_dir(app / "config.py") == tmp_path / "app" / "bench"
    assert find_bench_dir(Path("/config.py")).name == "bench"
