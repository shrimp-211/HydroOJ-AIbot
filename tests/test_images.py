"""Hydro 图片占位符解析与失败图片丢弃。"""
from types import SimpleNamespace

from hydroai.solve.solver import Config, OJClient


def _oj(tmp_path):
    return OJClient(Config(config_path=str(tmp_path / "none.json")))


def test_file_placeholder_resolves_to_problem_file_url(tmp_path):
    oj = _oj(tmp_path)
    url = oj._abs_url("file://hj5s80cz.webp",
                      "https://oj.example/d/system/p/1267")
    assert url == ("https://oj.example/p/1267/file/hj5s80cz.webp"
                   "?type=additional_file")


def test_failed_image_download_is_dropped(tmp_path):
    oj = _oj(tmp_path)

    def missing(*args, **kwargs):
        return SimpleNamespace(status_code=404, headers={}, content=b"")

    oj.session = SimpleNamespace(get=missing)
    assert oj._download_images(["https://oj.example/p/1/file/missing.webp"]) == []


def test_embedded_data_url_is_kept(tmp_path):
    oj = _oj(tmp_path)
    data = "data:image/png;base64,AAAA"
    assert oj._download_images([data]) == [data]
