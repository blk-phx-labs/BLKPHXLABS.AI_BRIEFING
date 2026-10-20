"""
BLK PHX LABS CLOUD FUNCTION (V35 - PUBLISH TO BLKPHXLABS.COM)

CHANGES FROM V34:
  PUBLISHING:
  - Video, audio and a briefing JSON go to a separate public bucket
    (read by exact name only, no listing) that holds nothing else
  - blkphxlabs.com/briefing/ renders the JSON; the old GCS dashboard page is gone
  - The working bucket (job queue, memory, assets) no longer needs to be public

CHANGES FROM V33 (V34):
  DIAGNOSTIC:
  - Enhanced logging throughout pipeline
  - Emergency fallback when no articles fit context budget
  - Better error messages for debugging

  SEMANTIC RELEVANCE:
  - Replaced keyword matching with Gemini-based semantic scoring
  - Articles scored 0-10 instead of binary taxonomy match
  - Much more robust to varied terminology

  CONTEXT:
  - Increased budget from 6000 to 20000 chars
  - Better fragment size handling
  
  RANKING:
  - Kept V33 fix (zero-match articles included)
  - Added semantic scoring for quality
"""

import functions_framework
from google.cloud import texttospeech
from google.cloud import storage

import json, tempfile, time, math, random, datetime, re, html, email.utils
from concurrent.futures import ThreadPoolExecutor, as_completed
import httpx
from urllib.parse import urlparse
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

import video
from xml.etree import ElementTree


# ============================================================
# 1. CONFIGURATION
# ============================================================
VOICE_NAME      = "en-US-Journey-D"
GEMINI_URL      = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
WEBHOOK_URL     = "https://script.google.com/macros/s/AKfycbwhbNbEbx_SqdESnwwlpA4MJwGZf2iDHBk03o4vQOttrdWcyaRymj5MMYWxDDRtPgby/exec"
MEMORY_BLOB     = "memory/last_week_facts.json"
PUBLIC_BUCKET   = "blkphxlabs-briefing"
BRIEFING_URL    = "https://blkphxlabs.com/briefing/"
PRIVATE_PREFIX  = "private/notes/"   # in the working bucket; never public

# Pass 1 reads the whole context, so this is what the model actually sees (~8-10 fragments).
RAG_CONTEXT_CHAR_LIMIT = 14000

# Trade-press feeds read alongside Google Alerts; alerts alone miss most CGT manufacturing news.
FEEDS = [
    "https://www.cgtlive.com/rss",
    "https://www.bioprocessintl.com/rss.xml",
    "https://www.fiercepharma.com/rss/manufacturing/xml",
    "https://www.biopharmadive.com/feeds/news/",
    "https://www.biopharmadive.com/feeds/topic/gene-therapy/",
    "https://endpts.com/feed",
    "https://www.genengnews.com/feed",
    "https://www.regmednet.com/feed/",
    "https://www.labiotech.eu/feed/",
    "https://www.nature.com/nbt.rss",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/biologics/rss.xml",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
]
FEED_MAX_AGE_HOURS = 36
FEED_MAX_ITEMS     = 8      # per feed, newest first
MAX_FACT_AGE_DAYS  = 30     # older facts are background, not news

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'
]

tts_client     = texttospeech.TextToSpeechClient()
storage_client = storage.Client()


def get_secret(secret_id: str) -> str:
    import os
    value = os.environ.get(secret_id)
    if not value:
        raise ValueError(f"[SECRET] Environment variable '{secret_id}' not set")
    return value


# ============================================================
# 3. RAG SCRAPER (Unchanged from V33)
# ============================================================
HTTPX_TIMEOUT  = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
SCRAPE_WORKERS = 10


def fetch_one(item: dict) -> str:
    url         = item.get('url', '')
    title       = item.get('title', 'Unknown Signal')
    headers     = {'User-Agent': random.choice(USER_AGENTS)}
    body_text   = ""
    meta_verify = ""

    # --- PRIMARY FETCH ---
    try:
        with httpx.Client(timeout=HTTPX_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            print(f"[SCRAPER] {resp.status_code} | {url[:80]}")
            if resp.status_code == 200:
                html = resp.text
                soup = BeautifulSoup(html, 'html.parser')

                meta_tag = (soup.find('meta', attrs={'name': 'description'}) or
                            soup.find('meta', attrs={'property': 'og:description'}))
                if meta_tag:
                    meta_verify = meta_tag.get('content', '')

                for tag in soup(['script', 'style', 'nav', 'header', 'footer',
                                  'aside', 'form', 'noscript', 'iframe']):
                    tag.decompose()

                article = (soup.find('article') or
                           soup.find('main') or
                           soup.find('div', attrs={'class': lambda c: c and
                               any(x in str(c).lower() for x in
                                   ['article', 'content', 'body', 'post', 'entry'])}) or
                           soup.body)

                if article:
                    paragraphs = article.find_all(['p', 'h1', 'h2', 'h3', 'li'])
                    body_text  = ' '.join(p.get_text(separator=' ').strip()
                                          for p in paragraphs if len(p.get_text().strip()) > 40)

                if not body_text:
                    body_text = ' '.join(soup.get_text(separator=' ').split())

                print(f"[SCRAPER] body={len(body_text)} meta={len(meta_verify)} | {title[:60]}")
    except Exception as e:
        print(f"[SCRAPER] EXCEPTION {type(e).__name__}: {e} | {url[:80]}")

    # --- WAYBACK RECOVERY ---
    if len(body_text) < 200:
        try:
            with httpx.Client(
                timeout=httpx.Timeout(connect=3.0, read=6.0, write=3.0, pool=3.0),
                follow_redirects=True
            ) as client:
                wb_resp  = client.get(f"https://archive.org/wayback/available?url={url}")
                snapshot = (wb_resp.json()
                                   .get('archived_snapshots', {})
                                   .get('closest', {})
                                   .get('url'))
                if snapshot:
                    arch_resp = client.get(snapshot, headers=headers)
                    if arch_resp.status_code == 200:
                        arch_soup = BeautifulSoup(arch_resp.text, 'html.parser')
                        for tag in arch_soup(['script', 'style', 'nav', 'header', 'footer']):
                            tag.decompose()
                        paras     = arch_soup.find_all(['p', 'h1', 'h2', 'h3'])
                        recovered = ' '.join(p.get_text().strip() for p in paras
                                            if len(p.get_text().strip()) > 40)
                        if recovered:
                            body_text = "[Archive Recovered] " + recovered
        except Exception as e:
            print(f"[SCRAPER] Wayback recovery failed for {url}: {e}")

    # --- BUILD FRAGMENT ---
    if body_text:
        return (
            f"SOURCE: {title}\n"
            f"VERIFICATION: {meta_verify}\n"
            f"TEXT: {body_text[:1200]}...\n---\n"
        )
    if meta_verify:
        return (
            f"SOURCE: {title}\n"
            f"VERIFICATION: {meta_verify}\n"
            f"TEXT: [Body Blocked - Using Verification Data]\n---\n"
        )
    return (
        f"SOURCE: {title}\n"
        f"VERIFICATION: [Unavailable]\n"
        f"TEXT: [Signal Only]\n---\n"
    )


def fetch_all_articles(articles: list) -> list:
    fragments = [None] * len(articles)
    with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as executor:
        future_to_idx = {executor.submit(fetch_one, item): i
                         for i, item in enumerate(articles)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                fragments[idx] = future.result()
            except Exception as e:
                url = articles[idx].get('url', 'unknown')
                print(f"[SCRAPER] Thread exception for {url}: {e}")
                fragments[idx] = (
                    f"SOURCE: {articles[idx].get('title', 'Unknown')}\n"
                    f"VERIFICATION: [Unavailable]\nTEXT: [Signal Only]\n---\n"
                )
    return fragments


# ============================================================
# 4. RELEVANCE TRIAGE (V36: one batched call, two tiers)
# ============================================================
# Who the briefing is for. Every prompt shares this so the tiers mean the same thing throughout.
AUDIENCE = (
    "BLK PHX LABS is an early-stage company building LabOps.AI, decision-support software for "
    "cell and gene therapy (CGT) manufacturing and quality teams."
)

SCORE_BATCH_SIZE = 40
MIN_RELEVANCE    = 3


def score_articles_batch(articles: list, fragments: list, api_key: str) -> dict:
    """
    Triage every article in one Gemini call per SCORE_BATCH_SIZE items (V34 made one call each).
    Seeing the items side by side also keeps the scores comparable instead of all landing at 7-9.

    Returns: {index: (tier, score, domain)} where tier is core | adjacent | skip
    """
    results = {}
    for start in range(0, len(fragments), SCORE_BATCH_SIZE):
        items = []
        for i in range(start, min(start + SCORE_BATCH_SIZE, len(fragments))):
            frag  = fragments[i] or ""
            title = articles[i].get("title", "") if i < len(articles) else ""
            text  = frag.split("TEXT:", 1)[-1].strip()[:300]
            items.append(f"[{i}] {title}\nPreview: {text}")

        prompt = f"""You triage news for a daily briefing. {AUDIENCE}

Tag every numbered item below.

tier:
- "core": cell and gene therapy (CGT) manufacturing and quality only: CAR-T, other cell therapies, gene therapies and viral vectors. Manufacturing process, scale-up, CMC, GMP, quality and QA, batch records, deviations, release testing, CDMOs, supply chain and vein-to-vein logistics, manufacturing automation and analytics, regulatory actions that change how CGT products are made or released, and business moves by CGT manufacturers and CGT CDMOs. Other biomanufacturing (generics, antibodies, ADCs, biosimilars, general bioprocessing) is "adjacent", not core.
- "adjacent": the wider field a CGT founder should know. Biomanufacturing outside CGT, CGT approvals and clinical readouts, gene editing, stem cells and iPSCs, regenerative medicine and tissue engineering, AI in drug discovery or protein design, lab automation and lab software, biotech funding and M&A.
- "skip": everything else. That includes items that only share a keyword (cars, energy, chips, space, art, education software), stock tips and price targets, insider-trading or stock-option filings, career guides, and market-size or market-forecast reports (openPR, MarketsandMarkets, IndexBox and similar), even when the market is biomanufacturing.

score, 0-10, for how much a CGT manufacturing leader should care today:
- 9-10: rare, at most 2 items in this list. A concrete event that changes plans: FDA or EMA action, an approval or clinical hold, a manufacturing failure or recall, a major deal or financing in CGT manufacturing.
- 6-8: a specific, sourced development: new data, a product launch, a partnership, a draft guidance.
- 3-5: commentary, opinion, perspectives, reviews, incremental research. Opinion pieces stay in this band even when the topic is important.
- 0-2: skip-tier material.

Judge only from the text given. Return a JSON array with one object per item:
[{{"id": <number>, "tier": "core|adjacent|skip", "score": <0-10>, "domain": "<2-4 words>"}}]

ITEMS:
{chr(10).join(items)}
"""
        batch = generate_gemini_text(api_key, prompt, expect_json=True, max_tokens=4096)
        if not isinstance(batch, list):
            print(f"[TRIAGE] Batch at {start} failed to parse; those items fall back to adjacent/2")
            continue
        for row in batch:
            try:
                idx   = int(row.get("id"))
                tier  = row.get("tier") if row.get("tier") in ("core", "adjacent", "skip") else "adjacent"
                score = max(0, min(10, int(row.get("score", 0))))
                results[idx] = (tier, score, str(row.get("domain") or "unknown"))
            except (TypeError, ValueError):
                continue

    tiers = [t for t, _, _ in results.values()]
    print(f"[TRIAGE] {len(results)}/{len(fragments)} tagged | core={tiers.count('core')} "
          f"adjacent={tiers.count('adjacent')} skip={tiers.count('skip')}")
    return results


def parse_feed_date(stamp: Optional[str]) -> Optional[datetime.datetime]:
    # RSS uses RFC 822, Atom uses ISO 8601, and Fierce uses its own "Oct 12, 2026 7:44am".
    stamp = (stamp or "").strip()
    parsers = (lambda v: datetime.datetime.fromisoformat(v.replace("Z", "+00:00")),
               email.utils.parsedate_to_datetime,
               lambda v: datetime.datetime.strptime(v, "%b %d, %Y %I:%M%p"))
    for parse in parsers:
        try:
            when = parse(stamp)
            return when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)
        except (TypeError, ValueError, IndexError):
            continue
    return None


def fetch_feed(url: str, since: datetime.datetime) -> list:
    """Recent items from one RSS or Atom feed as {title, url}; an unreachable feed just returns []."""
    try:
        r = httpx.get(url, headers={"User-Agent": random.choice(USER_AGENTS)},
                      timeout=HTTPX_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        root = ElementTree.fromstring(r.content)
    except Exception as e:
        print(f"[FEEDS] {type(e).__name__} | {url}")
        return []
    items = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {c.tag.rsplit("}", 1)[-1]: c for c in node}
        # itertext: some feeds (Fierce) wrap the title in a child element, leaving .text empty.
        title = "".join(fields["title"].itertext()).strip() if "title" in fields else ""
        link  = fields.get("link")
        href  = ((link.text or "").strip() or link.get("href", "")) if link is not None else ""
        stamp = next((fields[k].text for k in ("pubDate", "published", "updated", "date") if k in fields), None)
        when = parse_feed_date(stamp)
        if title and href.startswith("http") and (when is None or when >= since):
            items.append({"title": title, "url": href})
        if len(items) >= FEED_MAX_ITEMS:
            break
    return items


def fetch_feeds(now: datetime.datetime) -> list:
    since = now - datetime.timedelta(hours=FEED_MAX_AGE_HOURS)
    with ThreadPoolExecutor(max_workers=len(FEEDS)) as executor:
        per_feed = list(executor.map(lambda u: fetch_feed(u, since), FEEDS))
    for url, items in zip(FEEDS, per_feed):
        print(f"[FEEDS] {len(items):2d} | {url}")
    return [item for items in per_feed for item in items]


def merge_feed_articles(articles: list, feed_items: list) -> list:
    # Feeds are already capped per source, so they skip the per-domain cap; only drop repeat URLs.
    seen = {a.get("url") for a in articles}
    merged = list(articles)
    for item in feed_items:
        if item["url"] not in seen:
            seen.add(item["url"])
            merged.append({**item, "title": html.unescape(item["title"])})
    return merged


def deduplicate_by_domain(articles: list, max_per_domain: int = 3, hard_cap: int = 150) -> list:
    domain_counts: dict = {}
    seen_urls: set = set()
    unique = []
    for item in articles:
        url    = item.get('url', '')
        domain = urlparse(url).netloc
        if not domain or url in seen_urls:
            continue
        count = domain_counts.get(domain, 0)
        if count < max_per_domain:
            domain_counts[domain] = count + 1
            seen_urls.add(url)
            # Gmail alert titles arrive with HTML entities (&#39;, &amp;).
            unique.append({**item, 'title': html.unescape(item.get('title', ''))})
        if len(unique) >= hard_cap:
            break
    return unique


def rank_and_chunk_context(
    articles: list,
    raw_fragments: list,
    api_key: str,
    max_chars: int = RAG_CONTEXT_CHAR_LIMIT
) -> Tuple[str, list]:
    """
    Triage, drop skips and low scores, put core items ahead of adjacent ones, and fill the
    context budget. Each fragment is labelled [S1], [S2], ... so pass 1 can cite it and the
    code (not the model) attaches the URL.
    """
    print(f"[RAG] Starting ranking with {len(raw_fragments)} raw fragments")
    triage = score_articles_batch(articles, raw_fragments, api_key)

    scored = []
    for i, frag in enumerate(raw_fragments):
        if not frag or not frag.strip():
            continue

        # Signal quality tier
        if "[Signal Only]" in frag:
            signal_tier = 1
        elif "[Body Blocked" in frag:
            signal_tier = 2
        elif "[Archive Recovered]" in frag:
            signal_tier = 3
        else:
            signal_tier = 4

        tier, relevance, domain = triage.get(i, ("adjacent", 2, "unknown"))
        if tier == "skip" or relevance < MIN_RELEVANCE:
            continue

        article = articles[i] if i < len(articles) else {}
        scored.append({
            # Core outranks adjacent unless the adjacent item is far stronger.
            "score":     relevance * signal_tier + (12 if tier == "core" else 0),
            "relevance": relevance,
            "signal":    signal_tier,
            "tier":      tier,
            "domains":   [domain],
            "fragment":  frag,
            "title":     article.get("title", ""),
            "url":       article.get("url", ""),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    print(f"[RELEVANCE] Kept {len(scored)}/{len(raw_fragments)} | "
          f"core={sum(s['tier'] == 'core' for s in scored)} adjacent={sum(s['tier'] == 'adjacent' for s in scored)}")
    for item in scored[:5]:
        print(f"  score={item['score']} ({item['tier']}, rel={item['relevance']}, sig={item['signal']}) | {item['title'][:60]}")

    # Greedy fill context budget
    selected_frags = []
    selected_meta  = []
    budget = 0
    for item in scored:
        labelled = f"[S{len(selected_meta) + 1}] {item['fragment']}"
        if budget + len(labelled) > max_chars:
            continue
        selected_frags.append(labelled)
        selected_meta.append({**item, "source_id": f"S{len(selected_meta) + 1}"})
        budget += len(labelled)

    print(f"[RAG] Selected {len(selected_frags)}/{len(scored)} fragments ({budget:,} chars)")
    return "\n".join(selected_frags), selected_meta


# ============================================================
# 5. MEMORY STORE (Unchanged)
# ============================================================
def load_memory(bucket) -> list:
    try:
        blob = bucket.blob(MEMORY_BLOB)
        data = json.loads(blob.download_as_string())
        print(f"[MEMORY] Loaded {len(data)} facts from last week")
        return data
    except Exception:
        print("[MEMORY] No prior memory found")
        return []


def save_memory(bucket, facts: list):
    try:
        bucket.blob(MEMORY_BLOB).upload_from_string(
            json.dumps(facts, indent=2), content_type='application/json'
        )
        print(f"[MEMORY] Saved {len(facts)} facts")
    except Exception as e:
        print(f"[MEMORY] Save failed: {e}")


# ============================================================
# 6. THREE-PASS GEMINI ENGINE (V36: sourced facts, two-tier analysis, founder voice)
# ============================================================
SPOKEN_BRAND   = "Black Phoenix Labs"   # how the TTS voice says BLK PHX LABS
SCRIPT_WORDS   = (120, 150)             # about 60 seconds of speech
# Words the public script may only use when attributing them to a source.
BANNED_WORDS   = ("autonomous", "compliant", "compliance-ready", "ensuring compliance",
                  "revolutionary", "game-changing", "game changer", "breakthrough", "critical clarity")


def generate_gemini_text(api_key: str, prompt: str, expect_json: bool = False,
                         max_tokens: Optional[int] = None):
    config = {
        "temperature":     0.3 if expect_json else 0.5,
        "maxOutputTokens": max_tokens or (2048 if expect_json else 8192),
        "thinkingConfig":  {"thinkingBudget": 0},
    }
    if expect_json:
        config["responseMimeType"] = "application/json"
    data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "safetySettings": [
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ],
        "generationConfig": config,
    }
    try:
        response = httpx.post(
            GEMINI_URL,
            params={'key': api_key},
            headers={'Content-Type': 'application/json'},
            json=data,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)
        )
        if response.status_code != 200:
            print(f"[GEMINI] Non-200: {response.status_code}")
            return None

        raw = response.json()['candidates'][0]['content']['parts'][0]['text']

        if expect_json:
            clean = re.sub(r'<thinking>.*?</thinking>', '', raw, flags=re.DOTALL).strip()
            for fence in ["```json", "```JSON", "```"]:
                clean = clean.replace(fence, "")
            clean = clean.strip()
            try:
                # strict=False accepts raw line breaks inside strings, which multi-line fields produce.
                return json.loads(clean, strict=False)
            except json.JSONDecodeError:
                # Fall back: trim to the outermost brackets and drop trailing commas
                start = min(i for i in (clean.find('['), clean.find('{'), len(clean)) if i >= 0)
                end   = max(clean.rfind(']'), clean.rfind('}')) + 1
                clean = re.sub(r',\s*([}\]])', r'\1', clean[start:end])
                return json.loads(clean, strict=False)

        return raw

    except Exception as e:
        print(f"[GEMINI] Exception: {e}")
        return None


def pass1_extract_facts(api_key: str, ranked_context: str, sources: list) -> list:
    prompt = f"""You extract facts from news fragments for a daily briefing. {AUDIENCE}
Each fragment starts with a source id such as [S3].

Return a JSON array. One object per distinct event: if several fragments report the same event, return it once and list every source id.
{{
  "source_ids": ["S3"],
  "company_or_institution": "string or null",
  "event_type": "funding | acquisition | partnership | regulatory | clinical | manufacturing | research | product | other",
  "key_metric": "string or null. Only a figure stated in the text: dollar amount, percentage, patient count, timeline",
  "date_mentioned": "string or null",
  "event_date": "YYYY-MM-DD or null. When the event itself happened, only if the text states it (year and month at least; use day 01 if no day)",
  "domain": "2-4 words, e.g. CAR-T manufacturing, gene editing, AI drug discovery",
  "one_line_summary": "max 25 words, factual, no hype words",
  "signal_strength": "high | medium | low"
}}

signal_strength: high = a primary source or a concrete event; medium = credible secondary reporting; low = opinion, marketing, or a headline with little text.
Use only what the fragments say; use null when a field isn't stated. A fragment marked [Signal Only] is just a headline: mark it low and add nothing beyond the headline.
Leave out fragments that aren't news: ads, stock-option filings, market-size press releases.

RESEARCH FRAGMENTS:
{ranked_context}
"""
    print("[PASS 1] Running extraction...")
    raw_facts = generate_gemini_text(api_key, prompt, expect_json=True, max_tokens=4096)
    if not isinstance(raw_facts, list):
        print("[PASS 1] Failed to parse JSON")
        return []

    by_id = {s["source_id"]: s for s in sources}
    facts, seen = [], set()
    for f in raw_facts:
        if not isinstance(f, dict):
            continue
        cited = [by_id[s] for s in f.get("source_ids") or [] if s in by_id]
        if not cited:
            continue   # an unattributable fact can't be published
        key = (str(f.get("company_or_institution") or "").lower(), str(f.get("key_metric") or "").lower(),
               str(f.get("one_line_summary") or "").lower()[:60])
        if key in seen:
            continue
        seen.add(key)
        # Links and tier come from the code's own records, never from model output.
        f.pop("source_ids", None)
        f["tier"]         = "core" if any(s["tier"] == "core" for s in cited) else "adjacent"
        f["source_title"] = cited[0]["title"]
        f["source_url"]   = cited[0]["url"]
        f["sources"]      = [{"title": s["title"], "url": s["url"]} for s in cited]
        facts.append(f)

    print(f"[PASS 1] Extracted {len(facts)} facts "
          f"(core={sum(f['tier'] == 'core' for f in facts)}, dropped {len(raw_facts) - len(facts)})")
    return facts


def fact_age_days(fact: dict, today: datetime.date) -> Optional[int]:
    try:
        return (today - datetime.date.fromisoformat(str(fact.get("event_date"))[:10])).days
    except ValueError:
        return None


def pass2_analyze(api_key: str, current_facts: list, prior_facts: list, current_date: str,
                  today: datetime.date) -> Optional[dict]:
    numbered = [
        {"id": f"F{i + 1}",
         **{k: f.get(k) for k in ("tier", "company_or_institution", "event_type", "key_metric",
                                  "domain", "one_line_summary", "signal_strength")},
         # The model can see an opinion or vendor URL and how old the event is; it can't work either out itself.
         "source_url": f.get("source_url"),
         "days_old":   fact_age_days(f, today)}
        for i, f in enumerate(current_facts)
    ]
    prior = [f.get("one_line_summary") for f in prior_facts if isinstance(f, dict) and f.get("one_line_summary")]

    prompt = f"""You write today's LabMind briefing, {current_date}. {AUDIENCE}

TODAY'S FACTS (tier "core" = CGT manufacturing and quality, "adjacent" = the wider field, including other biomanufacturing):
{json.dumps(numbered, indent=1, ensure_ascii=False)}

YESTERDAY'S HEADLINES (continuity only; don't present them as new unless today's facts add something):
{json.dumps(prior[:15], ensure_ascii=False)}

Return JSON:
{{
  "lead": {{"fact_ids": ["F1"], "headline": "max 12 words",
            "what_happened": "2-3 sentences",
            "why_it_matters": "2-3 sentences for people who make, test or release CGT products"}},
  "also_notable": [{{"fact_ids": ["F2"], "headline": "max 12 words", "summary": "1-2 sentences"}}],
  "confidence": {{"level": "high | medium | low", "reason": "one sentence about source quality"}},
  "discussion_question": "see below",
  "private_notes": "see below"
}}

Rules:
- Lead: the most important "core" fact with medium or high strength that is actually new. days_old over 30 means background, not news: never lead with it, and if you mention it, give its date. A source_url containing /opinion/, /perspective/, /blog/, /sponsored/ or /spons/ is commentary or advertising, not news. If no core fact qualifies, lead with the strongest new fact of any tier. Don't apologize for it or say what it isn't (never \"while not directly about manufacturing\"); say what it means for CGT manufacturing and quality teams.
- also_notable: 2 or 3 other items, ordered by how much they matter to CGT manufacturing and quality teams: CGT and biomanufacturing first, then gene editing, AI in drug discovery and lab automation. Leave out curiosities far from that (neurotech demos, consumer health, product advertorials). Don't repeat the lead.
- confidence: "high" only if the lead comes from direct news reporting or a primary source with a clear date. Opinion pieces, vendor content, or headline-only sources mean "medium" or "low". Say which in the reason.
- Use only the facts. Mark inference with "likely" or "may". No numbers, names or dates that aren't in the facts.
- When a company uses a buzzword for its own work ("autonomous science", "AI-native"), attribute it: "what Danaher calls autonomous science".
- Plain words a QA or manufacturing lead would use. No hype or filler: never "breakthrough", "game-changer", "revolutionary", "critical clarity", "ensuring compliance", "streamlining".
- discussion_question: one question about the lead that a CGT manufacturing or QA practitioner could answer from their own experience in a comment (max 20 words). Concrete, not rhetorical, no yes/no fishing for agreement. Example: "What would it take for your QA team to release on an automated potency assay?"
- private_notes: for the founder only, never published. 3 to 5 lines, each starting with "- ". Specific and blunt: name the companies, groups or regulators in today's facts that matter to a CGT manufacturing software startup and say why (likely competitor, possible customer or design partner, signal about what buyers will need). No generic positioning statements, and stay within CGT manufacturing and QA. End with one concrete action for this week, such as a person or company to contact, a document to read, or a question to put to prospects. Don't assume LabOps.AI has any particular feature; it is early stage.
"""
    print(f"[PASS 2] Running analysis for {current_date}...")
    result = generate_gemini_text(api_key, prompt, expect_json=True, max_tokens=4096)
    if not isinstance(result, dict) or not isinstance(result.get("lead"), dict):
        print("[PASS 2] Analysis failed")
        return None
    print(f"[PASS 2] Lead: {result['lead'].get('headline')} | also_notable={len(result.get('also_notable') or [])}")
    return result


def compose_public_analysis(analysis: dict) -> str:
    # The page renders "**Heading**" lines as headings and "- " lines as bullets.
    lead  = analysis.get("lead") or {}
    lines = [f"**Lead: {lead.get('headline', '').strip()}**",
             str(lead.get("what_happened", "")).strip(),
             str(lead.get("why_it_matters", "")).strip()]
    notable = analysis.get("also_notable") or []
    if notable:
        lines += ["", "**Also notable**"]
        lines += [f"- **{n.get('headline', '').strip()}**: {str(n.get('summary', '')).strip()}" for n in notable]
    conf = analysis.get("confidence") or {}
    if conf.get("level"):
        lines += ["", f"**Confidence: {str(conf['level']).capitalize()}**", str(conf.get("reason", "")).strip()]
    return "\n".join(l for l in lines if l is not None)


def pass3_narrate(api_key: str, analysis: dict, spoken_date: str) -> Optional[str]:
    public = {k: analysis.get(k) for k in ("lead", "also_notable", "confidence")}   # never the private notes
    lo, hi = SCRIPT_WORDS
    prompt = f"""Write the spoken script for today's LabMind briefing. A text-to-speech voice reads it over a short video.

VOICE: the founder of {SPOKEN_BRAND}, talking to people who make and release cell and gene therapies. Calm, direct, a little opinionated, never hype. Use first person plural at least twice: once in the lead to give our take ("what we'd want to know is...", "our read:"), and once to say what we're watching next.

LENGTH: {lo} to {hi} words. Never more than {hi + 10}.

STRUCTURE:
1. Open with exactly: "This is LabMind from {SPOKEN_BRAND}, {spoken_date}."
2. The lead story in about 70 words: what happened, why it matters for cell and gene therapy manufacturing, and our take.
3. Say "Also notable." then cover the other items in one short sentence each (under 20 words).
4. Close with exactly: "Sources and the full analysis are on our site. See you tomorrow."

RULES:
- Plain text only: no markdown, bullets, asterisks, emoji, headings or stage directions.
- Say common abbreviations as they are: FDA, CAR-T, GMP, AI, CDMO, CRISPR, AAV, M&A. Spell out any other abbreviation the first time.
- Write numbers the way they're spoken: "forty to fifty billion dollars", "two point one billion".
- Use only the content below. If confidence is low, say so in a few words.
- Never describe what LabOps.AI does, and never claim customers or partners.
- Never use these words except inside a clearly attributed quote of a source: {", ".join(BANNED_WORDS)}.

CONTENT:
{json.dumps(public, indent=1, ensure_ascii=False)}
"""
    print("[PASS 3] Running narration...")
    script = None
    for attempt in (1, 2):
        draft = generate_gemini_text(api_key, prompt, expect_json=False, max_tokens=1024)
        if not draft:
            break
        script  = re.sub(r'[*#_`>]', '', draft).strip()
        words   = len(script.split())
        # A banned word is fine only inside an attribution ("what Danaher calls autonomous science").
        flagged = [w for w in BANNED_WORDS
                   if re.search(rf"(?<!calls )\b{re.escape(w)}", script.lower())]
        print(f"[PASS 3] Draft {attempt}: {words} words" + (f" | flagged: {flagged}" if flagged else ""))
        if words <= hi + 10 and not flagged:
            break
        problems = []
        if words > hi + 10:
            problems.append(f"it is {words} words; cut it to {lo}-{hi}")
        if flagged:
            problems.append(f"remove or attribute these words: {', '.join(flagged)}")
        prompt += f"\n\nYOUR PREVIOUS DRAFT:\n{script}\n\nRewrite it: {'; '.join(problems)}. Keep everything else."

    if not script:
        print("[PASS 3] Narration failed")
        return None
    print(f"[PASS 3] Script ready ({len(script.split())} words, {len(script)} chars)")
    return script


# ============================================================
# 7. PRODUCTION UTILS
# ============================================================
def build_briefing_json(script_text: str, analysis: str, facts: list,
                        video_url: str, audio_url: str, ts: int, source_count: int) -> str:
    # Plain data only; blkphxlabs.com renders it as text, so scraped or model-written
    # content can never become markup on the page.
    est = ZoneInfo("America/New_York")
    return json.dumps({
        "generated_at": datetime.datetime.now(est).isoformat(timespec="seconds"),
        "sync_id":      ts,
        "video_url":    video_url,
        "audio_url":    audio_url,
        "transcript":   script_text,
        "analysis":     analysis,
        "facts":        facts,
        "source_count": source_count,
    }, ensure_ascii=False, indent=2)


def write_briefing(api_key: str, articles: list, prior_facts: list,
                   now: Optional[datetime.datetime] = None) -> Optional[dict]:
    """Everything between the scraped articles and the finished text. No storage, TTS or video,
    so it can be run locally against a saved article list."""
    now = now or datetime.datetime.now(ZoneInfo("America/New_York"))

    articles = merge_feed_articles(articles, fetch_feeds(now))
    print(f"[FEEDS] {len(articles)} articles with feeds")

    raw_fragments = fetch_all_articles(articles)
    content_count = len([f for f in raw_fragments if f and '[Signal Only]' not in f])
    print(f"[SCRAPER] {content_count}/{len(articles)} yielded content")

    ranked_context, sources = rank_and_chunk_context(articles, raw_fragments, api_key)
    if not ranked_context.strip():
        print("[RAG] No relevant articles after triage; nothing to publish today")
        return None

    facts = pass1_extract_facts(api_key, ranked_context, sources)
    fresh = [f for f in facts if (fact_age_days(f, now.date()) or 0) <= MAX_FACT_AGE_DAYS]
    if len(fresh) < len(facts):
        print(f"[PASS 1] Dropped {len(facts) - len(fresh)} facts older than {MAX_FACT_AGE_DAYS} days")
    facts = fresh
    if not facts:
        print("[PASS 1] No facts extracted; nothing to publish today")
        return None

    analysis = pass2_analyze(api_key, facts, prior_facts, f"{now:%A, %B} {now.day}, {now.year}", now.date())
    if not analysis:
        return None

    script = pass3_narrate(api_key, analysis, f"{now:%A, %B} {now.day}")
    if not script:
        return None

    return {
        "script":        script,
        "analysis":      analysis,
        "analysis_md":   compose_public_analysis(analysis),
        "private_notes": str(analysis.get("private_notes") or "").strip(),
        "facts":         facts,
        "source_count":  len(sources),
        "date":          now.strftime("%Y-%m-%d"),
    }


def deliver_private_notes(bucket, briefing: dict):
    """Private notes go to the working bucket and to the Scout's web app, which prepends them to
    the LabMind Private Notes Google Doc. They never touch the public bucket."""
    notes = briefing["private_notes"]
    if not notes:
        return
    lead = (briefing["analysis"].get("lead") or {}).get("headline", "")
    try:
        bucket.blob(f"{PRIVATE_PREFIX}{briefing['date']}.md").upload_from_string(
            f"# {briefing['date']}: {lead}\n\n{notes}\n", content_type="text/markdown")
    except Exception as e:
        print(f"[PRIVATE] Bucket copy failed: {e}")

    import os
    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret:
        print("[PRIVATE] WEBHOOK_SECRET not set; notes saved to the bucket only")
        return
    try:
        # Apps Script answers POSTs with a redirect to the result.
        resp = httpx.post(WEBHOOK_URL, follow_redirects=True, json={
            "secret": secret, "type": "private_notes", "date": briefing["date"],
            "lead": lead, "notes": notes, "briefing_url": BRIEFING_URL,
        }, timeout=httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0))
        print(f"[PRIVATE] Notes doc: {resp.status_code} {resp.text[:80]}")
    except Exception as e:
        print(f"[PRIVATE] Webhook failed: {e}")


def notify_discord(payload: dict, file_path: Optional[str] = None):
    """Post to the founder's private Discord channel. Optional, and never breaks the run."""
    import os
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    body = {"username": "LabMind", **payload}
    try:
        if file_path:
            # Multipart: the message as payload_json plus the file (the Reel is ~2 MB, under Discord's limit).
            with open(file_path, "rb") as f:
                resp = httpx.post(url, data={"payload_json": json.dumps(body)},
                                  files={"files[0]": (os.path.basename(file_path), f, "video/mp4")},
                                  timeout=60.0)
        else:
            resp = httpx.post(url, json=body, timeout=10.0)
        print(f"[DISCORD] {resp.status_code}")
    except Exception as e:
        print(f"[DISCORD] Failed: {type(e).__name__}")


INSTAGRAM_COLLAB = "spamzy4L"
HASHTAGS = "#celltherapy #genetherapy #CGT #biomanufacturing #labops #biotech"


def _first_sentence(text) -> str:
    return re.split(r"(?<=[.!?])\s+", str(text or "").strip())[0]


def featured_companies(briefing: dict) -> list:
    """Companies and institutions behind the lead and also-notable items, worth tagging when posting."""
    facts, names = briefing.get("facts") or [], []
    analysis = briefing["analysis"]
    for item in [analysis.get("lead") or {}] + list(analysis.get("also_notable") or []):
        for fid in (item.get("fact_ids") or []) if isinstance(item, dict) else []:
            m = re.fullmatch(r"F(\d+)", str(fid))
            if m and 0 < int(m.group(1)) <= len(facts):
                name = str(facts[int(m.group(1)) - 1].get("company_or_institution") or "").strip()
                if name and name.lower() not in ("none", "null") and name not in names:
                    names.append(name)
    return names


def instagram_caption(briefing: dict) -> str:
    """Caption for the day's Reel, built from the analysis (no extra model call)."""
    analysis = briefing["analysis"]
    lead     = analysis.get("lead") or {}
    notable  = [i.get("headline", "") for i in (analysis.get("also_notable") or []) if isinstance(i, dict)]
    question = str(analysis.get("discussion_question") or "").strip()
    lines    = [str(lead.get("headline", "")).strip(), "", _first_sentence(lead.get("what_happened"))]
    if notable:
        lines += ["", "Also notable:"] + [f"• {h}" for h in notable if h]
    if question:
        lines += ["", question]
    lines += ["", "Sources and full analysis: blkphxlabs.com/briefing (link in bio)", "",
              "LabMind is a daily AI-assisted briefing from BLK PHX LABS.", "", HASHTAGS]
    return "\n".join(lines)[:2200]


def linkedin_post(briefing: dict) -> str:
    """Same story for LinkedIn: hook, what happened, the founder's read, a question, the link."""
    analysis = briefing["analysis"]
    lead     = analysis.get("lead") or {}
    question = str(analysis.get("discussion_question") or "").strip()
    lines    = [str(lead.get("headline", "")).strip(), "",
                str(lead.get("what_happened", "")).strip(), "",
                "My read: " + str(lead.get("why_it_matters", "")).strip()]
    if question:
        lines += ["", question]
    lines += ["", "Daily CGT manufacturing briefing, with sources: https://blkphxlabs.com/briefing/", "",
              "#CellTherapy #GeneTherapy #Biomanufacturing"]
    return "\n".join(lines)[:2800]


def discord_reel(briefing: dict) -> dict:
    tags = featured_companies(briefing)
    return {"content": (
        "**Today's Reel**: save the video, then post it on @blkphxlabs as a Reel.\n"
        f"Tag people → **Invite collaborator** → @{INSTAGRAM_COLLAB}, and accept the invite from that account.\n"
        + (f"Worth tagging: {', '.join(tags)}\n" if tags else "")
        + "Caption (tap and hold to copy):\n"
        f"```\n{instagram_caption(briefing)}\n```")[:2000]}


def discord_linkedin(briefing: dict) -> dict:
    return {"content": (
        "**LinkedIn**: post the same video natively (not a link), with this text:\n"
        f"```\n{linkedin_post(briefing)}\n```")[:2000]}


def discord_summary(briefing: dict) -> dict:
    analysis = briefing["analysis"]
    lead     = analysis.get("lead") or {}
    notable  = "\n".join(f"- {i.get('headline', '')}" for i in (analysis.get("also_notable") or [])
                         if isinstance(i, dict))
    conf     = analysis.get("confidence") or {}
    return {
        "content": f"**Today's LabMind is live:** {BRIEFING_URL}",
        "embeds": [{
            "title":       str(lead.get("headline", "LabMind"))[:256],
            "url":         BRIEFING_URL,
            "description": str(lead.get("why_it_matters", ""))[:1500],
            "color":       0xE8520A,
            "fields": [
                {"name": "Also notable",  "value": notable[:1024] or "None today"},
                {"name": "Private notes", "value": briefing["private_notes"][:1024] or "None today"},
            ],
            "footer": {"text": f"Confidence: {conf.get('level', '?')} | {briefing['source_count']} sources"},
        }],
    }


def redact(text: str) -> str:
    # Gemini errors echo the request URL, key included.
    return re.sub(r"(key|access_token)=[^&\s'\"]+", lambda m: m.group(1) + "=***", text)


# ============================================================
# 8. MAIN CLOUD EVENT TRIGGER
# ============================================================
@functions_framework.cloud_event
def generate_content(cloud_event):
    data        = cloud_event.data
    bucket_name = data["bucket"]
    file_name   = data["name"]

    if "queue/trigger_job.json" not in file_name:
        return

    try:
        print("[PIPELINE] ===== V36 START =====")

        # Load secrets
        api_key = get_secret("GEMINI_API_KEY")
        print("[SECRETS] Credentials loaded")

        # Read job ticket
        bucket      = storage_client.bucket(bucket_name)
        blob_ticket = bucket.blob(file_name)

        lock_blob_name = file_name.replace("trigger_job.json", "processing.lock")
        try:
            bucket.copy_blob(blob_ticket, bucket, lock_blob_name)
            blob_ticket.delete()
        except Exception:
            print("[TRIGGER] Job already claimed")
            return

        lock_blob = bucket.blob(lock_blob_name)
        job_data  = json.loads(lock_blob.download_as_string())

        article_count = len(job_data.get('articles', []))
        print(f"[TRIGGER] Job loaded: {article_count} articles in payload")

        # RAG Phase
        articles = deduplicate_by_domain(job_data.get('articles', []))
        print(f"[RAG] After dedup: {len(articles)} articles")

        if len(articles) == 0:
            # Not fatal any more: the trade-press feeds still give the briefing something to work with.
            print("[RAG] ⚠️ Zero alert articles in payload; continuing with feeds only")
            print("[RAG] Check Apps Script logs for Gmail sourcing issues")

        prior_facts = load_memory(bucket)
        briefing    = write_briefing(api_key, articles, prior_facts)
        if not briefing:
            notify_discord({"content": "No LabMind briefing today: nothing relevant made it through triage, "
                                       "or a writing step failed. Check the function logs."})
            return
        script_text   = briefing["script"]
        current_facts = briefing["facts"]

        save_memory(bucket, current_facts)
        deliver_private_notes(bucket, briefing)

        # Voice + video: one TTS call per sentence drives captions and the host's mouth (video.py).
        def synthesize(text: str) -> bytes:
            return tts_client.synthesize_speech(
                input=texttospeech.SynthesisInput(text=text),
                voice=texttospeech.VoiceSelectionParams(language_code="en-US", name=VOICE_NAME),
                audio_config=texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                    sample_rate_hertz=video.SAMPLE_RATE),
            ).audio_content

        now          = datetime.datetime.now(ZoneInfo("America/New_York"))
        assets       = video.download_assets(bucket, tempfile.mkdtemp())
        headlines    = [i.get("headline", "") for i in (briefing["analysis"].get("also_notable") or [])
                        if isinstance(i, dict)]
        output_video = "/tmp/final.mp4"
        audio_path   = "/tmp/final.mp3"
        info = video.render_reel(script_text, headlines, f"[ {now:%a} · {now:%b} {now.day} ]".upper(),
                                 synthesize, assets, output_video, audio_path,
                                 hook=str((briefing["analysis"].get("lead") or {}).get("headline", "")))
        print(f"[VIDEO] Production complete | {info}")

        ts = int(time.time())

        # Publish: only these three files are public, and only in PUBLIC_BUCKET.
        public_bucket = storage_client.bucket(PUBLIC_BUCKET)
        public_bucket.blob("latest_briefing.mp3").upload_from_filename(audio_path, content_type='audio/mpeg')
        public_bucket.blob("latest_briefing.mp4").upload_from_filename(output_video, content_type='video/mp4')

        video_url     = f"https://storage.googleapis.com/{PUBLIC_BUCKET}/latest_briefing.mp4?v={ts}"
        audio_url     = f"https://storage.googleapis.com/{PUBLIC_BUCKET}/latest_briefing.mp3?v={ts}"
        dashboard_url = BRIEFING_URL

        briefing_blob = public_bucket.blob("latest_briefing.json")
        # The name never changes, so browsers must revalidate to see each new run.
        briefing_blob.cache_control = "no-cache"
        briefing_blob.upload_from_string(
            build_briefing_json(script_text, briefing["analysis_md"], current_facts,
                                video_url, audio_url, ts, briefing["source_count"]),
            content_type='application/json'
        )
        print(f"[PUBLISH] {dashboard_url}")

        try:
            lock_blob.delete()
        except Exception:
            pass

        notify_discord(discord_summary(briefing))
        notify_discord(discord_reel(briefing), file_path=output_video)
        notify_discord(discord_linkedin(briefing))
        print("[PIPELINE] ===== SUCCESS =====")

    except Exception as e:
        print(f"[CRITICAL] Unhandled exception: {redact(str(e))}")
        notify_discord({"content": f"LabMind run failed: `{type(e).__name__}: {redact(str(e))[:300]}`. "
                                   "The page still shows the last good briefing."})
        import traceback
        traceback.print_exc()
        raise
