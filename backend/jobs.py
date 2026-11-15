#!/usr/bin/env python3
"""AI Models Tracker fetch jobs. Usage: python -m backend.jobs [prices|deprecations|news|icons|all]

prices        -> data/catalog.json, data/best.json, data/changes.json
deprecations  -> data/deprecations.json
news          -> data/news.json
icons         -> data/icons/                                            (run daily)
All jobs also write data/status.json (per-source health) and data/meta.json.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dparse

ROOT = Path(os.environ.get("MODEL_ATLAS_ROOT", Path(__file__).resolve().parents[1])).resolve()
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
ICONS = DATA / "icons"
CFG = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
UA = CFG["user_agent"]
ALIAS = {a: pid for pid, p in CFG["providers"].items() for a in p.get("aliases", []) + [pid]}


# ---------- helpers ----------
def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load(name, default):
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def save(name, obj):
    target = DATA / name
    temp = DATA / f".{name}.tmp"
    temp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    temp.replace(target)


def get(url):
    err = None
    for i in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            return r
        except Exception as e:  # retry with backoff
            err = e
            time.sleep(2 ** i)
    raise err


def tracked(name, fn):
    """Run a fetcher. On failure keep old data and record the error in status.json."""
    st = load("status.json", {})
    record = None
    n = None
    try:
        n = fn()
        record = {"ok": True, "last_ok": now(), "rows": n, "error": None}
        if n == 0:
            record["warning"] = "0 rows found - the source layout may have changed"
    except Exception as e:
        record = {**load("status.json", {}).get(name, st.get(name, {})),
                  "ok": False, "last_attempt": now(), "error": str(e)[:300]}
        print(f"[{name}] FAILED: {e}", file=sys.stderr)
    st = load("status.json", {})
    st[name] = record
    save("status.json", st)
    return n


def add_changes(items):
    if not items:
        return
    ts = now()
    keep = CFG.get("retention", {}).get("changes", 5000)
    save("changes.json", ([{"ts": ts, **i} for i in items] + load("changes.json", []))[:keep])
    hook = os.getenv("WEBHOOK_URL")  # optional Slack/Discord-compatible webhook
    if hook:
        text = "AI Models Tracker: " + "; ".join(f"{i['type']}: {i['name']}" for i in items[:10])
        try:
            requests.post(hook, json={"text": text, "content": text}, timeout=15)
        except Exception:
            pass


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.split("/")[-1].lower())


def per_m(x):
    try:
        return round(float(x) * 1e6, 4)  # OpenRouter prices are per token
    except Exception:
        return None


def write_meta():
    icons = {f.stem: f"data/icons/{f.name}" for f in sorted(ICONS.glob("*"))} if ICONS.exists() else {}
    save("meta.json", {"providers": CFG["providers"], "icons": icons,
                       "purposes": {k: {"label": v["label"], "description": v["description"]}
                                    for k, v in CFG["purposes"].items()}})


# ---------- prices / catalog ----------
def num(x):
    try:
        return float(x)
    except Exception:
        return None


def fetch_openrouter():
    out = {}
    for m in get(CFG["openrouter_url"]).json().get("data", []):
        pr, arch, top = m.get("pricing") or {}, m.get("architecture") or {}, m.get("top_provider") or {}
        params, created = m.get("supported_parameters") or [], m.get("created")
        desc = (m.get("description") or "").strip()
        out[norm(m["id"])] = {
            "id": m["id"], "openrouter_id": m["id"], "name": m.get("name") or m["id"],
            "provider": ALIAS.get(m["id"].split("/")[0].lower(), m["id"].split("/")[0].lower()),
            "context": m.get("context_length") or top.get("context_length"),
            "max_output": top.get("max_completion_tokens"),
            "input_price": per_m(pr.get("prompt")), "output_price": per_m(pr.get("completion")),
            "cache_read_price": per_m(pr.get("input_cache_read")), "cache_write_price": per_m(pr.get("input_cache_write")),
            "reasoning_price": per_m(pr.get("internal_reasoning")),
            "request_price": num(pr.get("request")) or None,
            "release_date": dt.datetime.fromtimestamp(created, dt.timezone.utc).date().isoformat() if created else None,
            "expiration_date": m.get("expiration_date"),
            "tool_call": "tools" in params,
            "reasoning": True if "reasoning" in params else None,
            "structured_output": "structured_outputs" in params,
            "json_mode": "response_format" in params,
            "web_search": "web_search_options" in params,
            "temperature": "temperature" in params,
            "modalities": arch.get("input_modalities"), "outputs": arch.get("output_modalities"),
            "tokenizer": arch.get("tokenizer"), "supported_parameters": params,
            "description": desc,
            "hugging_face_id": m.get("hugging_face_id") or None, "moderated": top.get("is_moderated"),
            "source": ["openrouter"],
        }
    return out


def fetch_modelsdev():
    out = {}
    for pid, prov in get(CFG["modelsdev_url"]).json().items():
        first = pid in ALIAS
        if not first and not CFG.get("modelsdev_all"):
            continue
        for mid, m in (prov.get("models") or {}).items():
            c, lim = m.get("cost") or {}, m.get("limit") or {}
            if not first and norm(mid) in out:  # first-party / earlier entry wins over resellers
                continue
            out[norm(mid)] = {
                "id": f"{pid}/{mid}", "name": m.get("name", mid), "provider": ALIAS.get(pid, pid), "reseller": not first,
                "context": lim.get("context"), "max_output": lim.get("output"),
                "input_price": c.get("input"), "output_price": c.get("output"),
                "cache_read_price": c.get("cache_read"), "cache_write_price": c.get("cache_write"),
                "reasoning_price": c.get("reasoning"),
                "reasoning": m.get("reasoning"), "tool_call": m.get("tool_call"), "attachment": m.get("attachment"),
                "temperature": m.get("temperature"), "structured_output": m.get("structured_output"),
                "open_weights": m.get("open_weights"), "release_date": m.get("release_date"),
                "last_updated": m.get("last_updated"), "knowledge": m.get("knowledge"),
                "status": m.get("status"), "family": m.get("family"),
                "modalities": (m.get("modalities") or {}).get("input"), "outputs": (m.get("modalities") or {}).get("output"),
                "source": ["models.dev"],
            }
    return out


def parse_qwen_catalog(html, catalog_url):
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for card in soup.select("a.adoc-help-center-card[href]"):
        title = card.select_one(".adoc-help-center-card-title")
        if not title:
            continue
        model_id = title.get_text(" ", strip=True)
        if not re.match(r"^qwen(?:[-_.]|\d|$)", model_id, re.IGNORECASE):
            continue
        description = card.select_one(".adoc-help-center-card-body")
        image = card.select_one("img[src]")
        key = norm(model_id)
        entry = out.setdefault(key, {
            "id": model_id,
            "name": model_id,
            "provider": ALIAS.get("alibaba", "alibaba"),
            "official_description": "",
            "official_url": urljoin(catalog_url, card["href"]),
            "catalog_url": catalog_url,
            "image_url": urljoin(catalog_url, image["src"]) if image else None,
            "source": ["alibaba_model_studio"],
        })
        if description:
            detail = description.get_text(" ", strip=True)
            if detail and detail not in entry["official_description"].split("; "):
                entry["official_description"] = "; ".join(filter(None, [entry["official_description"], detail]))
    return out


def fetch_qwen_catalog():
    url = CFG["qwen_catalog_url"]
    models = parse_qwen_catalog(get(url).text, url)
    if not models:
        raise RuntimeError("No Qwen models found in the Alibaba Model Studio catalog")
    return models


def parse_qwen_capabilities(html, details_url, catalog_url):
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for table in soup.find_all("table"):
        headers = [cell.get_text(" ", strip=True).lower() for cell in table.find_all("th")]
        model_column = next((i for i, h in enumerate(headers) if "model id" in h), None)
        capability_column = next((i for i, h in enumerate(headers) if "capability" in h), None)
        if model_column is None or capability_column is None:
            continue
        for row in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if max(model_column, capability_column) >= len(cells):
                continue
            model_id = cells[model_column]
            if not re.match(r"^qwen(?:[-_.]|\d|$)", model_id, re.IGNORECASE):
                continue
            key = norm(model_id)
            out[key] = {
                "id": model_id,
                "name": model_id,
                "provider": ALIAS.get("alibaba", "alibaba"),
                "official_capabilities": [cap.strip() for cap in cells[capability_column].split(",") if cap.strip()],
                "official_url": details_url,
                "catalog_url": catalog_url,
                "source": ["alibaba_model_studio"],
            }
    for primary, alias in re.findall(r"\bThe alias of ([\w.-]+) is ([\w.-]+)\b", soup.get_text(" ", strip=True), re.IGNORECASE):
        key = norm(primary)
        if key in out:
            out[key]["aliases"] = [alias]
    return out


def fetch_qwen_capabilities():
    url = CFG["qwen_details_url"]
    models = parse_qwen_capabilities(get(url).text, url, CFG["qwen_catalog_url"])
    if not models:
        raise RuntimeError("No Qwen capability rows found in Alibaba Model Studio documentation")
    return models


OPS = {"eq": lambda a, b: a == b, "gt": lambda a, b: a is not None and a > b,
       "gte": lambda a, b: a is not None and a >= b, "lte": lambda a, b: a is not None and a <= b,
       "truthy": lambda a, _: bool(a)}


def rank(models):
    today = dt.date.today().isoformat()
    live = [m for m in models if not (m.get("expiration_date") or "9999") < today]
    out = {}
    for pid, spec in CFG["purposes"].items():
        pool = [m for m in live if all(OPS[op](m.get(f), v) for f, op, v in spec.get("filter", []))]
        for f, d in reversed(spec["sort"]):  # stable multi-key sort, missing values last
            if d == "desc":
                pool.sort(key=lambda m: (m.get(f) is not None, m.get(f) if m.get(f) is not None else 0), reverse=True)
            else:
                pool.sort(key=lambda m: (m.get(f) is None, m.get(f) if m.get(f) is not None else 0))
        by_provider = {}
        for m in pool:
            if m["provider"] in CFG["providers"]:
                by_provider.setdefault(m["provider"], m["key"])
        out[pid] = {"top": [m["key"] for m in pool[:10]], "by_provider": by_provider}
    return out


KEEP_FRESH = {"key", "expiration_date", "listed", "last_seen", "first_seen", "updated_at", "price_history", "missing_since"}


def prices_job():
    def run():
        prev = {m["key"]: m for m in load("catalog.json", {}).get("models", [])}
        today = dt.date.today().isoformat()
        errs, orr, md, qwen = [], {}, {}, {}
        try:
            orr = fetch_openrouter()
        except Exception as e:
            errs.append(f"openrouter: {e}")
        try:
            md = fetch_modelsdev()
        except Exception as e:
            errs.append(f"models.dev: {e}")
        qwen_result = tracked("qwen_model_studio", lambda: qwen.update(fetch_qwen_catalog()) or len(qwen))
        if qwen_result is None:
            errs.append("Alibaba Model Studio Qwen catalog unavailable")
        qwen_details = {}
        details_result = tracked(
            "qwen_model_studio_details",
            lambda: qwen_details.update(fetch_qwen_capabilities()) or len(qwen_details),
        )
        if details_result is None:
            errs.append("Alibaba Model Studio Qwen capability data unavailable")
        for key, details in qwen_details.items():
            if key not in qwen:
                qwen[key] = details
            else:
                qwen[key]["official_capabilities"] = details["official_capabilities"]
                if details.get("aliases"):
                    qwen[key]["aliases"] = details["aliases"]
                qwen[key]["source"] = sorted(set(qwen[key]["source"] + details["source"]))
        if not orr and not md and not qwen:
            raise RuntimeError("; ".join(errs))
        models, changes = [], []
        for k in set(orr) | set(md) | set(qwen):
            a, b, c = orr.get(k, {}), md.get(k, {}), qwen.get(k, {})
            if b.get("reseller") and a:  # OpenRouter knows the real author; keep it
                b = {x: y for x, y in b.items() if x not in ("provider", "id", "reseller")}
            m = {**a, **{x: y for x, y in b.items() if y is not None}, **{x: y for x, y in c.items() if y is not None}}
            m["key"] = k
            for f in ("modalities", "outputs"):  # union, so one narrower source can't hide a capability
                m[f] = sorted(set(a.get(f) or []) | set(b.get(f) or []) | set(c.get(f) or [])) or None
            m["source"] = sorted(set(a.get("source", []) + b.get("source", []) + c.get("source", [])))
            p = prev.get(k)
            if p:
                for f, v in p.items():  # keep known values that the sources omitted this time
                    if f not in KEEP_FRESH and m.get(f) is None and v is not None:
                        m[f] = v
                price_changed = (p.get("input_price"), p.get("output_price")) != (m.get("input_price"), m.get("output_price"))
                m["first_seen"] = p.get("first_seen")
                m["updated_at"] = today if price_changed or p.get("context") != m.get("context") else p.get("updated_at")
                hist = list(p.get("price_history") or [])
                if price_changed:
                    hist.append({"date": today, "input": m.get("input_price"), "output": m.get("output_price")})
                    changes.append({"type": "price_change", "key": k, "name": m["name"], "provider": m["provider"],
                                    "from": [p.get("input_price"), p.get("output_price")],
                                    "to": [m.get("input_price"), m.get("output_price")]})
                if p.get("listed") is False:
                    changes.append({"type": "relisted", "key": k, "name": m["name"], "provider": m["provider"]})
                m["price_history"] = hist[-30:]
            else:
                m["first_seen"] = m["updated_at"] = today if prev else None
                m["price_history"] = []
                if prev:
                    changes.append({"type": "new_model", "key": k, "name": m["name"], "provider": m["provider"]})
            m["listed"], m["last_seen"] = True, today
            models.append(m)
        cur = {m["key"] for m in models}
        for k, p in prev.items():  # NEVER delete: keep models the sources no longer report, flagged
            if k not in cur:
                qwen_only = set(p.get("source", [])) == {"alibaba_model_studio"}
                if not errs and not qwen_only and p.get("listed") is not False:  # only judge complete catalogs
                    changes.append({"type": "removed", "key": k, "name": p["name"], "provider": p["provider"]})
                    p = {**p, "listed": False, "missing_since": today}
                models.append(p)
        models.sort(key=lambda m: m.get("release_date") or "", reverse=True)
        add_changes(changes)
        save("catalog.json", {"updated": now(), "partial_errors": errs, "models": models})
        save("best.json", rank([m for m in models if m.get("listed") is not False]))
        return len(models)

    write_meta()
    tracked("prices", run)


# ---------- deprecations ----------
MONTH = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
DATE_RE = re.compile(rf"(\d{{4}}-\d{{2}}-\d{{2}}|(?:{MONTH})[a-z]*\.? \d{{1,2}},? \d{{4}}|\d{{1,2}} (?:{MONTH})[a-z]* \d{{4}})")


def parse_date(s):
    try:
        return dparse.parse(s, fuzzy=False).date().isoformat()
    except Exception:
        return None


def parse_page(name, url):
    soup = BeautifulSoup(get(url).text, "html.parser")
    rows = []
    for t in soup.find_all("table"):
        heads = [h.get_text(" ", strip=True) for h in t.find_all("th")][:6]
        for tr in t.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            if not cells:
                continue
            dates = sorted({d for d in map(parse_date, DATE_RE.findall(" | ".join(cells))) if d})
            if dates:
                rows.append({"provider": name, "model": cells[0][:120], "headers": heads, "cells": [c[:120] for c in cells[:6]],
                             "dates": dates, "retire_guess": dates[-1], "source_url": url, "origin": "official page"})
    return rows


def parse_qwen_retirements(html, url):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for item in soup.find_all(["li", "p"]):
        text = item.get_text(" ", strip=True)
        if "retired after the preview ends" not in text.lower():
            continue
        model = re.search(r"\b([\w.-]+)\s+preview benefits\b", text, re.IGNORECASE)
        end_time = re.search(r"\bto\s+(\d{1,2}:\d{2})\s+on\b", text, re.IGNORECASE)
        if not model or not end_time:
            continue
        date_match = DATE_RE.search(text, end_time.end())
        end_date = parse_date(date_match.group(0)) if date_match else None
        if not end_date:
            continue
        retire_date = dt.date.fromisoformat(end_date)
        if end_time.group(1) == "24:00":
            retire_date += dt.timedelta(days=1)
        date = retire_date.isoformat()
        rows.append({
            "provider": ALIAS.get("alibaba", "alibaba"),
            "model": model.group(1),
            "headers": [],
            "cells": [text[:500]],
            "dates": [date],
            "retire_guess": date,
            "source_url": url,
            "origin": "official page",
        })
    return rows


def deprecations_job():
    write_meta()
    today = dt.date.today().isoformat()
    old = load("deprecations.json", {}).get("rows", [])
    sig = lambda r: (r["provider"], r["model"], r["retire_guess"])
    oldmap = {sig(r): r for r in old}

    def fresh(r):  # row seen on the source right now
        p = oldmap.get(sig(r))
        r.update(listed=True, last_seen=today, first_seen=p.get("first_seen") if p else (today if old else None))
        return r

    rows = []
    for name, url in CFG["deprecation_pages"].items():
        got = []

        def run():
            if name == "alibaba":
                got.extend(parse_qwen_retirements(get(url).text, url))
            else:
                got.extend(parse_page(name, url))
            return len(got)

        tracked(f"deprecations:{name}", run)
        rows += [fresh(r) for r in got] if got else [r for r in old if r["source_url"] == url]  # failed/empty: keep as is
    for m in load("catalog.json", {}).get("models", []):  # API-reported shutdown dates
        if m.get("expiration_date"):
            d = parse_date(str(m["expiration_date"]))
            if d:
                rows.append(fresh({"provider": m["provider"], "model": m["name"], "headers": [], "cells": [],
                                   "dates": [d], "retire_guess": d, "source_url": "https://openrouter.ai/models",
                                   "origin": "openrouter api"}))
    cur = {sig(r) for r in rows}
    rows += [{**r, "listed": False} for r in old if sig(r) not in cur]  # NEVER delete: keep rows that left the page
    if old:
        add_changes([{"type": "deprecation", "name": f"{r['model']} ({r['retire_guess']})", "provider": r["provider"],
                      "link": r["source_url"]} for r in rows if sig(r) not in oldmap][:20])
    save("deprecations.json", {"updated": now(), "rows": rows})


# ---------- news ----------
def news_job():
    def run():
        old = load("news.json", {}).get("items", [])
        old_by_link = {item["link"]: item for item in old if item.get("link")}
        new = []

        def description(entry):
            parts = [entry.get("summary")]
            parts.extend(
                content.get("value")
                for content in (entry.get("content") or [])
                if content.get("value")
            )
            for part in parts:
                if not part:
                    continue
                text = BeautifulSoup(str(part), "html.parser").get_text(" ", strip=True)
                if text:
                    return text[:497].rsplit(" ", 1)[0] + "..." if len(text) > 500 else text
            return None

        def image(entry, article_url):
            candidates = []
            for key in ("media_thumbnail", "media_content"):
                for media in (entry.get(key) or []):
                    media_type = str(media.get("type") or "").lower()
                    if key == "media_thumbnail" or media.get("medium") == "image" or media_type.startswith("image/"):
                        candidates.append(media.get("url"))
            for enclosure in (entry.get("links") or []):
                if enclosure.get("rel") == "enclosure" and str(enclosure.get("type") or "").startswith("image/"):
                    candidates.append(enclosure.get("href"))
            for markup in (
                entry.get("summary"),
                *(content.get("value") for content in (entry.get("content") or [])),
            ):
                if markup:
                    candidates.extend(
                        tag.get("src") for tag in BeautifulSoup(str(markup), "html.parser").find_all("img")
                    )
            for candidate in candidates:
                if not candidate:
                    continue
                url = urljoin(article_url, str(candidate))
                if url.startswith(("https://", "http://")):
                    return url
            return None

        for name, url in CFG["feeds"].items():
            for e in feedparser.parse(url, agent=UA).entries[:30]:
                link = e.get("link")
                if not link:
                    continue
                ts = e.get("published_parsed") or e.get("updated_parsed")
                item = {
                    "source": name,
                    "title": str(e.get("title") or "").strip(),
                    "link": link,
                    "published": time.strftime("%Y-%m-%dT%H:%M:%SZ", ts) if ts else None,
                    "description": description(e),
                    "image": image(e, link),
                }
                existing = old_by_link.get(link)
                if existing:
                    for field in ("description", "image", "published"):
                        if not existing.get(field) and item.get(field):
                            existing[field] = item[field]
                    continue
                new.append(item)
        items = sorted(
            new + old,
            key=lambda item: item.get("published") or "",
            reverse=True,
        )[:CFG.get("retention", {}).get("news", 3000)]
        save("news.json", {"updated": now(), "items": items})
        return len(items)

    write_meta()
    tracked("news", run)


# ---------- provider icons (downloaded from each provider's own website) ----------
EXT = {"image/svg+xml": "svg", "image/png": "png", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico",
       "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def icon_candidates(page):
    out = []
    try:
        soup = BeautifulSoup(get(page).text, "html.parser")
        for l in soup.find_all("link", rel=True):
            rel = " ".join(l["rel"]) if isinstance(l["rel"], list) else l["rel"]
            if "icon" in rel.lower() and l.get("href"):
                out.append(urljoin(page, l["href"]))
    except Exception:
        pass
    return out + [urljoin(page, "/favicon.ico")]


def icons_job():
    def run():
        ICONS.mkdir(exist_ok=True)
        n = 0
        for pid, p in CFG["providers"].items():
            for u in icon_candidates(p["url"]):
                try:
                    r = requests.get(u, headers={"User-Agent": UA}, timeout=20)
                    ext = EXT.get(r.headers.get("content-type", "").split(";")[0].strip().lower())
                    if r.status_code == 200 and ext and 0 < len(r.content) < 200_000:
                        for old in ICONS.glob(f"{pid}.*"):
                            old.unlink()
                        (ICONS / f"{pid}.{ext}").write_bytes(r.content)
                        n += 1
                        break
                except Exception:
                    continue
        write_meta()
        return n

    tracked("icons", run)


JOBS = {"prices": prices_job, "deprecations": deprecations_job, "news": news_job, "icons": icons_job}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fetch data for AI Models Tracker.")
    parser.add_argument("job", nargs="?", choices=("all", *JOBS), default="all")
    which = parser.parse_args(argv).job
    selected_jobs = JOBS.values() if which == "all" else (JOBS[which],)
    for fn in selected_jobs:
        fn()


if __name__ == "__main__":
    main()
