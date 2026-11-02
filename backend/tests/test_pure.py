"""可在无真机/无网环境运行的纯逻辑测试：命令构造、比价排序、实体抽取、display 解析。

这些覆盖了不依赖 adb 执行 / 不依赖 LLM key 的部分。真机执行、MediaProjection、
scrcpy 虚拟屏创建需在 Android 真机上验证（见 README）。
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.bridge.adb_bridge import AdbBridge
from backend.bridge.virtual_display import VirtualDisplayManager
from backend.tools.comparison_prices import ComparisonPrices
from backend.memory.ingest import extract_entities


def test_tap_targets_virtual_display():
    b = AdbBridge(serial="ABC123")
    cmd = b._build_tap(100, 200, display_id=2)
    assert cmd == ["adb", "-s", "ABC123", "shell", "input", "-d", "2",
                   "tap", "100", "200"]


def test_start_app_uses_display_flag():
    b = AdbBridge()
    cmd = b._build_start_app("com.taobao.taobao", display_id=3)
    assert "--display" in cmd and "3" in cmd
    assert "com.taobao.taobao" in cmd


def test_screencap_targets_display():
    b = AdbBridge()
    cmd = b._build_screencap(display_id=5)
    assert cmd[-3:] == ["screencap", "-d", "5"] or \
           ("-d" in cmd and "5" in cmd and "screencap" in cmd)


def test_text_escapes_spaces():
    b = AdbBridge()
    cmd = b._build_text("hello world", display_id=1)
    joined = " ".join(cmd)
    assert "hello%sworld" in joined


def test_parse_new_display():
    line = "[server] INFO: New display: 1080x2340/320 (id=2)"
    disp = VirtualDisplayManager._parse_display(line)
    assert disp is not None
    assert disp.display_id == 2
    assert disp.width == 1080 and disp.height == 2340 and disp.density == 320


def test_price_comparison_sorts_and_picks_cheapest():
    c = ComparisonPrices()
    res = c.compare("华为 Mate 60 Pro")
    prices = [r["price"] for r in res["results"]]
    assert prices == sorted(prices)
    assert res["cheapest"]["price"] == prices[0]
    assert "最低价" in res["summary"]


def test_empty_query_price():
    c = ComparisonPrices()
    res = c.compare("  ")
    assert res["results"] == []


def test_extract_entities_brand_and_price():
    ents = extract_entities("画面里是一台华为手机，售价 ¥6799")
    assert "华为" in ents
    assert any("6799" in e for e in ents)


def test_extract_entities_dedup():
    ents = extract_entities("iPhone iPhone 15")
    assert ents.count("iPhone") == 1
