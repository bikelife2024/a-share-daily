#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时探测：拿新浪行业名单、东财一级/二级板块名单（含代码），
用于确定最终的板块取数方案。"""
import json
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def get(url, ref="", enc="utf-8"):
    hdr = {"User-Agent": UA, "Accept": "*/*"}
    if ref:
        hdr["Referer"] = ref
    try:
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode(enc, "replace"), None
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__ + ":" + str(e)[:60]


print("=== 新浪行业全名单 ===")
t, e = get("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
           "https://finance.sina.com.cn/", "gbk")
if t:
    body = t.split("=", 1)[1].strip().rstrip(";")
    j = json.loads(body)
    print("共 %d 个：" % len(j))
    rows = []
    for k, v in j.items():
        f = v.split(",")
        if len(f) >= 6:
            rows.append((f[1], float(f[5])))
    rows.sort(key=lambda x: x[1])
    for name, pct in rows:
        print("  %-10s %+.2f%%" % (name, pct))
else:
    print("  失败", e)

print("\n=== 东财板块（按代码排序，pn=1）===")
u = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=0&np=1"
     "&fltt=2&invt=2&fid=f12&fs=m:90+t:2&fields=f12,f14,f3")
t, e = get(u, "https://quote.eastmoney.com/")
if t:
    j = json.loads(t)
    d = (j.get("data") or {})
    print("total=%s 返回%d" % (d.get("total"), len(d.get("diff") or [])))
    names = [(x["f12"], x["f14"], x.get("f3")) for x in d.get("diff") or []]
    lvl1 = [x for x in names if "Ⅲ" not in x[1] and "Ⅱ" not in x[1]]
    print("  其中无级别后缀的 %d 个：" % len(lvl1))
    for c, n, p in lvl1:
        print("    %-8s %-12s %s" % (c, n, p))
else:
    print("  失败", e)

print("\n=== 东财板块（按代码排序，pn=2）===")
u2 = u.replace("pn=1", "pn=2")
t, e = get(u2, "https://quote.eastmoney.com/")
if t:
    j = json.loads(t)
    d = (j.get("data") or {})
    names = [(x["f12"], x["f14"], x.get("f3")) for x in d.get("diff") or []]
    lvl1 = [x for x in names if "Ⅲ" not in x[1] and "Ⅱ" not in x[1]]
    print("  返回%d，无后缀 %d 个：" % (len(names), len(lvl1)))
    for c, n, p in lvl1:
        print("    %-8s %-12s %s" % (c, n, p))
else:
    print("  失败", e)
