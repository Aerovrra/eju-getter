# eju-getter

命令行抓取自己的 **EJU（日本留学試験）成绩**：存一次账号密码，之后一条命令就能把历次成绩连同官方 `成績確認書` PDF 一起存到本地。

成绩公布当天还有一个专门的 `watch` 模式——挂在后台一直盯着，分数一公布立刻抓下来、存盘、弹通知。

```
■ 2025年度第2回    受験番号 99*0000*000001
   試験日 2025/11/09 ｜ 氏名（アルファベット） TEST TARO
   科目                  得点   得点範囲   平均点
   日本語 聴解・聴読解     74   0 ～ 200      100
   日本語 読解            126   0 ～ 200    135.4
   日本語 合計            200   0 ～ 400    235.4
   日本語 記述             35    0 ～ 50     34.3
   理科 物理               55   0 ～ 100     53.7
   総合科目               ---   0 ～ 200    122.7
   数学 コース1           122   0 ～ 200     98.3
```

目标站点：[EJU オンライン（JASSO）](https://eju-online.jasso.go.jp/src/CMNLOGIN010.php)

> [!IMPORTANT]
> 这是给**考生查自己成绩**用的个人工具。它只会读你自己账号下的页面，不涉及任何他人数据，也不绕过任何认证——你输入的就是你平时登录用的那个账号密码。

---

## 目录

- [它能做什么](#它能做什么)
- [开始使用](#开始使用)
- [命令一览](#命令一览)
- [成绩公布日怎么用](#成绩公布日怎么用)
- [输出文件说明](#输出文件说明)
- [账号密码存在哪](#账号密码存在哪)
- [安全约束](#安全约束)
- [常见问题](#常见问题)
- [重要限制](#重要限制)
- [实现细节](#实现细节)
- [开发](#开发)

---

## 它能做什么

| | |
| --- | --- |
| 📊 **抓成绩** | 自动登录 → 找到成绩页 → 抓取所有能看的回次，含每科 `得点 / 得点範囲 / 全体平均点` |
| 📄 **下载 PDF** | 每个已公布回次的官方 `成績確認書` 一并存下来 |
| 🎯 **盯公布** | `watch` 模式挂后台，分数一出来立刻抓 + 存盘 + 弹系统通知 |
| 💾 **多种格式** | JSON（完整结构）、明细 CSV、横向对比 CSV、全文留档 TXT |
| 🔐 **凭据不落项目** | macOS 存系统钥匙串；其他系统存 `~/.config/`，权限 600 |
| 🛡️ **只读** | 报名 / 缴费 / 修改类按钮硬编码拉黑，永远不会被点到 |

---

## 开始使用

### 环境要求

- **Python 3.9+**
- **macOS / Linux**（Windows 见下方说明）
- 一个能登录 EJU オンライン 的 **MyPageID + 密码**

> **macOS 用户额外能用到**：密码存进系统钥匙串、抓到分数弹通知中心、`--say` 语音播报。
> 其他系统一切核心功能都正常，只是密码改存文件、通知降级成终端响铃。

### 安装

```bash
git clone https://github.com/Aerovrra/eju-getter.git
cd eju-getter
./setup.sh
```

`setup.sh` 会做三件事：建 `.venv` 虚拟环境 → 装依赖 → 提示你输入 MyPageID 和密码。密码输入时不显示，存好之后就不用再输了。

想顺便验证密码对不对：

```bash
./setup.sh --verify        # 存完立刻试登录一次
```

<details>
<summary><b>不想用 setup.sh？手动安装</b></summary>

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python eju_getter.py setup
```

依赖只有三个：`requests`、`beautifulsoup4`、`lxml`。

</details>

<details>
<summary><b>Windows 用户</b></summary>

`.sh` 脚本在 Windows 上跑不了，但主脚本本身是跨平台的：

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python eju_getter.py setup
.venv\Scripts\python eju_getter.py fetch
```

密码会存到 `~/.config/eju-getter/credentials.json`（钥匙串是 macOS 专有）。
系统通知和语音播报不可用，会降级成终端响铃。
或者用 WSL / Git Bash，就能直接跑 `./eju.sh`。

</details>

### 抓一次成绩

```bash
./eju.sh fetch
```

结果写进 `out/`，终端同时打印一份带「本人得点 / 得点範囲 / 全体平均点」的对照表。

就这样，日常用只需要这一条命令。

---

## 命令一览

所有命令都通过 `./eju.sh` 调用，它会自动用 `.venv` 里的 Python。

```bash
./eju.sh fetch                 # 抓取所有能看的回次 → 写入 out/
./eju.sh watch                 # 【公布日用这个】盯着，分数一出来立刻抓
./eju.sh setup                 # 保存 / 更新账号密码
./eju.sh forget                # 删除本机保存的凭据
./eju.sh explore               # 只登录并列出页面上的所有入口（排查用）
```

**常用选项**

```bash
./eju.sh fetch --out ~/eju     # 换输出目录（默认 out/）
./eju.sh fetch --dump          # 同时保存每页原始 HTML（默认存 out/pages/）
./eju.sh fetch --dump-dir tmp  # 换 HTML 保存目录
./eju.sh fetch --pick          # 自动识别成绩入口失败时，列出菜单让你手选
./eju.sh -v fetch              # 打印调试信息（-v / --verbose 要写在子命令前面）
```

`--dump` / `--dump-dir` 对 `fetch`、`watch`、`explore` 都可用。

<details>
<summary><b>watch 的全部选项</b></summary>

```bash
./eju.sh watch --start-at 14:00        # 到 14:00 才开始（公布时间已知时用）
./eju.sh watch --exam 2026年度第1回     # 只盯指定回次，可重复写多个
./eju.sh watch --interval 10           # 轮询间隔秒数（默认 20，硬下限 3）
./eju.sh watch --fast-interval 5       # 拥堵/报错时的重试间隔（默认 5）
./eju.sh watch --burst 3               # 连续失败几次内按快速间隔重试（默认 3，之后指数退避）
./eju.sh watch --hours 0               # 最长盯多久，0 = 不限（默认 12 小时）
./eju.sh watch --include-existing      # 连已公布的回次也一起抓（默认只盯「成績公表前」的）
./eju.sh watch --keep-going            # 抓到后继续盯（默认目标全抓到就退出）
./eju.sh watch --no-notify             # 不弹系统通知
./eju.sh watch --say                   # 抓到时用语音念出来（macOS）
./eju.sh watch --log ~/eju-watch.log   # 换日志路径（默认 <out>/watch.log）
```

`--interval` 有 3 秒硬下限。再快只是给站点添堵，还容易被前置 WAF 拉黑。

</details>

<details>
<summary><b>退出码</b></summary>

写脚本判断结果时能用上：

| 码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 一般错误（凭据被拒、解析失败等） |
| `2` | 网络请求失败 |
| `3` | 站点在计划维护中 |
| `4` | 跑通了但一条记录都没抓到 |
| `130` | 被 Ctrl-C 中断 |

</details>

---

## 成绩公布日怎么用

公布那一刻站点会被全国考生挤爆，所以策略是：**提前挂上，把会话养热，每轮只花 1 个请求。**

### 最省事的办法：`rush.sh`

一条命令把「等站点可用 → 预热登录 → 到点开抢」全串起来，挂后台就不用管了：

```bash
# 假设 0:00 公布，就让它 23:45 开始盯
nohup caffeinate -i ./rush.sh 23:45 > /dev/null 2>&1 &

tail -f out/rush.log        # 随时看进度
./rush.sh --stop            # 停掉
```

它按顺序做这些事：

1. 每 2 分钟探一次站点，等维护窗口 / 临时 503 过去
2. 站点一活就跑一次 `fetch` 预热——确认凭据没问题、学好站点结构、顺手把已公布的回次存档
3. 到点交给 `watch`。**刻意比公布时间早十几分钟**，这样出分那一刻会话已经是热的
4. `caffeinate -i` 防止 Mac 空闲休眠
5. PID 文件防止重复挂

可用环境变量微调：

```bash
INTERVAL=12 FAST=4 HOURS=8 ./rush.sh 23:45
```

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `INTERVAL` | `12` | 正常轮询间隔（秒） |
| `FAST` | `4` | 拥堵时的重试间隔 |
| `HOURS` | `8` | 最长盯多久 |
| `PROBE_MAX` | `60` | 等站点结束维护最多探测几次（每次隔 120 秒） |

> `rush.sh` 依赖 `caffeinate`（macOS 自带）。Linux 上直接用 `nohup ./eju.sh watch --start-at 23:45 &` 即可。

### 或者直接用 `watch`

```bash
./eju.sh watch                       # 盯着所有「成績公表前」的回次
./eju.sh watch --start-at 00:00      # 公布时间已知
```

<details>
<summary><b>watch 内部做了什么</b></summary>

1. **默认目标 = 现在还写着「成績公表前」的回次**。第一轮先把这些回次记一条占位进 `eju_scores.json`，然后循环等它们冒出「詳細へ」按钮。

2. **每轮只花 1 个请求**：一条长会话养着，每轮从当前页面 POST 一次左侧菜单回到成绩列表页（实测 ~1.4s）。只有冷启动或会话被挤掉时才重新登录（3 个请求）。公布瞬间站点被挤爆时，请求数越少越容易挤进去。

3. **分数一到手立刻落地，顺序是刻意设计的**：
   原始 HTML 落盘 → 写 JSON/CSV（原子写，Ctrl-C 也不会留半截文件）→ 打印到屏幕 → 弹系统通知 → **最后**才下载 PDF。PDF 又大又慢，公布日十有八九超时，所以绝不让它挡在分数落地前面。

4. **拥堵时的重试策略**：
   - 5xx / 超时 / 连不上 → 前 3 次按 `--fast-interval` 硬顶，之后指数退避到最多 120 秒
   - `システムエラー`（会话被挤掉）→ 扔掉会话，下一轮重新登录
   - 整站维护页 → 认出来并每 60 秒重试，不瞎冲
   - 每轮加随机抖动，避免和别人的脚本卡在整分钟同时冲
   - **账号密码被拒绝 → 立刻停止。** 连续错误登录会锁账号，这种情况绝不重试

5. **一个不带会话的轻量探针**：站点挂掉后另开一个线程（独立 cookie jar）低频 GET 登录页，判断站点活了没。一恢复就立刻唤醒主循环，不用等下一个轮询周期。

6. 全程写 `out/watch.log`，挂一整天回来能看清每一轮发生了什么。

</details>

<details>
<summary><b>为什么不用多线程并发抓？</b></summary>

对这个站点是**负优化**：

- 同一账号并发多会话会直接触发 `システムエラー`——登录页自己写着「同一PCで、複数のページ・タブからログインをした場合」会出错。开 N 个登录只会互相踢掉。
- 每个页面的 `datTimeStamp` 是一次性令牌，两个线程共用一个会话会互相把令牌作废。
- 高频并发大概率被前置 WAF（站点用了 F5 + CloudFront）判为攻击拉黑，那就彻底抓不到了。

真正有效的是**降低每次尝试的成本**（热会话 = 1 个请求）+ **恢复瞬间零延迟响应**（探针）+ **失败立刻快速重试**（burst）。

</details>

---

## 输出文件说明

默认都在 `out/`（可用 `--out` 改）：

| 文件 | 内容 |
| --- | --- |
| `eju_scores.json` | 完整结构化结果：回次、受験番号、每科 `得点/得点範囲/平均点`、基本信息、原始表格 |
| `eju_scores.csv` | 明细表，一行一个科目：`回次, 試験日, 受験番号, 科目, 得点, 得点範囲, 平均点` |
| `eju_scores_wide.csv` | 横表，一行一个回次、一列一个科目，方便多次考试横向对比 |
| `eju_scores_raw.txt` | 成绩页表格与正文全文留档，万一自动解析漏了也不丢信息 |
| `files/*.pdf` | 官方 `成績確認書` PDF（每个已公布回次一份） |
| `raw/*.html` | 抓到成绩那一刻的详情页原始 HTML（先落盘再解析，解析失败也不丢分数） |
| `watch.log` | `watch` 模式的逐轮日志 |
| `rush.log` | `rush.sh` 的全流程日志 |

> [!WARNING]
> **`out/` 里全是你的个人信息。** `eju_scores.json` 和 `raw/*.html` 包含姓名、生年月日、性别、国籍、受験番号和真实分数，PDF 是官方成绩单原件。
>
> 仓库的 `.gitignore` 已经把 `out/` 挡住了，但如果你换了输出目录，记得**别让它进版本控制、别贴到网上**。

---

## 账号密码存在哪

**永远不在项目目录里，也不在代码里。** 三种来源，按优先级：

| 优先级 | 方式 | 位置 |
| --- | --- | --- |
| 1 | 环境变量 | `EJU_ID` / `EJU_PASSWORD`（完全不落盘） |
| 2 | macOS 钥匙串 | 服务名 `eju-getter`，用系统 `security` 命令读写 |
| 3 | 文件 | `~/.config/eju-getter/credentials.json`，权限 `600` |

临时用一次、什么都不留：

```bash
EJU_ID=xxxxx EJU_PASSWORD=yyyyy ./eju.sh fetch
```

另外会在 `~/.config/eju-getter/state.json`（权限 `600`）里缓存学到的站点结构（菜单页、成绩列表页地址），省掉每次重新找入口。

删除所有本机凭据：

```bash
./eju.sh forget
```

<details>
<summary><b>为什么不缓存 cookie 来省请求？</b></summary>

试过，**这个站点不行**。实测两条路都被拒（一律回 `システムエラー`）：

- 带着有效 cookie 直接 `GET /src/PSLSCORE010.php`
- 复用上一页的 `datTimeStamp`，直接 `POST /common/moveUserMenu.php`

它的每次跳转都要求走完整的 POST 导航链，服务端会校验会话的「当前所在画面」。所以跨进程复用 cookie 省不下请求——**因此脚本干脆不往硬盘写会话 cookie**（少一个等于登录态的敏感文件）。

真正省请求的做法是在一个进程里把会话一直养着：

| 路径 | 请求数 | 实测耗时 | 什么时候走这条 |
| --- | --- | --- | --- |
| `warm` | **1** | ~1.4s | 会话还活着 → 从当前页面 POST 一次左侧菜单回到列表页 |
| `login` | 3 | ~4.2s | 冷启动，或会话被挤掉 / 超时 |

所以 `watch` 只在第一轮花 3 个请求，之后每轮 1 个，顺带还把会话养着不会超时。这也是为什么建议比公布时间**提前十几分钟**挂上。

</details>

---

## 安全约束

EJU オンライン 同时也是**报名和缴费入口**，所以脚本对自己下了硬限制，全部写死在代码里：

- **只做只读浏览。** 任何文字或 URL 里带 `支払 / 決済 / 購入 / 登録 / 変更 / 削除 / 取消 / 再発行 / アップロード / 送信 / 同意 / 確定` 的按钮会被硬性拉黑，永远不会被点击。
- 带 `申込 / 出願 / 受験票` 字样的入口，只有在同时明确写着「成績」时才允许进入。
- **请求之间间隔 1.2 秒，不做并发。** 遇到 5xx 按 6 / 20 / 45 秒退避重试。
- **密码被拒绝立刻停止**，绝不重试——连续错误登录会锁账号。
- 识别整站维护页，不在维护窗口里瞎冲。

---

## 常见问题

<details>
<summary><b>提示「還没有保存凭据」</b></summary>

先跑 `./setup.sh`，或者直接 `./eju.sh setup`。也可以用环境变量 `EJU_ID` / `EJU_PASSWORD` 临时传。

</details>

<details>
<summary><b>回 `システムエラー`</b></summary>

九成是**同一账号有并发会话**。检查一下：

- 浏览器里是不是还开着 EJU オンライン 的登录页？关掉。
- 是不是同时跑了两个 `eju.sh`？只留一个。

`watch` 模式遇到这个会自动扔掉会话重新登录，但会白白浪费拥堵时最宝贵的请求。

</details>

<details>
<summary><b>提示站点正在维护</b></summary>

站点有例行维护窗口（比如 22:00–23:00 JST 会整站 503）。脚本会认出维护页并直接告诉你维护时段，等结束再跑就行。`rush.sh` 会自己等。

</details>

<details>
<summary><b>抓不到成绩 / 找不到成绩入口</b></summary>

站点改版时可能发生。按顺序试：

```bash
./eju.sh explore              # 先看登录后页面上有哪些入口
./eju.sh fetch --pick         # 手动从菜单里选成绩入口
./eju.sh -v fetch --dump      # 打印调试信息 + 保存原始 HTML
```

`--dump` 存下来的 HTML 在 `out/pages/`，拿它开 issue 时**记得先手动删掉姓名、生年月日、受験番号等个人信息**。

</details>

<details>
<summary><b>分数解析出来是错的</b></summary>

得点表用了 `rowspan`（日本語 / 理科 等大类）和 `colspan`（表头「科目」占两列），脚本会先展开单元格合并再按表头取「得点」列。如果对不上，`out/eju_scores_raw.txt` 里有全文留档，分数不会丢。

</details>

<details>
<summary><b>看不到更早的考试</b></summary>

站点自己写的：「既に終了した直近4回の試験について受験結果を確認することができます」——**只提供最近 4 次**。更早的官方就不给了，脚本也拿不到。所以建议每次考完都跑一次存档。

</details>

<details>
<summary><b>Mac 挂了一晚上，回来发现睡过去了</b></summary>

`caffeinate -i` 只能挡住空闲休眠，**挡不住合盖休眠**。公布日让盖子开着、插着电最稳。

</details>

---

## 重要限制

- **只能看最近 4 次考试**，这是站点的限制，不是脚本的。
- **成绩公布前没有详情按钮**，列表里写的是「成績公表前」。脚本会把这些回次也记进结果，标成 `status: 成績公表前`。
- **同一账号别在浏览器里同时登录着**，这个系统对并发会话敏感。
- **连续输错密码会锁账号**，锁了只能按站点指引处理。
- **国外考场考生 / 团体一括登録方式的考生不适用**：这类考生的成绩是在登录页那个「成績確認専用ページ」用**受験番号**查的，不走 MyPage，凭据体系不同，本脚本不覆盖。
- 站点改版随时可能让解析失效。脚本的设计是「先把原始 HTML 落盘再解析」，所以即使解析挂了，分数也还在 `out/raw/` 里。

---

## 实现细节

<details>
<summary><b>实际抓取路径</b></summary>

```
POST /src/CMNLOGIN010.php            btnClick=1 + txtMyPageID + pwdPassword
  → /src/CMNMMENU010.php             マイページ首页
POST /common/moveUserMenu.php        menuUrl=PSLSCORE010.php
  → /src/PSLSCORE010.php             受験結果の一覧（每行一个回次 + 「詳細へ」）
POST /src/PSLSCORE010.php            btnClick=1 + hidApplicantId=AP……（页面内联 detailClick()）
  → /src/PSLSCORE020.php             得点の詳細（科目 / 得点 / 得点範囲 / 平均点）
GET  /src/CMNSCOREPDF.php?ID=AP……   官方成績確認書 PDF
POST /common/loguoutUser.php         退出，释放会话
```

</details>

<details>
<summary><b>踩过的坑（都已在代码里处理）</b></summary>

- 站点所有跳转都是「把当前页面 form 原样 POST 回去，只改 `btnClick` / `menuUrl`」，`datTimeStamp` 是每页下发的一次性令牌——**每次提交都必须重新采集当前页面的全部表单字段**。
- `moveUserMenu.php` / `loguoutUser.php` 的路径是**相对当前页面目录**的（`../common/…`），写死成 `/src/common/…` 会 404。
- 「詳細へ」不是普通链接，而是页面内联函数 `detailClick(1,'AP……')`，会往隐藏字段 `hidApplicantId` 里写值。脚本会解析页面内联 JS，自动学出「哪个实参写进哪个字段」。
- 得点表用了 `rowspan` 和 `colspan`，必须先展开单元格合并，再按表头列取「得点」那一列——否则会把「平均点」当成自己的分数。
- 页脚有「特定商取引法に基づく表示」，里面也有「表示」二字，所以回次入口只在**表格行内**寻找，不扫全页。
- 站点的登出路径拼写是 `loguoutUser.php`（官方就是这么拼的，不是笔误）。

</details>

---

## 开发

### 跑测试

```bash
./.venv/bin/python tests/test_parse.py     # 页面解析
./.venv/bin/python tests/test_watch.py     # 抢分数循环（假会话，约 10 秒）
```

两套都是**完全离线**的回归测试，不联网、不需要账号。`tests/fixtures/` 里是按真实页面结构做的**脱敏**样本（个人信息已全部换成占位值），覆盖：

- 列表页 / 详情页解析
- `rowspan` / `colspan` 展开
- 未公布回次的占位处理
- 危险按钮的拉黑规则
- 两个真实账号覆盖不到的分支：「多个已公布回次」和「等待中 → 分数公布」的状态翻转

### 文件结构

```
eju_getter.py           主脚本，凭据不写在里面
eju.sh                  用 .venv 里的 python 跑主脚本
setup.sh                一次性环境准备 + 保存凭据
rush.sh                 公布日一条龙：等站点 → 预热 → 到点开抢
requirements.txt        requests / beautifulsoup4 / lxml
tests/test_parse.py     页面解析的离线回归测试
tests/test_watch.py     抢分数循环的离线测试
tests/fixtures/         脱敏页面样本
```

### 提 issue 前

如果是解析问题，附上 `./eju.sh -v fetch --dump` 的输出会很有帮助。但**发之前请务必手动删掉姓名、生年月日、国籍、受験番号等个人信息**——`out/pages/` 里的 HTML 是原始页面，什么都有。

---

## 免责声明

个人自用工具，与 JASSO（日本学生支援機構）无任何关联，未经其授权或背书。

使用者应遵守 EJU オンライン 的服务条款，只用它访问**自己**的账号。脚本已内置只读约束和请求限速（1.2 秒间隔、不并发），请不要修改这些限制去做高频请求——那既会给站点添麻烦，也可能让你的账号或 IP 被封。

站点结构随时可能变化，本工具不保证长期可用。
