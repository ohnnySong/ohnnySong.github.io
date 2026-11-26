# -*- coding: utf-8 -*-
"""舆情抓取管线（本地/GitHub Actions 通用版）。

功能：
1. 读取仓库根目录 companies.json 作为监控公司名单；
2. Google News RSS 逐公司检索最近 30 天新闻，DeepSeek 生成中文摘要与情绪判断，写 data.json；
3. 通用行业检索 + DeepSeek 识别名单之外的超声波/压电企业，合并写 pending.json（看板「待确认」区）；
4. 全程 8 路并发 + 总时长预算，支持通过 HTTPS_PROXY 环境变量走代理（国内网络必需）。

用法：
    set DEEPSEEK_API_KEY=sk-xxx        :: Windows cmd
    export DEEPSEEK_API_KEY=sk-xxx     # macOS / Linux
    set HTTPS_PROXY=http://127.0.0.1:7890   :: 如网络无法直连 Google News
    python scripts/fetch_news.py
运行后把 data.json / pending.json 提交推送即可更新网站。
"""
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

# 个别公司名称容易撞名，给它们定制检索词；其余公司默认用「公司名 + ultrasonic」
QUERY_OVERRIDES = {
    "SONOTEC": "SONOTEC ultrasonic",
    "Zimmer Medizin Systeme": "Zimmer MedizinSysteme",
    "Söring": "Söring ultrasonic OR Soering ultrasonic",
    "PHYSIOMED": "PHYSIOMED ultrasonic",
    "Microsonic": "Microsonic ultrasonic sensor",
    "SONOSYS Ultraschallsysteme": "SONOSYS megasonic",
    "SONIMAT": "SONIMAT ultrasonic",
    "EDAP-Focal One": "EDAP TMS OR Focal One",
    "Ultex": "Ultex ultrasonic Japan",
    "Tokyo Instruments": "Tokyo Instruments ultrasonic",
    "JAPAN PROBE": "Japan Probe ultrasonic",
    "Insight K.K.": "Insight K.K. ultrasonic",
    "Fuji Ceramics": "Fuji Ceramics OR fujicera",
    "honda-el": "Honda Electronics ultrasonic",
    "Pillarhouse International": "Pillarhouse soldering",
    "Telsonic": "Telsonic ultrasonic",
    "Dukane": "Dukane ultrasonic",
}

# 自动发现新企业用的行业通用检索词
DISCOVERY_QUERIES = [
    "ultrasonic equipment manufacturer",
    "ultrasonic welding OR cutting equipment company",
    "piezoelectric transducer manufacturer",
    "ultrasonic cleaning equipment company",
    "ultrasonic testing NDT company",
]

WINDOW_DAYS = 30
MAX_PER_COMPANY = 5
MAX_TOTAL_TO_LLM = 60
MAX_FINAL = 40
MAX_PENDING = 30
FETCH_WORKERS = 8
FETCH_BUDGET_SEC = 240
PER_REQ_TIMEOUT = 25
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def load_json_file(name, default):
    path = os.path.join(ROOT, name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log("警告：%s 读取失败（%s），使用默认值" % (name, e))
        return default


def save_json_file(name, obj):
    path = os.path.join(ROOT, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    log("已写出 %s" % path)


def fetch_rss(query):
    url = ("https://news.google.com/rss/search?q=" + quote(query)
           + "&hl=en-US&gl=US&ceid=US:en")
    req = urllib.request.Request(url, headers=UA)
    # urllib 自动读取 HTTPS_PROXY / https_proxy 环境变量
    with urllib.request.urlopen(req, timeout=PER_REQ_TIMEOUT) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or ""
        src = it.find("source")
        source = (src.text or "").strip() if src is not None and src.text else "Google News"
        try:
            dt = parsedate_to_datetime(pub)
        except Exception:
            continue
        if not title or not link:
            continue
        out.append({"title": title, "url": link, "source": source, "dt": dt})
    return out


def clean_title(t):
    return re.sub(r"\s+-\s+[^-]{2,60}$", "", t).strip()


def _fetch_company(name, query, cutoff):
    try:
        items = fetch_rss(query)
    except Exception:
        return []
    out = []
    for it in items:
        if it["dt"] < cutoff:
            continue
        out.append({"company": name, "title": clean_title(it["title"]),
                    "url": it["url"], "source": it["source"],
                    "date": it["dt"].strftime("%Y-%m-%d"), "ts": it["dt"].timestamp()})
        if len(out) >= MAX_PER_COMPANY:
            break
    return out


def collect_raw(company_queries):
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    deadline = time.monotonic() + FETCH_BUDGET_SEC
    raw, seen = [], set()
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(_fetch_company, name, q, cutoff): name
                   for name, q in company_queries}
        for fut in as_completed(futures):
            if time.monotonic() > deadline:
                log("警告：抓取达到总时长预算，提前收尾")
                break
            try:
                rows = fut.result(timeout=max(1, deadline - time.monotonic()))
            except Exception:
                continue
            for r in rows:
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                raw.append(r)
    raw.sort(key=lambda x: -x["ts"])
    return raw[:MAX_TOTAL_TO_LLM]


def deepseek_chat(api_key, base_url, model, prompt, max_tokens=8000):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.load(r)
    return json.loads(resp["choices"][0]["message"]["content"])


def deepseek_analyze(api_key, base_url, model, raw):
    lines = []
    for i, it in enumerate(raw):
        lines.append("%d | %s | %s | %s | %s" % (i, it["company"], it["date"], it["source"], it["title"]))
    prompt = (
        "你是舆情分析员。下面是 Google News 抓取到的新闻标题清单，格式为：序号 | 目标公司 | 日期 | 来源 | 英文标题。\n"
        "目标公司是超声波设备/压电材料领域的厂商。请逐条处理：\n"
        "1. 判断这条新闻是否真的与该公司相关（同名不同行业、仅为顺带提及、或纯学术与该公司无关的，relevant=false）。\n"
        "2. 对相关新闻：title_zh=忠实的中文标题；summary=中文摘要2-3句（只能基于标题与常识推断，不得编造具体数字）；"
        "sentiment 三选一：positive（订单/新品/合作/融资/扩张/获奖/业绩增长）、negative（诉讼/亏损/召回/裁员/处罚/被收购中不利方）、neutral（其他）；"
        "reason=一句话情绪判断依据。\n"
        "只输出 JSON：{\"items\":[{\"i\":序号,\"relevant\":true/false,\"title_zh\":\"...\",\"summary\":\"...\",\"sentiment\":\"...\",\"reason\":\"...\"}]}，"
        "不相关的条目只需 i 和 relevant 两个字段。不要输出任何解释性文字。\n\n"
        + "\n".join(lines)
    )
    data = deepseek_chat(api_key, base_url, model, prompt)
    result = {}
    for obj in data.get("items", []):
        if isinstance(obj, dict) and "i" in obj:
            result[int(obj["i"])] = obj
    return result


def discover_new_companies(api_key, base_url, model, existing_names):
    """行业通用检索 → DeepSeek 识别名单之外的超声波/压电企业。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    raw, seen = [], set()
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = [pool.submit(fetch_rss, q) for q in DISCOVERY_QUERIES]
        for fut in as_completed(futures):
            try:
                items = fut.result(timeout=60)
            except Exception:
                continue
            for it in items:
                if it["dt"] < cutoff or it["url"] in seen:
                    continue
                seen.add(it["url"])
                raw.append(it)
    if not raw:
        return []
    raw = raw[:40]
    lines = ["%d | %s | %s | %s" % (i, it["source"], it["dt"].strftime("%Y-%m-%d"), clean_title(it["title"]))
             for i, it in enumerate(raw)]
    prompt = (
        "下面是超声波行业的新闻标题清单，格式为：序号 | 来源 | 日期 | 标题。\n"
        "已知监控名单：%s。\n"
        "请找出标题中明确提到、且属于超声波设备/超声换能器/压电材料制造商、但不在已知名单里的公司。"
        "排除：大学、研究所、仅有产品型号没有公司名的条目、经销商转述。\n"
        "只输出 JSON：{\"items\":[{\"i\":序号,\"name\":\"公司官方英文名\",\"country\":\"国家（不确定留空）\","
        "\"website\":\"官网域名（不确定留空）\",\"note\":\"一句中文说明依据\"}]}，没有就输出 {\"items\":[]}。\n\n"
        % ("、".join(existing_names))
        + "\n".join(lines)
    )
    data = deepseek_chat(api_key, base_url, model, prompt, max_tokens=3000)
    out = []
    for obj in data.get("items", []):
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        idx = obj.get("i")
        if not isinstance(idx, (int, float)) or not (0 <= int(idx) < len(raw)):
            continue
        it = raw[int(idx)]
        out.append({
            "name": str(obj["name"]).strip(),
            "country": str(obj.get("country") or "").strip(),
            "website": str(obj.get("website") or "").strip(),
            "note": str(obj.get("note") or "").strip()[:200],
            "url": it["url"], "source": it["source"],
            "date": it["dt"].strftime("%Y-%m-%d"),
            "firstSeen": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        })
    return out


def merge_pending(old_items, new_items, existing_names):
    exist_keys = {n.strip().lower() for n in existing_names}
    merged = {}
    for it in (old_items or []):
        k = (it.get("name") or "").strip().lower()
        if k and k not in exist_keys:
            merged[k] = it
    for it in new_items:
        k = it["name"].strip().lower()
        if k and k not in exist_keys:
            merged[k] = it
    items = sorted(merged.values(), key=lambda x: x.get("date", ""), reverse=True)
    return items[:MAX_PENDING]


def main():
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        sys.exit("缺少环境变量 DEEPSEEK_API_KEY")
    base_url = (os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip()
    model = (os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat").strip()

    companies = load_json_file("companies.json", {}).get("companies", [])
    if not companies:
        sys.exit("companies.json 为空或缺失，请先准备公司名单")
    names = [c["name"] for c in companies if c.get("name")]
    queries = [(n, QUERY_OVERRIDES.get(n, n + " ultrasonic")) for n in names]
    log("监控公司 %d 家，开始抓取…" % len(queries))

    raw = collect_raw(queries)
    log("抓取到候选新闻 %d 条，调用 DeepSeek 分析…" % len(raw))

    items = []
    if raw:
        verdicts = deepseek_analyze(api_key, base_url, model, raw)
        for i, it in enumerate(raw):
            v = verdicts.get(i)
            if not v or not v.get("relevant"):
                continue
            sentiment = v.get("sentiment")
            if sentiment not in ("positive", "neutral", "negative"):
                sentiment = "neutral"
            items.append({
                "company": it["company"],
                "title": str(v.get("title_zh") or it["title"])[:200],
                "summary": str(v.get("summary") or "")[:600],
                "sentiment": sentiment,
                "reason": str(v.get("reason") or "")[:200],
                "source": it["source"],
                "url": it["url"],
                "date": it["date"],
            })
            if len(items) >= MAX_FINAL:
                break

    now = datetime.now(timezone(timedelta(hours=8)))
    save_json_file("data.json", {
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "windowDays": WINDOW_DAYS,
        "items": items,
    })
    log("舆情条目 %d 条" % len(items))

    log("开始自动发现名单外新企业…")
    try:
        found = discover_new_companies(api_key, base_url, model, names)
    except Exception as e:
        log("警告：新企业发现失败（%s），保留原有 pending.json" % e)
        found = []
    old_pending = load_json_file("pending.json", {}).get("items", [])
    pending = merge_pending(old_pending, found, names)
    save_json_file("pending.json", {
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "items": pending,
    })
    log("待确认企业 %d 家（本次新发现 %d 家）" % (len(pending), len(found)))
    log("完成。请 git add data.json pending.json 并提交推送。")


if __name__ == "__main__":
    main()
