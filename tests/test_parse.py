#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线回归测试：不联网，只用 tests/fixtures/ 里按真实页面结构做的脱敏样本。
跑法：./.venv/bin/python tests/test_parse.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eju_getter as E  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
failed = []


def check(name, got, want):
    if got != want:
        failed.append("%s\n    实际: %r\n    期望: %r" % (name, got, want))


def load(fn):
    with open(os.path.join(FIX, fn), encoding="utf-8") as f:
        return E.soupify(f.read())


# ---------- 成绩列表页 ----------
lst = load("PSLSCORE010_list.html")
navs = E.find_session_navs(lst)
check("列表页找到 2 个可查看回次", len(navs), 2)
check("回次入口带上了隐藏字段", [n.extra for n in navs],
      [{"btnClick": "1", "hidApplicantId": "AP0000000001"},
       {"btnClick": "1", "hidApplicantId": "AP0000000002"}])
check("回次标签", [E.find_exam_label(n.context) for n in navs],
      ["2025年度第2回", "2025年度第1回"])
check("未公布的回次", [E.find_exam_label(r) for r in E.find_pending_rows(lst)],
      ["2026年度第1回"])
check("列表页不是成绩页", E.looks_like_score_page(lst), False)
check("列表页表单字段齐全",
      sorted(k for k, _ in E.collect_fields(lst)),
      ["btnClick", "datTimeStamp", "hidApplicantId", "menuUrl", "pageClick"])

# 页脚链接不能被当成回次入口
check("页脚没被误当回次",
      [n for n in navs if "特定商取引" in n.context or "プライバシー" in n.context], [])

# ---------- 成绩详情页 ----------
det = load("PSLSCORE020_detail.html")
rec = E.parse_score_page(det, "https://x/PSLSCORE020.php")
check("详情页是成绩页", E.looks_like_score_page(det), True)
check("回次", rec["exam"], "2025年度第2回")
check("受験番号", rec["exam_number"], "99*0000*000001")
check("科目得点", rec["subjects"], {
    "日本語 聴解・聴読解": "74", "日本語 読解": "126", "日本語 合計": "200",
    "日本語 記述": "35", "理科 物理": "55", "理科 化学": "44", "理科 合計": "99",
    "数学 コース1": "122",
})
check("未受験科目也记进 details（得点 ---）",
      rec["details"]["理科 生物"], {"得点": "---", "得点範囲": "0 ～ 100", "平均点": "64.2"})
check("rowspan 展开后大类没丢",
      rec["details"]["総合科目"]["平均点"], "122.7")
check("試験日", rec["info"].get("試験日"), "2025/11/09")
check("可下载文件只有成績確認書",
      rec["documents"], [("成績確認書", "./CMNSCOREPDF.php?ID=AP0000000001")])

# ---------- 拉黑规则 ----------
check("缴费类按钮被拉黑", E.Nav("menu", "PSLREISS010.php", "受験票再発行").blocked, True)
check("改资料被拉黑", E.Nav("menu", "PSLMAINT010.php", "マイページ情報変更").blocked, True)
check("成绩入口不被拉黑", E.Nav("menu", "PSLSCORE010.php", "受験結果").blocked, False)
check("受験申し込み不是成绩入口",
      E.Nav("menu", "PSLAPPLI010.php", "受験申し込み").score() >= 10, False)

if failed:
    print("FAIL %d 项：" % len(failed))
    for f in failed:
        print("  ✗ " + f)
    sys.exit(1)
print("全部通过 ✅")
