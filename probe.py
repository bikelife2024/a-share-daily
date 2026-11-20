#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时探测：测东财 clist 全A 分页的可持续频率，找安全的请求间隔。"""
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

BASE = ("https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
        "&fltt=2&invt=2&fid=f12"
        "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
        "&fields=f12,f14,f3,f20,f100")


def hit(pn):
    req = urllib.request.Request(BASE.format(pn=pn), headers={
        "User-Agent": UA, "Referer": "https://quote.eastmoney.com/", "Accept": "*/*"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
        dt = time.time() - t0
        import json
        j = json.loads(raw.decode())
        d = j.get("data") or {}
        return True, len(d.get("diff") or []), d.get("total"), dt
    except urllib.error.HTTPError as e:
        return False, 0, str(e.code), time.time() - t0
    except Exception as e:  # noqa: BLE001
        return False, 0, type(e).__name__, time.time() - t0


def run(label, gap, pages=22):
    print("\n--- %s：间隔 %.2fs，连发 %d 页 ---" % (label, gap, pages))
    okc = 0
    for pn in range(1, pages + 1):
        ok, n, total, dt = hit(pn)
        flag = "OK " if ok else "ERR"
        print("  p%-3d %s  n=%-4s total=%-6s %.2fs" % (pn, flag, n, total, dt))
        if ok:
            okc += 1
        time.sleep(gap)
    print("  成功 %d/%d" % (okc, pages))
    return okc


print("=== 频率测试（先慢后快，确认限流阈值）===")
run("A", 1.0, 22)
time.sleep(20)
run("B", 0.35, 12)
