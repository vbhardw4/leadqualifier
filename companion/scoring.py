"""LeadQualifier scoring engine.

LLM_PROVIDER selects the backend:
  stub      — deterministic keyword-based scorer. Used by tests and CI; costs $0.
  gemini    — Google Gemini free-tier API (default for the live demo; costs $0,
              key in GEMINI_API_KEY).
  ollama    — local model via Ollama (self-hosted option; costs $0).
  openai    — OpenAI chat completions (client deployments; client pays their key).
  anthropic — Anthropic messages API (client deployments; client pays their key).

The rubric prompt is loaded from prompts/bant-rubric.md so buyers can audit/edit it.
Output is always {"score": int 0-100, "rationale": str, "dimensions": {...}}.
"""
import json
import os
import re

import requests

PROMPTS_DIR = os.environ.get("PROMPTS_DIR", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "prompts"))


def load_rubric():
    with open(os.path.join(PROMPTS_DIR, "bant-rubric.md")) as f:
        return f.read()


def load_enrichment_prompt():
    with open(os.path.join(PROMPTS_DIR, "enrichment-prompt.md")) as f:
        return f.read()


def band_for(score):
    if score >= 70:
        return "hot"
    if score >= 50:
        return "warm"
    return "junk"


def build_scoring_prompt(lead, enrichment):
    rubric = load_rubric()
    lead_txt = "\n".join(f"{k}: {v}" for k, v in lead.items()
                         if v not in (None, "") and k != "id")
    enr_txt = "\n".join(f"{k}: {v}" for k, v in (enrichment or {}).items()
                        if v not in (None, ""))
    return (f"{rubric}\n\n---\n## Lead to score\n{lead_txt}\n\n"
            f"## Enrichment (source: {(enrichment or {}).get('source', 'none')})\n{enr_txt}\n")


def _extract_json(text):
    """Pull the first {...} JSON object out of model output."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    return json.loads(match.group(0))


def _normalize(payload):
    dims = payload.get("dimensions", {}) or {}
    score = max(0, min(100, int(payload.get("score", 0))))
    rationale = str(payload.get("rationale", "")).strip() or "No rationale returned."
    norm_dims = {k: max(0, min(25, int(dims.get(k, 0))))
                 for k in ("budget", "authority", "need", "timeline")}
    return {"score": score, "rationale": rationale, "dimensions": norm_dims,
            "band": band_for(score)}


def _call_ollama(prompt, base_url, model):
    r = requests.post(f"{base_url}/api/generate", json={
        "model": model, "prompt": prompt, "stream": False,
        "format": "json", "options": {"temperature": 0.2, "num_ctx": 4096},
    }, timeout=180)
    r.raise_for_status()
    return r.json()["response"]


def _call_openai(prompt, api_key, model):
    r = requests.post("https://api.openai.com/v1/chat/completions",
                      headers={"Authorization": f"Bearer {api_key}"},
                      json={"model": model, "temperature": 0.2,
                            "response_format": {"type": "json_object"},
                            "messages": [
                                {"role": "system",
                                 "content": "You are a B2B lead-qualification analyst."},
                                {"role": "user", "content": prompt}]},
                      timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _call_anthropic(prompt, api_key, model):
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": api_key,
                               "anthropic-version": "2023-06-01"},
                      json={"model": model, "max_tokens": 800, "temperature": 0.2,
                            "system": "You are a B2B lead-qualification analyst."
                                      " Reply with ONLY the JSON object.",
                            "messages": [{"role": "user", "content": prompt}]},
                      timeout=120)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in
                   r.json()["content"] if b.get("type") == "text")


GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# JSON Schemas sent as Gemini `responseSchema` so the model returns strict JSON.
# `_extract_json` stays as a safety net for non-conforming replies.
_SCORING_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "Total BANT score 0-100"},
        "rationale": {"type": "string",
                      "description": "One sentence, plain language, "
                                     "citing what the lead actually said"},
        "dimensions": {
            "type": "object",
            "properties": {
                "budget": {"type": "integer"},
                "authority": {"type": "integer"},
                "need": {"type": "integer"},
                "timeline": {"type": "integer"},
            },
            "required": ["budget", "authority", "need", "timeline"],
        },
    },
    "required": ["score", "rationale", "dimensions"],
}

_ENRICHMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "industry": {"type": "string"},
        "size_bucket": {"type": "string",
                        "enum": ["1-10", "11-50", "51-200", "201-1000",
                                 "1000+", "unknown"]},
        "confidence": {"type": "number"},
        "basis": {"type": "string",
                  "description": "Short phrase quoting the text that "
                                 "supports the inference"},
    },
    "required": ["company_name", "industry", "size_bucket", "confidence", "basis"],
}


def _call_gemini(prompt, api_key, model, schema):
    r = requests.post(
        f"{GEMINI_API_BASE}/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key},
        json={
            "system_instruction": {"parts": [{"text":
                "You are a B2B lead-qualification analyst. "
                "Reply with ONLY the JSON object."}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        },
        timeout=120)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def _stub_score(lead, enrichment):
    """Deterministic test double. Keyword-based, no LLM. Never used in production."""
    text = " ".join(str(lead.get(k, "")) for k in
                    ("role", "message", "budget_range", "timeline", "company")).lower()
    budget = 22 if any(w in text for w in ("budget", "$", "approved", "5k", "10k")) else \
        12 if "startup" in text else 5
    authority = 22 if any(w in text for w in ("founder", "ceo", "owner", "director", "vp ")) else \
        14 if any(w in text for w in ("manager", "lead", "head")) else 6
    need = 22 if any(w in text for w in ("leads", "follow-up", "manual", "copy", "paste",
                                        "spreadsheet", "lose track", "slow")) else \
        12 if "automat" in text else 5
    timeline = 22 if any(w in text for w in ("asap", "this month", "urgent", "now")) else \
        14 if "quarter" in text else 6
    dims = {"budget": budget, "authority": authority, "need": need, "timeline": timeline}
    score = sum(dims.values())
    return {"score": score, "rationale": f"[stub] keyword score {score}/100.",
            "dimensions": dims, "band": band_for(score)}


def score_lead(lead, enrichment=None, provider=None):
    provider = provider or os.environ.get("LLM_PROVIDER", "gemini")
    if provider == "stub":
        return _stub_score(lead, enrichment)
    prompt = build_scoring_prompt(lead, enrichment)
    if provider == "gemini":
        raw = _call_gemini(prompt, os.environ["GEMINI_API_KEY"],
                           os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                           _SCORING_SCHEMA)
    elif provider == "ollama":
        raw = _call_ollama(prompt,
                           os.environ.get("OLLAMA_URL", "http://ollama:11434"),
                           os.environ.get("SCORING_MODEL", "llama3.2:3b"))
    elif provider == "openai":
        raw = _call_openai(prompt, os.environ["OPENAI_API_KEY"],
                           os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    elif provider == "anthropic":
        raw = _call_anthropic(prompt, os.environ["ANTHROPIC_API_KEY"],
                             os.environ.get("ANTHROPIC_MODEL",
                                            "claude-3-5-haiku-20241022"))
    else:
        raise ValueError(f"unknown LLM_PROVIDER: {provider}")
    return _normalize(_extract_json(raw))


def llm_infer_enrichment(message, provider=None):
    """Fallback enrichment: infer firmographics from free text. Confidence capped."""
    provider = provider or os.environ.get("LLM_PROVIDER", "gemini")
    prompt = (load_enrichment_prompt() + "\n\n---\n## Lead message\n" + (message or ""))
    if provider == "stub":
        return {"company_name": "unknown", "industry": "unknown",
                "size_bucket": "unknown", "confidence": 0.0,
                "basis": "stub provider", "source": "llm_inference"}
    if provider == "gemini":
        raw = _call_gemini(prompt, os.environ["GEMINI_API_KEY"],
                           os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                           _ENRICHMENT_SCHEMA)
    elif provider == "ollama":
        raw = _call_ollama(prompt, os.environ.get("OLLAMA_URL", "http://ollama:11434"),
                           os.environ.get("SCORING_MODEL", "llama3.2:3b"))
    elif provider == "openai":
        raw = _call_openai(prompt, os.environ["OPENAI_API_KEY"],
                           os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    elif provider == "anthropic":
        raw = _call_anthropic(prompt, os.environ["ANTHROPIC_API_KEY"],
                             os.environ.get("ANTHROPIC_MODEL",
                                            "claude-3-5-haiku-20241022"))
    else:
        raise ValueError(f"unknown LLM_PROVIDER: {provider}")
    data = _extract_json(raw)
    data["confidence"] = min(0.6, float(data.get("confidence", 0)))
    data["source"] = "llm_inference"
    return data
