#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时探测：为「不依赖东财 clist 分页」的新取数方案选接口。
重点确认：腾讯/新浪能否供指数与K线、新浪行业分类能否供板块、东财 ulist 的配额。"""
import json
import time
import urllib.error
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
            raw = r.read()
        return raw.decode(enc, "replace"), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s" % e.code
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__


print("=== 1. 腾讯行情（批量，GBK）===")
t, e = get("https://qt.gtimg.cn/q=sh000001,sz399001,sz399006,sh000688,sh000852,bj899050")
print("  " + ("OK" if t else "FAIL " + str(e)))
if t:
    for seg in t.strip().split(";"):
        if "~" in seg:
            f = seg.split("~")
            print("    %-10s %-10s 收%-10s 涨跌%s" % (f[2], f[1][:8], f[3], f[32] if len(f) > 32 else "?"))

print("\n=== 2. 腾讯 K线（用于均线）===")
t, e = get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,70,qfq")
print("  " + ("OK" if t else "FAIL " + str(e)))
if t:
    try:
        j = json.loads(t)
        d = j["data"]["sh000001"]
        k = d.get("qfqday") or d.get("day") or []
        print("    K线条数 %d，最后一条 %s" % (len(k), k[-1] if k else "无"))
    except Exception as ex:  # noqa: BLE001
        print("    解析失败:", ex, t[:150])

print("\n=== 3. 腾讯海外指数 ===")
t, e = get("https://qt.gtimg.cn/q=usDJI,usIXIC,us.INX,hkHSI,jpN225,krKS11,twTWII")
print("  " + ("OK" if t else "FAIL " + str(e)))
if t:
    print("    " + t[:260].replace("\n", " "))

print("\n=== 4. 新浪行业分类（板块涨跌，GBK）===")
t, e = get("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php", "https://finance.sina.com.cn/", "gbk")
print("  " + ("OK" if t else "FAIL " + str(e)))
if t:
    try:
        body = t.split("=", 1)[1].strip().rstrip(";")
        j = json.loads(body)
        print("    行业数 %d" % len(j))
        for k, v in list(j.items())[:4]:
            print("    ", v)
    except Exception as ex:  # noqa: BLE001
        print("    解析失败:", ex, t[:200])

print("\n=== 5. 新浪 K线 ===")
t, e = get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
           "?symbol=sh000001&scale=240&ma=no&datalen=70", "https://finance.sina.com.cn/")
print("  " + ("OK" if t else "FAIL " + str(e)))
if t:
    print("    前 120 字:", t[:120])

print("\n=== 6. 东财 ulist 配额（连发 8 次，间隔 0.6s）===")
okc = 0
for i in range(8):
    u = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
         "&fields=f2,f3,f12,f14,f104,f105,f106&secids=1.000001,0.399106,0.899050")
    t, e = get(u, "https://quote.eastmoney.com/")
    if t and '"rc":0' in t:
        okc += 1
        if i == 0:
            print("    首次返回:", t[:200])
    else:
        print("    第 %d 次失败: %s" % (i + 1, e or t[:80]))
    time.sleep(0.6)
print("    ulist 成功 %d/8" % okc)

print("\n=== 7. 东财 push2ex 涨停池 ===")
t, e = get("https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
           "&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=fbt%3Aasc&date=20261120")
print("  " + ("OK " + t[:120] if t else "FAIL " + str(e)))

print("\n=== 8. 东财 datacenter 两融 ===")
t, e = get("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ"
           "&columns=ALL&pageNumber=1&pageSize=2&sortColumns=DIM_DATE&sortTypes=-1&source=WEB&client=WEB")
print("  " + ("OK " + t[:150] if t else "FAIL " + str(e)))
