import os, re
from typing import List

def bulletify(text: str, n:int=8) -> List[str]:
    # fallback extractive "summary" if no LLM key is present
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return lines[:n]

def llm_summarize(text: str, max_bullets:int=8) -> List[str]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    provider = os.getenv("LLM_PROVIDER", "openai")
    if not api_key or provider == "none":
        return bulletify(text, n=max_bullets)
    try:
        # Minimal OpenAI client usage (works with openai>=1.0)
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        prompt = f"Summarize the following council/commission agenda into {max_bullets} plain-English bullets, focusing on budget, ordinances/resolutions, land use, fees, contracts, executive sessions, appointments. Keep each bullet short.\n\n---\n{text}\n---"
        chat = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role":"user","content":prompt}],
            temperature=0.2,
            max_tokens=500
        )
        content = chat.choices[0].message.content
        # Simple split into bullets
        out = []
        for ln in content.splitlines():
            ln = ln.strip("-•* ").strip()
            if ln:
                out.append(ln)
        return out[:max_bullets] or bulletify(text, n=max_bullets)
    except Exception:
        return bulletify(text, n=max_bullets)
