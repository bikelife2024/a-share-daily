# A股收盘复盘 · 云端版

每天 16:10（北京时间）自动生成一份 A 股收盘复盘，发布成网页，并推送到你的微信。
**全程跑在 GitHub 的服务器上，你的电脑关不关机都不影响。**

---

## 一、这个仓库是什么

| 文件 | 作用 |
|---|---|
| `gen_report.py` | 抓数据、算指标、生成网页。只依赖 Python 标准库，不需要装任何包 |
| `push.py` | 把最新一期推到微信（Server酱 / PushPlus） |
| `.github/workflows/daily.yml` | 定时器：周一到周五 16:10 触发上面两个脚本 |
| `docs/` | 自动生成的网页，GitHub Pages 从这里发布（第一次运行后才会出现） |

数据来源是东方财富的公开行情接口，**不需要任何密钥、账号或付费订阅**。

---

## 二、部署步骤（约 10 分钟，只做一次）

### 第 1 步：有 GitHub 账号

没有就去 https://github.com/signup 注册，邮箱验证一下即可，免费。

### 第 2 步：新建一个仓库

1. 右上角 `+` → `New repository`
2. Repository name 填 `a-share-daily`（叫什么都可以）
3. 选 **Public**（私有仓库也行，但公开仓库的 Actions 时长不限量）
4. 勾上 `Add a README file`，点 `Create repository`

### 第 3 步：把文件传上去

在仓库首页点 `Add file` → `Upload files`，把本目录下的**全部内容**拖进去：

```
gen_report.py
push.py
.github/workflows/daily.yml
```

⚠️ `.github` 这个文件夹是隐藏的，拖拽整个文件夹才能带上它。
如果拖不上，用 `Add file` → `Create new file`，文件名直接输入
`.github/workflows/daily.yml`，再把文件内容粘进去。

最后点绿色按钮 `Commit changes`。

### 第 4 步：先手动跑一次

1. 仓库顶部点 `Actions`
2. 左侧点 `A股收盘复盘`
3. 右边点 `Run workflow` → 绿色按钮 `Run workflow`
4. 等 1–2 分钟刷新，应该出现一个绿勾

跑完仓库里会多出 `docs/` 文件夹，里面有 `index.html` 和 `archive/`。

> 如果报红叉，点进去看日志，把最后 20 行发我。

### 第 5 步：打开网页发布

1. 仓库 `Settings` → 左侧 `Pages`
2. `Source` 选 **Deploy from a branch**
3. `Branch` 选 **main**，右边文件夹选 **/docs**，点 `Save`
4. 等 1 分钟，页面顶部会显示网址：

```
https://你的用户名.github.io/a-share-daily/
```

这个网址是固定的，以后每天都是它，内容自动更新。

### 第 6 步：配微信推送

**先拿到推送密钥**，二选一：

- **Server酱**：https://sct.ftqq.com 微信扫码登录 → `SendKey` → 复制那串 `SCT...` 开头的
- **PushPlus**：https://www.pushplus.plus 微信扫码登录 → `一对一推送` → 复制 token

**再填进仓库**：
`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

| Name | Value |
|---|---|
| `SERVERCHAN_KEY` | 你的 SendKey（Server酱） |
| `PUSHPLUS_TOKEN` | 你的 token（PushPlus，用哪个填哪个） |

填一个就行。填完下次运行就会推到你微信。

### 第 7 步：完成

之后每个工作日 16:10 自动出报告、自动发布、自动推送。你什么都不用做。

---

## 三、说明几件事

**时间不准是正常的。** GitHub 免费版的定时任务允许延迟 5–20 分钟，偶尔会晚到 16:30。
它不会漏，只是不精确到分钟。对收盘复盘来说没影响。

**为什么不用 GitHub 网页版直接改代码。** 不需要，你不用碰代码。要改跟踪的股票或行业，
在 `gen_report.py` 顶部的 `HOLDINGS` / `SECTORS` 列表里改，那是唯一需要动的地方。

**休市会自动跳过。** 脚本会先看上证指数最新交易日是不是今天，不是就直接退出，
不会生成一份无意义的报告，也不会推空消息。

**往期怎么看。** 网页标题下面有一条「往期复盘」，点日期就能翻历史，
每一天的完整页面都存档在 `docs/archive/` 里，永久保存。

**数据口径。** 行业涨跌幅是**总市值加权自算**（东方财富二级行业分类）。
和券商软件里的申万口径会有一点差别，但在同一套分类下前后一致，可以纵向比较。

**不碰你的成本价。** 报告只跟踪当日强弱与相对指数表现，不推算盈亏——
如果期间除权，旧成本会失效，那需要你先核对复权。

**数据有疑义怎么办。** 脚本里的每个数字都来自公开接口，可以自己核对：
指数快照 `push2.eastmoney.com/api/qt/ulist.np/get`，
全 A 行情 `push2.eastmoney.com/api/qt/clist/get`，
两融历史 `datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ`。

---

## 四、本地试跑（可选）

装了 Python 3.8+ 就能在电脑上直接跑，不依赖 GitHub：

```bash
python gen_report.py --out docs
```

`--force` 参数可以在休市日强制生成一份（调试用）。

---

## 免责声明

本项目输出的所有内容基于公开数据和量化分析，仅供参考，不构成投资建议。
市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，
必要时咨询持牌专业机构。过往表现不预示未来收益。
