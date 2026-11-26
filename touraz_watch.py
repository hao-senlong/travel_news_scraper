#!/usr/bin/env python3
"""
TourAZ announcement watcher.

Checks touraz.kr's "공고·공모" boards for new posts, filters them for topics
relevant to the company (code words + optional cheap AI check), and emails a
memo with the interesting ones.

Usage:
  python touraz_watch.py                 # normal run (fetch, filter, email, save state)
  python touraz_watch.py --dry-run       # no email, no state save; writes out/memo.html
  python touraz_watch.py --backfill-days 30   # treat posts from the last 30 days as new
                                              # (handy for a first test run)

Environment variables (set as GitHub Actions secrets):
  SMTP_USER, SMTP_PASSWORD   – e.g. a Gmail address + Gmail "app password"
  SMTP_HOST, SMTP_PORT       – optional, default smtp.gmail.com / 465
  MAIL_TO                    – comma-separated recipients
  MAIL_FROM                  – optional, defaults to SMTP_USER
  ANTHROPIC_API_KEY          – optional; enables AI filtering + memo writing
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

BASE = "https://touraz.kr"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "seen.json"
OUT_DIR = ROOT / "out"
KST = dt.timezone(dt.timedelta(hours=9))
PER_PAGE = 16
UA = "Mozilla/5.0 (compatible; touraz-watch/1.0; internal announcement monitor)"
TAB_NAMES = {"ktoip": "투어라즈 공모 (KTO)", "other": "유관기관 공모 (related bodies)"}

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"})


# ───────────────────────── fetching ─────────────────────────
def get_html(url: str) -> str:
    fixtures = os.environ.get("TOURAZ_FIXTURES")  # offline testing
    if fixtures:
        m = re.search(r"tabMode=(\w+)", url)
        page = re.search(r"curPage=(\d+)", url)
        if "pssrpView" in url or (page and page.group(1) != "1"):
            return "<html></html>"
        return (Path(fixtures) / f"fixture_{m.group(1)}.html").read_text("utf-8")
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            if attempt == 2:
                raise
            print(f"  retry {url}: {e}", file=sys.stderr)
            time.sleep(5)
    return ""


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_ktoip(page_html: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    items = []
    for a in soup.select("div.subject a"):
        li = a.find_parent("li")
        if not li:
            continue
        fields = {}
        for dl in li.select("dl"):
            dt_ = dl.find("dt")
            dd = dl.find("dd")
            if dt_ and dd:
                fields[clean(dt_.get_text())] = clean(dd.get_text())
        status = li.select_one(".category")
        org = li.select_one(".noti")
        org_txt = clean(org.get_text()) if org else ""
        if status:
            org_txt = clean(org_txt.replace(clean(status.get_text()), "", 1))
        items.append({
            "tab": "ktoip",
            "title": clean(a.get_text()),
            "url": BASE + a["href"],
            "posted": fields.get("등록일", ""),
            "period": fields.get("신청기간", ""),
            "dept": fields.get("담당부서", ""),
            "org": org_txt,
            "status": clean(status.get_text()) if status else "",
        })
    return items


def parse_other(page_html: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    items = []
    for a in soup.select(".col-subject a"):
        row = a.find_parent(class_="board-data-group")
        if not row:
            continue
        team = row.select_one(".col-team")
        date = row.select_one(".col-date")
        items.append({
            "tab": "other",
            "title": clean(a.get_text()),
            "url": BASE + a["href"],
            "posted": clean(date.get_text()) if date else "",
            "period": "",
            "dept": "",
            "org": clean(team.get_text()) if team else "",
            "status": "",
        })
    return items


def fetch_list(tab: str, pages: int) -> list[dict]:
    parser = parse_ktoip if tab == "ktoip" else parse_other
    out = []
    for p in range(1, pages + 1):
        url = f"{BASE}/announcementList?curPage={p}&tabMode={tab}&cntPerPage={PER_PAGE}"
        got = parser(get_html(url))
        print(f"  {tab} page {p}: {len(got)} posts")
        if not got:
            break
        out.extend(got)
        time.sleep(1)  # be polite
    return out


def fetch_detail(item: dict, limit: int = 2500) -> str:
    try:
        soup = BeautifulSoup(get_html(item["url"]), "html.parser")
    except Exception as e:  # detail is nice-to-have
        print(f"  detail failed for {item['title']}: {e}", file=sys.stderr)
        return ""
    node = soup.select_one(".view-content") or soup.select_one(".trend-view-body")
    if not node:
        return ""
    for bad in node.select("script, style"):
        bad.decompose()
    return clean(node.get_text(" "))[:limit]


def item_key(it: dict) -> str:
    # KTO tab IDs are re-encrypted on every request, so key on stable fields.
    raw = "|".join([it["tab"], it["title"], it["posted"], it["dept"] or it["org"]])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ───────────────────────── filtering ─────────────────────────
def compile_keywords(cfg: dict) -> list[tuple[str, re.Pattern]]:
    pats = []
    for cat, words in cfg["keywords"].items():
        parts = []
        for w in words:
            w = str(w)
            esc = re.escape(w)
            parts.append(rf"(?<![A-Za-z]){esc}(?![A-Za-z])" if re.fullmatch(r"[A-Za-z\- ]+", w) else esc)
        pats.append((cat, re.compile("|".join(parts), re.IGNORECASE)))
    return pats


def keyword_hits(it: dict, pats) -> dict[str, list[str]]:
    text = " ".join([it["title"], it["dept"], it["org"], it.get("detail", "")])
    hits = {}
    for cat, pat in pats:
        found = sorted({m.group(0) for m in pat.finditer(text)})
        if found:
            hits[cat] = found
    return hits


def call_claude(prompt: str, model: str, max_tokens: int = 2000) -> str:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])


def extract_json(text: str):
    m = re.search(r"\[.*\]", text, re.S)
    return json.loads(m.group(0)) if m else []


def llm_screen(items: list[dict], cfg: dict) -> dict[int, dict]:
    """One cheap call on titles only. Returns {index: {relevant, category, why}}."""
    lines = []
    for i, it in enumerate(items):
        kw = "; ".join(f"{c}: {', '.join(w)}" for c, w in it["kw"].items())
        lines.append(f"{i}. [{it['org']}/{it['dept']}] {it['title']}" + (f"  (keyword hits: {kw})" if kw else ""))
    prompt = f"""You screen Korean tourism-industry announcements for a company.

Company profile:
{cfg['company_profile']}

Below are new announcement titles (Korean), with the posting organisation/department.
Keyword hits are hints only: a hit can be a false positive, and relevant posts can have no hit.
Mark a post relevant only if it plausibly matters to this company's focus areas.

{chr(10).join(lines)}

Answer with ONLY a JSON array, one object per relevant post, e.g.
[{{"i": 3, "category": "MICE", "why": "one short {cfg['memo_language']} phrase"}}]
Use categories: "Russia/CIS", "Spanish-speaking", "MICE", "Medical/wellness", "Sports", "Other".
Return [] if nothing is relevant."""
    res = extract_json(call_claude(prompt, cfg["llm_model"], 1500))
    return {int(r["i"]): r for r in res if str(r.get("i", "")).isdigit() and int(r["i"]) < len(items)}


def llm_memo(items: list[dict], cfg: dict) -> list[dict]:
    """Second call, only for the relevant posts: short summary of each, from the detail text."""
    blocks = []
    for i, it in enumerate(items):
        blocks.append(f"### {i}\nTitle: {it['title']}\nOrg: {it['org']} {it['dept']}\n"
                      f"Posted: {it['posted']}  Application period: {it['period']}\n"
                      f"Text: {it.get('detail') or '(not available)'}")
    prompt = f"""Write a short internal memo entry in {cfg['memo_language']} for each announcement below,
for a company with this profile:
{cfg['company_profile']}

For each, give: an {cfg['memo_language']} translation of the title, a 1–3 sentence summary of what it is
(who can apply, what is offered, where/when), the deadline if stated, and one line on why it may matter to us.
Be factual; do not invent details that are not in the text.

{chr(10).join(blocks)}

Answer with ONLY a JSON array:
[{{"i": 0, "title_en": "...", "summary": "...", "deadline": "... or empty", "relevance": "..."}}]"""
    return extract_json(call_claude(prompt, cfg["llm_model"], 3000))


# ───────────────────────── memo + email ─────────────────────────
def build_memo(picked: list[dict], rejected: list[dict], scanned: int, new_count: int, ai: bool) -> tuple[str, str]:
    today = dt.datetime.now(KST).strftime("%Y-%m-%d")
    subject = f"TourAZ watch {today}: {len(picked)} relevant post(s)" if picked else f"TourAZ watch {today}: nothing relevant"
    h = [f"<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px;max-width:760px'>",
         f"<h2 style='margin-bottom:4px'>TourAZ announcements – {today}</h2>",
         f"<p style='color:#666;margin-top:0'>{new_count} new post(s) since the last check "
         f"({scanned} scanned); {len(picked)} look relevant. "
         f"Filter: {'code words + AI' if ai else 'code words'}.</p>"]
    by_cat: dict[str, list[dict]] = {}
    for it in picked:
        by_cat.setdefault(it["category"], []).append(it)
    for cat, its in by_cat.items():
        h.append(f"<h3 style='border-bottom:1px solid #ddd;padding-bottom:3px'>{html.escape(cat)}</h3>")
        for it in its:
            title = html.escape(it["title"])
            en = html.escape(it.get("title_en", ""))
            h.append("<div style='margin:0 0 16px'>")
            h.append(f"<a href='{html.escape(it['url'])}' style='font-weight:bold'>{en or title}</a>")
            if en:
                h.append(f"<br><span style='color:#555'>{title}</span>")
            meta = " · ".join(x for x in [it["org"], it["dept"], f"posted {it['posted']}",
                                          f"apply {it['period']}" if it["period"] else "",
                                          f"deadline {it['deadline']}" if it.get("deadline") else ""] if x)
            h.append(f"<br><small style='color:#777'>{html.escape(meta)}</small>")
            if it.get("summary"):
                h.append(f"<p style='margin:6px 0 2px'>{html.escape(it['summary'])}</p>")
            elif it.get("detail"):
                h.append(f"<p style='margin:6px 0 2px;color:#333'>{html.escape(it['detail'][:400])}…</p>")
            why = it.get("relevance") or it.get("why")
            if why:
                h.append(f"<p style='margin:2px 0;color:#2a6'><i>Why: {html.escape(why)}</i></p>")
            if it["kw"]:
                kws = "; ".join(f"{c}: {', '.join(w)}" for c, w in it["kw"].items())
                h.append(f"<small style='color:#999'>Code words: {html.escape(kws)}</small>")
            h.append("</div>")
    if rejected:
        h.append("<p style='color:#999;font-size:12px'>Code-word matches the AI judged not relevant: "
                 + "; ".join(html.escape(r["title"]) for r in rejected) + "</p>")
    h.append("<p style='color:#aaa;font-size:11px'>Automated check of "
             "<a href='https://touraz.kr/announcementList?tabMode=ktoip'>touraz.kr 공고·공모</a>. "
             "Edit config.yaml in the repo to change code words.</p></div>")
    return subject, "\n".join(h)


def send_email(subject: str, body_html: str) -> None:
    user = os.environ["SMTP_USER"]
    to = [x.strip() for x in os.environ["MAIL_TO"].split(",") if x.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM") or user
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(BeautifulSoup(body_html, "html.parser").get_text("\n"), "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "465")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx) as s:
            s.login(user, os.environ["SMTP_PASSWORD"])
            s.sendmail(msg["From"], to, msg.as_string())
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls(context=ctx)
            s.login(user, os.environ["SMTP_PASSWORD"])
            s.sendmail(msg["From"], to, msg.as_string())
    print(f"Email sent to {', '.join(to)}")


# ───────────────────────── main ─────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no email, no state save")
    ap.add_argument("--backfill-days", type=int, default=0,
                    help="on a first run, treat posts from the last N days as new")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
    pats = compile_keywords(cfg)
    use_ai = bool(os.environ.get("ANTHROPIC_API_KEY"))

    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    seen: dict = state.get("seen", {})
    first_run = not seen

    print("Fetching lists…")
    items = []
    for tab, pages in cfg["tabs"].items():
        items.extend(fetch_list(tab, int(pages)))
    if not items:
        print("ERROR: no posts parsed – the site layout may have changed.", file=sys.stderr)
        return 2

    today = dt.datetime.now(KST).date()
    uniq = {}
    for it in items:
        uniq.setdefault(item_key(it), it)
    new = []
    for k, it in uniq.items():
        if k in seen:
            continue
        if first_run:
            try:
                age = (today - dt.date.fromisoformat(it["posted"][:10])).days
            except ValueError:
                age = 9999
            if age > args.backfill_days:
                seen[k] = today.isoformat()   # baseline: remember silently
                continue
        new.append((k, it))
    print(f"{len(uniq)} posts scanned, {len(new)} new")

    picked, rejected = [], []
    if new:
        news = [it for _, it in new]
        for it in news:
            it["kw"] = keyword_hits(it, pats)
        if use_ai:
            try:
                verdict = llm_screen(news, cfg)
                for i, it in enumerate(news):
                    if i in verdict:
                        it["category"] = verdict[i].get("category", "Other")
                        it["why"] = verdict[i].get("why", "")
                        picked.append(it)
                    elif it["kw"]:
                        rejected.append(it)
            except Exception as e:
                print(f"AI screen failed ({e}); falling back to code words", file=sys.stderr)
                use_ai = False
        if not use_ai:
            picked = [it for it in news if it["kw"]]
            for it in picked:
                it["category"] = next(iter(it["kw"]))
        for it in picked:
            it["detail"] = fetch_detail(it)
            it["kw"] = keyword_hits(it, pats) or it["kw"]
            time.sleep(1)
        if use_ai and picked:
            try:
                for r in llm_memo(picked, cfg):
                    i = int(r.get("i", -1))
                    if 0 <= i < len(picked):
                        picked[i].update({k: r.get(k, "") for k in ("title_en", "summary", "deadline", "relevance")})
            except Exception as e:
                print(f"AI memo failed ({e}); sending without summaries", file=sys.stderr)

    subject, body = build_memo(picked, rejected, len(uniq), len(new), use_ai)
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "memo.html").write_text(body, "utf-8")
    print(subject)
    for it in picked:
        print(f"  [{it['category']}] {it['title']}")

    if args.dry_run:
        print("Dry run: memo written to out/memo.html; no email, state not saved.")
        return 0

    if picked or cfg.get("send_when_empty"):
        send_email(subject, body)
    for k, _ in new:
        seen[k] = today.isoformat()
    cutoff = (today - dt.timedelta(days=400)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({"updated": today.isoformat(), "seen": seen}, indent=0, sort_keys=True), "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
