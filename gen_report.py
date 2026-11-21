#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股收盘复盘 · 云端生成器
数据源：东方财富 / 腾讯 / 新浪的公开行情接口（无需任何密钥 / 账号）
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
import socket
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
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

def _load_holdings():
    """持仓从同目录的 holdings.json 读取，该文件不进仓库（.gitignore 已排除）。

    这样公开仓库里的源码不会出现任何个股。文件格式：
        [{"secid": "1.600415", "code": "600415", "name": "小商品城", "cost": 11.5}, ...]
    cost 可省略；给了就在推送摘要里一并算出浮动盈亏。
    文件不存在时返回空列表，页面自动省掉持仓那一节。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "holdings.json")
    if not os.path.exists(path):
        return []
    try:
        rows = json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        code = str(r.get("code") or "").strip()
        name = str(r.get("name") or "").strip()
        secid = str(r.get("secid") or "").strip()
        if not (code and name):
            continue
        if not secid:
            secid = ("1." if code.startswith(("6", "5", "9")) else "0.") + code
        try:
            cost = float(r.get("cost")) if r.get("cost") not in (None, "") else None
        except (TypeError, ValueError):
            cost = None
        out.append((secid, code, name, cost))
    return out


HOLDINGS = _load_holdings()

# 持仓节是否写进页面：既要开关打开，也要确实有持仓数据
SHOW_HOLDINGS = (os.environ.get("SHOW_HOLDINGS", "1").strip()
                 not in ("0", "false", "False", "")) and bool(HOLDINGS)

OVERSEAS = [
    ("100.DJIA", "道琼斯"),
    ("100.NDX", "纳斯达克"),
    ("100.SPX", "标普500"),
    ("100.TWII", "中国台湾加权"),
    ("100.N225", "日经225"),
    ("100.KS11", "韩国KOSPI"),
    ("100.HSI", "恒生指数"),
]

# 固定跟踪的行业：申万一级（东方财富板块指数）。
# 用 BK 代码走 ulist 批量查询，两次请求就能拿全，不依赖 clist 分页
# （东财对云端 IP 的 clist 分页限制极严，60 页根本拉不完）。
# 口径说明：板块指数本身是板块内成分股等权，与「全 A 总市值加权」不同，
# 银行这类大小票分化的板块会被拉平，读的时候要记住这一点。
SECTORS_L1 = [
    ("BK0433", "农林牧渔"), ("BK1206", "基础化工"), ("BK0479", "钢铁"),
    ("BK0478", "有色金属"), ("BK1201", "电子"), ("BK0456", "家用电器"),
    ("BK0438", "食品饮料"), ("BK0436", "纺织服饰"), ("BK1212", "轻工制造"),
    ("BK1216", "医药生物"), ("BK0427", "公用事业"), ("BK1210", "交通运输"),
    ("BK1202", "房地产"), ("BK1213", "商贸零售"), ("BK1214", "社会服务"),
    ("BK1283", "银行"), ("BK1203", "非银金融"), ("BK1217", "综合"),
    ("BK1208", "建筑材料"), ("BK1209", "建筑装饰"), ("BK1200", "电力设备"),
    ("BK1205", "机械设备"), ("BK1204", "国防军工"), ("BK1207", "计算机"),
    ("BK0486", "传媒"), ("BK1215", "通信"), ("BK0437", "煤炭"),
    ("BK0464", "石油石化"), ("BK0728", "环保"), ("BK1035", "美容护理"),
]

# 科技链的代表行业（用于「是不是只有 AI 硬件在跌」这个判断）
TECH_NAMES = ("电子", "计算机", "通信")

SECTOR_ALIAS = {}


# ── HTTP ───────────────────────────────────────────────────────
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 优先走 IPv4：本机的 IPv6 出口到东财会被直接断连（curl -4 通、默认不通）。
_ORIG_GAI = socket.getaddrinfo


def _gai_ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
    if family in (0, socket.AF_UNSPEC):
        try:
            v4 = _ORIG_GAI(host, port, socket.AF_INET, type, proto, flags)
            try:
                v6 = _ORIG_GAI(host, port, socket.AF_INET6, type, proto, flags)
            except socket.gaierror:
                v6 = []
            return v4 + v6
        except socket.gaierror:
            pass
    return _ORIG_GAI(host, port, family, type, proto, flags)


socket.getaddrinfo = _gai_ipv4_first


_LAST_CALL = [0.0]
_MIN_GAP = 4.0  # 全局最小请求间隔：实测东财每窗口只放行 ~6 个请求，慢一点更稳

# 响应缓存：东财按"单位时间请求数"封禁，本地反复调试很容易撞线。
# 同一个 URL 当天成功过就复用，重跑只看新增的请求。
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
_CACHE_ON = os.environ.get("NO_CACHE", "") in ("", "0", "false", "False")


def _cache_path(url):
    import hashlib
    return os.path.join(_CACHE_DIR, hashlib.sha1(url.encode("utf-8")).hexdigest() + ".json")


def get_json(url, tries=5, timeout=25, referer="https://quote.eastmoney.com/"):
    """显式不走系统代理，带节流与当日缓存。

    东财对代理出口 IP 有很重的限流，走代理会被整站断连；直连反而正常。
    云端（Actions）本来没有代理，这些处理等于没写。
    """
    cp = _cache_path(url)
    if _CACHE_ON and os.path.exists(cp):
        try:
            with open(cp, "r", encoding="utf-8") as f:
                hit = json.load(f)
            if (hit.get("day") == datetime.now(TZ).strftime("%Y-%m-%d")
                    and hit.get("data") is not None):
                return hit["data"]
        except Exception:  # noqa: BLE001
            pass

    last = None
    for i in range(tries):
        gap = time.time() - _LAST_CALL[0]
        if gap < _MIN_GAP:
            time.sleep(_MIN_GAP - gap)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": referer,
                "Accept": "*/*",
            })
            with _OPENER.open(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            data = json.loads(raw)
            if _CACHE_ON:
                try:
                    os.makedirs(_CACHE_DIR, exist_ok=True)
                    with open(cp, "w", encoding="utf-8") as f:
                        json.dump({"day": datetime.now(TZ).strftime("%Y-%m-%d"),
                                   "url": url, "data": data}, f, ensure_ascii=False)
                except Exception:  # noqa: BLE001
                    pass
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            # 被 WAF 断连时要歇久一点，短重试基本无效
            time.sleep(15.0 * (i + 1))
        finally:
            _LAST_CALL[0] = time.time()
    raise RuntimeError(f"请求失败 {url[:90]} → {last}")


def get_text(url, referer="https://gu.qq.com/", encoding="utf-8",
             tries=3, timeout=20):
    """取纯文本（腾讯行情是 GBK，新浪也是）"""
    last = None
    for i in range(tries):
        gap = time.time() - _LAST_CALL[0]
        if gap < _MIN_GAP:
            time.sleep(_MIN_GAP - gap)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": referer,
                "Accept": "*/*",
            })
            with _OPENER.open(req, timeout=timeout) as r:
                return r.read().decode(encoding, "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2.0 * (i + 1))
        finally:
            _LAST_CALL[0] = time.time()
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
# ── 备用数据源 ──────────────────────────────────────────────────
# 东财 push2 域名对单个出口 IP 的限流极激进：连续几十次请求就会整域断连，
# 指数、板块、涨跌家数、资金流全在这一个域名上，一封就什么都取不到。
# 下面这组备用源只在主接口失败时启用——宁可某节降级，也不让整份报告出不来。
QQ_SECID = {
    "1.000001": "sh000001", "0.399001": "sz399001", "0.399006": "sz399006",
    "1.000688": "sh000688", "1.000852": "sh000852", "0.899050": "bj899050",
}
QQ_SECTOR_FLOW = {}   # 走腾讯兜底时顺手拿到的行业主力净额（元）
SECTOR_SOURCE = "东财行业板块指数（板块内等权）"
BREADTH_SOURCE = "三市指数成分统计"


def fetch_indices_qq():
    """腾讯 qt.gtimg.cn：一次拿到全部指数的现价、涨跌、成交额"""
    codes = [QQ_SECID.get(s) for s, _ in INDICES]
    if not all(codes):
        return []
    txt = get_text("https://qt.gtimg.cn/q=" + ",".join(codes), encoding="gbk")
    by = {}
    for line in txt.strip().split("\n"):
        if '="' not in line:
            continue
        key = line.split("=", 1)[0].replace("v_", "").strip()
        parts = line.split('="', 1)[1].rstrip('";').split("~")
        if len(parts) < 38:
            continue
        by[key] = {
            "code": parts[2],
            "name": parts[1],
            "close": num(parts[3]),
            "chg": num(parts[31]),
            "pct": num(parts[32]),
            "amount": (num(parts[37]) or 0.0) * 1e4,
        }
    out = []
    for secid, label in INDICES:
        r = by.get(QQ_SECID.get(secid))
        if r and r["close"] is not None:
            r = dict(r)
            r["name"] = label
            out.append(r)
    return out


def fetch_sectors_qq():
    """腾讯行业板块排行：一次拿到 30+ 个一级行业的涨跌幅与主力净额

    zdf 是板块涨跌幅，zljlr 是主力净流入（单位万元）。这两个字段正好
    对上页面要的两个数，所以兜底时一次请求就够，不用再逐个查。
    """
    url = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=hy"
           "&sort_type=price&direct=down&offset=0&count=40&type=1")
    j = get_json(url, referer="https://gu.qq.com/", tries=3, timeout=20)
    rows = ((j.get("data") or {}).get("rank_list")) or []
    names = {n for _c, n in SECTORS_L1}
    out = []
    for r in rows:
        name = str(r.get("name") or "").strip()
        pct = num(r.get("zdf"))
        if name not in names or pct is None:
            continue
        out.append({"name": name, "pct": round(pct, 2), "n": 0, "amount": None})
        v = num(r.get("zljlr"))
        if v is not None:
            QQ_SECTOR_FLOW[name] = v * 1e4
    global SECTOR_SOURCE
    SECTOR_SOURCE = "腾讯行业板块指数（腾讯口径，含汽车共 31 个一级行业）"
    print("      （腾讯行业板块兜底，取到 %d / %d 个）" % (len(out), len(SECTORS_L1)))
    out.sort(key=lambda x: -x["pct"])
    return out


def fetch_breadth_zdfb(date_str):
    """涨跌家数兜底：东财涨跌分布（push2ex 域名，和 push2 不共用）

    返回值是按整百分点分档的家数，正档加总=上涨、负档加总=下跌。
    与精确的 f104/f105/f106 会有几十家的误差（分档取整导致），
    但趋势完全一致，够用来判断情绪。
    """
    url = ("https://push2ex.eastmoney.com/getTopicZDFenBu"
           "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&date=" + date_str)
    j = get_json(url, tries=3, timeout=15)
    items = (j.get("data") or {}).get("fenbu") or []
    up = dn = flat = 0
    for item in items:
        for k, v in (item or {}).items():
            try:
                bucket = int(float(k))
                cnt = int(v or 0)
            except (TypeError, ValueError):
                continue
            if bucket > 0:
                up += cnt
            elif bucket < 0:
                dn += cnt
            else:
                flat += cnt
    return (up, dn, flat) if (up + dn) else None


def fetch_overseas_sina():
    """海外指数兜底：新浪（美股、港股直接给点位和涨跌幅）"""
    sina = {
        "100.DJIA": "int_dji", "100.NDX": "int_nasdaq", "100.SPX": "int_sp500",
        "100.HSI": "rt_hkHSI", "100.N225": "int_nikkei",
        "100.KS11": "znb_kospi", "100.TWII": "znb_twii",
    }
    keys = [sina[s] for s, _ in OVERSEAS if s in sina]
    txt = get_text("https://hq.sinajs.cn/list=" + ",".join(keys),
                   referer="https://finance.sina.com.cn", encoding="gbk")
    by = {}
    for line in txt.strip().split("\n"):
        if '="' not in line:
            continue
        key = line.split("=", 1)[0].replace("var hq_str_", "").strip()
        parts = line.split('="', 1)[1].rstrip('";').split(",")
        by[key] = parts
    out = []
    for secid, label in OVERSEAS:
        s = sina.get(secid)
        p = by.get(s) if s else None
        if not p or len(p) < 4:
            continue
        if s == "rt_hkHSI":
            close, pct = num(p[6]), num(p[8])
        else:
            close, pct = num(p[1]), num(p[3])
        if close is None:
            continue
        out.append({"name": label, "close": close, "pct": pct})
    return out


def fetch_kline_qq(secid, limit=140):
    """K 线兜底：腾讯前复权日线"""
    code = QQ_SECID.get(secid)
    if not code:
        return []
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
           "%s,day,,,%d,qfq" % (code, limit))
    j = get_json(url, referer="https://gu.qq.com/", tries=3, timeout=20)
    d = (j.get("data") or {}).get(code) or {}
    rows = d.get("qfqday") or d.get("day") or []
    out = []
    for r in rows:
        if len(r) < 3:
            continue
        c = num(r[2])
        if c is None:
            continue
        out.append({"date": r[0], "close": c, "amount": None})
    return out


def fetch_indices():
    secids = ",".join(s for s, _ in INDICES)
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
           f"&secids={secids}&fields=f2,f3,f4,f6,f12,f14")
    try:
        j = get_json(url, tries=1, timeout=12)
        by_code = {}
        for row in (j.get("data") or {}).get("diff") or []:
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
        if len(out) == len(INDICES):
            return out
    except Exception:  # noqa: BLE001
        pass
    print("      ! 东财指数接口不可用，改走腾讯")
    return fetch_indices_qq()


def fetch_all_a():
    """全 A 股（含北交所），用于行业市值加权。

    东方财富这边的限制比想象中严：单页条数超过约 100 会直接断连，
    同一 IP 并发稍高就回 502。所以策略是——
      1) 先探第一页拿到总数，算出要翻几页；
      2) 并发压到 2，每轮之间留间隔；
      3) 第一轮没收齐的页，第二轮串行慢慢补。
    仍然收不齐就交给调用方降级，不让整份报告失败。
    """
    base = ("https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
            "&fltt=2&invt=2&fid=f12"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            "&fields=f12,f14,f3,f20,f100")

    def one(pn, tries=2, timeout=12):
        try:
            j = get_json(base.format(pn=pn), tries=tries, timeout=timeout)
            d = j.get("data") or {}
            return pn, (d.get("diff") or []), d.get("total")
        except Exception:  # noqa: BLE001
            return pn, None, None

    # 1) 探第一页，确定总页数
    _, diff0, total = one(1, tries=3, timeout=15)
    if diff0 is None:
        print("      ! 首页就取不到，跳过全 A")
        return [], 0, 99
    if not diff0:
        return [], 0, 0

    got = {1: diff0}
    npages = max(1, (int(total or len(diff0)) + 99) // 100)
    npages = min(npages, 80)

    # 2) 并发 2 逐轮拉
    rest = [p for p in range(2, npages + 1)]
    for i in range(0, len(rest), 2):
        batch = rest[i:i + 2]
        with ThreadPoolExecutor(max_workers=2) as ex:
            for pn, diff, tot in ex.map(one, batch):
                if diff is not None:
                    got[pn] = diff
                    if tot and not total:
                        total = tot
        time.sleep(0.2)

    # 3) 补拉漏掉的页，串行、慢一点；总时长封顶，避免把工作流拖到超时
    missing = [p for p in range(1, npages + 1) if p not in got]
    if missing:
        print("      补拉 %d 页" % len(missing))
        t0 = time.time()
        for pn in missing:
            if time.time() - t0 > 330:
                print("      ! 补拉已用 5.5 分钟，剩下的不追了")
                break
            _, diff, tot = one(pn, tries=1, timeout=12)
            if diff is not None:
                got[pn] = diff
                if tot and not total:
                    total = tot
            time.sleep(0.45)

    allrows = []
    for pn in sorted(got):
        allrows.extend(got[pn])
    failed = [p for p in range(1, npages + 1) if p not in got]
    if failed:
        print("      ! %d 页失败：%s" % (len(failed), str(failed[:12])[1:-1]))
    return allrows, (total or len(allrows)), len(failed)


def fetch_breadth_fast(date_str=None):
    """全市场涨跌家数。

    首选：上证指数、深证综指、北证50 的成分股分别覆盖沪、深、北三市全部 A 股，
    它们的 f104/f105/f106 就是各自市场的涨/跌/平家数，三项相加即全市场，
    一次请求就能拿到，不必依赖全 A 分页。
    兜底：东财涨跌分布（push2ex 域名），按整百分点分档汇总。
    """
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
           "&fields=f12,f14,f104,f105,f106"
           "&secids=1.000001,0.399106,0.899050")
    try:
        j = get_json(url, tries=1, timeout=12)
        up = dn = flat = 0
        got = False
        for d in (j.get("data") or {}).get("diff") or []:
            a, b, c = num(d.get("f104")), num(d.get("f105")), num(d.get("f106"))
            if a is None and b is None:
                continue
            got = True
            up += int(a or 0)
            dn += int(b or 0)
            flat += int(c or 0)
        if got:
            return (up, dn, flat)
    except Exception:  # noqa: BLE001
        pass
    if date_str:
        try:
            r = fetch_breadth_zdfb(date_str)
            if r:
                global BREADTH_SOURCE
                BREADTH_SOURCE = "东财涨跌分布（±整百分点分档汇总）"
                print("      （涨跌家数改走涨跌分布，按整百分点分档汇总）")
                return r
        except Exception:  # noqa: BLE001
            pass
    return None


def fetch_sector_boards():
    """申万一级行业的涨跌幅，走 ulist 按 BK 代码批量取。

    分两批发（15 个一批），每批一次请求。ulist 这个接口在云端 IP 上验证过很稳
    （连发 8 次全成功），比 clist 分页可靠得多。被封时退到腾讯行业排行。
    """
    got = {}
    try:
        for i in range(0, len(SECTORS_L1), 15):
            chunk = SECTORS_L1[i:i + 15]
            secids = ",".join("90." + c for c, _ in chunk)
            url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
                   "&fields=f2,f3,f6,f12,f14&secids=" + secids)
            j = get_json(url, tries=1, timeout=12)
            for r in (j.get("data") or {}).get("diff") or []:
                code = str(r.get("f12") or "").strip()
                pct = num(r.get("f3"))
                if code and pct is not None:
                    got[code] = (pct, num(r.get("f6")))
    except Exception:  # noqa: BLE001
        got = {}

    if len(got) < 20:
        print("      ! 东财板块接口不可用，改走腾讯")
        return fetch_sectors_qq()

    out = []
    for code, name in SECTORS_L1:
        v = got.get(code) or got.get("BK" + code.replace("BK", ""))
        if not v:
            continue
        out.append({"name": name, "pct": round(v[0], 2), "n": 0, "amount": v[1]})
    print("      板块取到 %d / %d 个" % (len(out), len(SECTORS_L1)))
    out.sort(key=lambda x: -x["pct"])
    return out


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
    """（保留给「全 A 自算」那条路，目前主路径走 fetch_sector_boards）"""
    idx = {s["name"]: s for s in all_sectors}
    picked = []
    for _code, name in SECTORS_L1:
        s = idx.get(name)
        if not s:
            continue
        picked.append({"name": name, "pct": s["pct"], "n": s["n"], "mcap": s["mcap"]})
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
    out = []
    try:
        j = get_json(url, tries=1, timeout=12)
        k = ((j.get("data") or {}).get("klines")) or []
        for line in k:
            parts = line.split(",")
            if len(parts) < 2:
                continue
            c = num(parts[1])
            if c is None:
                continue
            amt = num(parts[2]) if len(parts) > 2 else None
            out.append({"date": parts[0], "close": c, "amount": amt})
    except Exception:  # noqa: BLE001
        out = []
    if not out:
        print("      ! 东财 K 线不可用，改走腾讯")
        out = fetch_kline_qq(secid, limit)
    return out


def fetch_holdings():
    if not HOLDINGS:
        return []
    out = _holdings_em()
    if out:
        return out
    print("      ! 东财持仓接口不可用，改走腾讯")
    return _holdings_qq()


def _holdings_em():
    secids = ",".join(s for s, _, _, _ in HOLDINGS)
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
           f"&secids={secids}&fields=f2,f3,f6,f8,f10,f12,f14")
    j = None
    try:
        j = get_json(url, tries=1, timeout=12)
    except Exception:  # noqa: BLE001
        return []
    by_code = {}
    for row in (j.get("data") or {}).get("diff") or []:
        by_code[row.get("f12")] = row
    out = []
    for _, code, name, cost in HOLDINGS:
        row = by_code.get(code) or {}
        out.append({
            "code": code,
            "name": name,
            "close": num(row.get("f2")),
            "pct": num(row.get("f3")),
            "amount": num(row.get("f6")),
            "turnover": num(row.get("f8")),
            "volratio": num(row.get("f10")),
            "cost": cost,
        })
    # 一条都没取到就当作失败，交给腾讯兜底
    if all(x["close"] is None for x in out):
        return []
    return out


def _holdings_qq():
    """腾讯兜底：一次请求拿全部持仓的现价与涨跌幅。成交额字段是万元。"""
    codes = [("sh" if s.startswith("1.") else "sz") + c for s, c, _, _ in HOLDINGS]
    try:
        txt = get_text("https://qt.gtimg.cn/q=" + ",".join(codes), encoding="gbk")
    except Exception as e:  # noqa: BLE001
        print("      ! 腾讯持仓兜底失败：", e)
        return []
    by = {}
    for line in txt.strip().split("\n"):
        if '="' not in line:
            continue
        parts = line.split('="', 1)[1].rstrip('";').split("~")
        if len(parts) < 38:
            continue
        by[parts[2]] = parts
    out = []
    for _, code, name, cost in HOLDINGS:
        p = by.get(code)
        if not p:
            out.append({"code": code, "name": name, "close": None, "pct": None,
                        "amount": None, "turnover": None, "volratio": None, "cost": cost})
            continue
        out.append({
            "code": code,
            "name": name,
            "close": num(p[3]),
            "pct": num(p[32]),
            "amount": (num(p[37]) or 0) * 1e4,
            "turnover": num(p[38]),
            "volratio": None,
            "cost": cost,
        })
    return out


def fetch_overseas():
    secids = ",".join(s for s, _ in OVERSEAS)
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
           f"&secids={secids}&fields=f2,f3,f12,f14")
    try:
        j = get_json(url, tries=1, timeout=12)
        order = {s: i for i, (s, _) in enumerate(OVERSEAS)}
        rows = (j.get("data") or {}).get("diff") or []
        rows.sort(key=lambda r: order.get("100." + str(r.get("f12")), 99))
        out = []
        for r in rows:
            out.append({"name": r.get("f14"), "close": num(r.get("f2")),
                        "pct": num(r.get("f3"))})
        if len(out) >= 4:
            return out
    except Exception:  # noqa: BLE001
        pass
    print("      ! 东财海外接口不可用，改走新浪")
    return fetch_overseas_sina()


# ── 资金流向 ────────────────────────────────────────────────────
# 东财 fflow/kline：分钟级资金流，最后一条（15:00）就是全天累计。
# 字段顺序：f51 时间, f52 主力, f53 小单, f54 中单, f55 大单, f56 超大单；
# 恒等式：主力 = 大单 + 超大单；四档相加 = 0（有人买必有人卖）。
def _flow_parse(kline):
    p = kline.split(",")
    if len(p) < 6:
        return None
    return {
        "time": p[0],
        "main": num(p[1]) or 0.0,
        "sm": num(p[2]) or 0.0,
        "md": num(p[3]) or 0.0,
        "lg": num(p[4]) or 0.0,
        "xl": num(p[5]) or 0.0,
    }


def fetch_flow_one(secid="1.000001", secid2=None, tries=1, timeout=12):
    """单个标的的当日资金流（全天累计）"""
    url = ("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
           "?lmt=0&klt=1&secid=" + secid + (("&secid2=" + secid2) if secid2 else "")
           + "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56")
    j = get_json(url, tries=tries, timeout=timeout)
    k = (j.get("data") or {}).get("klines") or []
    return _flow_parse(k[-1]) if k else None


def fetch_flow():
    """大盘资金流：沪 + 深分开取，再合并。

    分钟接口允许传 secid2 一次拿两市合计，但分市场拆开看更有用
    （能看出是沪市失血还是深市失血），所以取两次自己加。
    push2 被限流时改从 push2his 取（两个域名是分开限的）。
    """
    def _one(secid):
        try:
            return fetch_flow_one(secid)
        except Exception:  # noqa: BLE001
            return None

    sh, sz = _one("1.000001"), _one("0.399001")
    if not sh and not sz:
        print("      ! 东财分钟资金流不可用，改走 push2his 日线")
        return fetch_flow_his()
    sh = sh or {k: 0.0 for k in ("main", "sm", "md", "lg", "xl")}
    sz = sz or {k: 0.0 for k in ("main", "sm", "md", "lg", "xl")}
    tot = {k: sh[k] + sz[k] for k in ("main", "sm", "md", "lg", "xl")}
    tot["sh"] = sh
    tot["sz"] = sz
    return tot


def fetch_flow_his():
    """资金流兜底：push2his 上的分钟资金流，最后一条即收盘全天累计"""
    def one(secid):
        url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get"
               "?lmt=0&klt=1&secid=" + secid
               + "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56")
        j = get_json(url, tries=2, timeout=15)
        k = (j.get("data") or {}).get("klines") or []
        return _flow_parse(k[-1]) if k else None

    sh = sz = None
    for secid, tag in (("1.000001", "sh"), ("0.399001", "sz")):
        try:
            v = one(secid)
        except Exception:  # noqa: BLE001
            v = None
        if tag == "sh":
            sh = v
        else:
            sz = v
    if not sh and not sz:
        return None
    sh = sh or {k: 0.0 for k in ("main", "sm", "md", "lg", "xl")}
    sz = sz or {k: 0.0 for k in ("main", "sm", "md", "lg", "xl")}
    tot = {k: sh[k] + sz[k] for k in ("main", "sm", "md", "lg", "xl")}
    tot["sh"] = sh
    tot["sz"] = sz
    return tot


def fetch_sector_flow(sectors):
    """各行业板块的当日主力净额。

    首选 ulist 批量带 f62（2 次请求）。该接口在云端或高频访问下会被断连，
    退路是逐个走 fflow/kline —— 但 30 个板块逐个查在限流时能拖到几分钟，
    所以退路只查涨跌幅两端的 8 个（正是"异动板块"要看的那几个），
    单次超时压到 8 秒、不重试，宁可少几个也不把工作流拖超时。

    返回 (dict 名称→净额, 口径说明)。
    """
    codes = [c for c, _ in SECTORS_L1]
    name_of = dict(SECTORS_L1)
    got = {}

    # 路径零：板块兜底时顺手拿到的腾讯主力净额（一次请求覆盖全部行业）
    if QQ_SECTOR_FLOW:
        return dict(QQ_SECTOR_FLOW), "全部 %d 个行业" % len(QQ_SECTOR_FLOW)

    # 路径一：ulist 批量
    try:
        for i in range(0, len(codes), 15):
            chunk = codes[i:i + 15]
            secids = ",".join("90." + c for c in chunk)
            url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
                   "&fields=f12,f14,f62,f184&secids=" + secids)
            j = get_json(url, tries=1, timeout=12)
            for r in (j.get("data") or {}).get("diff") or []:
                code = str(r.get("f12") or "").strip()
                v = num(r.get("f62"))
                if code in name_of and v is not None:
                    got[name_of[code]] = v
    except Exception:  # noqa: BLE001
        got = {}

    if len(got) >= len(codes) - 3:
        return got, "全部 %d 个行业" % len(got)

    # 路径二：只查涨跌幅两端
    got = {}
    picked = []
    if sectors:
        for s in sectors[:4] + sectors[-4:]:
            code = next((c for c, n in SECTORS_L1 if n == s["name"]), None)
            if code and code not in picked:
                picked.append(code)
    else:
        picked = codes[:8]
    for code in picked:
        try:
            f = fetch_flow_one("90." + code, tries=1, timeout=8)
            if f:
                got[name_of[code]] = f["main"]
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    print("      （板块资金流走逐个查询，拿到 %d 个）" % len(got))
    return got, "涨跌幅两端 %d 个行业" % len(got)


# ── 异动个股：涨停池 / 跌停池 / 炸板池 / 龙虎榜 ──────────────────
def _pool(kind, date_str, pagesize, sort):
    url = ("https://push2ex.eastmoney.com/getTopic" + kind + "Pool"
           "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0"
           f"&pagesize={pagesize}&sort={sort}&date={date_str}")
    j = get_json(url, tries=2, timeout=15)
    d = j.get("data") or {}
    return d.get("pool") or [], int(d.get("tc") or 0)


def _pct_of(v, base=1000.0):
    """池子里的价格是放大 1000 倍的整数"""
    return (v / base) if v is not None else None


def fetch_limit_pools(date_str):
    """涨停、跌停、炸板三个池子。

    这三个池子是判断情绪最直接的窗口：
    涨停家数看赚钱效应，跌停家数看亏钱效应，炸板率看承接力。
    """
    out = {"zt": [], "dt": [], "zb": [], "zt_n": 0, "dt_n": 0, "zb_n": 0}
    try:
        rows, tc = _pool("ZT", date_str, 90, "lbc%3Adesc")
        out["zt_n"] = tc or len(rows)
        for r in rows:
            out["zt"].append({
                "code": str(r.get("c") or ""),
                "name": r.get("n") or "",
                "pct": round(num(r.get("zdp")) or 0, 2),
                "close": _pct_of(num(r.get("p"))),
                "amount": num(r.get("amount")),
                "turnover": num(r.get("hs")),
                "boards": int(num(r.get("lbc")) or 1),
                "fund": num(r.get("fund")),
                "fbt": str(r.get("fbt") or "").zfill(6),
                "sector": r.get("hybk") or "",
            })
    except Exception as e:  # noqa: BLE001
        print("      ! 涨停池失败：", e)

    try:
        rows, tc = _pool("DT", date_str, 30, "fund%3Aasc")
        out["dt_n"] = tc or len(rows)
        for r in rows:
            out["dt"].append({
                "code": str(r.get("c") or ""),
                "name": r.get("n") or "",
                "pct": round(num(r.get("zdp")) or 0, 2),
                "amount": num(r.get("amount")),
                "fund": num(r.get("fund")),
                "turnover": num(r.get("hs")),
            })
    except Exception as e:  # noqa: BLE001
        print("      ! 跌停池失败：", e)

    try:
        rows, tc = _pool("ZB", date_str, 30, "zbc%3Adesc")
        out["zb_n"] = tc or len(rows)
        for r in rows:
            out["zb"].append({
                "code": str(r.get("c") or ""),
                "name": r.get("n") or "",
                "pct": round(num(r.get("zdp")) or 0, 2),
                "amount": num(r.get("amount")),
                "turnover": num(r.get("hs")),
                "open": int(num(r.get("zbc")) or 1),
            })
    except Exception as e:  # noqa: BLE001
        print("      ! 炸板池失败：", e)
    return out


def fetch_lhb(date_str):
    """龙虎榜：当日净买入额排行（按个股去重，取净买入最大那条）"""
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&pageNumber=1"
           "&pageSize=60&sortColumns=BILLBOARD_NET_AMT&sortTypes=-1&source=WEB&client=WEB"
           "&filter=(TRADE_DATE%3D%27" + date_str + "%27)")
    try:
        j = get_json(url, tries=2, timeout=15)
        rows = (j.get("result") or {}).get("data") or []
    except Exception as e:  # noqa: BLE001
        print("      ! 龙虎榜失败：", e)
        return []
    seen, out = set(), []
    for r in rows:
        code = str(r.get("SECURITY_CODE") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({
            "code": code,
            "name": r.get("SECURITY_NAME_ABBR") or "",
            "pct": num(r.get("CHANGE_RATE")),
            "net": num(r.get("BILLBOARD_NET_AMT")),
            "buy": num(r.get("BILLBOARD_BUY_AMT")),
            "sell": num(r.get("BILLBOARD_SELL_AMT")),
            "turnover": num(r.get("TURNOVERRATE")),
            "reason": (r.get("EXPLANATION") or "").strip(),
        })
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
    if abs(pct) < 0.5 * 10 ** (-digits):
        pct = 0.0          # 避免出现 -0.00%
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
    strong = [s for s in sectors if s["name"] in TECH_NAMES]
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
/* 资金流分叉条 */
.flow{background:var(--card); border:1px solid var(--line); padding:14px 15px; margin:12px 0;}
.flowrow{display:grid; grid-template-columns:56px 1fr 88px; gap:10px; align-items:center;
  margin:7px 0; font-size:12.5px;}
.flowrow .fl{color:var(--ink3); font-size:11.5px; text-align:left;}
.flowrow .fv{text-align:right; font-variant-numeric:tabular-nums; font-weight:600;}
.fbar{position:relative; height:16px; background:#f1f0ed; border-radius:2px;}
.fbar .z{position:absolute; left:50%; top:0; bottom:0; width:1px; background:#cfcdc7;}
.fbar i{position:absolute; top:2.5px; bottom:2.5px; border-radius:1.5px;}
.flow .foot{font-size:11px; color:var(--ink3); margin-top:10px; line-height:1.65;
  padding-top:9px; border-top:1px dashed var(--line);}
/* 两栏对照 */
.pair{display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:12px 0;}
.pair .col{min-width:0;}
.pair .col .t{font-size:11.5px; color:var(--ink3); margin-bottom:6px; padding-left:2px;}
.pair table{margin:0; font-size:12.3px;}
.pair th,.pair td{padding:6px 8px;}
.tag-mini{display:inline-block; font-size:10px; padding:0 5px; border-radius:2px;
  background:#f1f0ed; color:var(--ink3); margin-left:4px; vertical-align:1px;}
.tag-mini.hot{background:#fbeceb; color:var(--up);}
.tag-mini.cold{background:#e8f3ee; color:var(--down);}
.pool{display:grid; grid-template-columns:repeat(4,1fr); gap:9px; margin:12px 0;}
.pool .p{background:var(--card); border:1px solid var(--line); padding:11px 12px;}
.pool .p .k{font-size:11px; color:var(--ink3);}
.pool .p .v{font-size:18px; font-weight:700; margin-top:3px;}
.pool .p .s{font-size:10.5px; color:var(--ink3); margin-top:2px;}
@media (max-width:640px){
  h1{font-size:21px;} .cards{grid-template-columns:repeat(2,1fr);}
  .chain{grid-template-columns:1fr;} .arrow{transform:rotate(90deg); padding:6px 0;}
  .anchors{grid-template-columns:1fr;} table{font-size:12px;}
  th,td{padding:7px 7px;}
  .pair{grid-template-columns:1fr;} .pool{grid-template-columns:repeat(2,1fr);}
  .flowrow{grid-template-columns:48px 1fr 76px; gap:7px;}
}
"""

JS = """
(function(){
  var bar=document.getElementById('pastBar'),sel=document.getElementById('pastSelect'),meta=document.getElementById('pastMeta');
  if(!bar||!sel||!window.PAST||!window.PAST.length) return;
  var base=location.pathname.indexOf('/archive/')>=0?'../':'';
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
    var t=hit(); if(!t) return;
    // path 可能是相对路径（GitHub Pages 的 archive/），也可能是完整 URL（活页那版指向 Pages 的存档）
    location.href=/^https?:/i.test(t.path)?t.path:base+t.path;
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
                     f'<td>{s["n"] or "—"}</td></tr>')

    # —— 资金流向
    flow = data.get("flow") or {}
    flow_sec = data.get("flow_sectors") or {}
    flow_src = data.get("flow_src") or ""
    flow_html = ""
    flow_text = ""
    if flow:
        items = [("主力净额", flow.get("main")), ("超大单", flow.get("xl")),
                 ("大单", flow.get("lg")), ("中单", flow.get("md")), ("小单", flow.get("sm"))]
        mx = max([abs(v or 0) for _, v in items] + [1e8])
        frows = ""
        for label, v in items:
            v = v or 0
            w = abs(v) / mx * 50
            side = "left:50%" if v >= 0 else "right:50%"
            frows += ('<div class="flowrow"><span class="fl">%s</span>'
                      '<span class="fbar"><i class="z"></i>'
                      '<i style="%s;width:%.1f%%;background:%s"></i></span>'
                      '<span class="fv %s">%+.2f 亿</span></div>'
                      % (label, side, w, "var(--up)" if v >= 0 else "var(--down)",
                         cls(v), v / 1e8))
        shm = (flow.get("sh") or {}).get("main") or 0
        szm = (flow.get("sz") or {}).get("main") or 0
        flow_html = ('<div class="flow">' + frows
                     + '<div class="foot">沪市主力 <b class="%s">%+.2f 亿</b>　·　'
                       '深市主力 <b class="%s">%+.2f 亿</b>　·　'
                       '口径：超大单指单笔 ≥100 万元，大单 20–100 万元，中单 4–20 万元，小单 &lt;4 万元。'
                       '主力 = 超大单 + 大单。</div></div>'
                       % (cls(shm), shm / 1e8, cls(szm), szm / 1e8))

        # 钱从哪来到哪去
        inflow = sorted([(n, v) for n, v in flow_sec.items() if v > 0], key=lambda x: -x[1])
        outflow = sorted([(n, v) for n, v in flow_sec.items() if v < 0], key=lambda x: x[1])

        def flow_tbl(rows, empty="没有净流入的行业"):
            if not rows:
                return '<tr><td colspan="2" style="text-align:center;color:#8d8a83">%s</td></tr>' % empty
            return "".join('<tr><td>%s</td><td class="%s">%+.1f 亿</td></tr>'
                           % (n, cls(v), v / 1e8) for n, v in rows[:5])

        if flow_sec:
            pair_tbl = ('<div class="pair">'
                        '<div class="col"><div class="t">主力净流入 · %s</div>'
                        '<div class="tbl-wrap"><table><thead><tr><th>行业</th><th>净额</th></tr></thead>'
                        '<tbody>%s</tbody></table></div></div>'
                        '<div class="col"><div class="t">主力净流出 · %s</div>'
                        '<div class="tbl-wrap"><table><thead><tr><th>行业</th><th>净额</th></tr></thead>'
                        '<tbody>%s</tbody></table></div></div></div>'
                        % (flow_src, flow_tbl(inflow, "全部行业净流出"),
                           flow_src, flow_tbl(outflow, "全部行业净流入")))
            flow_html += pair_tbl

        main_yi = (flow.get("main") or 0) / 1e8
        sm_yi = (flow.get("sm") or 0) / 1e8
        if main_yi < 0 and sm_yi > 0:
            flow_text = ("主力全天净流出 <strong>%.0f 亿</strong>，小单净流入 <strong>%.0f 亿</strong>——"
                         "机构在卖，散户在接。这种结构在下跌途中反复出现，意味着抛压是从大资金那头出来的，"
                         "不是散户恐慌砸盘；也意味着只要大资金不再卖，指数就能稳住，但反之反弹也容易被再卖一次。"
                         % (abs(main_yi), sm_yi))
        elif main_yi > 0 and sm_yi < 0:
            flow_text = ("主力全天净流入 <strong>%.0f 亿</strong>，小单净流出 <strong>%.0f 亿</strong>——"
                         "大资金在买、散户在卖，是筹码由散转集中 的结构，通常比指数涨跌本身更值得关注。"
                         % (main_yi, abs(sm_yi)))
        else:
            flow_text = ("主力全天净额 <strong>%+.0f 亿</strong>，四档资金流向没有形成明显对倒结构，"
                         "说明当天多空双方力量接近，方向更多由板块轮动而非整体资金进出决定。"
                         % main_yi)
        if flow_sec and outflow:
            n0, v0 = outflow[0]
            word = "失血最重" if flow_src.startswith("全部") else "这批行业里失血最重"
            flow_text += ("行业层面%s的是 <strong>%s（%+.0f 亿）</strong>%s。"
                          % (word, n0, v0 / 1e8,
                             ("，净流入居前的是 <strong>%s（%+.0f 亿）</strong>" % (inflow[0][0], inflow[0][1] / 1e8))
                             if inflow else ""))

    # —— 异动板块：涨跌幅两端
    hot = sectors[:5] if sectors else []
    cold = sectors[-5:][::-1] if sectors else []
    # 全市场普跌时最强的那个也是负的，叫"领涨"不准确，换成"相对最强"
    up_word = "领涨" if (hot and hot[0]["pct"] > 0) else "相对最强"
    dn_word = "领跌" if (cold and cold[0]["pct"] < 0) else "相对最弱"

    def sec_rows(rows):
        return "".join('<tr><td>%s</td><td class="%s">%s</td></tr>'
                       % (s["name"], cls(s["pct"]), spct(s["pct"])) for s in rows)

    anomaly_sec_html = (
        '<div class="pair">'
        '<div class="col"><div class="t">%s（前 5）</div>'
        '<div class="tbl-wrap"><table><thead><tr><th>行业</th><th>涨跌幅</th></tr></thead>'
        '<tbody>%s</tbody></table></div></div>'
        '<div class="col"><div class="t">%s（后 5）</div>'
        '<div class="tbl-wrap"><table><thead><tr><th>行业</th><th>涨跌幅</th></tr></thead>'
        '<tbody>%s</tbody></table></div></div></div>'
        % (up_word, sec_rows(hot), dn_word, sec_rows(cold)))

    # —— 异动个股
    lp = data.get("pools") or {}
    zt_rows = lp.get("zt") or []
    dt_rows = lp.get("dt") or []
    zb_rows = lp.get("zb") or []
    lhb = data.get("lhb") or []
    zt_n = lp.get("zt_n") or 0
    dt_n = lp.get("dt_n") or 0
    zb_n = lp.get("zb_n") or 0
    open_rate = (zb_n / (zt_n + zb_n) * 100) if (zt_n + zb_n) else None

    def fmt_t(v):
        s = str(v or "").zfill(6)
        return "%s:%s" % (s[:2], s[2:4]) if len(s) == 6 and s != "000000" else "—"

    lianban = sorted([x for x in zt_rows if x["boards"] >= 2],
                     key=lambda x: (-x["boards"], -(x["fund"] or 0)))
    # 连板股通常只有几只，补上封单最大的首板，凑够 10 行才好读
    first_boards = sorted([x for x in zt_rows if x["boards"] < 2],
                          key=lambda x: -(x["fund"] or 0))
    zt_show = (lianban + first_boards)[:10]
    zt_tbl_rows = ""
    for x in zt_show:
        tag_txt = ("%d板" % x["boards"]) if x["boards"] >= 2 else "首板"
        tag_cls = "tag-mini hot" if x["boards"] >= 2 else "tag-mini"
        zt_tbl_rows += ('<tr><td>%s<span class="%s">%s</span></td>'
                        '<td class="%s">%s</td><td>%.2f 亿</td><td>%.2f%%</td>'
                        '<td>%s</td><td style="text-align:left;color:#8d8a83;font-size:11px;'
                        'white-space:normal;line-height:1.35">%s</td></tr>'
                        % (x["name"], tag_cls, tag_txt, cls(x["pct"]), spct(x["pct"]),
                           (x["fund"] or 0) / 1e8, x["turnover"] or 0,
                           fmt_t(x["fbt"]), x["sector"] or "—"))
    if not zt_tbl_rows:
        zt_tbl_rows = '<tr><td colspan="6" style="text-align:center;color:#8d8a83">今日无涨停</td></tr>'

    lhb_tbl_rows = "".join('<tr><td>%s</td><td class="%s">%s</td><td class="%s">%+.2f 亿</td></tr>'
                           % (x["name"], cls(x["pct"]), spct(x["pct"]),
                              cls(x["net"]), (x["net"] or 0) / 1e8)
                           for x in lhb[:8])
    if not lhb_tbl_rows:
        lhb_tbl_rows = '<tr><td colspan="3" style="text-align:center;color:#8d8a83">龙虎榜尚未公布</td></tr>'

    dt_tbl_rows = "".join('<tr><td>%s</td><td class="%s">%s</td><td>%s</td></tr>'
                          % (x["name"], cls(x["pct"]), spct(x["pct"]),
                             ("%.1f 万股" % ((x["fund"] or 0) / 1e4)) if x["fund"] else "—")
                          for x in dt_rows[:8])
    if not dt_tbl_rows:
        dt_tbl_rows = '<tr><td colspan="3" style="text-align:center;color:#8d8a83">今日无跌停</td></tr>'

    pool_cards = ('<div class="pool">'
                  '<div class="p"><div class="k">涨停</div><div class="v up">%d</div><div class="s">封住涨停的家数</div></div>'
                  '<div class="p"><div class="k">跌停</div><div class="v down">%d</div><div class="s">封死跌停的家数</div></div>'
                  '<div class="p"><div class="k">炸板</div><div class="v">%d</div><div class="s">涨停被打开的家数</div></div>'
                  '<div class="p"><div class="k">炸板率</div><div class="v">%s</div><div class="s">炸板 / (涨停+炸板)</div></div>'
                  '</div>'
                  % (zt_n, dt_n, zb_n,
                     ("%.0f%%" % open_rate) if open_rate is not None else "—"))

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
    has_cost = any(h.get("cost") for h in holdings)
    bench = next((x for x in idx if x["code"] == "000001"), None)
    bench_pct = bench["pct"] if bench else 0
    for h in holdings:
        rel = (h["pct"] - (bench_pct or 0)) if h["pct"] is not None else None
        close_txt = "{:,.2f}".format(h["close"]) if h["close"] is not None else "—"
        turn_txt = "%.2f%%" % h["turnover"] if h["turnover"] is not None else "—"
        vr_txt = "%.2f" % h["volratio"] if h["volratio"] is not None else "—"
        extra = ""
        if has_cost:
            cost = h.get("cost")
            if cost and h["close"]:
                fp = (h["close"] / cost - 1) * 100
                extra = (f'<td>{cost:.3f}</td>'
                         f'<td class="{cls(fp)}">{spct(fp, 1)}</td>')
            else:
                extra = "<td>—</td><td>—</td>"
        rows_hold += (f'<tr><td>{h["name"]}<span style="color:#8d8a83;font-size:11px"> {h["code"]}</span></td>'
                      f'<td>{close_txt}</td>'
                      f'<td class="{cls(h["pct"])}">{spct(h["pct"])}</td>'
                      + extra +
                      f'<td>{fmt_money(h["amount"])}</td>'
                      f'<td>{turn_txt}</td>'
                      f'<td>{vr_txt}</td>'
                      f'<td class="{cls(rel)}">{spct(rel, 1)}</td></tr>')

    # 持仓节：发布到公开站点时用 SHOW_HOLDINGS=0 关掉，节编号跟着顺延
    if SHOW_HOLDINGS:
        hold_section = ('<h2>十、持仓跟踪</h2>\n'
                        '<div class="tbl-wrap"><table>\n'
                        '<thead><tr><th>股票</th><th>收盘</th><th>涨跌幅</th>'
                        + ("<th>成本</th><th>浮动</th>" if has_cost else "") +
                        '<th>成交额</th><th>换手</th><th>量比</th><th>相对上证</th></tr></thead>\n'
                        '<tbody>' + rows_hold + '</tbody></table></div>\n'
                        '<p>' + (data.get("holding_text") or "") + '</p>\n'
                        '<div class="note">只跟踪当日强弱，不用历史记录中的成本价推算盈亏'
                        '——若期间发生除权，旧成本会失效。需要判断止盈止损时，先核对复权后再定基准。</div>\n')
        watch_no = "十一"
    else:
        hold_section = ""
        watch_no = "十"

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
    strong_secs = [s for s in sectors if s["name"] in TECH_NAMES]
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
  <div class="sub">{date_cn} · 收盘后 · 数据源：东方财富 / 腾讯 / 新浪公开接口 · 生成时点 {data["gen_at"]}</div>
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
<div class="cap">指数涨跌幅（%），红涨绿跌。数据源：东方财富 / 腾讯，{data["gen_at"]}。</div></div>

<h2>二、盘面质感</h2>
<p>{data["texture"]}</p>

<h2>三、板块强弱</h2>
<div class="chartbox"><div id="chartSector" class="chart" style="height:560px;"></div>
<div class="cap">固定跟踪的 30 个一级行业，数据源：<strong>{data["sector_src"]}</strong>，自下而上由弱到强。
口径提示：板块指数由各自平台的板块编制方式计算，与「全 A 总市值加权」不是一回事，银行这类大小票分化的板块会被拉平，读的时候要记住这一点。</div></div>
<div class="tbl-wrap"><table>
<thead><tr><th>行业</th><th>涨跌幅</th><th>成分股数</th></tr></thead>
<tbody>{rows_sec}</tbody></table></div>
<p>{data["sector_text"]}</p>

<h2>四、今日资金流向</h2>
{flow_html}
<p>{flow_text}</p>

<h2>五、异动板块</h2>
{anomaly_sec_html}
<p>{data["anomaly_sec_text"]}</p>

<h2>六、异动个股</h2>
{pool_cards}
<h3>涨停梯队（连板优先）</h3>
<div class="tbl-wrap"><table>
<thead><tr><th>股票</th><th>涨跌幅</th><th>封单</th><th>换手</th><th>首封</th><th style="text-align:left">所属板块</th></tr></thead>
<tbody>{zt_tbl_rows}</tbody></table></div>
<div class="pair">
<div class="col"><div class="t">跌停（封单从少到多）</div>
<div class="tbl-wrap"><table><thead><tr><th>股票</th><th>涨跌幅</th><th>封单量</th></tr></thead>
<tbody>{dt_tbl_rows}</tbody></table></div></div>
<div class="col"><div class="t">龙虎榜净买入（前 8）</div>
<div class="tbl-wrap"><table><thead><tr><th>股票</th><th>涨跌幅</th><th>净买入</th></tr></thead>
<tbody>{lhb_tbl_rows}</tbody></table></div></div>
</div>
<p>{data["anomaly_stock_text"]}</p>
<div class="note">涨停 / 跌停 / 炸板家数取东方财富涨停池、跌停池、炸板池；龙虎榜取东方财富数据中心当日明细，
同一只股票多条上榜原因只保留净买入最大的一条。封单额是收盘时未成交的委托金额；
跌停池接口给的是封单<strong>量</strong>（股），所以跌停表按"万股"显示，两者不是同一个口径。</div>

<h2>七、外围与杠杆：三层传导</h2>
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

<h2>八、位置坐标</h2>
<div class="tbl-wrap"><table>
<thead><tr><th>指数</th><th>收盘</th><th>距MA5</th><th>距MA20</th><th>距MA60</th><th>距MA120</th><th>距60日高点</th></tr></thead>
<tbody>{rows_ma}</tbody></table></div>
<p>{data["position_text"]}</p>
<div class="note">均线距离为收盘价相对该均线的偏离百分比，正值为在均线上方。支撑位是技术参考位，不是预测。</div>

<h2>九、破局判断：四条锚</h2>
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

    # 板块解读：把「科技链」和「非科技」分开看，才能判断是调仓还是全面收缩
    non_ai = [s for s in sectors if s["name"] not in TECH_NAMES]
    non_ai_deep = sorted([s for s in non_ai if s["pct"] <= -3], key=lambda x: x["pct"])
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

    # 异动板块解读
    flow_sec = data.get("flow_sectors") or {}
    ast = []
    if sectors:
        top, bot = sectors[0], sectors[-1]
        ast.append("板块层面，最强的是 <strong>%s（%s）</strong>，最弱的是 <strong>%s（%s）</strong>，"
                   "两端相差 %.1f 个百分点。" % (top["name"], spct(top["pct"]),
                                            bot["name"], spct(bot["pct"]),
                                            (top["pct"] or 0) - (bot["pct"] or 0)))
        up_n = len([s for s in sectors if s["pct"] > 0])
        scope = "%d 个一级行业" % len(sectors)
        ast.append("%s里 %d 个上涨、%d 个下跌。" % (scope, up_n, len(sectors) - up_n))
        if flow_sec and top["name"] in flow_sec:
            v = flow_sec[top["name"]]
            ast.append(("但领涨的 %s 主力资金仍是净流出 %.0f 亿——"
                        "这种「涨幅第一、资金净流出」的组合，说明它是<strong>跌得少</strong>，不是<strong>有人在买</strong>，"
                        "在下跌途中往往只是避险资金的临时停留地，不具备领涨资格。"
                        % (top["name"], abs(v) / 1e8))
                       if v < 0 else
                       ("领涨的 %s 主力净流入 %.0f 亿，涨幅和资金同向，是当天唯一有主动买盘的方向。"
                        % (top["name"], v / 1e8)))
        if flow_sec and bot["name"] in flow_sec:
            v = flow_sec[bot["name"]]
            if v < 0:
                ast.append("领跌的 %s 主力净流出 %.0f 亿，是杀跌的主要出口。" % (bot["name"], abs(v) / 1e8))
    data["anomaly_sec_text"] = "".join(ast)

    # 异动个股解读
    lp = data.get("pools") or {}
    zt_n, dt_n, zb_n = lp.get("zt_n") or 0, lp.get("dt_n") or 0, lp.get("zb_n") or 0
    zt_rows = lp.get("zt") or []
    lhb = data.get("lhb") or []
    pt = []
    if zt_n or dt_n:
        if dt_n > zt_n:
            pt.append("跌停 <strong>%d</strong> 家多于涨停 <strong>%d</strong> 家，亏钱效应盖过赚钱效应。"
                      % (dt_n, zt_n))
        else:
            pt.append("涨停 <strong>%d</strong> 家、跌停 <strong>%d</strong> 家，赚钱效应仍在。"
                      % (zt_n, dt_n))
        if zb_n:
            rate = zb_n / (zt_n + zb_n) * 100
            if rate >= 35:
                pt.append("炸板 %d 家，炸板率 %.0f%%，承接力偏弱——盘中冲上去封不住，说明买盘是脉冲式的。"
                          % (zb_n, rate))
            else:
                pt.append("炸板 %d 家，炸板率 %.0f%%，封住的居多。" % (zb_n, rate))
        maxb = max([x["boards"] for x in zt_rows], default=0)
        if maxb >= 3:
            leaders = [x for x in zt_rows if x["boards"] == maxb]
            pt.append("最高连板是 <strong>%s（%d 板）</strong>，这是当前的情绪高度；"
                      "它能走多远，决定短线资金敢不敢跟。"
                      % ("、".join(x["name"] for x in leaders[:3]), maxb))
        elif zt_rows:
            pt.append("最高只有 %d 板，没有龙头，短线资金处于无方向状态。" % maxb)
    if lhb:
        buy = [x for x in lhb if (x["net"] or 0) > 0]
        sell = [x for x in lhb if (x["net"] or 0) < -1e7]   # 小于 0.1 亿的不值得单独点名
        if buy:
            pt.append("龙虎榜净买入第一是 <strong>%s（%+.2f 亿）</strong>。"
                      % (buy[0]["name"], (buy[0]["net"] or 0) / 1e8))
        if sell:
            pt.append("榜内净卖出最多的是 %s（%+.2f 亿）。" % (sell[0]["name"], (sell[0]["net"] or 0) / 1e8))
        pt.append("龙虎榜只反映当日最活跃的少数个股，是情绪的切片，不是选股清单。")
    data["anomaly_stock_text"] = "".join(pt)

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


def _yi(v):
    """金额（元）→ 带符号的亿元文本，推送正文用"""
    if v is None:
        return "—"
    return "%+.1f 亿" % (v / 1e8)


def build_digest(data):
    """微信推送用的正文。

    微信把 github.io 以及各类个人域名都归进外链拦截，点开就是"已停止访问"。
    但 Server酱 的消息详情页本身在微信白名单内，正文能正常渲染，
    所以正文必须自给自足：关键数字全写进来，链接只当"想看图表"的出口。
    返回 (digest 长文, brief 卡片描述)。
    """
    idx = data.get("idx") or []
    sectors = data.get("sectors") or []
    flow = data.get("flow") or {}
    flow_sec = data.get("flow_sectors") or {}
    pools = data.get("pools") or {}
    lhb = data.get("lhb") or []
    hs = data.get("holdings") or []
    anchors = data.get("anchors") or []
    limits = data.get("limits") or [None, None]
    up, dn, flat = (data.get("breadth") or (None, None, None))[:3]
    headline = data.get("headline") or "今日复盘"

    cy_pct = None
    for x in idx:
        if str(x.get("code")) == "399006":
            cy_pct = x.get("pct")

    L = []
    L.append("**%s**" % headline)
    tag = []
    if data.get("grade"):
        tag.append("档位 %s（%d/4 条锚到位）" % (data["grade"], data.get("hit") or 0))
    if data.get("mode") and data["mode"] not in headline:
        tag.append(data["mode"])
    if tag:
        L.append("")
        L.append("　".join(tag))

    # ── 指数
    if idx:
        L.append("")
        L.append("**指数**")
        for x in idx[:5]:
            L.append("· %s %s（%s）" % (x["name"], "{:,.2f}".format(x["close"]),
                                      spct(x.get("pct"))))
        if len(idx) >= 2:
            amt = (idx[0].get("amount") or 0) + (idx[1].get("amount") or 0)
            if amt > 0:
                txt = "· 两市成交 %s" % fmt_money(amt)
                prev = data.get("two_mkt_prev")
                if prev:
                    chg = (amt / prev - 1) * 100
                    txt += "（较前日%s %.1f%%）" % ("放量" if chg > 0 else "缩量", abs(chg))
                L.append(txt)

    # ── 资金
    if flow:
        L.append("")
        L.append("**资金流向**")
        L.append("· 主力 %s（沪 %s ／ 深 %s）" % (
            _yi(flow.get("main")),
            _yi((flow.get("sh") or {}).get("main")),
            _yi((flow.get("sz") or {}).get("main"))))
        if flow.get("xl") is not None or flow.get("lg") is not None:
            L.append("· 超大单 %s ／ 大单 %s" % (_yi(flow.get("xl")), _yi(flow.get("lg"))))
        inflow = sorted([(k, v) for k, v in flow_sec.items() if v and v > 0],
                        key=lambda x: -x[1])[:3]
        outflow = sorted([(k, v) for k, v in flow_sec.items() if v and v < 0],
                         key=lambda x: x[1])[:3]
        if inflow:
            L.append("· 行业流入：" + "、".join("%s %s" % (k, _yi(v)) for k, v in inflow))
        if outflow:
            L.append("· 行业流出：" + "、".join("%s %s" % (k, _yi(v)) for k, v in outflow))

    # ── 情绪
    zt_rows = pools.get("zt") or []
    zt_n = pools.get("zt_n")
    dt_n = pools.get("dt_n")
    zb_n = pools.get("zb_n")
    if zt_n is None:
        zt_n = limits[0]
    if dt_n is None:
        dt_n = limits[1]
    emo = []
    if up is not None:
        tot = (up or 0) + (dn or 0) + (flat or 0)
        r = "，上涨占比 %.1f%%" % (up / tot * 100) if tot else ""
        emo.append("涨 %s ／ 跌 %s ／ 平 %s%s" % (up, dn, flat, r))
    one = "涨停 %s ／ 跌停 %s" % (zt_n if zt_n is not None else "—",
                               dt_n if dt_n is not None else "—")
    if zb_n is not None:
        one += " ／ 炸板 %s" % zb_n
        if zt_n and (zt_n + zb_n):
            one += "（炸板率 %.0f%%）" % (zb_n / (zt_n + zb_n) * 100)
    emo.append(one)
    if zt_rows:
        mx = max((x.get("boards") or 1) for x in zt_rows)
        if mx > 1:
            lead = [x["name"] for x in zt_rows if (x.get("boards") or 1) == mx][:3]
            emo.append("最高 %d 连板：%s" % (mx, "、".join(lead)))
    if emo:
        L.append("")
        L.append("**情绪**")
        for x in emo:
            L.append("· " + x)

    # ── 异动板块
    if sectors:
        top, bot = sectors[0], sectors[-1]
        upn = len([s for s in sectors if (s.get("pct") or 0) > 0])
        L.append("")
        L.append("**异动板块**")
        L.append("· 最强 %s %s ／ 最弱 %s %s（差 %.1f 个点）" % (
            top["name"], spct(top.get("pct")), bot["name"], spct(bot.get("pct")),
            (top.get("pct") or 0) - (bot.get("pct") or 0)))
        L.append("· %d 个一级行业：%d 涨 / %d 跌" % (
            len(sectors), upn, len(sectors) - upn))

    # ── 异动个股
    stock_rows = []
    buys = [x for x in lhb if (x.get("net") or 0) > 0][:3]
    if buys:
        stock_rows.append("· 龙虎榜净买：" + "、".join(
            "%s %s" % (x["name"], _yi(x.get("net"))) for x in buys))
    sells = sorted([x for x in lhb if (x.get("net") or 0) < -1e7],
                   key=lambda x: x.get("net"))
    if sells:
        stock_rows.append("· 龙虎榜净卖：" + "、".join(
            "%s %s" % (x["name"], _yi(x.get("net"))) for x in sells[:2]))
    if zt_rows:
        lead2 = sorted(zt_rows, key=lambda x: (-(x.get("boards") or 1),
                                               -(x.get("fund") or 0)))[:5]
        stock_rows.append("· 涨停梯队：" + "、".join(
            "%s %d板" % (x["name"], x.get("boards") or 1) for x in lead2))
    zbs = sorted(pools.get("zb") or [], key=lambda x: -(x.get("open") or 0))[:2]
    if zbs:
        stock_rows.append("· 反复炸板：" + "、".join(
            "%s %d 次" % (x["name"], x.get("open") or 1) for x in zbs))
    if stock_rows:
        L.append("")
        L.append("**异动个股**")
        L.extend(stock_rows)

    # ── 持仓（口径与页面一致：公开版 SHOW_HOLDINGS=0 时整段不写，
    #    因为 digest 会落进公开的 issues.json，不能只靠"页面不渲染"来挡）
    if hs and SHOW_HOLDINGS:
        L.append("")
        L.append("**持仓**")
        for h in hs:
            p = h.get("pct")
            t = "· %s %s" % (h["name"], spct(p) if p is not None else "—")
            c, cost = h.get("close"), h.get("cost")
            if c and cost:
                t += "（收 %.2f ／ 成本 %.2f ／ %s）" % (c, cost, spct((c / cost - 1) * 100))
            L.append(t)

    # ── 四条锚
    if anchors:
        L.append("")
        L.append("**四条锚**")
        for name, ok, txt in anchors:
            L.append("%s %s · %s" % ("[到位]" if ok else "[未到位]", name, txt))

    # ── 卡片描述：微信通知栏里就能看到的那行
    brief = headline
    if idx:
        brief += "｜上证 %s" % spct(idx[0].get("pct"))
        if cy_pct is not None:
            brief += " 创业板 %s" % spct(cy_pct)
    if flow:
        brief += " 主力 %s" % _yi(flow.get("main"))
    brief += " 涨停 %s/跌停 %s" % (zt_n if zt_n is not None else "—",
                                 dt_n if dt_n is not None else "—")
    if len(brief) > 90:
        brief = brief[:88] + "…"

    return "\n".join(L), brief


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    print(f"[1/10] 时间基准 {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # 指数
    idx = fetch_indices()
    print(f"[2/10] 指数 {len(idx)} 个：{idx[0]['name']} {idx[0]['close']} {idx[0]['pct']}%")

    # 交易日校验
    print("[3/10] 校验交易日")
    sh_k = fetch_kline("1.000001", 140)
    if not sh_k:
        print("✗ 拿不到上证 K 线，退出")
        return 2
    last_date = sh_k[-1]["date"]
    if last_date != today and not args.force:
        print(f"· 休市（最新交易日 {last_date}，今天 {today}），跳过")
        return 0

    # 涨跌家数：上证 / 深证综指 / 北证50 的成分统计，一次请求拿三市合计
    fast = fetch_breadth_fast(last_date.replace("-", ""))
    if fast:
        up, dn, flat = fast
        breadth_src = BREADTH_SOURCE
    else:
        print("✗ 拿不到涨跌家数，退出")
        return 2
    print(f"      上涨 {up} 下跌 {dn} 平 {flat}（{breadth_src}）")

    # 板块：用东财行业板块指数（单次请求）。
    # 注意口径——这是板块指数本身，板块内个股等权，不是总市值加权。
    # 之前那版是拉全 A 逐只加权，但东财对云端 IP 的分页请求限制极严
    # （实测每轮只能成功 3 页左右，60 页拿不完），所以换成板块指数。
    sectors = fetch_sector_boards()
    if not sectors:
        print("✗ 拿不到板块数据，退出")
        return 2
    sector_src = SECTOR_SOURCE
    print(f"[4/10] 板块 {len(sectors)} 个："
          f"{sectors[0]['name']} {sectors[0]['pct']}% … {sectors[-1]['name']} {sectors[-1]['pct']}%")

    # 涨停 / 跌停 / 炸板池 —— 顺便把涨跌停家数也带出来，省两次请求
    dstr = last_date.replace("-", "")
    pools = fetch_limit_pools(dstr)
    limits = (pools["zt_n"] or None, pools["dt_n"] or None)
    print(f"[5/10] 涨停 {pools['zt_n']} 跌停 {pools['dt_n']} 炸板 {pools['zb_n']}")

    # 龙虎榜
    lhb = fetch_lhb(last_date)
    print(f"      龙虎榜 {len(lhb)} 只")

    # 资金流向：大盘五档 + 行业主力净额
    flow = fetch_flow()
    flow_sectors, flow_src = fetch_sector_flow(sectors)
    if flow:
        print(f"[6/10] 主力 {flow['main']/1e8:+.1f} 亿"
              f"（沪 {flow['sh']['main']/1e8:+.1f} / 深 {flow['sz']['main']/1e8:+.1f}）"
              f"，行业资金 {len(flow_sectors)} 个")
    else:
        print("[6/10] ! 资金流拿不到，该节留空")

    # 两融
    margin = fetch_margin(60)
    if len(margin) < 2:
        print("✗ 两融数据不足")
        return 2
    margin_delta = margin[-1]["total"] - margin[-2]["total"]
    margin_cum = margin[-1]["total"] - margin[0]["total"]
    print(f"[7/10] 两融 {margin[-1]['total']/1e4:.4f} 万亿，单日 {margin_delta:+.1f} 亿")

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
        "sector_src": sector_src,
        "breadth_src": breadth_src,
        "two_mkt_prev": two_mkt_prev,
        "flow": flow or {},
        "flow_sectors": flow_sectors,
        "flow_src": flow_src,
        "pools": pools,
        "lhb": lhb,
        "date_cn": now.strftime("%Y年%-m月%-d日") if os.name != "nt" else now.strftime("%Y年%m月%d日"),
        "gen_at": now.strftime("%H:%M"),
    }
    # 星期
    wd = "一二三四五六日"[now.weekday()]
    data["date_cn"] = f'{now.year}年{now.month}月{now.day}日（周{wd}）'

    headline, mode = qualitative(idx, (up, dn, flat), sectors, limits)
    data["headline"], data["mode"] = headline, mode

    data["os_line"] = "、".join(f'{o["name"]} {spct(o["pct"])}' for o in overseas[:3])
    strong_secs = [s for s in sectors if s["name"] in TECH_NAMES]
    data["strong_avg"] = mean([s["pct"] for s in strong_secs]) if strong_secs else 0.0

    data["anchors"] = anchor_state(idx, overseas, sectors, limits, margin_delta, (up, dn, flat))
    grade, hit = stance(data["anchors"])
    data["grade"], data["hit"] = grade, hit

    data = build_texts(data)
    digest, brief = build_digest(data)
    print(f"[8/10] 定性：{headline}｜档位：{grade}（{hit}/4 到位）")

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
               f'中证1000 {spct(idx[4]["pct"])}')
    if flow:
        summary += f'，主力 {flow["main"]/1e8:+.0f} 亿'
    summary += '，涨停 %s / 跌停 %s 家' % (
        limits[0] if limits[0] is not None else "—",
        limits[1] if limits[1] is not None else "—")
    issues = [x for x in issues if x.get("date") != today]
    for x in issues:
        x.pop("digest", None)   # 老版本可能把正文写进来过，清掉
    issues.append({"date": today, "title": headline, "summary": summary,
                   "brief": brief, "path": f"archive/{today}.html"})
    issues.sort(key=lambda x: x["date"], reverse=True)
    issues = issues[:120]
    data["past"] = issues

    print("[9/10] 渲染页面")
    html = render(data)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(out, "archive", f"{today}.html"), "w", encoding="utf-8") as f:
        f.write(html)

    with open(issues_path, "w", encoding="utf-8") as f:
        json.dump(issues, f, ensure_ascii=False, indent=1)

    # 推送正文单独落盘：里面可能带持仓，而 docs/ 是要公开到 Pages 的，
    # 所以不能塞进 issues.json。push_payload.json 在仓库里被 gitignore 跳过。
    try:
        payload_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "push_payload.json")
        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump({"date": today, "title": headline, "brief": brief,
                       "digest": digest}, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        print("      ! 推送正文写入失败（不影响页面）：", e)

    # 活页那版：往期下拉要指向 GitHub Pages 的存档页。
    # 活页上没有 archive/ 目录，相对路径会被解析到站点根，点了一律 404；
    # 所以多渲染一份把 path 换成绝对地址。render 是纯函数，不重复打接口。
    abs_base = os.environ.get("PAST_ABS_BASE", "").rstrip("/")
    if abs_base:
        data_lp = dict(data)
        data_lp["past"] = [dict(x, path=abs_base + "/" + x["path"]) for x in issues]
        with open(os.path.join(out, "livepage.html"), "w", encoding="utf-8") as f:
            f.write(render(data_lp))
        print(f"      （活页版已生成 {out}/livepage.html，往期指向 {abs_base}）")

    print(f"[10/10] 已写入 {out}/index.html、{out}/archive/{today}.html、issues.json（往期 {len(issues)} 期）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
