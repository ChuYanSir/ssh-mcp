"""检查更新 / 升级的单元测试（不联网，全部打桩）。"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from ssh_mcp import update as upd


# --------------------------------------------------------------------------- #
# 版本比较
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("1.1.0", "1.0.2", True),
        ("v1.2.0", "1.1.0", True),
        ("1.10.0", "1.9.9", True),      # 按数字比，不是字符串
        ("1.1.0", "1.1.0", False),
        ("1.0.9", "1.0.10", False),
        ("v2.0.0-beta", "1.9.9", True),
        ("", "1.0.0", False),
        ("garbage", "1.0.0", False),
    ],
)
def test_is_newer(candidate, current, expected):
    assert upd.is_newer(candidate, current) is expected


def test_parse_version():
    assert upd.parse_version("v1.2.3") == (1, 2, 3)
    assert upd.parse_version("1.2") == (1, 2)
    assert upd.parse_version("1.2.3-rc1") == (1, 2, 3)
    assert upd.parse_version("") == (0,)


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
RELEASE_PAYLOAD = {
    "tag_name": "v1.2.0",
    "html_url": "https://github.com/ChuYanSir/ssh-mcp/releases/tag/v1.2.0",
    "published_at": "2026-11-20T01:00:00Z",
    "body": "## 更新\n- 修复了 xxx",
    "assets": [
        {"name": "ssh_mcp-1.2.0.tar.gz", "size": 100, "browser_download_url": "https://x/tar"},
        {"name": "ssh_mcp-1.2.0-py3-none-any.whl", "size": 200, "browser_download_url": "https://x/whl"},
    ],
}


def test_check_reports_update(monkeypatch):
    monkeypatch.setattr(upd, "_http_json", lambda url, timeout: RELEASE_PAYLOAD)
    res = upd.check("1.1.0")
    assert res["ok"] is True
    assert res["has_update"] is True
    assert res["latest"] == "1.2.0"
    assert res["info"]["wheel_name"].endswith(".whl")
    assert res["info"]["has_wheel"] is True


def test_check_same_version(monkeypatch):
    monkeypatch.setattr(upd, "_http_json", lambda url, timeout: RELEASE_PAYLOAD)
    res = upd.check("1.2.0")
    assert res["has_update"] is False


def test_check_without_wheel(monkeypatch):
    payload = dict(RELEASE_PAYLOAD, assets=[a for a in RELEASE_PAYLOAD["assets"] if a["name"].endswith(".tar.gz")])
    monkeypatch.setattr(upd, "_http_json", lambda url, timeout: payload)
    res = upd.check("1.0.0")
    assert res["ok"] is True
    assert res["info"]["has_wheel"] is False


def test_check_handles_http_error(monkeypatch):
    def boom(url, timeout):
        raise urllib.error.HTTPError(url, 403, "rate limited", {}, io.BytesIO(b""))

    monkeypatch.setattr(upd, "_http_json", boom)
    res = upd.check("1.0.0")
    assert res["ok"] is False
    assert "403" in res["error"] and "限流" in res["error"]


def test_check_handles_network_error(monkeypatch):
    def boom(url, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(upd, "_http_json", boom)
    res = upd.check("1.0.0")
    assert res["ok"] is False
    assert "网络不可用" in res["error"]


def test_check_handles_bad_json(monkeypatch):
    monkeypatch.setattr(upd, "_http_json", lambda url, timeout: json.loads("{oops"))
    res = upd.check("1.0.0")
    assert res["ok"] is False


def test_check_missing_tag(monkeypatch):
    monkeypatch.setattr(upd, "_http_json", lambda url, timeout: {"assets": []})
    res = upd.check("1.0.0")
    assert res["ok"] is False and "tag_name" in res["error"]


# --------------------------------------------------------------------------- #
# 下载 / 安装
# --------------------------------------------------------------------------- #
class _FakeResp(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_wheel(tmp_path: Path, monkeypatch):
    blob = b"PK\x03\x04fake wheel"
    monkeypatch.setattr(upd.urllib.request, "urlopen", lambda req, timeout: _FakeResp(blob))
    info = upd.ReleaseInfo(
        tag="v1.2.0", version="1.2.0", wheel_name="ssh_mcp-1.2.0-py3-none-any.whl",
        wheel_url="https://x/whl", wheel_size=len(blob),
    )
    seen: list[tuple[int, int]] = []
    path = upd.download_wheel(info, tmp_path, progress=lambda d, t: seen.append((d, t)))
    assert path.read_bytes() == blob
    assert seen and seen[-1][0] == len(blob)


def test_download_without_wheel(tmp_path: Path):
    with pytest.raises(ValueError):
        upd.download_wheel(upd.ReleaseInfo(tag="v1", version="1"), tmp_path)


def test_install_wheel_builds_pip_command(monkeypatch, tmp_path: Path):
    calls: dict = {}

    class R:
        returncode = 0
        stdout = "Successfully installed ssh-mcp-1.2.0"
        stderr = ""

    def fake_run(cmd, capture_output, text, timeout):
        calls["cmd"] = cmd
        return R()

    monkeypatch.setattr(upd.subprocess, "run", fake_run)
    wheel = tmp_path / "ssh_mcp-1.2.0-py3-none-any.whl"
    wheel.write_bytes(b"x")
    res = upd.install_wheel(wheel, python="py.exe")
    assert res["ok"] is True
    assert calls["cmd"][:4] == ["py.exe", "-m", "pip", "install"]
    assert "--upgrade" in calls["cmd"]
    assert str(wheel) in calls["cmd"]


def test_install_wheel_failure(monkeypatch, tmp_path: Path):
    class R:
        returncode = 1
        stdout = ""
        stderr = "ERROR: no matching distribution"

    monkeypatch.setattr(upd.subprocess, "run", lambda *a, **k: R())
    wheel = tmp_path / "x.whl"
    wheel.write_bytes(b"x")
    res = upd.install_wheel(wheel)
    assert res["ok"] is False
    assert "no matching distribution" in res["output"]


def test_pick_wheel_prefers_pure_python():
    assets = [
        {"name": "pkg-1.0-cp312-cp312-win_amd64.whl"},
        {"name": "pkg-1.0-py3-none-any.whl"},
    ]
    assert upd._pick_wheel(assets)["name"].endswith("py3-none-any.whl")
    assert upd._pick_wheel([]) is None
