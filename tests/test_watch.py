#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线测试抢分数循环：不联网，用假会话喂页面。
模拟「第一轮还是成績公表前 → 第二轮详情按钮出现了」。
跑法：./.venv/bin/python tests/test_watch.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eju_getter as E  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
failed = []


def check(name, got, want):
    if got != want:
        failed.append("%s\n    实际: %r\n    期望: %r" % (name, got, want))


def read(fn):
    with open(os.path.join(FIX, fn), encoding="utf-8") as f:
        return f.read()


LIST_PENDING = read("PSLSCORE010_list.html")
# 成绩公布后：原来写着「成績公表前」的那行冒出了「詳細へ」按钮
LIST_RELEASED = LIST_PENDING.replace(
    "<td><span>成績公表前</span></td>",
    "<td><div class=\"button_area_m hAuto\"><button class=\"btnC link m-b08\" "
    "type=\"button\" onclick=\"detailClick(1,'AP0000000003')\">詳細へ</button></div></td>")
assert LIST_RELEASED != LIST_PENDING, "fixture 替换失败"
DETAIL = read("PSLSCORE020_detail.html")


class FakeSession:
    """只实现 watch_scores / capture_detail 用到的那几个接口。"""

    def __init__(self):
        self.id = "testuser"
        self.verbose = False
        self.url = "https://x/src/PSLSCORE010.php"
        self.html = ""
        self.soup = E.soupify("")
        self.clicks = []
        self.downloads = 0

    def _log(self, *a):
        pass

    def _load(self, html, url):
        self.html, self.url = html, url
        self.soup = E.soupify(html)

    # capture_detail 会调用这个
    def click(self, nav, tag=""):
        self.clicks.append(nav.extra or nav.value)
        self._load(DETAIL, "https://x/src/PSLSCORE020.php")

    def goto_menu(self, url, tag=""):
        self._load(LIST_RELEASED, "https://x/src/PSLSCORE010.php")

    def download(self, rel, path):
        self.downloads += 1
        return None

    def drop_saved_cookies(self):
        pass

    def logout(self):
        pass


out = tempfile.mkdtemp(prefix="eju-watch-test-")
sess = FakeSession()
pages = [LIST_PENDING, LIST_PENDING, LIST_RELEASED]   # 第 3 轮才出分
rounds = {"n": 0}
real_open = E.open_score_list


def fake_open(s, state, pick=False, direct=True):
    i = rounds["n"]
    rounds["n"] += 1
    s._load(pages[min(i, len(pages) - 1)], "https://x/src/PSLSCORE010.php")
    return "direct"


E.open_score_list = fake_open
try:
    data = E.watch_scores(sess, out_dir=out, interval=3, fast_interval=3,
                          hours=0.02, do_notify=False, log_path=os.path.join(out, "w.log"))
finally:
    E.open_score_list = real_open

check("轮询 3 轮后抓到", rounds["n"], 3)
check("抓到 1 个回次", len(data["records"]), 1)
rec = data["records"][0]
check("抓到的是那个原本未公布的回次", rec["exam"], "2025年度第2回")  # 详情页 fixture 的回次
check("点的是新出现的那一行", sess.clicks,
      [{"btnClick": "1", "hidApplicantId": "AP0000000003"}])
check("科目分数解析出来了", rec["subjects"].get("日本語 読解"), "126")

# 落地文件
saved = json.load(open(os.path.join(out, "eju_scores.json"), encoding="utf-8"))
exams = [r.get("exam") for r in saved["records"]]
check("JSON 里有已抓到的成绩", any(r.get("subjects") for r in saved["records"]), True)
check("占位记录没有覆盖真成绩",
      [r for r in saved["records"] if r.get("status") == "成績公表前"
       and r.get("exam") == rec["exam"]], [])
check("csv 生成了", os.path.exists(os.path.join(out, "eju_scores.csv")), True)
raw = os.path.join(out, "raw")
check("原始 HTML 第一时间落盘了",
      len([f for f in os.listdir(raw) if f.endswith(".html")]) if os.path.isdir(raw) else 0, 1)
check("watch 日志写了", os.path.exists(os.path.join(out, "w.log")), True)

shutil.rmtree(out, ignore_errors=True)

if failed:
    print("FAIL %d 项：" % len(failed))
    for f in failed:
        print("  ✗ " + f)
    sys.exit(1)
print("全部通过 ✅")
