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
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

import json, tempfile, time, math, random, datetime, re
from concurrent.futures import ThreadPoolExecutor, as_completed
import httpx
from urllib.parse import urlparse
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


# ============================================================
# 1. CONFIGURATION
# ============================================================
VOICE_NAME      = "en-US-Journey-D"
GEMINI_URL      = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
WEBHOOK_URL     = "https://script.google.com/macros/s/AKfycbwhbNbEbx_SqdESnwwlpA4MJwGZf2iDHBk03o4vQOttrdWcyaRymj5MMYWxDDRtPgby/exec"
DRIVE_FOLDER_ID = "1Lxg5T-XXa_bbwR_vWb34Gr-HCqjNo_Kp"
MEMORY_BLOB     = "memory/last_week_facts.json"
PUBLIC_BUCKET   = "blkphxlabs-briefing"
BRIEFING_URL    = "https://blkphxlabs.com/briefing/"

# CHANGED: Increased from 6000 to 20000
RAG_CONTEXT_CHAR_LIMIT = 20000

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
# 4. SEMANTIC RELEVANCE SCORING (NEW!)
# ============================================================
def score_relevance_semantic(title: str, fragment: str, api_key: str) -> Tuple[int, list]:
    """
    NEW: Uses Gemini to score relevance semantically instead of keyword matching.
    Much more robust to varied terminology and phrasing.
    
    Returns: (relevance_score 0-10, list of matched domains)
    """
    prompt = f"""You are a biotech intelligence filter for a venture focused on:
- CRISPR & gene editing technologies
- AI-driven drug discovery and protein design
- Lab automation and autonomous research platforms
- Cell therapy, gene therapy, tissue engineering
- Synthetic biology and biomanufacturing
- Digital lab operations (LIMS, ELN, data platforms)
- Competitive intelligence: M&A, funding rounds, regulatory changes

Score this article's relevance on a 0-10 scale:

Article Title: {title}
Article Preview: {fragment[:1000]}

Return ONLY valid JSON with NO markdown fences, preamble, or explanation:
{{
  "relevance_score": <integer 0-10>,
  "domains": ["<domain1>", "<domain2>"],
  "reasoning": "<one sentence max>"
}}

Scoring guide:
0-2 = Irrelevant (general news, unrelated science)
3-5 = Tangentially related (adjacent fields, indirect relevance)
6-8 = Relevant (directly related to focus areas)
9-10 = Critical intelligence (major breakthrough, strategic opportunity, competitive threat)
"""
    
    try:
        response = httpx.post(
            GEMINI_URL,
            params={'key': api_key},
            headers={'Content-Type': 'application/json'},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 500,
                    "thinkingConfig": {"thinkingBudget": 0}
                }
            },
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
        )
        
        if response.status_code == 200:
            raw = response.json()['candidates'][0]['content']['parts'][0]['text']
            clean = re.sub(r'<thinking>.*?</thinking>', '', raw, flags=re.DOTALL)
            clean = clean.strip().replace('```json', '').replace('```', '').strip()

            # Find JSON object bounds
            start = clean.find('{')
            end = clean.rfind('}') + 1
            if start >= 0 and end > start:
                clean = clean[start:end]

            result = json.loads(clean)
            score = result.get('relevance_score', 0)
            domains = result.get('domains', [])
            reasoning = result.get('reasoning', '')
            
            print(f"[SEMANTIC] {score}/10 | {domains} | {title[:50]}")
            if reasoning:
                print(f"[SEMANTIC]   → {reasoning}")
            
            return (score, domains)
        else:
            print(f"[SEMANTIC] API error {response.status_code} for: {title[:50]}")
            
    except json.JSONDecodeError as e:
        print(f"[SEMANTIC] JSON parse error for {title[:50]}: {e}")
    except Exception as e:
        print(f"[SEMANTIC] Unexpected error for {title[:50]}: {e}")
    
    # Fallback: assign low score but don't drop
    return (2, ['unknown'])


def deduplicate_by_domain(articles: list, max_per_domain: int = 3, hard_cap: int = 150) -> list:
    domain_counts: dict = {}
    unique = []
    for item in articles:
        domain = urlparse(item.get('url', '')).netloc
        if not domain:
            continue
        count = domain_counts.get(domain, 0)
        if count < max_per_domain:
            domain_counts[domain] = count + 1
            unique.append(item)
        if len(unique) >= hard_cap:
            break
    return unique


def rank_and_chunk_context(
    articles: list,
    raw_fragments: list,
    api_key: str,  # NEW: Need API key for semantic scoring
    max_chars: int = RAG_CONTEXT_CHAR_LIMIT
) -> Tuple[str, list]:
    """
    UPDATED: Now uses semantic scoring via Gemini instead of keyword matching.
    Much more robust to varied terminology.
    """
    print(f"[RAG] Starting ranking with {len(raw_fragments)} raw fragments")
    
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

        title = articles[i].get("title", "") if i < len(articles) else ""
        
        # NEW: Semantic relevance scoring
        relevance, domains = score_relevance_semantic(title, frag, api_key)

        if relevance > 0:
            final_score = relevance * signal_tier
        else:
            final_score = signal_tier * 0.1

        scored.append({
            "score":      final_score,
            "relevance":  relevance,
            "signal":     signal_tier,
            "domains":    domains,
            "fragment":   frag,
            "title":      title,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    high_relevance = len([s for s in scored if s['relevance'] >= 6])
    medium_relevance = len([s for s in scored if 3 <= s['relevance'] < 6])
    low_relevance = len([s for s in scored if s['relevance'] < 3])
    
    print(f"[RELEVANCE] Scored {len(scored)} fragments:")
    print(f"[RELEVANCE]   High (6-10): {high_relevance}")
    print(f"[RELEVANCE]   Medium (3-5): {medium_relevance}")
    print(f"[RELEVANCE]   Low (0-2): {low_relevance}")

    for item in scored[:5]:
        print(f"  score={item['score']:.1f} (rel={item['relevance']}, sig={item['signal']}) | {item['domains']} | {item['title'][:60]}")

    # Greedy fill context budget
    selected_frags = []
    selected_meta  = []
    budget = 0
    skipped_oversized = 0
    
    for item in scored:
        frag_len = len(item["fragment"])
        if budget + frag_len > max_chars:
            if frag_len > max_chars * 0.3:  # Fragment > 30% of budget
                skipped_oversized += 1
            continue
        selected_frags.append(item["fragment"])
        selected_meta.append(item)
        budget += frag_len

    print(f"[RAG] Selected {len(selected_frags)}/{len(scored)} fragments ({budget:,} chars)")
    if skipped_oversized > 0:
        print(f"[RAG] Skipped {skipped_oversized} oversized fragments")

    # NEW: Emergency fallback if nothing fit
    if len(selected_frags) == 0 and len(scored) > 0:
        print("[RAG] ⚠️ EMERGENCY FALLBACK: No articles fit budget — taking top 10 regardless")
        emergency = scored[:10]
        selected_frags = [e["fragment"][:2000] for e in emergency]  # Truncate to fit
        selected_meta = emergency
        print(f"[RAG] Emergency fallback: {len(selected_frags)} fragments (truncated)")

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
# 6. THREE-PASS GEMINI ENGINE (Updated model)
# ============================================================
def generate_gemini_text(api_key: str, prompt: str, expect_json: bool = False):
    data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "safetySettings": [
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ],
        "generationConfig": {
            "temperature":    0.3 if expect_json else 0.5,
            "maxOutputTokens": 2048 if expect_json else 8192,
            "thinkingConfig": {"thinkingBudget": 0}
        }
    }
    try:
        response = httpx.post(
            GEMINI_URL,
            params={'key': api_key},
            headers={'Content-Type': 'application/json'},
            json=data,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)
        )
        if response.status_code != 200:
            print(f"[GEMINI] Non-200: {response.status_code}")
            return None

        raw = response.json()['candidates'][0]['content']['parts'][0]['text']

        if expect_json:
            clean = re.sub(r'<thinking>.*?</thinking>', '', raw, flags=re.DOTALL)
            clean = clean.strip()
            for fence in ["```json", "```JSON", "```"]:
                clean = clean.replace(fence, "")
            clean = clean.strip()
            # Strip control characters that break JSON parsing
            clean = re.sub(r'[\x00-\x1f\x7f]', ' ', clean)
            start = min(
                (clean.find('[') if '[' in clean else len(clean)),
                (clean.find('{') if '{' in clean else len(clean))
            )
            end_bracket = clean.rfind(']')
            end_brace   = clean.rfind('}')
            end = max(end_bracket, end_brace) + 1
            if start < end:
                clean = clean[start:end]
            clean = re.sub(r',\s*([}\]])', r'\1', clean)
            try:
                return json.loads(clean)
            except json.JSONDecodeError:
                # Fall back: extract only the outermost [ ] and retry
                bracket_start = clean.find('[')
                bracket_end = clean.rfind(']')
                if bracket_start != -1 and bracket_end > bracket_start:
                    clean = clean[bracket_start:bracket_end + 1]
                    clean = re.sub(r',\s*([}\]])', r'\1', clean)
                    return json.loads(clean)
                raise

        return raw

    except Exception as e:
        print(f"[GEMINI] Exception: {e}")
        return None


def pass1_extract_facts(api_key: str, ranked_context: str) -> list:
    prompt = f"""
You are a precise biotech intelligence analyst.
Read the following research fragments and extract structured facts.

Return ONLY a valid JSON array. No markdown, no explanation, no preamble.
Each element must have exactly these fields:
{{
  "source_title": "string",
  "company_or_institution": "string or null",
  "event_type": "funding | acquisition | research | product | regulation | other",
  "key_metric": "string or null (dollar amount, percentage, measurement — only if explicitly stated)",
  "date_mentioned": "string or null",
  "domain": "string (e.g. CRISPR, synthetic biology, neurotechnology)",
  "one_line_summary": "string (max 20 words, factual only)",
  "signal_strength": "high | medium | low"
}}

If a field cannot be determined, use null. Do NOT invent values.

RESEARCH FRAGMENTS:
{ranked_context[:12000]}
"""
    print("[PASS 1] Running extraction...")
    facts = generate_gemini_text(api_key, prompt, expect_json=True)

    if not isinstance(facts, list):
        print("[PASS 1] Failed to parse JSON")
        return []

    print(f"[PASS 1] Extracted {len(facts)} facts")
    return facts


def pass2_analyze(api_key: str, current_facts: list, prior_facts: list, current_date: str) -> str:
    prior_block = ""
    if prior_facts:
        prior_summary = json.dumps(prior_facts, indent=2)
        prior_block = f"""
LAST WEEK'S FACTS:
{prior_summary[:3000]}
"""

    current_block = json.dumps(current_facts, indent=2)

    prompt = f"""
Chief Intelligence Officer, {current_date}

Analyze these biotech intelligence signals:

THIS WEEK:
{current_block}
{prior_block}

Produce rigorous contextual analysis:

1. DOMINANT TREND: Most significant pattern this week?
2. RECURRING ENTITIES: Companies/institutions appearing multiple times or from last week?
3. EMERGING TECHNOLOGY: Which specific tech is gaining momentum?
4. OPERATIONAL CONSEQUENCE: What should a biotech venture DO this week?
5. CONFIDENCE: HIGH/MEDIUM/LOW based on source quality (explain why)
6. WATCH NEXT WEEK: One specific thing to monitor

ANTI-HALLUCINATION: Only reference data in the facts. Clearly distinguish confirmed vs inferred.
"""
    print("[PASS 2] Running analysis...")
    analysis = generate_gemini_text(api_key, prompt, expect_json=False)

    if not analysis:
        print("[PASS 2] Analysis failed")
        return "Analysis unavailable. " + " | ".join(
            f.get('one_line_summary', '') for f in current_facts if f.get('one_line_summary')
        )

    print(f"[PASS 2] Analysis complete ({len(analysis)} chars)")
    return analysis


def pass3_narrate(api_key: str, analysis: str, current_date: str) -> str:
    prompt = f"""
You are "BLK PHX LABS Voice," autonomous Chief of Staff.

Rewrite this analysis as a 300-word spoken executive briefing.

CLEAN READ:
1. NO MARKDOWN — no asterisks, hashes, bullets
2. NO ACRONYMS — spell everything phonetically
3. SPOKEN CADENCE — calm, authoritative
4. PRESERVE FACTS — don't add/remove claims
5. CONFIDENCE LANGUAGE — hedge if analysis says MEDIUM/LOW

STRUCTURE:
- Open: "System online. Date: {current_date}."
- Body: Two paragraphs (trend + consequence)
- Close: "End of line."

ANALYSIS:
{analysis}
"""
    print("[PASS 3] Running narration...")
    script = generate_gemini_text(api_key, prompt, expect_json=False)

    if not script:
        print("[PASS 3] Narration failed")
        return None

    script = script.replace('*', '').replace('#', '').replace('_', '').strip()
    print(f"[PASS 3] Script ready ({len(script)} chars)")
    return script


# ============================================================
# 7. PRODUCTION UTILS (Unchanged from V33)
# ============================================================
def upload_to_drive(file_path: str, folder_id: str, file_name: str) -> Optional[str]:
    try:
        service       = build('drive', 'v3', cache_discovery=False)
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        media         = MediaFileUpload(file_path, mimetype='video/mp4', resumable=True)
        file          = service.files().create(
            body=file_metadata, media_body=media, fields='id'
        ).execute()
        return file.get('id')
    except Exception as e:
        print(f"[DRIVE] Upload error: {e}")
        return None


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


# ============================================================
# 8. MAIN CLOUD EVENT TRIGGER (Updated with semantic scoring)
# ============================================================
@functions_framework.cloud_event
def generate_content(cloud_event):
    data        = cloud_event.data
    bucket_name = data["bucket"]
    file_name   = data["name"]

    if "queue/trigger_job.json" not in file_name:
        return

    try:
        print("[PIPELINE] ===== V34 SEMANTIC RAG START =====")
        
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
            print("[RAG] ⚠️ CRITICAL: Zero articles in payload")
            print("[RAG] Check Apps Script logs for Gmail sourcing issues")
            return

        raw_fragments = fetch_all_articles(articles)
        content_count = len([f for f in raw_fragments if f and '[Signal Only]' not in f])
        print(f"[SCRAPER] {content_count}/{len(articles)} yielded content")

        # NEW: Pass api_key to rank_and_chunk_context for semantic scoring
        ranked_context, scored_meta = rank_and_chunk_context(articles, raw_fragments, api_key)

        if not ranked_context.strip():
            print("[RAG] ⚠️ No content after ranking")
            return

        successful_sources = [m["title"] for m in scored_meta]
        print(f"[RELEVANCE] {len(successful_sources)} articles in final context")

        # Memory
        prior_facts = load_memory(bucket)

        # Three-pass Gemini
        current_date  = datetime.datetime.now().strftime("%B %d, %Y")

        current_facts = pass1_extract_facts(api_key, ranked_context)
        if not current_facts:
            print("[PASS 1] ⚠️ No facts extracted")

        analysis    = pass2_analyze(api_key, current_facts, prior_facts, current_date)
        script_text = pass3_narrate(api_key, analysis, current_date)

        if not script_text:
            print("[PASS 3] ⚠️ Narration failed")
            return

        save_memory(bucket, current_facts)

        # TTS → Audio
        s_input      = texttospeech.SynthesisInput(text=script_text)
        voice_params = texttospeech.VoiceSelectionParams(
            language_code="en-US", name=VOICE_NAME
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3
        )
        tts_resp = tts_client.synthesize_speech(
            input=s_input, voice=voice_params, audio_config=audio_config
        )
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(tts_resp.audio_content)
            audio_path = f.name
        print(f"[TTS] Audio: {audio_path}")

        # Video Production
        from moviepy.editor import VideoFileClip, AudioFileClip, CompositeAudioClip, concatenate_videoclips
        from moviepy.video.fx.all import crop

        bg_video_path = tempfile.mktemp(suffix=".mp4")
        bucket.blob("assets/background_loop.mp4").download_to_filename(bg_video_path)

        ac = AudioFileClip(audio_path)
        vc = VideoFileClip(bg_video_path)
        vc = crop(
            vc,
            width=vc.size[1] * (9 / 16), height=vc.size[1],
            x_center=vc.size[0] / 2, y_center=vc.size[1] / 2
        ).resize(height=1920)

        loops        = math.ceil(ac.duration / vc.duration) + 1
        final_vc     = concatenate_videoclips([vc] * loops).set_duration(ac.duration)
        bg_audio     = final_vc.audio
        merged_audio = (
            CompositeAudioClip([bg_audio.volumex(0.3).set_duration(ac.duration), ac])
            if bg_audio else ac
        )
        final_vc = final_vc.set_audio(merged_audio)

        output_video = "/tmp/final.mp4"
        final_vc.write_videofile(
            output_video,
            codec="libx264", audio_codec="aac",
            fps=24, preset="ultrafast", bitrate="2000k",
            threads=4, logger=None
        )
        print("[VIDEO] Production complete")

        # Archive
        ts           = int(time.time())
        archive_name = f"BLK PHX LABS_Raw_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.mp4"
        drive_id     = upload_to_drive(output_video, DRIVE_FOLDER_ID, archive_name)

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
            build_briefing_json(script_text, analysis, current_facts,
                                video_url, audio_url, ts, len(successful_sources)),
            content_type='application/json'
        )
        print(f"[ARCHIVE] Drive: {drive_id} | Published: {dashboard_url}")

        # Callback
        try:
            resp = httpx.post(WEBHOOK_URL, json={
                "status":        "success",
                "script_text":   script_text,
                "analysis":      analysis,
                "fact_count":    len(current_facts),
                "video_url":     video_url,
                "audio_url":     audio_url,
                "dashboard_url": dashboard_url,
                "drive_id":      drive_id,
                "source_count":  len(successful_sources)
            }, timeout=httpx.Timeout(connect=10.0, read=15.0, write=5.0, pool=5.0))
            print(f"[WEBHOOK] Callback: {resp.status_code}")
        except Exception as e:
            print(f"[WEBHOOK] Callback failed: {e}")

        try:
            lock_blob.delete()
        except Exception:
            pass

        print("[PIPELINE] ===== SUCCESS =====")

    except Exception as e:
        print(f"[CRITICAL] Unhandled exception: {e}")
        import traceback
        traceback.print_exc()
        raise
