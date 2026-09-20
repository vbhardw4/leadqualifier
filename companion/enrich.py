"""LeadQualifier enrichment.

Stage 1 — deterministic mock provider: a small directory of known demo domains.
           Deterministic, instant, costs $0. In client deployments this is swapped
           for a real lookup (Clearbit-style API, Apollo, etc.).
Stage 2 — LLM inference fallback: only when the domain is unknown AND no company was
           given. Results are labeled source=llm_inference with capped confidence.
"""
from scoring import llm_infer_enrichment

# Demo directory. In production, replace lookup() with a real provider call.
MOCK_DIRECTORY = {
    "acmerealty.com": {"company_name": "Acme Realty Group", "industry": "real estate",
                       "size_bucket": "51-200"},
    "brightsmile.dental": {"company_name": "Bright Smile Dental", "industry": "healthcare",
                           "size_bucket": "11-50"},
    "northwindtraders.com": {"company_name": "Northwind Traders", "industry": "e-commerce",
                             "size_bucket": "201-1000"},
    "harborlaw.com": {"company_name": "Harbor Law LLP", "industry": "legal",
                      "size_bucket": "11-50"},
    "sunsetstays.com": {"company_name": "Sunset Stays", "industry": "hospitality",
                        "size_bucket": "51-200"},
}

FREEMAIL = {"gmail.com", "yahoo.com", "yahoo.ca", "hotmail.com", "outlook.com",
            "icloud.com", "aol.com", "proton.me", "protonmail.com"}


def domain_of(email):
    try:
        return email.split("@", 1)[1].strip().lower()
    except (IndexError, AttributeError):
        return ""


def enrich(email, company, message, provider=None):
    domain = domain_of(email or "")
    hit = MOCK_DIRECTORY.get(domain)
    if hit:
        return {"company_name": hit["company_name"] or company or "unknown",
                "industry": hit["industry"], "size_bucket": hit["size_bucket"],
                "confidence": 0.95, "basis": f"directory match on {domain}",
                "source": "mock_directory"}
    if domain and domain not in FREEMAIL:
        # Unknown business domain: derive a display name, mark low confidence.
        name = (company or " ".join(p.capitalize() for p in
                                    domain.split(".")[0].replace("-", " ").split()))
        return {"company_name": name, "industry": "unknown", "size_bucket": "unknown",
                "confidence": 0.4, "basis": f"unlisted domain {domain}",
                "source": "domain_heuristic"}
    if not (company or "").strip():
        # Freemail + no company: ask the LLM to infer from the message text.
        return llm_infer_enrichment(message or "", provider=provider)
    return {"company_name": company, "industry": "unknown", "size_bucket": "unknown",
            "confidence": 0.5, "basis": "company stated on form, no directory record",
            "source": "self_reported"}
