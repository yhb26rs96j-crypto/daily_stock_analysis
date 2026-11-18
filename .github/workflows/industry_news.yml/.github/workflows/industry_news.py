#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
医疗+核药行业新闻 · 云端每日推送脚本
运行环境：GitHub Actions（ubuntu-latest，Python3），零第三方依赖。
用 DeepSeek Search API 联网检索 + DeepSeek chat 筛选概括 + QQ 邮箱推送。

凭证（从 GitHub Actions secrets 注入，见 workflow）：
  DEEPSEEK_API_KEY   股票分析已在用的同一把 key
  EMAIL_SENDER       QQ 邮箱地址
  EMAIL_PASSWORD     QQ 邮箱 SMTP 授权码（16 位，非登录密码）
  EMAIL_RECEIVERS    收件人，逗号分隔；留空=发给自己
"""
import os
import sys
import json
import ssl
import smtplib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

BASE = "https://api.deepseek.com"
SEARCH_URL = BASE + "/v1/tools/search"
CHAT_URL = BASE + "/v1/chat/completions"
MODEL = "deepseek-v4-flash"

# 检索关键词：覆盖核药（核心业务）+ 医疗大行业。每条独立 query，稳定可追溯。
QUERIES = [
    "钇90 Y-90 肝癌 放射性微球 临床 获批",
    "镥177 Lu-177 PSMA 前列腺癌 Pluvicto",
    "锕225 Ac-225 放射性核素 治疗",
    "放射性药物 RDC 核药 获批 临床 进展",
    "核医学 医用同位素 供应 产能 国产化",
    "核药 并购 融资 合作 授权 license",
    "诺华 Telix 纽瑞特 东诚 远大医药 核药 进展",
    "创新药 获批 上市 NMPA 临床",
    "医保 集采 医药 政策 降价",
    "生物医药 医疗器械 药企 融资 合作",
]


def http_json(url, payload, api_key, timeout=30):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ds_search(api_key, query, count=10):
    """DeepSeek 联网搜索，限定近 3 天（北京时间往前推），中英文源。"""
    beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
    after_date = (beijing_now - timedelta(days=3)).strftime("%Y-%m-%d")
    q = f"{query} lang:zh,en after:{after_date}"
    return http_json(SEARCH_URL, {"query": q, "count": count, "need_content": False}, api_key)


def ds_chat(api_key, messages):
    return http_json(CHAT_URL, {"model": MODEL, "messages": messages, "temperature": 0.2}, api_key, timeout=180)


def gather_candidates(api_key):
    """跑全部 query，按 URL 去重，返回候选新闻列表。"""
    seen = {}
    for q in QUERIES:
        try:
            res = ds_search(api_key, q)
            for r in res.get("results", []):
                url = r.get("url")
                if url and url not in seen:
                    snippet = r.get("snippet", "") or r.get("summary", "")[:200]
                    seen[url] = {
                        "title": r.get("title", ""),
                        "url": url,
                        "snippet": snippet,
                        "site_name": r.get("site_name") or "",
                        "date": r.get("date") or "",
                    }
        except Exception as e:
            print(f"[warn] 检索失败 {q!r}: {e}", file=sys.stderr)
    print(f"[info] 候选新闻 {len(seen)} 条（去重后）", file=sys.stderr)
    return list(seen.values())


def summarize(api_key, candidates):
    """交给模型：去重、判时效、分两类、写一句话概括。"""
    beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
    today = beijing_now.strftime("%Y-%m-%d")

    payload = json.dumps(candidates, ensure_ascii=False)
    prompt = f"""你是为医药行业北区销售总监整理每日行业资讯的助手。今天是 {today}（北京时间）。

下面是联网检索到的候选新闻（JSON，含 title/url/summary/site_name/date）。请：
1. 剔除：与医药/核药无关的、重复报道同一事件的（只留一条）、明显过时的（date 早于近 72 小时的旧闻）、低质量营销软文。
2. 分两类：
   - nuclear（核药行业）：放射性药物(RDC)/核医学/医用同位素/核药企业动态/临床与审批（钇90、镥177、锕225、PSMA 等）。
   - medical（医疗行业）：医药/生物医药/医疗器械/医疗服务/医保政策/药企动态/审批上市。
3. 每类最多 10 条，宁缺毋滥；不足 10 条就如实给实际条数，绝不虚构。
4. 每条写一句话概括（中文，30 字内，含关键数字/主体/时间），并给出来源名（site_name）和 url。

只输出 JSON，不要任何解释文字，格式：
{{"nuclear": [{{"headline": "...", "source": "...", "url": "..."}}], "medical": [...]}}

候选新闻：
{payload}"""

    res = ds_chat(
        api_key,
        [
            {"role": "system", "content": "你只输出合法 JSON，不输出任何其他文字。"},
            {"role": "user", "content": prompt},
        ],
    )
    content = res["choices"][0]["message"]["content"].strip()
    # 去掉可能的 ```json ... ``` 包裹
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
        content = content.strip()
    return json.loads(content)


def build_email_body(result, beijing_now):
    date_str = beijing_now.strftime("%Y-%m-%d")
    weekday = "一二三四五六日"[beijing_now.weekday()]
    lines = [f"📌 医疗+核药行业新闻 · {date_str}（周{weekday}）", ""]

    nuclear = result.get("nuclear", [])
    medical = result.get("medical", [])

    for label, items in [("核药行业", nuclear), ("医疗行业", medical)]:
        lines.append(f"【{label}】（{len(items)} 条）")
        if items:
            for i, it in enumerate(items, 1):
                src = it.get("source") or "来源"
                url = it.get("url", "")
                lines.append(f"{i}. {it.get('headline', '').strip()}（{src}）")
                if url:
                    lines.append(f"   🔗 {url}")
        else:
            lines.append("   （今日该板块无新增可验证新闻）")
        lines.append("")

    total = len(nuclear) + len(medical)
    lines.append(f"—— 云端自动推送 · 今日实际整理 {total} 条 · 来源均可点开验证")
    return "\n".join(lines)


def send_email(body, subject):
    sender = os.environ.get("EMAIL_SENDER", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "").strip()
    receivers = [r.strip() for r in os.environ.get("EMAIL_RECEIVERS", "").split(",") if r.strip()]
    if not receivers:
        receivers = [sender]
    if not sender or not password:
        raise RuntimeError("缺少 EMAIL_SENDER / EMAIL_PASSWORD 配置")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = ", ".join(receivers)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.qq.com", 465, context=ctx, timeout=30) as s:
        s.login(sender, password)
        s.sendmail(sender, receivers, msg.as_string())
    print(f"[info] 邮件已发送 -> {receivers}", file=sys.stderr)


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")

    candidates = gather_candidates(api_key)
    if not candidates:
        print("[warn] 无候选新闻，可能检索全部失败", file=sys.stderr)
        sys.exit(1)

    result = summarize(api_key, candidates)

    beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
    body = build_email_body(result, beijing_now)

    if "--no-send" in sys.argv:
        print(body)
        return

    subject = f"医疗+核药行业新闻 · {beijing_now.strftime('%Y-%m-%d')}"
    send_email(body, subject)
    print(body)


if __name__ == "__main__":
    main()
