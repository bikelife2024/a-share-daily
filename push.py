#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把最新一期复盘推到微信。
支持三种通道，配哪个用哪个（都配了就按顺序优先）：
  1. SERVERCHAN_URL  完整 webhook（Server酱³ 那种 https://xxxx.push.ft07.com/send/sctp....send）
  2. SERVERCHAN_KEY  只给 sendkey（走 https://sctapi.ftqq.com/<key>.send）
  3. PUSHPLUS_TOKEN  PushPlus 的 token

关于链接：微信把 github.io 和几乎所有个人域名都列进了外链拦截名单，
点开只有一句"已停止访问该网页"。所以这里不再把链接当主体——
正文（desp）自带当天全部关键数字，在微信里【不点链接就能看完】，
链接只留在最后当"想看图表和全表"的出口，并附上浏览器打开的办法。

站点地址：活页优先（LIVEPAGE_URL），其次 SITE_URL / GitHub Pages 推断。
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime

UA = "Mozilla/5.0 (compatible; a-share-daily/1.0)"


def site_url():
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"].rstrip("/") + "/"
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    if name.lower() == owner.lower() + ".github.io":
        return "https://%s.github.io/" % owner.lower()
    return "https://%s.github.io/%s/" % (owner.lower(), name)


def livepage_url():
    """活页分享地址（固定不变的 page 链接）"""
    return (os.environ.get("LIVEPAGE_URL") or "").strip().rstrip("/")


def post(url, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def latest():
    """优先读 gen_report.py 单独落盘的正文（可能含持仓，不进公开的 docs/），
    没有就退回 docs/issues.json。"""
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "push_payload.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    path = os.path.join(here, "docs", "issues.json")
    if not os.path.exists(path):
        return None
    try:
        rows = json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return rows[0] if rows else None


def build_body(row):
    """正文：自带全部关键数据，链接只做兜底"""
    date = row.get("date", "")
    head = row.get("title", "")
    digest = (row.get("digest") or "").strip()
    summary = row.get("summary", "")

    if digest:
        body = digest
    else:  # 老版本 issues.json 没有 digest，退回一句话摘要
        body = "**%s**\n\n%s" % (head, summary)

    lp = livepage_url()
    gh = site_url()

    tail = ["", "---", ""]
    if lp or gh:
        tail.append("**完整版（图表 · 板块全表 · 个股明细 · 往期）**")
        tail.append("")
        if lp:
            tail.append("活页：%s" % lp)
            tail.append("")
        if gh:
            tail.append("备用：%s" % gh)
            tail.append("")
        tail.append("> 微信里点外链常提示「已停止访问该网页」——那是微信拦域名，不是页面挂了。"
                    "遇到就点右上角「···」→「在浏览器打开」，或长按链接复制到手机浏览器。")
    else:
        tail.append("> 没配站点地址（LIVEPAGE_URL / SITE_URL），只发文字版。")

    return body + "\n".join(tail)


def main():
    row = latest()
    if not row:
        print("· 没有找到 docs/issues.json，跳过推送")
        return 0

    date = row.get("date", "")

    # 休市日（以及被 gen_report 跳过的重复运行）不推旧内容，免得周末收到上一条
    today = datetime.now().strftime("%Y-%m-%d")
    if date != today and os.environ.get("FORCE_PUSH", "").strip() not in ("1", "true", "True"):
        print("· 最新一期是 %s，不是今天（%s），跳过推送。要强制推，设 FORCE_PUSH=1" % (date, today))
        return 0

    head = row.get("title", "")
    short = date[5:].replace("-", "月") + "日"

    title = "%s A股复盘 · %s" % (short, head)
    if len(title) > 32:
        title = title[:32]

    desp = build_body(row)
    # Server酱 的 short 会显示在微信通知卡片上，不点进去也能看到核心数字
    brief = (row.get("brief") or row.get("summary") or head)[:100]

    sent = False

    full = os.environ.get("SERVERCHAN_URL")
    key = os.environ.get("SERVERCHAN_KEY")
    pp = os.environ.get("PUSHPLUS_TOKEN")

    try:
        if full:
            r = post(full, {"title": title, "desp": desp, "short": brief})
            print("Server酱(完整webhook)：", r[:200])
            sent = "code" not in r or '"code":0' in r.replace(" ", "")
        elif key:
            r = post("https://sctapi.ftqq.com/%s.send" % key,
                     {"title": title, "desp": desp, "short": brief})
            print("Server酱：", r[:200])
            sent = '"code":0' in r.replace(" ", "")
        elif pp:
            r = post("https://www.pushplus.plus/send", {
                "token": pp, "title": title, "content": desp, "template": "markdown"})
            print("PushPlus：", r[:200])
            sent = '"code":200' in r.replace(" ", "")
        else:
            print("· 三个推送通道都没配置（SERVERCHAN_URL / SERVERCHAN_KEY / PUSHPLUS_TOKEN），"
                  "只生成页面，不推送")
            return 0
    except Exception as e:  # noqa: BLE001
        print("✗ 推送请求失败：%s" % e)
        return 1

    if not sent:
        print("⚠ 推送回执看起来不正常，把上面那行原文发我，我调一下格式")
        return 1
    print("✓ 已推送：%s（正文 %d 字）" % (title, len(desp)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
