# -*- coding: utf-8 -*-
"""舆情抓取管线（GitHub Actions 版）：Google News RSS + DeepSeek，输出 data.json。"""
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

COMPANIES = [
    ("SONOTEC", "SONOTEC ultrasonic"),
    ("Zimmer Medizin Systeme", "Zimmer MedizinSysteme"),
    ("Söring", "Söring ultrasonic OR Soering ultrasonic"),
    ("PHYSIOMED", "PHYSIOMED ultrasonic"),
    ("Microsonic", "Microsonic ultrasonic sensor"),
    ("SONOSYS Ultraschallsysteme", "SONOSYS megasonic"),
    ("SONIMAT", "SONIMAT ultrasonic"),
    ("Imasonic", "Imasonic"),
    ("EDAP-Focal One", "EDAP TMS OR Focal One"),
    ("Pytheas technology", "Pytheas Technology"),
    ("Cedrat Technologies", "Cedrat Technologies"),
    ("Ultex", "Ultex ultrasonic Japan"),
    ("Tokyo Instruments", "Tokyo Instruments ultrasonic"),
    ("JAPAN PROBE", "Japan Probe ultrasonic"),
    ("Ueda Japan Radio", "Ueda Japan Radio"),
    ("Insight K.K.", "Insight K.K. ultrasonic"),
    ("Fuji Ceramics", "Fuji Ceramics OR fujicera"),
    ("honda-el", "Honda Electronics ultrasonic"),
    ("Blatek Industries", "Blatek"),
    ("Piezo Technologies", "Piezo Technologies"),
    ("Crest Ultrasonics", "Crest Ultrasonics"),
    ("Sono-Tek", "Sono-Tek"),
    ("Fabrisonic", "Fabrisonic"),
    ("Sonatest", "Sonatest"),
    ("Pillarhouse International", "Pillarhouse soldering"),
    ("Ionix Advanced Technologies", "Ionix Advanced Technologies"),
    ("Flosonics Medical", "Flosonics"),
    ("Telsonic", "Telsonic ultrasonic"),
    ("Dukane", "Dukane ultrasonic"),
]

WINDOW_DAYS = 30
MAX_PER_COMPANY = 5
MAX_TOTAL_TO_LLM = 60
MAX_FINAL = 40
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def fetch_rss(query):
    url = ("https://news.google.com/rss/search?q=" + quote(query)
           + "&hl=en-US&gl=US&ceid=US:en")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
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


def collect_raw():
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    raw, seen = [], set()
    for name, query in COMPANIES:
        try:
            items = fetch_rss(query)
        except Exception as e:
            print("RSS fail", name, e)
            continue
        kept = 0
        for it in items:
            if it["dt"] < cutoff or it["url"] in seen:
                continue
            seen.add(it["url"])
            raw.append({"company": name, "title": clean_title(it["title"]),
                        "url": it["url"], "source": it["source"],
                        "date": it["dt"].strftime("%Y-%m-%d"), "ts": it["dt"].timestamp()})
            kept += 1
            if kept >= MAX_PER_COMPANY:
                break
    raw.sort(key=lambda x: -x["ts"])
    return raw[:MAX_TOTAL_TO_LLM]


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
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 8000,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.load(r)
    data = json.loads(resp["choices"][0]["message"]["content"])
    result = {}
    for obj in data.get("items", []):
        if isinstance(obj, dict) and "i" in obj:
            result[int(obj["i"])] = obj
    return result


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY 环境变量")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip()

    raw = collect_raw()
    print("raw items:", len(raw))
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
    artifact = {
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "windowDays": WINDOW_DAYS,
        "items": items,
    }
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=1)
    print("written", out, "items:", len(items))


if __name__ == "__main__":
    main()
