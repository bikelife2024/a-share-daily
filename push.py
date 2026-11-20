#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把最新一期复盘推到微信。
支持三种通道，配哪个用哪个（都配了就按 顺序 优先）：
  1. SERVERCHAN_URL  完整 webhook（Server酱³ 那种 https://xxxx.push.ft07.com/send/sctp....send）
  2. SERVERCHAN_KEY  只给 sendkey（走 https://sctapi.ftqq.com/<key>.send）
  3. PUSHPLUS_TOKEN  PushPlus 的 token

站点地址自动从 GitHub 环境推断，也可以用 SITE_URL 覆盖。
"""

import json
import os
import sys
import urllib.parse
import urllib.request

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


def post(url, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def latest():
    path = os.path.join("docs", "issues.json")
    if not os.path.exists(path):
        return None
    try:
        rows = json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return rows[0] if rows else None


def main():
    row = latest()
    if not row:
        print("· 没有找到 docs/issues.json，跳过推送")
        return 0

    base = site_url()
    date = row.get("date", "")
    head = row.get("title", "")
    summary = row.get("summary", "")
    short = date[5:].replace("-", "月") + "日"

    title = "%s A股复盘 · %s" % (short, head)
    if len(title) > 32:
        title = title[:32]

    lines = ["## %s" % head, "", "**%s**" % summary, ""]
    link = base + (row.get("path") or "")
    if base:
        lines.append("[点这里看完整复盘（含图表与持仓）](%s)" % link)
        lines.append("")
        lines.append("> %s" % base)
    else:
        lines.append("（未配置站点地址，请在仓库变量里加 SITE_URL）")
    desp = "\n".join(lines)

    sent = False

    full = os.environ.get("SERVERCHAN_URL")
    key = os.environ.get("SERVERCHAN_KEY")
    pp = os.environ.get("PUSHPLUS_TOKEN")

    try:
        if full:
            r = post(full, {"title": title, "desp": desp})
            print("Server酱(完整webhook)：", r[:200])
            sent = "code" not in r or '"code":0' in r.replace(" ", "")
        elif key:
            r = post("https://sctapi.ftqq.com/%s.send" % key, {"title": title, "desp": desp})
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
    print("✓ 已推送：%s" % title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
