#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eju_getter — 只存一次 EJU 账号密码，之后一条命令抓取历次 EJU 成绩详情。

目标站点：EJU オンライン（JASSO）
  https://eju-online.jasso.go.jp/src/CMNLOGIN010.php

用法：
  python3 eju_getter.py setup            # 保存 MyPageID / 密码（macOS 钥匙串优先）
  python3 eju_getter.py explore          # 登录后打印可点击的菜单项（第一次排查用）
  python3 eju_getter.py fetch            # 登录 → 找成绩页 → 抓全部回次 → 存 JSON/CSV
  python3 eju_getter.py fetch --dump     # 同时把每一页原始 HTML 存下来（排查解析问题）
  python3 eju_getter.py fetch --pick     # 自动识别失败时，手动从菜单里选
  python3 eju_getter.py forget           # 删除已保存的凭据

设计要点：
  * 该站点所有跳转都是「把当前页面 form 原样 POST 回去 + 改 btnClick / menuUrl」，
    并且 datTimeStamp 是服务器每页下发的一次性令牌 —— 所以每次提交都必须重新
    采集当前页面的全部表单字段。本脚本按这个规则实现。
  * 只做只读操作。带有 申込/出願/支払/決済/登録/変更/削除/再発行 等字样的按钮
    会被硬性拉黑，永远不会被点击（该系统同时也是报名和缴费入口）。
"""

from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time
import warnings
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

warnings.filterwarnings("ignore")  # LibreSSL / urllib3 噪音

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit(
        "缺少依赖。请先执行：\n"
        "  python3 -m venv .venv && ./.venv/bin/pip install requests beautifulsoup4 lxml\n"
        "然后用 ./.venv/bin/python eju_getter.py ... 运行（或直接跑 ./setup.sh）"
    )


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

BASE = "https://eju-online.jasso.go.jp"
LOGIN_URL = BASE + "/src/CMNLOGIN010.php"
# 站点 JS 里这两个 action 是相对当前页面目录的（../common/...），必须按当前 URL 解析，
# 不能写死绝对路径 —— 登录后页面并不一定在 /src/ 下。
MOVE_MENU_REL = "../common/moveUserMenu.php"
LOGOUT_REL = "../common/loguoutUser.php"  # 站点自带拼写
LOGIN_BTN = "1"  # ログイン按钮的 btnClick 值

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

KEYCHAIN_SERVICE = "eju-getter"
CONFIG_DIR = os.path.expanduser("~/.config/eju-getter")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CRED_FILE = os.path.join(CONFIG_DIR, "credentials.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")      # 学到的站点结构（页面地址）

REQUEST_DELAY = 1.2  # 秒；对站点客气一点
RETRY_CODES = (429, 500, 502, 503, 504)
RETRY_WAITS = (6, 20, 45)  # 遇到 5xx / 限流时的退避秒数

# 成绩相关的正向关键词（命中即视为候选）
SCORE_WORDS = [
    "成績", "得点", "点数", "スコア", "成绩",
    "score", "Score", "SCORE", "result", "Result",
]
# 只读加分词
READ_WORDS = ["照会", "確認", "参照", "閲覧", "表示", "詳細", "通知", "inquiry", "view"]
# 绝对不点：会改数据 / 花钱 / 退出登录
HARD_BLOCK_WORDS = [
    "支払", "決済", "購入", "入金", "返金", "領収",
    "登録", "変更", "修正", "取消", "取り消", "削除", "再発行", "発行申請",
    "アップロード", "同意", "送信", "確定", "決定", "退会",
    "ログアウト", "logout", "Logout", "パスワード", "写真",
]
# 谨慎对待：只有在同时明确写着「成績」之类字样时才允许点
SOFT_BLOCK_WORDS = ["申込", "申請", "出願", "受付票", "受験票"]

# 科目/成绩表识别用
SUBJECT_WORDS = [
    "日本語", "記述", "読解", "聴解", "聴読解", "聴解・聴読解",
    "理科", "物理", "化学", "生物", "総合科目", "数学", "コース",
    "得点", "配点", "合計", "総合",
]

# 实际页面上的写法是「2025年度（令和7年度）日本留学試験（第2回）」，年度和回次之间还夹着别的字
EXAM_RE = re.compile(
    r"(20\d{2})\s*年度?[^\n]{0,40}?(?:第\s*([0-9０-９一二]+)\s*回|(前期|後期))"
)
EXAM_RE_LOOSE = re.compile(r"第\s*([0-9０-９一二]+)\s*回")
"""受験番号 的实际形态是 99*0000*000001（数字 + * 分隔），不要匹配到「2025年度」的年份。"""
EXAMNO_RE = re.compile(r"受験番号[^0-9]{0,4}([0-9][0-9*\-]{5,19})(?!\s*年)")
EXAMNO_ONLY_RE = re.compile(r"^[0-9][0-9*\-]{5,19}$")


# --------------------------------------------------------------------------- #
# 凭据存储
# --------------------------------------------------------------------------- #

def _keychain_available() -> bool:
    return platform.system() == "Darwin" and _which("security") is not None


def _which(cmd: str) -> Optional[str]:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        f = os.path.join(p, cmd)
        if os.path.isfile(f) and os.access(f, os.X_OK):
            return f
    return None


def _keychain_set(account: str, password: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-s", KEYCHAIN_SERVICE,
         "-a", account, "-w", password, "-U"],
        check=True, capture_output=True,
    )


def _keychain_get(account: str) -> Optional[str]:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
         "-a", account, "-w"],
        capture_output=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8").rstrip("\n")


def _keychain_del(account: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
        capture_output=True,
    )


def save_credentials(mypage_id: str, password: str) -> str:
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    if _keychain_available():
        _keychain_set(mypage_id, password)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"mypage_id": mypage_id, "store": "keychain"}, f, ensure_ascii=False)
        os.chmod(CONFIG_FILE, 0o600)
        return "macOS 钥匙串（服务名 %s）" % KEYCHAIN_SERVICE
    with open(CRED_FILE, "w", encoding="utf-8") as f:
        json.dump({"mypage_id": mypage_id, "password": password}, f, ensure_ascii=False)
    os.chmod(CRED_FILE, 0o600)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"mypage_id": mypage_id, "store": "file"}, f, ensure_ascii=False)
    os.chmod(CONFIG_FILE, 0o600)
    return CRED_FILE + "（权限 600）"


def load_credentials() -> Optional[Tuple[str, str]]:
    env_id, env_pw = os.environ.get("EJU_ID"), os.environ.get("EJU_PASSWORD")
    if env_id and env_pw:
        return env_id, env_pw

    cfg: Dict[str, Any] = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

    mypage_id = cfg.get("mypage_id")
    if mypage_id and cfg.get("store") == "keychain":
        pw = _keychain_get(mypage_id)
        if pw:
            return mypage_id, pw

    if os.path.exists(CRED_FILE):
        try:
            with open(CRED_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("mypage_id") and d.get("password"):
                return d["mypage_id"], d["password"]
        except Exception:
            pass
    return None


def _write_private_json(path: str, obj: Any) -> None:
    """原子写 + 权限 600（cookie 和凭据都算敏感数据）。"""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_state() -> Dict[str, Any]:
    """上次学到的站点结构：菜单页地址、成绩列表页地址等。"""
    return _read_json(STATE_FILE)


def save_state(state: Dict[str, Any]) -> None:
    _write_private_json(STATE_FILE, state)


def forget_credentials() -> None:
    cfg: Dict[str, Any] = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
    if cfg.get("mypage_id") and _keychain_available():
        _keychain_del(cfg["mypage_id"])
    for p in (CRED_FILE, CONFIG_FILE, STATE_FILE):
        if os.path.exists(p):
            os.remove(p)


# --------------------------------------------------------------------------- #
# 页面工具
# --------------------------------------------------------------------------- #

def soupify(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def page_text(soup: BeautifulSoup) -> str:
    # 注意：不能 decompose script/style —— 页面内联的 detailClick() 等跳转函数还要用，
    # 这个函数必须是非破坏性的。
    txt = "\n".join(
        t for t in soup.find_all(string=True)
        if t.parent is not None and t.parent.name not in ("script", "style")
    )
    txt = re.sub(r"[ \t\r　]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n", txt)
    return txt.strip()


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("　", " ")).strip()


def collect_fields(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """采集当前页面 form 的全部可提交字段（含一次性 datTimeStamp）。"""
    scope = soup.find("form", attrs={"name": "main_form"}) or soup.find("form")
    if scope is None or not scope.find_all(["input", "select", "textarea"]):
        scope = soup  # HTML 有时嵌套异常，退回整篇文档

    out: List[Tuple[str, str]] = []
    for el in scope.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name:
            continue
        if el.name == "input":
            t = (el.get("type") or "text").lower()
            if t in ("submit", "button", "image", "reset", "file"):
                continue
            if t in ("checkbox", "radio"):
                if el.has_attr("checked"):
                    out.append((name, el.get("value", "on")))
                continue
            out.append((name, el.get("value") or ""))
        elif el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            if opt is not None:
                out.append((name, opt.get("value", clean(opt.get_text()))))
        else:
            out.append((name, el.get_text() or ""))
    return out


# 站点的 JS 跳转入口
NAV_PATTERNS = [
    ("menu", re.compile(r"moveUserMenu\(\s*['\"]([^'\"]+)['\"]")),
    ("logout", re.compile(r"moveUserLogOut\(\s*['\"]([^'\"]+)['\"]")),
    ("btn", re.compile(r"clickBtn(?:Disable|Agree)?\(\s*['\"]?([0-9]+)")),
    ("radio", re.compile(r"changeRdo\(\s*['\"]?([0-9]+)")),
]


class Nav:
    """页面上一个可点击项。"""

    def __init__(self, kind: str, value: str, label: str, href: Optional[str] = None,
                 extra: Optional[Dict[str, str]] = None, context: str = ""):
        self.kind = kind          # menu | btn | link | logout | radio | js
        self.value = value        # menuUrl 或 btnClick 值
        self.label = label
        self.href = href
        self.extra = extra or {}  # js 型入口要额外写入的隐藏字段
        self.context = context    # 所在表格行的整行文字（用来认回次）

    def __repr__(self) -> str:
        return "Nav(%s=%s%s, %r)" % (
            self.kind, self.value,
            "+" + json.dumps(self.extra, ensure_ascii=False) if self.extra else "",
            self.label[:40])

    @property
    def key(self) -> str:
        return "%s|%s|%s" % (self.kind, self.value,
                             json.dumps(self.extra, sort_keys=True))

    @property
    def _blob(self) -> str:
        return self.label + " " + self.value + " " + self.context

    @property
    def has_score_word(self) -> bool:
        return any(w in self._blob for w in SCORE_WORDS)

    @property
    def blocked(self) -> bool:
        blob = self._blob
        if any(w in blob for w in HARD_BLOCK_WORDS):
            return True
        # 报名类字样：只有同时明确提到成绩时才放行（成绩页的说明文字里常提到出願）
        if any(w in blob for w in SOFT_BLOCK_WORDS) and not self.has_score_word:
            return True
        return False

    def score(self) -> int:
        blob = self._blob
        if self.blocked:
            return -100
        s = 0
        if self.has_score_word:
            s += 10
        if any(w in blob for w in READ_WORDS):
            s += 3
        if "成績" in blob and any(w in blob for w in READ_WORDS):
            s += 5
        if self.kind == "menu":
            s += 1
        return s


# 页面内联的一次性跳转函数，例如成绩列表页的
#   function detailClick(num,ApplicantId){
#       document.main_form.btnClick.value=num;
#       document.main_form.hidApplicantId.value=ApplicantId; document.main_form.submit(); }
# 这里把它们解析出来，从而知道每个 onclick 实参该写进哪个表单字段。
FUNC_RE = re.compile(r"function\s+(\w+)\s*\(([^)]*)\)\s*\{(.*?)\}", re.S)
ASSIGN_RE = re.compile(r"document\.main_form\.(\w+)\.value\s*=\s*([^;}]+)")
CALL_RE = re.compile(r"(\w+)\s*\(([^)]*)\)")
ARG_RE = re.compile(r"'[^']*'|\"[^\"]*\"|[^,]+")

Handlers = Dict[str, Tuple[List[str], List[Tuple[str, str]]]]


def parse_inline_handlers(soup: BeautifulSoup) -> Handlers:
    handlers: Handlers = {}
    for sc in soup.find_all("script"):
        if sc.get("src"):
            continue
        code = sc.string or sc.get_text() or ""
        for m in FUNC_RE.finditer(code):
            name, params_s, body = m.group(1), m.group(2), m.group(3)
            assigns = [(f, e.strip()) for f, e in ASSIGN_RE.findall(body)]
            if not any(f == "btnClick" for f, _ in assigns):
                continue  # 只认「设置 btnClick 后提交」这一类跳转
            params = [p.strip() for p in params_s.split(",") if p.strip()]
            handlers[name] = (params, assigns)
    return handlers


def _unquote(s: str) -> Optional[str]:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    return None


def handler_overrides(handlers: Handlers, call: str,
                      args_s: str) -> Optional[Dict[str, str]]:
    """把 onclick="foo(1,'AP0001')" 翻译成要提交的字段字典。"""
    if call not in handlers:
        return None
    params, assigns = handlers[call]
    args = [a.strip() for a in ARG_RE.findall(args_s) if a.strip()]
    env = dict(zip(params, args))
    out: Dict[str, str] = {}
    for field, expr in assigns:
        lit = _unquote(expr)
        if lit is not None:
            out[field] = lit
        elif expr in env:
            lit = _unquote(env[expr])
            out[field] = lit if lit is not None else env[expr]
        else:
            return None  # 表达式看不懂，宁可不提交
    return out


def collect_navs(soup: BeautifulSoup, handlers: Optional[Handlers] = None) -> List[Nav]:
    navs: List[Nav] = []
    seen = set()
    if handlers is None:
        handlers = parse_inline_handlers(soup)

    for el in soup.find_all(["a", "button", "input", "li", "div", "span", "td"]):
        blob = " ".join(
            str(el.get(attr) or "") for attr in ("onclick", "href", "onchange")
        )
        if not blob.strip():
            continue
        label = clean(el.get_text()) or clean(str(el.get("value") or "")) or clean(
            str(el.get("alt") or "")
        )
        for kind, pat in NAV_PATTERNS:
            for m in pat.finditer(blob):
                key = (kind, m.group(1), label)
                if key in seen:
                    continue
                seen.add(key)
                navs.append(Nav(kind, m.group(1), label))
        # 页面内联函数（如 detailClick(1,'AP0000...')）
        for m in CALL_RE.finditer(blob):
            ov = handler_overrides(handlers, m.group(1), m.group(2))
            if not ov:
                continue
            key = ("js", json.dumps(ov, sort_keys=True), label)
            if key in seen:
                continue
            seen.add(key)
            navs.append(Nav("js", ov.get("btnClick", ""), label, extra=ov))
        href = el.get("href")
        if href and href.endswith(".php") and not href.startswith("#"):
            key = ("link", href, label)
            if key not in seen:
                seen.add(key)
                navs.append(Nav("link", href, label, href=href))
    return navs


def is_system_error(soup: BeautifulSoup) -> bool:
    t = page_text(soup)
    return "システムエラー" in t or "A system error occurred" in t


def is_login_page(soup: BeautifulSoup) -> bool:
    return soup.find(attrs={"name": "txtMyPageID"}) is not None


def login_error(soup: BeautifulSoup) -> Optional[str]:
    t = page_text(soup)
    for pat in (
        r"[^\n]*(?:ID|ＩＤ)[^\n]*(?:違い|誤|一致しません)[^\n]*",
        r"[^\n]*パスワード[^\n]*(?:違い|誤|一致しません|ロック)[^\n]*",
        r"[^\n]*(?:incorrect|does not match|locked)[^\n]*",
        r"[^\n]*ロックされ[^\n]*",
    ):
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            return clean(m.group(0))[:200]
    return None


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #

class MaintenanceError(RuntimeError):
    """站点在计划维护中（返回 503 + メンテナンス 页面）。"""


class LoginRejected(RuntimeError):
    """账号或密码被站点拒绝 —— 绝对不能重试（连续失败会锁账号）。"""


def maintenance_window(resp: requests.Response) -> Optional[str]:
    """从维护页里抠出维护时间段；不是维护页就返回 None。"""
    try:
        body = resp.content.decode("utf-8", "replace")
    except Exception:
        return None
    if "メンテナンス" not in body and "maintenance" not in body.lower():
        return None
    txt = page_text(soupify(body))
    m = re.search(r"\[Maintenance Window\]\s*\n(.+)", txt)
    if m:
        return clean(m.group(1))
    m = re.search(r"(20\d{2}[^\n]*?(?:から|-)[^\n]*?)(?:を\s*予定|JST)", txt)
    return clean(m.group(1)) if m else "（站点公告未给出具体时段）"


class EjuSession:
    def __init__(self, mypage_id: str, password: str, verbose: bool = False,
                 dump_dir: Optional[str] = None):
        self.id = mypage_id
        self.pw = password
        self.verbose = verbose
        self.dump_dir = dump_dir
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7",
        })
        self.url = LOGIN_URL
        self.html = ""
        self.soup: BeautifulSoup = soupify("")
        self._dump_n = 0
        self.logged_in = False

    def reset_session(self) -> None:
        """扔掉当前会话（cookie + 登录状态），下一步从登录页干净地重来。"""
        self.s.cookies.clear()
        self.logged_in = False
        self.url = LOGIN_URL
        self.soup = soupify("")

    # -- 底层 ------------------------------------------------------------- #

    def _log(self, *a: Any) -> None:
        if self.verbose:
            print("[debug]", *a, file=sys.stderr)

    def _absorb(self, resp: requests.Response, tag: str) -> None:
        resp.encoding = resp.encoding or "utf-8"
        if not resp.encoding or resp.encoding.lower() in ("iso-8859-1",):
            resp.encoding = "utf-8"
        self.url = resp.url
        self.html = resp.text
        self.soup = soupify(self.html)
        self._log("%s -> %s (%d bytes)" % (tag, resp.url, len(self.html)))
        if self.dump_dir:
            self._dump_n += 1
            os.makedirs(self.dump_dir, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", tag)[:60]
            path = os.path.join(self.dump_dir, "%02d_%s.html" % (self._dump_n, safe))
            with open(path, "w", encoding="utf-8") as f:
                f.write("<!-- %s -->\n" % resp.url + self.html)

    def _request(self, method: str, url: str, **kw: Any) -> requests.Response:
        """站点偶尔返回 503/502（维护或限流），退避重试几次。"""
        last: Optional[requests.Response] = None
        for attempt, wait in enumerate(RETRY_WAITS, 1):
            time.sleep(REQUEST_DELAY)
            r = self.s.request(method, url, timeout=60, allow_redirects=True, **kw)
            if r.status_code not in RETRY_CODES:
                r.raise_for_status()
                return r
            window = maintenance_window(r)
            if window:  # 计划维护，重试没意义
                raise MaintenanceError(window)
            last = r
            self._log("HTTP %d，%d 秒后重试（第 %d 次）" % (r.status_code, wait, attempt))
            time.sleep(wait)
        assert last is not None
        last.raise_for_status()
        return last

    def get(self, url: str, tag: str = "get") -> None:
        self._absorb(self._request("GET", url), tag)

    def download(self, rel_url: str, path: str) -> Optional[str]:
        """下载成绩确认书之类的文件；返回落地路径，失败返回 None。"""
        url = self.resolve(rel_url)
        r = self._request("GET", url, headers={"Referer": self.url})
        if r.status_code != 200:
            self._log("下载失败 %s -> HTTP %d" % (url, r.status_code))
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        data = r.content
        if "html" in ctype or data[:5] in (b"<!DOC", b"<html"):
            self._log("下载得到的是 HTML 而不是文件：%s" % url)
            return None
        if data[:4] == b"%PDF" or "pdf" in ctype:
            ext = ".pdf"
        else:
            ext = os.path.splitext(rel_url.split("?")[0])[1] or ".bin"
        path = os.path.splitext(path)[0] + ext
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        self._log("已下载 %s (%d bytes)" % (path, len(data)))
        return path

    def submit(self, overrides: Dict[str, str], action: Optional[str] = None,
               tag: str = "post") -> None:
        """把当前页面的表单原样回传（含一次性令牌），只覆盖指定字段。"""
        fields = collect_fields(self.soup)
        keys = {k for k, _ in fields}
        merged: List[Tuple[str, str]] = [
            (k, overrides.get(k, v)) for k, v in fields
        ]
        for k, v in overrides.items():
            if k not in keys:
                merged.append((k, v))
        # 站点 form 是 multipart/form-data，照原样发，避免 WAF 误判
        files = [(k, (None, v)) for k, v in merged]
        r = self._request("POST", action or self.url, files=files,
                          headers={"Referer": self.url})
        self._absorb(r, tag)

    # -- 流程 ------------------------------------------------------------- #

    def login(self) -> None:
        self.get(LOGIN_URL, tag="login_page")
        if not is_login_page(self.soup):
            raise RuntimeError("打开的不是登录页，站点结构可能变了：%s" % self.url)
        self.submit(
            {"txtMyPageID": self.id, "pwdPassword": self.pw, "btnClick": LOGIN_BTN},
            action=LOGIN_URL,
            tag="login_post",
        )
        if is_system_error(self.soup):
            raise RuntimeError(
                "登录后返回「システムエラー」。常见原因：同一账号已在浏览器里登录着、"
                "会话超时、或站点在维护。请先在浏览器里退出登录，稍后重试。"
            )
        err = login_error(self.soup)
        if err:
            raise LoginRejected("登录失败：%s" % err)
        if is_login_page(self.soup):
            raise LoginRejected(
                "登录失败：仍停留在登录页（ID 或密码可能不对；连续失败会被锁定，"
                "请到浏览器里确认）。"
            )
        self.logged_in = True
        self._log("登录成功，当前页：", self.url)

    def resolve(self, rel: str) -> str:
        return requests.compat.urljoin(self.url, rel)

    def goto_menu(self, menu_url: str, tag: str = "menu") -> None:
        self.submit({"menuUrl": menu_url}, action=self.resolve(MOVE_MENU_REL), tag=tag)

    def click(self, nav: Nav, tag: Optional[str] = None) -> None:
        tag = tag or ("click_" + (nav.label[:20] or nav.value))
        if nav.blocked:
            raise RuntimeError("拒绝点击（可能是报名/缴费/修改类操作）：%r" % nav.label)
        if nav.kind == "menu":
            self.goto_menu(nav.value, tag=tag)
        elif nav.kind == "js":
            self.submit(dict(nav.extra), tag=tag)
        elif nav.kind in ("btn", "radio"):
            self.submit({"btnClick": nav.value}, tag=tag)
        elif nav.kind == "link":
            url = nav.href or nav.value
            if not url.startswith("http"):
                base = self.url.rsplit("/", 1)[0] + "/"
                url = requests.compat.urljoin(base, url)
            self.get(url, tag=tag)
        else:
            raise RuntimeError("不支持的跳转类型：%s" % nav.kind)

    def logout(self) -> None:
        if not self.logged_in:
            return
        try:
            navs = [n for n in collect_navs(self.soup) if n.kind == "logout"]
            target = navs[0].value if navs else "../src/CMNLOGIN010.php"
            self.submit({"menuUrl": target},
                        action=self.resolve(LOGOUT_REL), tag="logout")
            self._log("已退出登录")
        except Exception as e:  # 退出失败不影响已抓到的数据
            self._log("退出登录失败（可忽略）：", e)
        finally:
            self.logged_in = False


# --------------------------------------------------------------------------- #
# 表格 / 成绩解析
# --------------------------------------------------------------------------- #

def _span(cell, attr: str) -> int:
    try:
        return max(1, int(cell.get(attr, 1)))
    except (TypeError, ValueError):
        return 1


def table_to_rows(table) -> List[List[str]]:
    """展开 rowspan / colspan —— 成绩表用 rowspan 合并「日本語」「理科」这类科目大类。"""
    carry: Dict[Tuple[int, int], str] = {}   # 被上方 rowspan 占住的格子
    rows: List[List[str]] = []
    for r, tr in enumerate(table.find_all("tr")):
        row: List[str] = []
        c = 0
        for cell in tr.find_all(["th", "td"]):
            while (r, c) in carry:
                row.append(carry[(r, c)])
                c += 1
            text = clean(cell.get_text(" "))
            cs, rs = _span(cell, "colspan"), _span(cell, "rowspan")
            for _ in range(cs):
                row.append(text)
                for rr in range(1, rs):
                    carry[(r + rr, c)] = text
                c += 1
        while (r, c) in carry:
            row.append(carry[(r, c)])
            c += 1
        if any(row):
            rows.append(row)
    return rows


def extract_tables(soup: BeautifulSoup) -> List[List[List[str]]]:
    out = []
    for t in soup.find_all("table"):
        if t.find("table"):  # 只取最内层，避免布局表格重复
            continue
        rows = table_to_rows(t)
        if rows:
            out.append(rows)
    return out


def looks_like_score_table(rows: List[List[str]]) -> bool:
    blob = " ".join(" ".join(r) for r in rows)
    has_subject = any(w in blob for w in SUBJECT_WORDS)
    has_number = re.search(r"(?<!\d)\d{1,3}(?!\d)", blob) is not None
    return has_subject and has_number


def looks_like_score_page(soup: BeautifulSoup) -> bool:
    txt = page_text(soup)
    if not any(w in txt for w in ("成績", "得点", "score", "Score")):
        return False
    return any(looks_like_score_table(r) for r in extract_tables(soup))


def find_exam_label(text: str) -> Optional[str]:
    m = EXAM_RE.search(text)
    if m:
        year, nth, term = m.group(1), m.group(2), m.group(3)
        return "%s年度%s" % (year, ("第%s回" % clean(nth)) if nth else term)
    m2 = EXAM_RE_LOOSE.search(text)
    if m2:
        return clean(m2.group(0))
    return None


FILLER_CELLS = ("", "-", "－", "—", "‐", "*", "※", "/", "／", "---", "―", "--")


def _is_value_cell(c: str) -> bool:
    """看起来像一个分数格（数字、或缺考/未受验之类的占位）。"""
    return bool(re.search(r"[0-9０-９]", c)) or c in FILLER_CELLS or c in (
        "欠席", "未受験", "非該当", "N/A")


def _dedup_labels(cells: List[str]) -> str:
    """['総合科目','総合科目'] → '総合科目'；['日本語','読解'] → '日本語 読解'"""
    parts: List[str] = []
    for c in cells:
        c = c.strip()
        if not c or c in FILLER_CELLS:
            continue
        if parts and parts[-1] == c:
            continue
        parts.append(c)
    return " ".join(parts)


def parse_score_table(rows: List[List[str]]) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    """
    按表头列解析「得点の詳細」表：
        科目 | 科目 | 得点 | 得点範囲 | 平均点
        日本語 | 聴解・聴読解 | 74 | 0 ～ 200 | 100
    返回 (subjects, details)。认不出表头就返回空，交给通用启发式兜底。
    """
    hdr_i: Optional[int] = None
    for i, row in enumerate(rows):
        if any(c == "得点" or c.startswith("得点") for c in row) and any("科目" in c for c in row):
            hdr_i = i
            break
    if hdr_i is None:
        return {}, {}
    hdr = rows[hdr_i]

    def col_of(*words: str) -> Optional[int]:
        for w in words:                       # 先精确匹配，避免「得点」命中「得点範囲」
            for j, c in enumerate(hdr):
                if c == w:
                    return j
        for w in words:
            for j, c in enumerate(hdr):
                if w in c:
                    return j
        return None

    score_col = col_of("得点", "点数")
    if score_col is None or score_col == 0:
        return {}, {}
    range_col = col_of("得点範囲", "範囲")
    avg_col = col_of("平均点", "平均")

    subjects: Dict[str, str] = {}
    details: Dict[str, Dict[str, str]] = {}
    for row in rows[hdr_i + 1:]:
        if len(row) <= score_col:
            continue
        name = _dedup_labels(row[:score_col])
        if not name:
            continue
        score = row[score_col].strip()
        info = {"得点": score}
        if range_col is not None and range_col < len(row):
            info["得点範囲"] = row[range_col].strip()
        if avg_col is not None and avg_col < len(row):
            info["平均点"] = row[avg_col].strip()
        details.setdefault(name, info)
        if re.search(r"[0-9０-９]", score):
            subjects.setdefault(name, score)
    return subjects, details


# 详情页的基本信息是 <dl><dt>标签</dt><dd>值</dd></dl> 结构
def extract_dl_meta(soup: BeautifulSoup) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    for dl in soup.find_all("dl"):
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            k, v = clean(dt.get_text(" ")), clean(dd.get_text(" "))
            if k and v and v not in FILLER_CELLS and k not in meta:
                meta[k] = v
    return meta


# 「成績確認書」按钮：onclick="window.location.href ='./CMNSCOREPDF.php?ID=AP...'"
HREF_JS_RE = re.compile(r"window\.location\.href\s*=\s*['\"]([^'\"]+)['\"]")


def find_document_links(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """详情页上可下载的成绩文件（返回 [(标签, 相对URL)]）。"""
    out: List[Tuple[str, str]] = []
    seen = set()
    for el in soup.find_all(["a", "button"]):
        blob = str(el.get("onclick") or "") + " " + str(el.get("href") or "")
        label = clean(el.get_text(" "))
        urls = HREF_JS_RE.findall(blob)
        href = el.get("href")
        if href and ("PDF" in href.upper() or href.lower().endswith(".pdf")):
            urls.append(href)
        for u in urls:
            # 必须明确和成绩有关，否则会把页脚的「プライバシーポリシー（PDF）」也算进来
            if not any(w in (label + u) for w in ("成績", "得点", "SCORE", "Score", "score")):
                continue
            if u in seen:
                continue
            seen.add(u)
            out.append((label or "document", u))
    return out


def parse_score_page(soup: BeautifulSoup, url: str) -> Dict[str, Any]:
    """把一页成绩页解析成结构化数据；同时保留原始表格，绝不丢信息。"""
    txt = page_text(soup)
    tables = extract_tables(soup)
    score_tables = [t for t in tables if looks_like_score_table(t)]

    subjects: Dict[str, str] = {}
    details: Dict[str, Dict[str, str]] = {}

    # 首选：按表头列解析（真实的「得点の詳細」表就是这种）
    for rows in score_tables:
        s, d = parse_score_table(rows)
        for k, v in s.items():
            subjects.setdefault(k, v)
        for k, v in d.items():
            details.setdefault(k, v)

    for rows in [] if subjects else score_tables:
        # 兜底形态 A：横表（表头一行是科目，下一行是分数）
        for i, row in enumerate(rows):
            hdr_hits = [c for c in row if any(w in c for w in SUBJECT_WORDS)]
            if len(row) >= 2 and len(hdr_hits) >= 2 and i + 1 < len(rows):
                nxt = rows[i + 1]
                # 数值行：允许第一格是行标题（如「日本語」），其余必须像分数
                body = nxt[1:] if len(nxt) == len(row) else []
                if (body
                        and all(_is_value_cell(c) for c in body)
                        and not any(any(w in c for w in SUBJECT_WORDS) for c in body)
                        and any(re.search(r"[0-9０-９]", c) for c in nxt)):
                    for k, v in zip(row, nxt):
                        if (k and any(w in k for w in SUBJECT_WORDS)
                                and _is_value_cell(v) and v not in FILLER_CELLS):
                            subjects.setdefault(k, v)
        # 形态 B：竖表（左列科目，右列分数）
        for row in rows:
            if len(row) == 2:
                key, val = row[0], row[1]
            elif len(row) >= 3 and re.search(r"\d", row[-1]) and not any(
                re.search(r"\d", c) for c in row[:-1]
            ):
                # 例如 ["日本語", "読解", "180"]
                key = clean(" ".join(c for c in row[:-1] if c not in FILLER_CELLS))
                val = row[-1]
            else:
                continue  # 多列数字行由形态 A 处理
            if (key and any(w in key for w in SUBJECT_WORDS)
                    and _is_value_cell(val) and val not in FILLER_CELLS):
                subjects.setdefault(key, val)

    exam = find_exam_label(txt)

    # 受験番号：优先从「受験番号 | 值」这样的表格行里取，再退回正则
    exam_no = None
    for rows in tables:
        for row in rows:
            for j, cell in enumerate(row):
                if "受験番号" in cell and j + 1 < len(row) and EXAMNO_ONLY_RE.match(row[j + 1]):
                    exam_no = row[j + 1]
                    break
            if exam_no:
                break
        if exam_no:
            break
    if not exam_no:
        m = EXAMNO_RE.search(txt.replace("\n", " "))
        if m:
            exam_no = m.group(1)

    meta = extract_dl_meta(soup)
    if not exam:
        for k in ("試験", "試験名"):
            if meta.get(k):
                exam = find_exam_label(meta[k]) or meta[k]
                break
    if not exam_no and meta.get("受験番号"):
        exam_no = meta["受験番号"]

    return {
        "exam": exam,
        "exam_number": exam_no,
        "subjects": subjects,       # {科目: 得点}
        "details": details,         # {科目: {得点, 得点範囲, 平均点}}
        "info": meta,               # 試験日 / 氏名 / 受験地 / 予約種別 …
        "documents": find_document_links(soup),
        "url": url,
        "tables": score_tables if score_tables else tables,
        "text": txt,
    }


DETAIL_WORDS = ("詳細", "表示", "確認する", "成績を見る", "照会", "detail", "Detail")


def find_session_navs(soup: BeautifulSoup) -> List[Nav]:
    """
    「受験結果の一覧」这种列表页：每行一个回次，行末是「詳細へ」按钮。
    只在表格行里找入口 —— 否则会把页脚链接（「特定商取引法に基づく表示」里也有
    「表示」二字）误当成回次。
    """
    handlers = parse_inline_handlers(soup)
    out: List[Nav] = []
    seen = set()
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            row_text = clean(tr.get_text(" "))
            if not row_text:
                continue
            row_has_exam = bool(EXAM_RE.search(row_text) or EXAM_RE_LOOSE.search(row_text))
            for nav in collect_navs(tr, handlers):
                if nav.blocked or nav.kind in ("logout", "link"):
                    continue
                if not (row_has_exam or any(w in nav.label for w in DETAIL_WORDS)):
                    continue
                nav.context = row_text
                if nav.key in seen:
                    continue
                seen.add(nav.key)
                out.append(nav)
    return out


def find_pending_rows(soup: BeautifulSoup) -> List[str]:
    """列表里还没公布成绩的回次（没有「詳細へ」按钮，只写着「成績公表前」）。"""
    pending: List[str] = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            text = clean(tr.get_text(" "))
            if not text or not (EXAM_RE.search(text) or EXAM_RE_LOOSE.search(text)):
                continue
            if tr.find_all(["a", "button"]) or "詳細" in text:
                continue
            pending.append(text)
    return pending


# --------------------------------------------------------------------------- #
# 抓取主流程
# --------------------------------------------------------------------------- #

def find_score_entry(sess: EjuSession, pick: bool = False) -> Optional[Nav]:
    navs = [n for n in collect_navs(sess.soup) if not n.blocked and n.kind != "logout"]
    ranked = sorted(navs, key=lambda n: n.score(), reverse=True)
    best = [n for n in ranked if n.score() >= 10]
    if best and not pick:
        return best[0]

    if not navs:
        return None
    print("\n登录后页面上可点击的入口（选一个成绩相关的）：")
    for i, n in enumerate(ranked, 1):
        print("  [%2d] %-40s  (%s=%s, 匹配度 %d)" % (
            i, n.label[:40] or "(无文字)", n.kind, n.value, n.score()))
    ans = input("输入编号（回车放弃）：").strip()
    if not ans.isdigit() or not (1 <= int(ans) <= len(ranked)):
        return None
    return ranked[int(ans) - 1]


def _safe_name(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z぀-ヿ一-鿿]+", "_", s).strip("_")[:60]


def download_documents(sess: EjuSession, rec: Dict[str, Any], out_dir: str) -> List[str]:
    """把详情页上的「成績確認書」等文件下载到 out_dir/files/。"""
    saved: List[str] = []
    stem = _safe_name(rec.get("exam") or "exam")
    for label, url in rec.get("documents", []):
        name = "%s_%s" % (stem, _safe_name(label) or "document")
        try:
            p = sess.download(url, os.path.join(out_dir, "files", name))
        except requests.RequestException as e:
            sess._log("下载出错：%s" % e)
            p = None
        if p:
            saved.append(p)
    return saved


def is_score_list_page(soup: BeautifulSoup) -> bool:
    """当前页面是不是「受験結果の一覧」（或本身就是成绩页）。"""
    if is_login_page(soup) or is_system_error(soup):
        return False
    if find_session_navs(soup) or find_pending_rows(soup):
        return True
    txt = page_text(soup)
    return "受験結果" in txt and ("一覧" in txt or "得点" in txt)


def open_score_list(sess: EjuSession, state: Dict[str, Any],
                    pick: bool = False) -> str:
    """
    走到成绩列表页，返回走的是哪条路：
      warm   1 个请求：会话还活着 —— 从当前页面 POST 一次左侧菜单就回到列表页
      login  3 个请求：GET 登录页 → POST 登录 → POST 菜单（冷启动或会话失效）

    实测结论（别再试了）：带着有效 cookie 直接 GET 内页、或者复用上一页的
    datTimeStamp 直接 POST，站点都一律回「システムエラー」——它的每次跳转都要求
    走完整的 POST 导航链。所以「少发请求」的正确做法不是跨进程复用 cookie，
    而是在一个进程里把会话一直养着：抢分数时每轮只花 1 个请求，顺带还防会话超时。
    """
    if (sess.logged_in and state.get("score_menu_url")
            and not is_login_page(sess.soup)):
        try:
            sess.goto_menu(state["score_menu_url"], tag="warm_list")
            if is_score_list_page(sess.soup):
                return "warm"
            sess._log("热会话已失效（拿到的不是成绩列表页），重新登录")
        except requests.HTTPError as e:
            sess._log("热会话跳转失败：%s" % e)
        sess.reset_session()

    sess.reset_session()
    sess.login()
    menu_url = sess.url
    if looks_like_score_page(sess.soup) or is_score_list_page(sess.soup):
        return "login"
    entry = find_score_entry(sess, pick=pick)
    if entry is None:
        raise RuntimeError(
            "没能在登录后的页面上找到成绩入口。请用 `explore` 子命令打印菜单，"
            "或用 `fetch --pick` 手动选择；`--dump` 会保存 HTML 便于排查。"
        )
    sess.click(entry, tag="score_entry")
    if is_system_error(sess.soup):
        raise RuntimeError("进入成绩页时返回系统错误，请重试（会话可能已超时）。")

    # 把学到的地址记下来，下次可以一步直达
    state.update({
        "menu_url": menu_url,
        "score_menu_url": entry.value if entry.kind == "menu" else None,
        "score_list_url": sess.url,
        "score_entry_label": entry.label,
        "learned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    try:
        save_state(state)
    except OSError:
        pass
    return "login"


def capture_detail(sess: EjuSession, nav: Nav, out_dir: Optional[str],
                   tag: str = "detail") -> Dict[str, Any]:
    """
    打开某个回次的详情页并解析。
    顺序刻意如此：先把原始 HTML 落盘 → 再解析 → 最后才下载 PDF，
    这样即使解析出错或 PDF 下载超时，分数原文也已经存在硬盘上了。
    """
    sess.click(nav, tag=tag)
    if is_system_error(sess.soup):
        raise RuntimeError("详情页返回系统错误")

    label = find_exam_label(nav.context) or clean(nav.context)[:40] or "detail"
    if out_dir:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        raw_path = os.path.join(out_dir, "raw", "%s_%s.html" % (stamp, _safe_name(label)))
        try:
            os.makedirs(os.path.dirname(raw_path), exist_ok=True)
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write("<!-- %s -->\n%s" % (sess.url, sess.html))
        except OSError as e:
            sess._log("原始 HTML 落盘失败：%s" % e)
            raw_path = None
    else:
        raw_path = None

    rec = parse_score_page(sess.soup, sess.url)
    if not rec["subjects"]:
        rec["warning"] = "未识别出科目分数，请查看 tables/text 原文。"
    rec["exam"] = (rec["exam"] or find_exam_label(nav.context)
                   or clean(nav.context)[:60] or None)
    if not rec["exam_number"]:
        m = re.search(r"\b([0-9][0-9*]{5,}[0-9*]*)\b", nav.context)
        if m:
            rec["exam_number"] = m.group(1)
    rec["list_row"] = nav.context
    if raw_path:
        rec["raw_html"] = raw_path
    return rec


def pending_record(row: str, url: str) -> Dict[str, Any]:
    return {
        "exam": find_exam_label(row) or row[:40], "exam_number": None,
        "subjects": {}, "details": {}, "info": {}, "documents": [],
        "status": "成績公表前", "url": url, "tables": [], "text": row,
    }


def fetch_scores(sess: EjuSession, pick: bool = False,
                 out_dir: Optional[str] = None) -> Dict[str, Any]:
    state = load_state()
    how = open_score_list(sess, state, pick=pick)
    print("→ 已到成绩页（入口方式：%s）" % how)

    menu_entries = [
        "%s=%s %s" % (n.kind, n.value, n.label[:40])
        for n in collect_navs(sess.soup) if not n.blocked
    ]
    results: List[Dict[str, Any]] = []
    notes: List[str] = ["入口方式：%s" % how]

    list_url = state.get("score_menu_url")
    session_navs = find_session_navs(sess.soup)
    for row in find_pending_rows(sess.soup):
        rec = pending_record(row, sess.url)
        notes.append("尚未公布成绩（列表里没有详情按钮）：%s" % rec["exam"])
        results.append(rec)

    if session_navs:
        print("→ 发现 %d 个已公布回次" % len(session_navs))
        for i, nav in enumerate(session_navs, 1):
            if i > 1:
                # 回到列表页（moveUserMenu 可从任意页面跳转）
                if list_url:
                    sess.goto_menu(list_url, tag="back_to_list")
                elif state.get("score_list_url"):
                    sess.get(state["score_list_url"], tag="back_to_list")
                if not is_score_list_page(sess.soup):
                    notes.append("返回回次列表失败，剩余回次未抓取。")
                    break
                nav = next(
                    (n for n in find_session_navs(sess.soup) if n.key == nav.key), nav)
            print("  · %s" % (find_exam_label(nav.context)
                              or nav.context[:50] or nav.label or nav.value))
            try:
                rec = capture_detail(sess, nav, out_dir, tag="detail_%02d" % i)
            except (RuntimeError, requests.RequestException) as e:
                notes.append("回次 %r 打开失败：%s" % (nav.label, e))
                continue
            if out_dir:
                rec["files"] = download_documents(sess, rec, out_dir)
            results.append(rec)
    elif looks_like_score_page(sess.soup):
        rec = parse_score_page(sess.soup, sess.url)
        if out_dir:
            rec["files"] = download_documents(sess, rec, out_dir)
        results.append(rec)
    elif not results:
        results.append(parse_score_page(sess.soup, sess.url))
        notes.append("成绩页结构未识别，已保存整页文本与表格原文。")

    return {
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mypage_id": sess.id,
        "source": LOGIN_URL,
        "records": results,
        "notes": notes,
        "menu_entries": menu_entries,
    }


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def _atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """写临时文件再 rename —— 抢分数时被 Ctrl-C 也不会留下半截文件。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def write_outputs(data: Dict[str, Any], out_dir: str) -> Tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    payload = {k: v for k, v in data.items() if not k.startswith("_")}

    json_path = os.path.join(out_dir, "eju_scores.json")
    _atomic_write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2))

    # 明细表：一行一个科目，带得点範囲和全体平均分
    csv_path = os.path.join(out_dir, "eju_scores.csv")
    buf = io.StringIO()
    if True:
        f = buf
        w = csv.writer(f)
        w.writerow(["回次", "試験日", "受験番号", "科目", "得点", "得点範囲", "平均点"])
        for rec in payload["records"]:
            exam = rec.get("exam") or ""
            date = (rec.get("info") or {}).get("試験日", "")
            no = rec.get("exam_number") or ""
            details = rec.get("details") or {}
            if details:
                for name, d in details.items():
                    w.writerow([exam, date, no, name, d.get("得点", ""),
                                d.get("得点範囲", ""), d.get("平均点", "")])
            elif rec.get("subjects"):
                for name, v in rec["subjects"].items():
                    w.writerow([exam, date, no, name, v, "", ""])
            else:
                w.writerow([exam, date, no, rec.get("status") or "（无成绩）", "", "", ""])

    _atomic_write_text(csv_path, buf.getvalue(), encoding="utf-8-sig")

    # 横表：一行一个回次，方便几次考试横向对比
    keys: List[str] = []
    for rec in payload["records"]:
        for k in rec.get("subjects", {}):
            if k not in keys:
                keys.append(k)
    wide_path = os.path.join(out_dir, "eju_scores_wide.csv")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["回次", "受験番号"] + keys)
    for rec in payload["records"]:
        subs = rec.get("subjects", {})
        w.writerow([rec.get("exam") or "", rec.get("exam_number") or ""]
                   + [subs.get(k, "") for k in keys])
    _atomic_write_text(wide_path, buf.getvalue(), encoding="utf-8-sig")

    # 原始文本留档，解析失败也不会丢信息
    parts: List[str] = []
    for rec in payload["records"]:
        parts.append("=" * 70)
        parts.append("%s  (%s)" % (rec.get("exam") or "?", rec.get("url")))
        parts.append("-" * 70)
        for rows in rec.get("tables", []):
            for row in rows:
                parts.append(" | ".join(row))
            parts.append("-" * 70)
        parts.append((rec.get("text") or "") + "\n")
    _atomic_write_text(os.path.join(out_dir, "eju_scores_raw.txt"), "\n".join(parts))

    return json_path, csv_path


def merge_and_write(out_dir: str, new_records: List[Dict[str, Any]],
                    mypage_id: str, notes: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    把新抓到的记录并进已有的 eju_scores.json 再整体重写。
    抢分数时是一条一条落地的，不能让新一轮把上一轮抓到的成绩冲掉；
    也不能让「成績公表前」的占位覆盖掉已经抓到分数的记录。
    """
    old = _read_json(os.path.join(out_dir, "eju_scores.json"))
    merged: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for rec in (old.get("records") or []) + list(new_records):
        key = rec.get("exam") or rec.get("url") or str(len(merged))
        prev = merged.get(key)
        if prev and prev.get("subjects") and not rec.get("subjects"):
            continue  # 别用占位覆盖真成绩
        merged[key] = rec
    data = {
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mypage_id": mypage_id,
        "source": LOGIN_URL,
        "records": list(merged.values()),
        "notes": notes or [],
    }
    write_outputs(data, out_dir)
    return data


INFO_SHOW = ("試験日", "受験地", "申込区分", "氏名（アルファベット）", "予約種別")


def _w(s: str) -> int:
    """按终端显示宽度算（CJK 占两格），用于对齐。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - _w(s))
    return gap + s if right else s + gap


def print_summary(data: Dict[str, Any]) -> None:
    recs = data["records"]
    print("\n抓到 %d 条记录：" % len(recs))
    for rec in recs:
        head = rec.get("exam") or "（回次未识别）"
        if rec.get("exam_number"):
            head += "    受験番号 %s" % rec["exam_number"]
        if rec.get("status"):
            head += "    [%s]" % rec["status"]
        print("\n■ %s" % head)

        info = rec.get("info") or {}
        shown = ["%s %s" % (k, info[k]) for k in INFO_SHOW if info.get(k)]
        if shown:
            print("   " + " ｜ ".join(shown))

        details = rec.get("details") or {}
        subs = rec.get("subjects") or {}
        if details:
            width = max(_w(k) for k in details)
            print("   %s %s %s %s" % (_pad("科目", width), _pad("得点", 6, True),
                                      _pad("得点範囲", 10, True), _pad("平均点", 8, True)))
            for name, d in details.items():
                print("   %s %s %s %s" % (
                    _pad(name, width), _pad(d.get("得点", "-"), 6, True),
                    _pad(d.get("得点範囲", "-"), 10, True),
                    _pad(d.get("平均点", "-"), 8, True)))
        elif subs:
            width = max(_w(k) for k in subs)
            for k, v in subs.items():
                print("   %s : %s" % (_pad(k, width), v))
        elif not rec.get("status"):
            print("   （未自动识别科目分数，见 eju_scores_raw.txt）")

        for p in rec.get("files") or []:
            print("   已下载：%s" % p)
    for n in data.get("notes", []):
        print("\n注意：%s" % n)


# --------------------------------------------------------------------------- #
# 抢分数模式（watch）
# --------------------------------------------------------------------------- #

MIN_INTERVAL = 3.0  # 硬下限：再快就只是给站点添堵，还容易被 WAF 拉黑


def notify(title: str, message: str, say: bool = False) -> None:
    """macOS 通知中心 + 终端响铃；失败就静默忽略。"""
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass
    if platform.system() != "Darwin":
        return
    if _which("osascript"):
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')
        script = 'display notification "%s" with title "%s" sound name "Glass"' % (
            esc(message[:300]), esc(title[:80]))
        subprocess.run(["osascript", "-e", script], capture_output=True)
    if say and _which("say"):
        subprocess.run(["say", message[:200]], capture_output=True)


class HealthProbe:
    """
    轻量探针：只对登录页做不带会话的 GET，用来判断「站点是不是活了」。
    它跟抓取用的会话完全隔离（独立 cookie jar），所以不会打扰一次性令牌，
    也不会造成同一账号多会话。站点一恢复就立刻唤醒主循环，不用等下一个轮询周期。
    """

    def __init__(self, interval: float = 8.0, verbose: bool = False):
        self.interval = max(4.0, interval)
        self.verbose = verbose
        self.alive = threading.Event()     # 站点可用
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_code: Optional[int] = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        while not self._stop.is_set():
            try:
                r = s.get(LOGIN_URL, timeout=20)
                self.last_code = r.status_code
                if r.status_code == 200 and "メンテナンス" not in r.text:
                    self.alive.set()
                else:
                    self.alive.clear()
            except requests.RequestException:
                self.last_code = None
                self.alive.clear()
            self._stop.wait(self.interval)


def _sleep_until(seconds: float, wake: Optional[threading.Event] = None,
                 floor: float = MIN_INTERVAL) -> None:
    """
    等待 seconds 秒，但探针一报告站点恢复就提前醒。
    floor 保证每轮至少间隔这么久 —— 否则探针刚好已经置位时会退化成空转刷请求。
    """
    if wake is None:
        time.sleep(seconds)
        return
    t0 = time.time()
    wake.wait(timeout=seconds)
    left = floor - (time.time() - t0)
    if left > 0:
        time.sleep(left)


def _parse_start_at(spec: str) -> datetime:
    """'23:00' / '2026-08-05 14:00' → datetime（只给 HH:MM 时按最近的将来算）。"""
    now = datetime.now()
    for fmt in ("%Y-%m-%d %H:%M", "%m-%d %H:%M", "%H:%M"):
        try:
            t = datetime.strptime(spec, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            t = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if t <= now:
                t += timedelta(days=1)
        elif fmt == "%m-%d %H:%M":
            t = t.replace(year=now.year)
        return t
    raise ValueError("时间格式看不懂：%r（用 23:00 或 2026-08-05 14:00）" % spec)


def _backoff(errors: int, burst: int, fast: float) -> float:
    """前 burst 次失败都按快速间隔硬顶，之后指数退避（上限 120 秒）。"""
    if errors <= burst:
        return fast
    return min(fast * 2 ** min(errors - burst, 4), 120.0)


class WatchLog:
    def __init__(self, path: Optional[str]):
        self.path = path
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def __call__(self, line: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line))
        except OSError:
            pass
        del stamp


def watch_scores(sess: EjuSession, out_dir: str, targets: Optional[List[str]] = None,
                 interval: float = 20.0, fast_interval: float = 5.0, burst: int = 3,
                 hours: float = 12.0, include_existing: bool = False,
                 keep_going: bool = False, do_notify: bool = True, say: bool = False,
                 log_path: Optional[str] = None) -> Dict[str, Any]:
    """
    分数公布日的抢分数循环：
      · 站点被挤爆（5xx / 超时 / 系统错误）→ 快速重试 + 探针盯着，一恢复立刻上
      · 每一轮尽量只花 1 个请求（cookie 直连成绩列表页）
      · 分数一到手立刻按「原始 HTML → JSON/CSV → 屏幕 → 通知 → PDF」的顺序落地
    """
    interval = max(MIN_INTERVAL, interval)
    fast_interval = max(MIN_INTERVAL, fast_interval)
    log = WatchLog(log_path)
    deadline = time.time() + hours * 3600 if hours > 0 else float("inf")
    probe = HealthProbe(interval=min(10.0, max(4.0, fast_interval)), verbose=sess.verbose)

    wanted: Optional[List[str]] = list(targets) if targets else None
    captured: Dict[str, Dict[str, Any]] = {}
    notes: List[str] = []
    attempts = 0
    consecutive_errors = 0
    state = load_state()
    probing = False

    if hours <= 0:
        limit = "不限"
    elif hours < 1:
        limit = "%.0f 分钟" % (hours * 60)
    else:
        limit = "%.1f 小时" % hours
    # 重定向到日志文件时就不刷那行 \r 状态了（watch.log 里每轮都有记录）
    tty = sys.stdout.isatty()

    def status(msg: str) -> None:
        if tty:
            print("\r" + msg + "   ", end="", flush=True)

    print("开始盯成绩：间隔 %.0f 秒（拥堵时 %.0f 秒），最长 %s。Ctrl-C 可随时停。"
          % (interval, fast_interval, limit))
    log("watch start interval=%.0f fast=%.0f hours=%s targets=%s"
        % (interval, fast_interval, hours, targets))

    try:
        while time.time() < deadline:
            attempts += 1
            wait = interval
            wake_on_recovery = False
            try:
                how = open_score_list(sess, state)
                consecutive_errors = 0
                if probing:
                    probe.stop()
                    probing = False

                navs = find_session_navs(sess.soup)
                pending = find_pending_rows(sess.soup)
                available = {}
                for n in navs:
                    key = find_exam_label(n.context) or clean(n.context)[:60]
                    available[key] = n

                if wanted is None:
                    # 第一轮定目标：默认盯「成績公表前」的回次
                    wanted = [find_exam_label(r) or r[:40] for r in pending]
                    if include_existing or not wanted:
                        wanted += [k for k in available if k not in wanted]
                    print("盯这些回次：%s" % ("、".join(wanted) if wanted else "（列表是空的）"))
                    log("targets=%s available=%s" % (wanted, list(available)))
                    # 未公布的先记一笔占位，随时能看到状态
                    if pending:
                        merge_and_write(out_dir, [pending_record(r, sess.url)
                                                  for r in pending], sess.id)

                hits = [(k, n) for k, n in available.items()
                        if k in wanted and k not in captured]
                if hits:
                    for key, nav in hits:
                        print("\n🎉 %s 的成绩出来了，正在抓……" % key)
                        log("HIT %s" % key)
                        rec = capture_detail(sess, nav, out_dir, tag="watch_detail")
                        captured[key] = rec
                        data = merge_and_write(out_dir, [rec], sess.id, notes)
                        print_summary({"records": [rec], "notes": []})
                        print("已保存到 %s" % os.path.join(out_dir, "eju_scores.json"))
                        if do_notify:
                            subs = rec.get("subjects") or {}
                            msg = "、".join("%s %s" % (k.split()[-1], v)
                                           for k, v in list(subs.items())[:4]) or "已抓到成绩"
                            notify("EJU 成绩已出：%s" % key, msg, say=say)
                        # PDF 放最后：它慢，而且失败也不影响分数已经存好
                        try:
                            rec["files"] = download_documents(sess, rec, out_dir)
                            if rec["files"]:
                                merge_and_write(out_dir, [rec], sess.id, notes)
                                print("成績確認書已下载：%s" % "、".join(rec["files"]))
                        except requests.RequestException as e:
                            print("（PDF 稍后再下，现在下载失败：%s）" % e)
                        del data
                        # 多个回次时要回列表页
                        if len(hits) > 1 and state.get("score_menu_url"):
                            sess.goto_menu(state["score_menu_url"], tag="back_to_list")

                    if not keep_going and all(k in captured for k in wanted):
                        print("\n目标回次都抓到了，收工。")
                        log("done")
                        break
                else:
                    still = [k for k in (wanted or []) if k not in captured]
                    state_txt = "等待中：%s" % ("、".join(still) if still else "（无目标）")
                    status("[%d 次尝试 %s] %s，%.0f 秒后再试"
                           % (attempts, datetime.now().strftime("%H:%M:%S"), state_txt, wait))
                    log("attempt=%d how=%s pending=%s" % (attempts, how, still))

                wake_on_recovery = False

            except MaintenanceError as e:
                wait = max(60.0, fast_interval)
                wake_on_recovery = True
                status("[%d] 站点维护中（%s），%.0f 秒后重试" % (attempts, e, wait))
                log("maintenance %s" % e)
                if not probing:
                    probe.start()
                    probing = True
            except LoginRejected as e:
                # 账号密码有问题：绝不能循环重试，会把账号锁死
                log("ABORT login rejected: %s" % e)
                raise
            except requests.RequestException as e:
                # 网络层面连不上 / 5xx —— 典型的「被挤爆」，探针盯着一恢复就上
                consecutive_errors += 1
                wait = _backoff(consecutive_errors, burst, fast_interval)
                wake_on_recovery = True
                short = str(e).split("\n")[0][:70]
                status("[%d 次尝试 %s] 连不上（%s），%.0f 秒后重试"
                       % (attempts, datetime.now().strftime("%H:%M:%S"), short, wait))
                log("neterror=%s wait=%.0f" % (short, wait))
                if consecutive_errors >= 2 and not probing:
                    probe.start()
                    probing = True
            except RuntimeError as e:
                # 站点是活的，但会话被挤掉/超时（システムエラー）——扔掉 cookie 重登
                consecutive_errors += 1
                sess.reset_session()
                wait = _backoff(consecutive_errors, burst, fast_interval)
                wake_on_recovery = False
                short = str(e).split("\n")[0][:70]
                status("[%d 次尝试 %s] 会话被挤掉（%s），%.0f 秒后重登"
                       % (attempts, datetime.now().strftime("%H:%M:%S"), short, wait))
                log("session error=%s wait=%.0f" % (short, wait))
            except Exception as e:  # 兜底：盯了一整天不能被一个意外异常打断
                consecutive_errors += 1
                wait = _backoff(consecutive_errors, burst, max(fast_interval, 10.0))
                short = "%s: %s" % (type(e).__name__, str(e).split("\n")[0][:60])
                status("[%d 次尝试 %s] 出了个意外（%s），%.0f 秒后重试"
                       % (attempts, datetime.now().strftime("%H:%M:%S"), short, wait))
                log("unexpected=%s wait=%.0f" % (short, wait))

            # 加抖动：避免和别人的脚本在整分钟同时冲
            jitter = random.uniform(0, min(3.0, wait * 0.25))
            if probing and probe.alive.is_set():
                probe.alive.clear()
            _sleep_until(wait + jitter,
                         probe.alive if (probing and wake_on_recovery) else None)
        else:
            print("\n到时间了（%s），先停。抓到 %d 个回次。" % (limit, len(captured)))
            log("deadline reached captured=%d" % len(captured))
    except KeyboardInterrupt:
        print("\n手动停止。已抓到 %d 个回次，结果都在 %s。" % (len(captured), out_dir))
        log("interrupted captured=%d" % len(captured))
    finally:
        probe.stop()

    return {
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mypage_id": sess.id,
        "source": LOGIN_URL,
        "records": list(captured.values()),
        "notes": notes,
        "attempts": attempts,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def cmd_setup(args: argparse.Namespace) -> int:
    print("保存 EJU オンライン 登录凭据（只保存在本机）")
    mypage_id = input("MyPageID: ").strip()
    if not mypage_id:
        print("已取消。")
        return 1
    pw = getpass.getpass("密码（输入时不显示）: ")
    if not pw:
        print("已取消。")
        return 1
    where = save_credentials(mypage_id, pw)
    print("已保存到：%s" % where)
    if args.verify:
        sess = EjuSession(mypage_id, pw, verbose=args.verbose)
        try:
            sess.login()
            print("登录验证成功。")
        finally:
            sess.logout()
    return 0


def cmd_forget(_: argparse.Namespace) -> int:
    forget_credentials()
    print("已删除本机保存的 EJU 凭据。")
    return 0


def _require_creds() -> Tuple[str, str]:
    creds = load_credentials()
    if not creds:
        sys.exit("还没有保存凭据。先运行：python3 eju_getter.py setup"
                 "（或设置环境变量 EJU_ID / EJU_PASSWORD）")
    return creds


def cmd_explore(args: argparse.Namespace) -> int:
    mypage_id, pw = _require_creds()
    sess = EjuSession(mypage_id, pw, verbose=args.verbose,
                      dump_dir=args.dump_dir if args.dump else None)
    try:
        sess.login()
        print("登录成功：%s\n" % sess.url)
        navs = collect_navs(sess.soup)
        print("页面可点击项（★=脚本认为和成绩有关，✗=已拉黑不会点）：")
        for n in sorted(navs, key=lambda x: x.score(), reverse=True):
            mark = "★" if n.score() >= 10 else ("✗" if n.blocked else " ")
            print(" %s [%s=%s] %s" % (mark, n.kind, n.value, n.label[:60] or "(无文字)"))
        if args.dump:
            print("\n原始 HTML 已存到：%s" % args.dump_dir)
    finally:
        sess.logout()
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    mypage_id, pw = _require_creds()
    sess = EjuSession(mypage_id, pw, verbose=args.verbose,
                      dump_dir=args.dump_dir if args.dump else None)
    try:
        data = fetch_scores(sess, pick=args.pick, out_dir=args.out)
    finally:
        sess.logout()

    json_path, csv_path = write_outputs(data, args.out)
    print_summary(data)
    print("\n已写入：\n  %s\n  %s\n  %s\n  %s" % (
        json_path, csv_path,
        os.path.join(args.out, "eju_scores_wide.csv"),
        os.path.join(args.out, "eju_scores_raw.txt")))
    if args.dump:
        print("  %s/ （逐页 HTML）" % args.dump_dir)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    mypage_id, pw = _require_creds()
    if args.start_at:
        target = _parse_start_at(args.start_at)
        gap = (target - datetime.now()).total_seconds()
        if gap > 0:
            print("等到 %s 再开始（还有 %.0f 分钟）……"
                  % (target.strftime("%m-%d %H:%M"), gap / 60))
            try:
                time.sleep(gap)
            except KeyboardInterrupt:
                print("\n已取消。")
                return 130

    sess = EjuSession(mypage_id, pw, verbose=args.verbose,
                      dump_dir=args.dump_dir if args.dump else None)
    try:
        data = watch_scores(
            sess, out_dir=args.out,
            targets=args.exam or None,
            interval=args.interval, fast_interval=args.fast_interval,
            burst=args.burst, hours=args.hours,
            include_existing=args.include_existing, keep_going=args.keep_going,
            do_notify=not args.no_notify, say=args.say,
            log_path=args.log or os.path.join(args.out, "watch.log"),
        )
    finally:
        sess.logout()
    return 0 if data["records"] else 4


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="存一次 EJU 账号密码，之后一条命令抓取历次 EJU 成绩详情。")
    p.add_argument("-v", "--verbose", action="store_true", help="打印调试信息")
    sub = p.add_subparsers(dest="cmd")

    ps = sub.add_parser("setup", help="保存 MyPageID / 密码")
    ps.add_argument("--verify", action="store_true", help="保存后立刻试登录一次")
    ps.set_defaults(func=cmd_setup)

    sub.add_parser("forget", help="删除已保存的凭据").set_defaults(func=cmd_forget)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--dump", action="store_true", help="保存每一页原始 HTML")
        sp.add_argument("--dump-dir", default="out/pages", help="HTML 保存目录")

    pe = sub.add_parser("explore", help="登录后打印菜单入口（排查用）")
    add_common(pe)
    pe.set_defaults(func=cmd_explore)

    pf = sub.add_parser("fetch", help="抓取成绩")
    add_common(pf)
    pf.add_argument("--out", default="out", help="结果输出目录（默认 out/）")
    pf.add_argument("--pick", action="store_true", help="手动选择成绩入口")
    pf.set_defaults(func=cmd_fetch)

    pw_ = sub.add_parser(
        "watch", help="盯着成绩公布：一直重试，分数一出来立刻保存并通知",
        description="分数公布日用这个。站点被挤爆也会不停重试，"
                    "抓到分数立刻落盘 + 弹通知。Ctrl-C 可随时停。")
    add_common(pw_)
    pw_.add_argument("--out", default="out", help="结果输出目录（默认 out/）")
    pw_.add_argument("--exam", action="append", metavar="回次",
                     help="只盯指定回次，如 --exam 2026年度第1回（可重复）")
    pw_.add_argument("--interval", type=float, default=20.0,
                     help="正常轮询间隔秒数（默认 20，下限 3）")
    pw_.add_argument("--fast-interval", type=float, default=5.0,
                     help="站点拥堵/报错时的重试间隔（默认 5）")
    pw_.add_argument("--burst", type=int, default=3,
                     help="连续失败几次之内都按快速间隔重试（默认 3，之后指数退避）")
    pw_.add_argument("--hours", type=float, default=12.0,
                     help="最长盯多少小时，0 = 不限（默认 12）")
    pw_.add_argument("--start-at", metavar="HH:MM",
                     help="到点才开始，如 --start-at 14:00（公布时间已知时用）")
    pw_.add_argument("--include-existing", action="store_true",
                     help="连已经公布的回次也一起抓（默认只盯「成績公表前」的）")
    pw_.add_argument("--keep-going", action="store_true",
                     help="抓到之后继续盯（默认目标都抓到就退出）")
    pw_.add_argument("--no-notify", action="store_true", help="不弹系统通知")
    pw_.add_argument("--say", action="store_true", help="抓到时用语音念出来")
    pw_.add_argument("--log", help="日志文件（默认 <out>/watch.log）")
    pw_.set_defaults(func=cmd_watch)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        # 默认行为：没存凭据就 setup，否则 fetch
        args = p.parse_args((["setup"] if not load_credentials() else ["fetch"]))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except MaintenanceError as e:
        print("EJU オンライン 正在维护，现在连不上。维护时间：%s\n"
              "请在维护结束后再跑一次。" % e, file=sys.stderr)
        return 3
    except requests.RequestException as e:
        print("网络请求失败：%s" % e, file=sys.stderr)
        return 2
    except RuntimeError as e:
        print("错误：%s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
