#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时探测：在 GitHub Actions 环境里逐个试候选行情接口，看哪些可用。"""
import json
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

TESTS = [
    ("东财 clist（现用·已知502）",
     "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
     "&fs=m:0+t:6,m:1+t:2&fields=f12,f14,f3,f20,f100", {}),
    ("东财 82.push2 clist",
     "https://82.push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
     "&fs=m:0+t:6,m:1+t:2&fields=f12,f14,f3,f20,f100", {}),
    ("东财 push2delay clist",
     "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
     "&fs=m:0+t:6,m:1+t:2&fields=f12,f14,f3,f20,f100", {}),
    ("东财 push2his clist",
     "https://push2his.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
     "&fs=m:0+t:6,m:1+t:2&fields=f12,f14,f3,f20,f100", {}),
    ("东财 ulist（现用·已知可用）",
     "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&fields=f2,f3,f12,f14,f104,f105,f106"
     "&secids=1.000001,0.399001", {}),
    ("东财 ulist 拿板块指数",
     "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&fields=f2,f3,f12,f14"
     "&secids=90.BK0475,90.BK0477,90.BK0428", {}),
    ("东财 push2ex 涨停池",
     "https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
     "&dpt=wz.ztzt&Pageindex=0&pagesize=3&sort=fbt%3Aasc&date=20261120", {}),
    ("东财 datacenter 两融",
     "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ"
     "&columns=ALL&pageNumber=1&pageSize=3&sortColumns=DIM_DATE&sortTypes=-1&source=WEB&client=WEB", {}),
    ("腾讯 行情",
     "https://qt.gtimg.cn/q=sh000001,sz399001", {}),
    ("新浪 行情",
     "https://hq.sinajs.cn/list=sh000001,sz399001",
     {"Referer": "https://finance.sina.com.cn/"}),
    ("新浪 行业分类",
     "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php", {}),
    ("腾讯 板块排行",
     "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=hs&sort_type=price&direct=down&offset=0&count=20", {}),
]


def probe(name, url, extra):
    hdr = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/", "Accept": "*/*"}
    hdr.update(extra)
    try:
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            code = r.status
        txt = raw.decode("utf-8", "replace")
        n = len(raw)
        short = txt[:110].replace("\n", " ")
        print("  OK  %-24s %s  %dB  %s" % (name, code, n, short))
        return True
    except urllib.error.HTTPError as e:
        print("  HTTP%-4s %-24s %s" % (e.code, name, (e.read() or b"")[:90].decode("utf-8", "replace")))
    except Exception as e:  # noqa: BLE001
        print("  FAIL %-24s %s" % (name, str(e)[:110]))
    return False


print("=== 候选接口探测 ===")
ok = []
for n, u, x in TESTS:
    if probe(n, u, x):
        ok.append(n)
print("\n=== 可用 %d 个 ===" % len(ok))
for n in ok:
    print("  ·", n)
