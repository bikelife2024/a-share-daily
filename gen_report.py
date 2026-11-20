#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股收盘复盘 · 云端生成器
数据源：东方财富公开行情接口（无需任何密钥 / 账号）
输出：docs/index.html、docs/archive/YYYY-MM-DD.html、docs/issues.json

用法：
    python gen_report.py --out docs
    python gen_report.py --out docs --force      # 休市也强制生成（调试用）
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=8))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# ── 固定跟踪清单 ────────────────────────────────────────────────
INDICES = [
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000688", "科创50"),
    ("1.000852", "中证1000"),
    ("0.899050", "北证50"),
]

HOLDINGS = [
    ("1.600415", "600415", "小商品城"),
    ("0.002416", "002416", "爱施德"),
    ("1.600420", "600420", "国药现代"),
    ("0.300586", "300586", "美联新材"),
    ("1.600589", "600589", "大位科技"),
]

# 持仓跟踪这一节是否写进页面。发布到公开站点时应设为 0（SHOW_HOLDINGS=0），
# 本地自己看默认保留。仓库是 public 才能用免费 Pages，页面等于对外公开。
SHOW_HOLDINGS = os.environ.get("SHOW_HOLDINGS", "1").strip() not in ("0", "false", "False", "")

OVERSEAS = [
    ("100.DJIA", "道琼斯"),
    ("100.NDX", "纳斯达克"),
    ("100.SPX", "标普500"),
    ("100.TWII", "中国台湾加权"),
    ("100.N225", "日经225"),
    ("100.KS11", "韩国KOSPI"),
    ("100.HSI", "恒生指数"),
]

# 固定跟踪的行业（东方财富二级行业名，总市值加权自算）
SECTORS = [
    "半导体", "软件开发", "计算机设备", "IT服务Ⅱ", "通信设备", "元件", "消费电子",
    "光伏设备", "电池", "电网设备", "风电设备", "军工电子Ⅱ", "航空装备Ⅱ",
    "汽车零部件", "工业金属", "化学制药", "医疗器械",
    "房地产开发", "银行Ⅱ", "证券Ⅱ", "保险Ⅱ", "煤炭开采", "白酒Ⅱ", "白色家电", "食品加工",
]

SECTOR_ALIAS = {
    "IT服务Ⅱ": "IT服务", "军工电子Ⅱ": "军工电子", "航空装备Ⅱ": "航空装备",
    "银行Ⅱ": "银行", "证券Ⅱ": "证券", "保险Ⅱ": "保险", "白酒Ⅱ": "白酒",
}


# ── HTTP ───────────────────────────────────────────────────────
def get_json(url, tries=4, timeout=25):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "*/*",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"请求失败 {url[:90]} → {last}")


def num(v):
    """东财用 '-' 表示停牌/缺失"""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


# ── 采集 ───────────────────────────────────────────────────────
def fetch_indices():
    secids = ",".join(s for s, _ in INDICES)
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
           f"&secids={secids}&fields=f2,f3,f4,f6,f12,f14")
    j = get_json(url)
    by_code = {}
    for row in j["data"]["diff"]:
        by_code[row.get("f12")] = {
            "code": row.get("f12"),
            "name": row.get("f14"),
            "close": num(row.get("f2")),
            "pct": num(row.get("f3")),
            "chg": num(row.get("f4")),
            "amount": num(row.get("f6")),
        }
    # 按 INDICES 声明顺序输出，避免接口顺序变动导致取错
    out = [by_code[c.split(".")[1]] for c, _ in INDICES if c.split(".")[1] in by_code]
    return out


def fetch_all_a():
    """全 A 股（含北交所），用于涨跌家数与行业加权。

    东方财富对 clist 的单页条数有限制（pz 明显超过 100 会直接断连），
    且对短时间内的请求频次敏感。所以固定 pz=100、每页之间留间隔，
    单页失败先重试（指数退避），仍失败才跳过该页，不让整份报告崩掉。
    """
    base = ("https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
            "&fltt=2&invt=2&fid=f12"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            "&fields=f12,f14,f3,f20,f100")
    allrows, total, failed = [], None, 0
    for pn in range(1, 75):
        j, last = None, None
        for attempt in range(4):
            try:
                j = get_json(base.format(pn=pn), tries=2, timeout=30)
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.5 * (2 ** attempt))
        if j is None:
            failed += 1
            print("      ! 第 %d 页取数失败（%s），跳过" % (pn, last))
            continue
        data = j.get("data") or {}
        diff = data.get("diff") or []
        if not diff:
            break
        total = data.get("total") or total
        allrows.extend(diff)
        if total and len(allrows) >= total:
            break
        time.sleep(0.3)
    if failed:
        print("      ! 共 %d 页跳过，涨跌家数会略有偏差" % failed)
    return allrows, (total or len(allrows))


def compute_breadth(rows):
    up = dn = flat = 0
    for r in rows:
        p = num(r.get("f3"))
        if p is None:
            continue
        if p > 0:
            up += 1
        elif p < 0:
            dn += 1
        else:
            flat += 1
    return up, dn, flat


def compute_sectors(rows):
    buckets = {}
    for r in rows:
        name = r.get("f100")
        pct = num(r.get("f3"))
        cap = num(r.get("f20"))
        if not name or pct is None or not cap or cap <= 0:
            continue
        b = buckets.setdefault(name, [0.0, 0.0, 0])
        b[0] += pct * cap
        b[1] += cap
        b[2] += 1
    out = []
    for name, (wsum, msum, n) in buckets.items():
        if msum <= 0:
            continue
        out.append({"name": name, "pct": round(wsum / msum, 2), "n": n, "mcap": msum})
    return out


def pick_sectors(all_sectors):
    idx = {s["name"]: s for s in all_sectors}
    picked = []
    for name in SECTORS:
        s = idx.get(name)
        if not s:
            continue
        picked.append({
            "name": SECTOR_ALIAS.get(name, name),
            "pct": s["pct"],
            "n": s["n"],
            "mcap": s["mcap"],
        })
    picked.sort(key=lambda x: -x["pct"])
    return picked


def fetch_limits(date_str):
    """涨停 / 跌停家数（东财涨停池、跌停池）"""
    def one(kind):
        url = ("https://push2ex.eastmoney.com/getTopic" + kind + "Pool"
               "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0"
               f"&pagesize=1&sort=fund%3Aasc&date={date_str}")
        try:
            j = get_json(url)
            return int((j.get("data") or {}).get("tc") or 0)
        except Exception:  # noqa: BLE001
            return None
    return one("ZT"), one("DT")


def fetch_margin(days=60):
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ"
           "&columns=ALL&pageNumber=1&pageSize=%d&sortColumns=DIM_DATE&sortTypes=-1"
           "&source=WEB&client=WEB" % days)
    j = get_json(url)
    rows = (j.get("result") or {}).get("data") or []
    series = []
    for r in rows:
        d = str(r.get("DIM_DATE") or "")[:10]
        rzye = num(r.get("RZYE")) or 0.0
        rqye = num(r.get("RQYE")) or 0.0
        series.append({"date": d, "rz": rzye / 1e8, "total": (rzye + rqye) / 1e8})
    series.sort(key=lambda x: x["date"])
    return series


def fetch_kline(secid, limit=140):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2&fields2=f51,f53,f57&klt=101&fqt=1"
           f"&end=20500101&lmt={limit}")
    j = get_json(url)
    k = ((j.get("data") or {}).get("klines")) or []
    out = []
    for line in k:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        c = num(parts[1])
        if c is None:
            continue
        amt = num(parts[2]) if len(parts) > 2 else None
        out.append({"date": parts[0], "close": c, "amount": amt})
    return out


def fetch_holdings():
    secids = ",".join(s for s, _, _ in HOLDINGS)
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
           f"&secids={secids}&fields=f2,f3,f6,f8,f10,f12,f14")
    j = get_json(url)
    by_code = {}
    for row in j["data"]["diff"]:
        by_code[row.get("f12")] = row
    out = []
    for _, code, name in HOLDINGS:
        row = by_code.get(code) or {}
        out.append({
            "code": code,
            "name": name,
            "close": num(row.get("f2")),
            "pct": num(row.get("f3")),
            "amount": num(row.get("f6")),
            "turnover": num(row.get("f8")),
            "volratio": num(row.get("f10")),
        })
    return out


def fetch_overseas():
    secids = ",".join(s for s, _ in OVERSEAS)
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
           f"&secids={secids}&fields=f2,f3,f12,f14")
    j = get_json(url)
    order = {s: i for i, (s, _) in enumerate(OVERSEAS)}
    rows = j["data"]["diff"]
    rows.sort(key=lambda r: order.get("100." + str(r.get("f12")), 99))
    out = []
    for r in rows:
        out.append({"name": r.get("f14"), "close": num(r.get("f2")), "pct": num(r.get("f3"))})
    return out


# ── 计算 ───────────────────────────────────────────────────────
def mean(xs):
    return sum(xs) / len(xs) if xs else None


def tech(closes):
    c = [x["close"] for x in closes]
    last = c[-1]
    res = {"close": last}
    for n in (5, 20, 60, 120):
        if len(c) >= n:
            ma = mean(c[-n:])
            res[f"ma{n}"] = ma
            res[f"d{n}"] = (last / ma - 1) * 100
        else:
            res[f"ma{n}"] = None
            res[f"d{n}"] = None
    win = c[-60:] if len(c) >= 60 else c
    hi, lo = max(win), min(win)
    res["hi60"] = hi
    res["lo60"] = lo
    res["dd"] = (last / hi - 1) * 100
    res["up60"] = (last / lo - 1) * 100
    res["prev"] = c[-2] if len(c) >= 2 else None
    return res


def fmt_money(yuan):
    if yuan is None:
        return "—"
    if yuan >= 1e12:
        return f"{yuan / 1e12:.2f} 万亿"
    return f"{yuan / 1e8:,.0f} 亿"


def cls(pct):
    if pct is None:
        return ""
    return "up" if pct > 0 else ("down" if pct < 0 else "flat")


def spct(pct, digits=2):
    if pct is None:
        return "—"
    return f"{pct:+.{digits}f}%"


# ── 判断逻辑 ───────────────────────────────────────────────────
def qualitative(idx, breadth, sectors, limits):
    up, dn, flat = breadth
    sh = next((x for x in idx if x["code"] == "000001"), None)
    cy = next((x for x in idx if x["code"] == "399006"), None)
    zz = next((x for x in idx if x["code"] == "000852"), None)
    ratio = (dn / up) if up else 99

    pos_secs = [s for s in sectors if s["pct"] > 0]
    deep = [s for s in sectors if s["pct"] <= -4]

    if sh and cy and zz and sh["pct"] and cy["pct"]:
        spread = cy["pct"] - sh["pct"]
    else:
        spread = 0

    if -1.5 < (sh["pct"] if sh and sh["pct"] is not None else 0) < 1.5 and \
       len(pos_secs) >= 3 and up > dn * 0.8:
        return "权重与中小盘各走一边，指数看着没事", "结构分化"
    if len(pos_secs) >= 3 and dn > up * 6 and spread <= -1.5:
        return "权重护盘，中小盘坍缩", "权重护盘"
    if dn > up * 8 and len(pos_secs) <= 2:
        return "全面普跌，无处躲藏", "全面普跌"
    if up > dn:
        return "普涨，赚钱效应扩散", "普涨"
    if spread <= -2:
        return "中小盘杀跌，权重勉强撑住", "权重护盘"
    return "结构分化，强弱分明", "结构分化"


def anchor_state(idx, overseas, sectors, limits, margin_delta, breadth):
    """四条锚，返回 [(名称, 是否到位, 依据文字)]"""
    out = []

    # 1 外部锚：美股 AI 链是否止跌
    us = [x for x in overseas if x["name"] in ("道琼斯", "纳斯达克", "标普500")]
    asia = [x for x in overseas if x["name"] in ("中国台湾加权", "日经225", "韩国KOSPI")]
    us_ok = us and all((x["pct"] or 0) > -0.8 for x in us)
    asia_ok = asia and all((x["pct"] or 0) > -1.5 for x in asia)
    if us and us_ok and asia_ok:
        out.append(("外部锚", True, "美股三大指数与中国台湾、日韩市场均未再破位，外部压力缓解"))
    else:
        worst = min(us + asia, key=lambda x: x["pct"] if x["pct"] is not None else 0) if (us or asia) else None
        txt = f"隔夜美股与亚洲市场仍在下跌，最弱 {worst['name']} {spct(worst['pct'])}" if worst else "海外数据缺失"
        out.append(("外部锚", False, txt))

    # 2 杠杆锚：两融单日降幅是否收敛到 50 亿内并转增
    if margin_delta is None:
        out.append(("杠杆锚", False, "两融数据缺失，无法判定"))
    elif margin_delta >= 0:
        out.append(("杠杆锚", True, f"两融余额单日回升 {margin_delta:.0f} 亿，杠杆开始回补"))
    elif margin_delta > -50:
        out.append(("杠杆锚", True, f"两融单日仅降 {abs(margin_delta):.0f} 亿，降幅已收敛到 50 亿内"))
    else:
        out.append(("杠杆锚", False, f"两融单日再降 {abs(margin_delta):.0f} 亿，被动去杠杆未结束"))

    # 3 结构锚：前期强势方向是否补跌
    strong = [s for s in sectors if s["name"] in ("半导体", "通信设备", "软件开发", "计算机设备", "元件")]
    if strong:
        avg = mean([s["pct"] for s in strong])
        if avg <= -3:
            out.append(("结构锚", False, f"算力与半导体链仍在补跌（均 {avg:+.2f}%），没有止血"))
        elif avg <= -1:
            out.append(("结构锚", False, f"科技链跌幅收窄至 {avg:+.2f}%，但尚未转正"))
        else:
            out.append(("结构锚", True, f"科技链已止血（均 {avg:+.2f}%），强势方向不再拖累"))
    else:
        out.append(("结构锚", False, "板块数据缺失"))

    # 4 情绪锚：跌停家数 <= 20 且 上涨家数 >= 2500
    up_cnt, dn_cnt, _ = breadth
    dt = limits[1]
    if dt is None:
        out.append(("情绪锚", False, "涨跌停数据缺失，无法判定"))
    elif dt <= 20 and up_cnt >= 2500:
        out.append(("情绪锚", True, f"跌停收至 {dt} 家、上涨回到 {up_cnt} 家，恐慌开始消退"))
    elif dt <= 20:
        out.append(("情绪锚", False, f"跌停已收到 {dt} 家，但上涨只有 {up_cnt} 家，赚钱效应没回来"))
    else:
        out.append(("情绪锚", False, f"跌停 {dt} 家，仍高于 20 家的收敛线；上涨仅 {up_cnt} 家"))
    return out


def stance(anchors):
    hit = sum(1 for a in anchors if a[1])
    if hit >= 3:
        return "进攻档", hit
    if hit == 2:
        return "震荡档", hit
    return "防守档", hit


# ── 渲染 ───────────────────────────────────────────────────────
CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#f6f6f4; --card:#ffffff; --line:#e4e3df;
  --ink:#1a1a18; --ink2:#55534e; --ink3:#8d8a83;
  --up:#c4362c; --down:#18855a; --accent:#1f4b8e; --warn:#b8791f;
}
body{background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  font-size:14px; line-height:1.72; -webkit-font-smoothing:antialiased;}
.wrap{max-width:860px; margin:0 auto; padding:0 18px 70px;}
header{padding:38px 0 22px;}
.kicker{font-size:11.5px; letter-spacing:.18em; color:var(--ink3); text-transform:uppercase;}
h1{font-size:27px; line-height:1.32; font-weight:700; margin:10px 0 8px; letter-spacing:-.3px;}
.sub{font-size:12px; color:var(--ink3);}
.cards{display:grid; grid-template-columns:repeat(3,1fr); gap:9px; margin:22px 0 6px;}
.card{background:var(--card); border:1px solid var(--line); padding:12px 13px;}
.card .k{font-size:11px; color:var(--ink3); margin-bottom:5px;}
.card .v{font-size:19px; font-weight:700; letter-spacing:-.4px;}
.card .s{font-size:11px; color:var(--ink3); margin-top:3px;}
h2{font-size:16.5px; font-weight:700; margin:38px 0 13px; padding-left:10px;
   border-left:3px solid var(--ink); line-height:1.25;}
h3{font-size:14px; font-weight:600; margin:20px 0 8px;}
p{margin:9px 0; color:var(--ink2);}
p strong,li strong{color:var(--ink); font-weight:600;}
.up{color:var(--up);} .down{color:var(--down);} .flat{color:var(--ink3);}
table{width:100%; border-collapse:collapse; margin:8px 0; font-size:12.8px; background:var(--card);}
th,td{padding:8px 9px; text-align:right; border-bottom:1px solid var(--line); white-space:nowrap;}
th{font-size:11px; color:var(--ink3); font-weight:500; background:#fbfbfa;}
td:first-child,th:first-child{text-align:left;}
tr:last-child td{border-bottom:none;}
.tbl-wrap{overflow-x:auto; border:1px solid var(--line);}
.chartbox{background:var(--card); border:1px solid var(--line); padding:12px 6px 4px; margin:12px 0;}
.chart{width:100%;}
.cap{font-size:11px; color:var(--ink3); padding:8px 8px 6px; line-height:1.6;}
.chain{display:grid; grid-template-columns:1fr 26px 1fr 26px 1fr; gap:0; margin:14px 0;}
.box{background:var(--card); border:1px solid var(--line); padding:12px;}
.box .t{font-size:11px; color:var(--ink3); margin-bottom:6px;}
.box .b{font-size:12.5px; color:var(--ink2); line-height:1.65;}
.arrow{display:flex; align-items:center; justify-content:center; color:var(--ink3); font-size:14px;}
.anchors{display:grid; grid-template-columns:repeat(2,1fr); gap:9px; margin:12px 0;}
.anchor{background:var(--card); border:1px solid var(--line); padding:12px 13px; border-left-width:3px;}
.anchor.ok{border-left-color:var(--down);}
.anchor.no{border-left-color:var(--up);}
.anchor .h{display:flex; justify-content:space-between; align-items:center; margin-bottom:5px;}
.anchor .n{font-weight:600; font-size:13px;}
.anchor .b{font-size:11.5px; color:var(--ink3); line-height:1.6;}
.tag{font-size:10.5px; padding:1px 7px; border-radius:2px;}
.tag.ok{background:#e8f3ee; color:var(--down);}
.tag.no{background:#fbeceb; color:var(--up);}
.verdict{background:var(--card); border:1px solid var(--line); border-top:3px solid var(--accent);
  padding:16px 18px; margin:16px 0;}
.verdict .l{font-size:11px; color:var(--ink3); letter-spacing:.08em;}
.verdict .v{font-size:20px; font-weight:700; margin:4px 0 8px;}
.play{display:grid; gap:9px; margin:12px 0;}
.play .row{background:var(--card); border:1px solid var(--line); padding:12px 13px; font-size:12.6px;}
.play .row b{display:block; margin-bottom:4px; font-size:12.5px;}
.play .row span{color:var(--ink3);}
ul{margin:8px 0 8px 20px;} li{margin:5px 0; color:var(--ink2); font-size:13px;}
.note{font-size:11.5px; color:var(--ink3); line-height:1.7; margin:10px 0;}
.past-bar{display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:-6px 0 20px;
  padding:10px 12px; background:var(--card); border:1px solid var(--line); font-size:12.5px;}
.past-bar .lab{color:var(--ink3); white-space:nowrap;}
.past-bar .sel{position:relative; display:inline-block;}
.past-bar select{appearance:none; -webkit-appearance:none; -moz-appearance:none;
  padding:5px 30px 5px 12px; border:1px solid var(--line); border-radius:14px;
  background:var(--card); font-family:inherit; font-size:12.5px; color:var(--ink2);
  line-height:1.65; cursor:pointer; max-width:min(72vw,420px); text-overflow:ellipsis;}
.past-bar select:hover{border-color:var(--accent); color:var(--accent);}
.past-bar select:focus{outline:none; border-color:var(--accent); box-shadow:0 0 0 2px rgba(31,75,142,.12);}
.past-bar .sel::after{content:""; position:absolute; right:12px; top:50%; width:5px; height:5px;
  border-right:1.6px solid var(--ink3); border-bottom:1.6px solid var(--ink3);
  transform:translateY(-72%) rotate(45deg); pointer-events:none;}
.past-bar .mt{font-size:11px; color:var(--ink3); flex:1 1 200px; min-width:0;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.disc{margin-top:46px; padding-top:14px; border-top:1px solid var(--line);
  font-size:11.5px; color:var(--ink3); line-height:1.75;}
@media (max-width:640px){
  h1{font-size:21px;} .cards{grid-template-columns:repeat(2,1fr);}
  .chain{grid-template-columns:1fr;} .arrow{transform:rotate(90deg); padding:6px 0;}
  .anchors{grid-template-columns:1fr;} table{font-size:12px;}
  th,td{padding:7px 7px;}
}
"""

JS = """
(function(){
  var bar=document.getElementById('pastBar'),sel=document.getElementById('pastSelect'),meta=document.getElementById('pastMeta');
  if(!bar||!sel||!window.PAST||!window.PAST.length) return;
  var base=/\/archive\//.test(location.pathname)?'../':'';
  var rows=window.PAST.slice().sort(function(a,b){return String(b.date).localeCompare(String(a.date))});
  function label(d){
    var w=['周日','周一','周二','周三','周四','周五','周六'],s=String(d||'');
    if(s.length<10) return s;
    var t=new Date(s+'T00:00:00');
    var wd=isNaN(t.getTime())?'':w[t.getDay()];
    return s.slice(5,7)+'月'+s.slice(8,10)+'日\u3000'+wd;
  }
  function hit(){for(var i=0;i<rows.length;i++){if(rows[i].path===sel.value)return rows[i];}return null;}
  sel.innerHTML='';
  rows.forEach(function(row,i){
    var o=document.createElement('option');
    o.value=row.path;
    o.textContent=label(row.date)+(row.title?'\u3000\u00b7\u3000'+row.title:'');
    if(i===0) o.selected=true;
    sel.appendChild(o);
  });
  var r=hit(); if(meta) meta.textContent=r?(r.summary||''):'';
  sel.addEventListener('change',function(){
    var t=hit(); if(t) location.href=base+t.path;
  });
  bar.hidden=false;
})();
function drawCharts(D){
  if(!window.echarts) return;
  var up='#c4362c', dn='#18855a', ink='#55534e', line='#e4e3df';
  function bar(id,data,opt){
    var el=document.getElementById(id); if(!el) return;
    var c=echarts.init(el);
    c.setOption({
      grid:opt.grid, tooltip:{trigger:'axis',axisPointer:{type:'shadow'}},
      xAxis:opt.xAxis, yAxis:opt.yAxis, series:opt.series
    });
    window.addEventListener('resize',function(){c.resize()});
  }
  /* 指数 */
  var iel=document.getElementById('chartIndex');
  if(iel){
    var ic=echarts.init(iel);
    ic.setOption({
      grid:{left:44,right:16,top:18,bottom:26},
      tooltip:{trigger:'axis'},
      xAxis:{type:'category',data:D.idx.map(function(x){return x.name}),
        axisLabel:{fontSize:10,color:ink,interval:0},axisLine:{lineStyle:{color:line}}},
      yAxis:{type:'value',axisLabel:{formatter:'{value}%',fontSize:10,color:ink},
        splitLine:{lineStyle:{color:line}},axisLine:{show:false}},
      series:[{type:'bar',data:D.idx.map(function(x){
        return {value:x.pct,itemStyle:{color:x.pct>=0?up:dn}}}),
        barMaxWidth:30,label:{show:true,position:'top',fontSize:10,
          formatter:function(p){return p.value.toFixed(2)+'%'},
          color:ink}}]
    });
    window.addEventListener('resize',function(){ic.resize()});
  }
  /* 板块 */
  var sel=document.getElementById('chartSector');
  if(sel){
    var sc=echarts.init(sel);
    var S=D.sectors.slice().sort(function(a,b){return a.pct-b.pct});
    sc.setOption({
      grid:{left:88,right:52,top:14,bottom:22},
      tooltip:{trigger:'axis',axisPointer:{type:'shadow'},
        formatter:function(p){return p[0].name+'　'+p[0].value.toFixed(2)+'%'}},
      xAxis:{type:'value',axisLabel:{formatter:'{value}%',fontSize:10,color:ink},
        splitLine:{lineStyle:{color:line}},axisLine:{show:false}},
      yAxis:{type:'category',data:S.map(function(x){return x.name}),
        axisLabel:{fontSize:10,color:ink},axisLine:{lineStyle:{color:line}},axisTick:{show:false}},
      series:[{type:'bar',data:S.map(function(x){
        return {value:x.pct,itemStyle:{color:x.pct>=0?up:dn}}}),
        barMaxWidth:13,
        label:{show:true,position:'right',fontSize:9.5,color:ink,
          formatter:function(p){return p.value.toFixed(2)}}}]
    });
    window.addEventListener('resize',function(){sc.resize()});
  }
  /* 两融 */
  var mel=document.getElementById('chartMargin');
  if(mel){
    var mc=echarts.init(mel);
    mc.setOption({
      grid:{left:56,right:20,top:18,bottom:26},
      tooltip:{trigger:'axis',formatter:function(p){return p[0].name+'<br/>两融余额 '+p[0].value+' 万亿'}},
      xAxis:{type:'category',data:D.margin.map(function(x){return x.date.slice(5)}),
        axisLabel:{fontSize:9.5,color:ink,interval:Math.max(0,Math.floor(D.margin.length/8))},
        axisLine:{lineStyle:{color:line}}},
      yAxis:{type:'value',scale:true,axisLabel:{fontSize:10,color:ink},
        splitLine:{lineStyle:{color:line}},axisLine:{show:false}},
      series:[{type:'line',smooth:true,showSymbol:false,
        data:D.margin.map(function(x){return x.total}),
        lineStyle:{width:2,color:'#1f4b8e'},
        areaStyle:{color:'rgba(31,75,142,.07)'}}]
    });
    window.addEventListener('resize',function(){mc.resize()});
  }
}
"""


def render(data):
    idx = data["idx"]
    breadth = data["breadth"]
    sectors = data["sectors"]
    limits = data["limits"]
    margin = data["margin"]
    anchors = data["anchors"]
    grade, hit = data["grade"], data["hit"]
    holdings = data["holdings"]
    overseas = data["overseas"]
    techs = data["tech"]
    date_cn = data["date_cn"]
    headline = data["headline"]
    mode = data["mode"]

    up, dn, flat = breadth
    total = up + dn + flat
    up_ratio = up / total * 100 if total else 0
    ratio_txt = f"1:{dn / up:.1f}" if up else "—"

    sh = idx[0]
    sz = idx[1]
    two_mkt = (sh["amount"] or 0) + (sz["amount"] or 0)
    two_mkt_prev = data.get("two_mkt_prev")
    if two_mkt_prev:
        vol_chg = (two_mkt / two_mkt_prev - 1) * 100
        vol_txt = f"较前一交易日{'放量' if vol_chg > 0 else '缩量'} {abs(vol_chg):.1f}%"
    else:
        vol_chg, vol_txt = None, ""

    # —— 指数行
    rows_idx = ""
    for x in idx:
        rows_idx += (f'<tr><td>{x["name"]}</td>'
                     f'<td>{x["close"]:,.2f}</td>'
                     f'<td class="{cls(x["pct"])}">{spct(x["pct"])}</td>'
                     f'<td class="{cls(x["chg"])}">{x["chg"]:+.2f}</td>'
                     f'<td>{fmt_money(x["amount"])}</td></tr>')

    # —— 板块行
    rows_sec = ""
    for s in sectors:
        rows_sec += (f'<tr><td>{s["name"]}</td>'
                     f'<td class="{cls(s["pct"])}">{spct(s["pct"])}</td>'
                     f'<td>{s["n"]}</td></tr>')

    # —— 海外行
    rows_os = ""
    for o in overseas:
        rows_os += (f'<tr><td>{o["name"]}</td><td>{o["close"]:,.2f}</td>'
                    f'<td class="{cls(o["pct"])}">{spct(o["pct"])}</td></tr>')

    # —— 均线行
    rows_ma = ""
    for name, t in techs:
        def cell(k):
            v = t.get(k)
            return f'<td class="{cls(v)}">{spct(v, 1)}</td>' if v is not None else '<td>—</td>'
        rows_ma += (f'<tr><td>{name}</td><td>{t["close"]:,.2f}</td>'
                    + cell("d5") + cell("d20") + cell("d60") + cell("d120")
                    + f'<td class="{cls(t["dd"])}">{spct(t["dd"], 1)}</td></tr>')

    # —— 持仓行
    rows_hold = ""
    bench = next((x for x in idx if x["code"] == "000001"), None)
    bench_pct = bench["pct"] if bench else 0
    for h in holdings:
        rel = (h["pct"] - (bench_pct or 0)) if h["pct"] is not None else None
        rows_hold += (f'<tr><td>{h["name"]}<span style="color:#8d8a83;font-size:11px"> {h["code"]}</span></td>'
                      f'<td>{h["close"]:,.2f}</td>'
                      f'<td class="{cls(h["pct"])}">{spct(h["pct"])}</td>'
                      f'<td>{fmt_money(h["amount"])}</td>'
                      f'<td>{h["turnover"]:.2f}%</td>'
                      f'<td>{h["volratio"]:.2f}</td>'
                      f'<td class="{cls(rel)}">{spct(rel, 1)}</td></tr>')

    # 持仓节：发布到公开站点时用 SHOW_HOLDINGS=0 关掉，节编号跟着顺延
    if SHOW_HOLDINGS:
        hold_section = ('<h2>七、持仓跟踪</h2>\n'
                        '<div class="tbl-wrap"><table>\n'
                        '<thead><tr><th>股票</th><th>收盘</th><th>涨跌幅</th><th>成交额</th>'
                        '<th>换手</th><th>量比</th><th>相对上证</th></tr></thead>\n'
                        '<tbody>' + rows_hold + '</tbody></table></div>\n'
                        '<p>' + (data.get("holding_text") or "") + '</p>\n'
                        '<div class="note">只跟踪当日强弱，不用历史记录中的成本价推算盈亏'
                        '——若期间发生除权，旧成本会失效。需要判断止盈止损时，先核对复权后再定基准。</div>\n')
        watch_no = "八"
    else:
        hold_section = ""
        watch_no = "七"

    # —— 锚
    anchor_html = ""
    for name, ok, txt in anchors:
        anchor_html += (f'<div class="anchor {"ok" if ok else "no"}">'
                        f'<div class="h"><span class="n">{name}</span>'
                        f'<span class="tag {"ok" if ok else "no"}">{"到位" if ok else "未到位"}</span></div>'
                        f'<div class="b">{txt}</div></div>')

    # —— 三档应对
    if grade == "防守档":
        plays = [
            ("满仓 / 带杠杆", "反抽即降杠杆。上证回到 3570–3620 一带先把融资仓位砍掉三成以上，不等回本——强平的价格你说了不算。", "风险：反抽不来，被动平在更低的位置。"),
            ("中等仓位", "不动仓位动结构。把纯叙事、无现金流的方向换成有分红有回购的品种，同一个仓位风险敞口能低一档。", "风险：换错方向，两头挨打。"),
            ("低仓 / 空仓", "等到四条锚至少两条到位再动手。现在只做观察，不做预判式加仓。", "风险：底部突然出现时手中无货，但那是机会成本，不是亏损。"),
        ]
    elif grade == "震荡档":
        plays = [
            ("满仓 / 带杠杆", "杠杆降到你能睡着觉的水平，其余仓位不动。四条锚已有两条转好，问题不再是方向而是波动。", "风险：降得太早，反抽时少赚。"),
            ("中等仓位", "拿住抗跌的低估值方向，不加科技仓。等结构锚转正再谈进攻。", "风险：抗跌品种在反弹时弹性不足。"),
            ("低仓 / 空仓", "可以开始分批，单批不超过总资金三分之一，先宽基后行业。", "风险：假突破，需要接受第一笔浮亏。"),
        ]
    else:
        plays = [
            ("满仓 / 带杠杆", "可以保留杠杆，但把成本压到现价之下，别在反弹里加杠杆。", "风险：情绪反复，单日大阴线打回原点。"),
            ("中等仓位", "补回被错杀的方向，优先有订单有业绩的，不追纯概念。", "风险：反弹一日游。"),
            ("低仓 / 空仓", "分批建仓，节奏可以快一点，但仍然分三批。", "风险：追高。"),
        ]
    play_html = "".join(
        f'<div class="row"><b>{a}</b>{b}<br><span>{c}</span></div>' for a, b, c in plays
    )

    # —— 图表数据
    chart_json = json.dumps({
        "idx": [{"name": x["name"], "pct": x["pct"] or 0} for x in idx],
        "sectors": [{"name": s["name"], "pct": s["pct"]} for s in sectors],
        "margin": [{"date": m["date"], "total": round(m["total"] / 1e4, 4)} for m in margin],
    }, ensure_ascii=False)

    past_json = json.dumps(
        [{"date": r["date"], "title": r.get("title", ""), "summary": r.get("summary", ""),
          "path": r.get("path") or ("archive/%s.html" % r["date"])} for r in data.get("past", [])],
        ensure_ascii=False, separators=(",", ":"))

    # —— 关键数字卡
    dt = limits[1]
    zt = limits[0]
    cards = [
        ("上证指数", f'{sh["close"]:,.2f}', f'{spct(sh["pct"])}　成交 {fmt_money(sh["amount"])}',
         cls(sh["pct"])),
        ("创业板指", f'{idx[2]["close"]:,.2f}', f'{spct(idx[2]["pct"])}　成交 {fmt_money(idx[2]["amount"])}',
         cls(idx[2]["pct"])),
        ("中证1000", f'{idx[4]["close"]:,.2f}', spct(idx[4]["pct"]), cls(idx[4]["pct"])),
        ("两市成交", fmt_money(two_mkt), vol_txt or "—", ""),
        ("涨跌家数", f'{up} / {dn}', f'涨跌比 {ratio_txt}　上涨占比 {up_ratio:.1f}%', ""),
        ("涨停 / 跌停", f'{zt if zt is not None else "—"} / {dt if dt is not None else "—"}',
         "东财涨停池 / 跌停池", ""),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{k}</div>'
        f'<div class="v {kc}">{v}</div>'
        f'<div class="s">{s}</div></div>' for k, v, s, kc in cards
    )

    # —— 传导链
    worst_os = min(overseas, key=lambda x: x["pct"] if x["pct"] is not None else 0) if overseas else None
    us_line = "、".join(f'{o["name"]} {spct(o["pct"])}' for o in overseas[:3])
    asia_line = "、".join(f'{o["name"]} {spct(o["pct"])}' for o in overseas[3:6])
    strong_secs = [s for s in sectors if s["name"] in ("半导体", "通信设备", "软件开发", "计算机设备")]
    strong_txt = "、".join(f'{s["name"]} {spct(s["pct"])}' for s in strong_secs) or "—"
    m_latest = margin[-1] if margin else None
    m_delta = data.get("margin_delta")

    # —— 观察清单
    watch = data["watch"]
    watch_html = "".join(f"<li>{w}</li>" for w in watch)

    head_cls = "up" if (sh["pct"] or 0) > 0 else ("down" if (sh["pct"] or 0) < 0 else "")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{date_cn} A股复盘 · {headline}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="kicker">A 股收盘复盘</div>
  <h1>{headline}</h1>
  <div class="sub">{date_cn} · 收盘后 · 数据源：东方财富公开行情接口 · 生成时点 {data["gen_at"]}</div>
</header>

<div class="past-bar" id="pastBar" hidden>
  <span class="lab">往期复盘</span>
  <span class="sel"><select id="pastSelect" aria-label="选择复盘日期"></select></span>
  <span class="mt" id="pastMeta"></span>
</div>

<div class="cards">{cards_html}</div>

<p>今天盘面性质：<strong>{mode}</strong>。有成交的 {total} 只股票里，上涨 {up} 只、下跌 {dn} 只、平盘 {flat} 只，
上涨占比仅 <strong>{up_ratio:.1f}%</strong>。涨停 {zt if zt is not None else "—"} 家、跌停 {dt if dt is not None else "—"} 家。</p>

<h2>一、今日指数</h2>
<div class="tbl-wrap"><table>
<thead><tr><th>指数</th><th>收盘</th><th>涨跌幅</th><th>涨跌点</th><th>成交额</th></tr></thead>
<tbody>{rows_idx}</tbody></table></div>
<div class="chartbox"><div id="chartIndex" class="chart" style="height:250px;"></div>
<div class="cap">指数涨跌幅（%），红涨绿跌。数据源：东方财富，{data["gen_at"]}。</div></div>

<h2>二、盘面质感</h2>
<p>{data["texture"]}</p>

<h2>三、板块强弱</h2>
<div class="chartbox"><div id="chartSector" class="chart" style="height:560px;"></div>
<div class="cap">固定跟踪的 25 个行业（东方财富二级行业分类），<strong>总市值加权</strong>自算，自下而上由弱到强。
口径提示：历史几期用的是申万分类，从本期起统一为东财分类以便与公开接口对齐，数值会与旧报告存在小幅口径差。</div></div>
<div class="tbl-wrap"><table>
<thead><tr><th>行业</th><th>涨跌幅</th><th>成分股数</th></tr></thead>
<tbody>{rows_sec}</tbody></table></div>
<p>{data["sector_text"]}</p>

<h2>四、外围与杠杆：三层传导</h2>
<div class="chain">
  <div class="box"><div class="t">① 海外</div><div class="b">{us_line}<br>{asia_line}</div></div>
  <div class="arrow">→</div>
  <div class="box"><div class="t">② 国内结构</div><div class="b">{strong_txt}<br>科技链均 {data["strong_avg"]:+.2f}%</div></div>
  <div class="arrow">→</div>
  <div class="box"><div class="t">③ 杠杆</div><div class="b">两融余额 {m_latest["total"]/1e4:.4f} 万亿<br>单日 {m_delta:+.0f} 亿</div></div>
</div>
<div class="tbl-wrap"><table>
<thead><tr><th>市场</th><th>收盘</th><th>涨跌幅</th></tr></thead>
<tbody>{rows_os}</tbody></table></div>
<div class="chartbox"><div id="chartMargin" class="chart" style="height:250px;"></div>
<div class="cap">沪深两市融资融券余额（万亿元），近 {len(margin)} 个交易日。区间累计变化 {data["margin_cum"]:+.0f} 亿。
数据源：东方财富数据中心，取最近已公布的交易日。</div></div>
<p>{data["chain_text"]}</p>

<h2>五、位置坐标</h2>
<div class="tbl-wrap"><table>
<thead><tr><th>指数</th><th>收盘</th><th>距MA5</th><th>距MA20</th><th>距MA60</th><th>距MA120</th><th>距60日高点</th></tr></thead>
<tbody>{rows_ma}</tbody></table></div>
<p>{data["position_text"]}</p>
<div class="note">均线距离为收盘价相对该均线的偏离百分比，正值为在均线上方。支撑位是技术参考位，不是预测。</div>

<h2>六、破局判断：四条锚</h2>
<div class="anchors">{anchor_html}</div>
<div class="verdict">
  <div class="l">当前档位</div>
  <div class="v">{grade}　<span style="font-size:13px;color:#8d8a83;font-weight:400">四条锚已到位 {hit} 条</span></div>
  <div style="font-size:12.6px;color:#55534e">{data["verdict_text"]}</div>
</div>
<div class="play">{play_html}</div>

{hold_section}
<h2>{watch_no}、下个交易日先看什么</h2>
<ul>{watch_html}</ul>

<div class="disc">
<strong>免责声明：</strong>以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。
任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。
</div>
</div>
<script>window.PAST = {past_json};</script>
<script src="https://cdn.bootcdn.net/ajax/libs/echarts/5.5.0/echarts.min.js"></script>
<script>{JS}</script>
<script>
var D = {chart_json};
if (document.readyState !== 'loading') {{ drawCharts(D); }}
else {{ document.addEventListener('DOMContentLoaded', function() {{ drawCharts(D); }}); }}
</script>
</body>
</html>"""
    return html


def build_texts(data):
    """把数字翻译成该说的话"""
    idx = data["idx"]
    sectors = data["sectors"]
    breadth = data["breadth"]
    up, dn, flat = breadth
    limits = data["limits"]
    sh = idx[0]
    cy = idx[2]
    zz = idx[4]
    techs = data["tech"]

    # 盘面质感
    parts = []
    parts.append(f'上证收 {sh["close"]:,.2f}（{spct(sh["pct"])}），'
                 f'创业板指 {cy["close"]:,.2f}（{spct(cy["pct"])}），'
                 f'中证1000 {zz["close"]:,.2f}（{spct(zz["pct"])}）。'
                 f'创业板与上证的日内落差 {abs((cy["pct"] or 0) - (sh["pct"] or 0)):.1f} 个百分点。')
    pos = [s for s in sectors if s["pct"] > 0]
    neg = [s for s in sectors if s["pct"] <= -3]
    if pos:
        parts.append("抗跌的是 " + "、".join(f'{s["name"]}（{spct(s["pct"])}）' for s in pos[:3]) + "。")
    if neg:
        parts.append("杀跌端是 " + "、".join(f'{s["name"]}（{spct(s["pct"])}）' for s in sorted(neg, key=lambda x: x["pct"])[:4]) + "。")
    parts.append(f'跌停 {limits[1] if limits[1] is not None else "—"} 家、涨停 {limits[0] if limits[0] is not None else "—"} 家，'
                 f'涨跌比 1:{dn / up:.1f}。' if up else "")
    data["texture"] = "".join(p for p in parts if p)

    # 板块解读
    non_ai = [s for s in sectors if s["name"] in ("光伏设备", "电池", "电网设备", "风电设备",
                                                  "军工电子", "航空装备", "房地产开发", "汽车零部件")]
    non_ai_deep = [s for s in non_ai if s["pct"] <= -3]
    st = []
    if non_ai_deep:
        names = "、".join("%s（%s）" % (s["name"], spct(s["pct"])) for s in non_ai_deep[:4])
        st.append("这一点必须点出来：跌得狠的不只是 AI 硬件。" + names +
                  " 与算力叙事没有直接关系，照样跌 3% 以上。")
        st.append('说明这是<strong>整体风险偏好在收缩</strong>（贴现率与风险溢价同时上行），不是资金在板块之间调仓。'
                  '两者的应对完全不同：调仓换方向就行，风险偏好收缩只能降敞口。')
    else:
        st.append("非 AI 方向相对抗跌，跌幅主要集中在科技链，更像是板块间的资金再平衡，而不是全面撤退。")
    data["sector_text"] = "".join(st)

    # 传导链
    m = data["margin"]
    data["chain_text"] = (
        f'链条的第一环在海外：{data["os_line"]}。'
        f'第二环落到国内结构：科技链（{data["strong_avg"]:+.2f}%）继续被压。'
        f'第三环是杠杆：两融余额 {m[-1]["total"]/1e4:.4f} 万亿，单日 {data["margin_delta"]:+.0f} 亿，'
        f'近 {len(m)} 个交易日累计 {data["margin_cum"]:+.0f} 亿。'
        '指数跌幅与两融降幅的比值，决定了这一轮下跌里有多少是"看空"，有多少是"不得不卖"。'
    )

    # 位置
    t_sh = dict((n, t) for n, t in techs)["上证指数"]
    pos_txt = (f'上证距 5 日线 {spct(t_sh["d5"], 1)}、距 20 日线 {spct(t_sh["d20"], 1)}、'
               f'距 60 日线 {spct(t_sh["d60"], 1)}、距 120 日线 {spct(t_sh["d120"], 1)}，'
               f'距 60 日高点 {spct(t_sh["dd"], 1)}。')
    t_cy = dict((n, t) for n, t in techs)["创业板指"]
    pos_txt += (f'创业板指距 60 日高点 {spct(t_cy["dd"], 1)}、距 120 日线 {spct(t_cy["d120"], 1)}，'
                f'弹性比上证大，回撤也更深。')
    data["position_text"] = pos_txt

    # 结论
    anchors = data["anchors"]
    miss = [a[0] for a in anchors if not a[1]]
    if data["grade"] == "防守档":
        data["verdict_text"] = (
            f'四条锚里 {len(miss)} 条仍未到位（{"、".join(miss)}）。'
            '这意味着下跌的驱动因素一个都没解除，此时任何抄底都是在赌"消息面突然转向"，不是在赌估值。'
            '急跌之后 3–5 个交易日出现技术性反抽是常态，但反抽的高度受制于 5 日与 10 日均线，过不去就是逃命波。'
        )
    elif data["grade"] == "震荡档":
        data["verdict_text"] = (
            f'已有 {data["hit"]} 条锚到位，剩下 {"、".join(miss)} 未解除。'
            '可以从"只防守"切换到"边防守边观察"，但还不能进攻——真正的转折需要四条同时松动。'
        )
    else:
        data["verdict_text"] = (
            f'四条锚里 {data["hit"]} 条到位，驱动的反转正在形成。'
            '这时可以开始建仓，但节奏仍然分三批，别一次打完。'
        )

    # 持仓点评
    hs = data["holdings"]
    ht = []
    bp = sh["pct"] or 0
    sync = [h for h in hs if h["pct"] is not None and abs(h["pct"] - bp) <= 0.5]
    win = [h for h in hs if h["pct"] is not None and h["pct"] - bp > 0.5]
    lose = [h for h in hs if h["pct"] is not None and bp - h["pct"] > 0.5]
    if win:
        ht.append("明显强于上证的是 " + "、".join(
            "%s（%s，超额 %.1f 个百分点）" % (h["name"], spct(h["pct"]), h["pct"] - bp) for h in win) + "。")
    if sync:
        ht.append("与上证基本同步的是 " + "、".join(
            "%s（%s）" % (h["name"], spct(h["pct"])) for h in sync) + "。")
    if lose:
        ht.append("明显跑输的是 " + "、".join(
            "%s（%s，落后 %.1f 个百分点）" % (h["name"], spct(h["pct"]), bp - h["pct"]) for h in lose) + "。")
    ht.append("相对强弱只是一天的切片，别用单日排名做加减仓决定。")
    data["holding_text"] = "".join(ht)

    # 观察清单
    w = []
    w.append(f'开盘半小时看创业板能否站上 {cy["close"]:,.0f}（今日收盘位），站不上说明反抽只是修复。')
    w.append(f'看跌停家数能否从 {limits[1] if limits[1] is not None else "—"} 家收到 20 家以内——这是情绪锚唯一的量化出口。')
    w.append('看两融数据（次日公布 T-1）单日降幅是否收敛到 50 亿内，若转增，杠杆锚先转好。')
    if data["overseas"]:
        w.append(f'隔夜看纳斯达克与{data["overseas"][3]["name"] if len(data["overseas"]) > 3 else "亚洲市场"}，'
                 '海外不再破位是国内企稳的前提。')
    data["watch"] = w
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    print(f"[1/8] 时间基准 {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # 指数
    idx = fetch_indices()
    print(f"[2/8] 指数 {len(idx)} 个：{idx[0]['name']} {idx[0]['close']} {idx[0]['pct']}%")

    # 交易日校验
    sh_k = fetch_kline("1.000001", 140)
    if not sh_k:
        print("✗ 拿不到上证 K 线，退出")
        return 2
    last_date = sh_k[-1]["date"]
    if last_date != today and not args.force:
        print(f"· 休市（最新交易日 {last_date}，今天 {today}），跳过")
        return 0

    # 全 A
    rows_a, total_a = fetch_all_a()
    print(f"[3/8] 全 A {len(rows_a)} / {total_a}")
    up, dn, flat = compute_breadth(rows_a)
    print(f"      上涨 {up} 下跌 {dn} 平 {flat}")

    # 板块
    all_sec = compute_sectors(rows_a)
    sectors = pick_sectors(all_sec)
    print(f"[4/8] 板块 {len(sectors)} 个：{sectors[0]['name']} {sectors[0]['pct']}% … {sectors[-1]['name']} {sectors[-1]['pct']}%")

    # 涨跌停
    limits = fetch_limits(last_date.replace("-", ""))
    print(f"[5/8] 涨停 {limits[0]} 跌停 {limits[1]}")

    # 两融
    margin = fetch_margin(60)
    if len(margin) < 2:
        print("✗ 两融数据不足")
        return 2
    margin_delta = margin[-1]["total"] - margin[-2]["total"]
    margin_cum = margin[-1]["total"] - margin[0]["total"]
    print(f"[6/8] 两融 {margin[-1]['total']/1e4:.4f} 万亿，单日 {margin_delta:+.1f} 亿")

    # 海外
    overseas = fetch_overseas()

    # 均线
    techs = []
    for key, label in (("1.000001", "上证指数"), ("0.399006", "创业板指"), ("1.000852", "中证1000")):
        k = fetch_kline(key, 140)
        techs.append((label, tech(k)))

    # 持仓
    holdings = fetch_holdings()

    # 昨日两市成交额（用于判断放量/缩量）
    two_mkt_prev = None
    try:
        sz_k = fetch_kline("0.399001", 5)
        if len(sh_k) >= 2 and len(sz_k) >= 2:
            p1 = (sh_k[-2]["amount"] or 0) + (sz_k[-2]["amount"] or 0)
            two_mkt_prev = p1 if p1 > 0 else None
    except Exception:  # noqa: BLE001
        two_mkt_prev = None

    data = {
        "idx": idx,
        "breadth": (up, dn, flat),
        "sectors": sectors,
        "limits": limits,
        "margin": margin,
        "margin_delta": margin_delta,
        "margin_cum": margin_cum,
        "overseas": overseas,
        "tech": techs,
        "holdings": holdings,
        "two_mkt_prev": two_mkt_prev,
        "date_cn": now.strftime("%Y年%-m月%-d日") if os.name != "nt" else now.strftime("%Y年%m月%d日"),
        "gen_at": now.strftime("%H:%M"),
    }
    # 星期
    wd = "一二三四五六日"[now.weekday()]
    data["date_cn"] = f'{now.year}年{now.month}月{now.day}日（周{wd}）'

    headline, mode = qualitative(idx, (up, dn, flat), sectors, limits)
    data["headline"], data["mode"] = headline, mode

    data["os_line"] = "、".join(f'{o["name"]} {spct(o["pct"])}' for o in overseas[:3])
    strong_secs = [s for s in sectors if s["name"] in ("半导体", "通信设备", "软件开发", "计算机设备", "元件")]
    data["strong_avg"] = mean([s["pct"] for s in strong_secs]) if strong_secs else 0.0

    data["anchors"] = anchor_state(idx, overseas, sectors, limits, margin_delta, (up, dn, flat))
    grade, hit = stance(data["anchors"])
    data["grade"], data["hit"] = grade, hit

    data = build_texts(data)
    print(f"[7/8] 定性：{headline}｜档位：{grade}（{hit}/4 到位）")

    out = args.out
    os.makedirs(os.path.join(out, "archive"), exist_ok=True)

    # 先把往期索引整理好，再渲染——下拉框的数据要在生成时就写进页面里
    issues_path = os.path.join(out, "issues.json")
    issues = []
    if os.path.exists(issues_path):
        try:
            issues = json.load(open(issues_path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            issues = []
    summary = (f'上证 {spct(idx[0]["pct"])}，创业板 {spct(idx[2]["pct"])}，'
               f'中证1000 {spct(idx[4]["pct"])}，跌停 {limits[1] if limits[1] is not None else "—"} 家')
    issues = [x for x in issues if x.get("date") != today]
    issues.append({"date": today, "title": headline, "summary": summary,
                   "path": f"archive/{today}.html"})
    issues.sort(key=lambda x: x["date"], reverse=True)
    issues = issues[:120]
    data["past"] = issues

    html = render(data)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(out, "archive", f"{today}.html"), "w", encoding="utf-8") as f:
        f.write(html)

    with open(issues_path, "w", encoding="utf-8") as f:
        json.dump(issues, f, ensure_ascii=False, indent=1)

    print(f"[8/8] 已写入 {out}/index.html、{out}/archive/{today}.html、issues.json（往期 {len(issues)} 期）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
