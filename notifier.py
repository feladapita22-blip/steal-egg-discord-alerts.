import asyncio
import json
import os
import re
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
from playwright.async_api import async_playwright

SITE_URL = os.getenv("SITE_URL", "https://www.stealegg.com/en/live/")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DEFAULT_USER_IDS = "1159975695599026196,954450810236964905,1339365119653711923,785601374082957322"
USER_IDS = [x.strip() for x in os.getenv("DISCORD_USER_IDS", DEFAULT_USER_IDS).split(",") if x.strip()]
POLL_SECONDS = max(60, int(os.getenv("POLL_SECONDS", "60")))
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_records.json"))

RARITIES = ("Divine", "Eternal", "Secret")
RARITY_MAP = {
    "divine": "Divino",
    "eternal": "Eterno",
    "secret": "Secreto",
    "divino": "Divino",
    "eterno": "Eterno",
    "secreto": "Secreto",
}

def log(message):
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}", flush=True)

def load_seen():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except (OSError, json.JSONDecodeError):
        return set()

def save_seen(seen):
    # Retém apenas os 500 registros mais recentes para evitar crescimento infinito.
    STATE_FILE.write_text(json.dumps(list(seen)[-500:], ensure_ascii=False, indent=2), encoding="utf-8")

def allowed_mentions():
    return {"parse": [], "users": USER_IDS}

def send_webhook(content, embeds=None):
    if not WEBHOOK_URL:
        log("DISCORD_WEBHOOK_URL não configurado; alerta não enviado.")
        return False
    payload = {
        "content": content,
        "allowed_mentions": allowed_mentions(),
    }
    if embeds:
        payload["embeds"] = embeds
    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=20)
        if response.status_code >= 300:
            log(f"Webhook respondeu HTTP {response.status_code}: {response.text[:300]}")
            return False
        return True
    except requests.RequestException as exc:
        log(f"Erro ao enviar webhook: {exc}")
        return False

async def read_records(page):
    # O site renderiza os registros via JavaScript. Lemos o texto da página
    # em um navegador headless; não usamos captura de tela nem abrimos Roblox.
    await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_function(
            "() => document.body && /Secret|Eternal|Divine|Secreto|Eterno|Divino/i.test(document.body.innerText)",
            timeout=45000,
        )
    except Exception:
        pass
    await page.wait_for_timeout(2500)

    records = await page.evaluate("""() => {
      const out = [];
      const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
      const rarityRe = /\\\\b(secret|eternal|divine|secreto|eterno|divino)\\\\b/i;
      // Prefer actual table rows if the page has them.
      for (const tr of document.querySelectorAll('tr')) {
        const text = norm(tr.innerText);
        if (text && rarityRe.test(text) && !/rarity|spawn history|history/i.test(text)) out.push(text);
      }
      // Fallback: visible text lines, useful if the site uses div-based rows.
      if (!out.length) {
        const lines = (document.body?.innerText || '').split(/\\n+/).map(norm).filter(Boolean);
        for (let i = 0; i < lines.length; i++) {
          if (rarityRe.test(lines[i]) && !/rarity|spawn history|history/i.test(lines[i])) {
            out.push(lines.slice(Math.max(0, i-2), Math.min(lines.length, i+3)).join(' | '));
          }
        }
      }
      return [...new Set(out)].slice(0, 300);
    }""")
    # Filter to records that actually mention one of the supported rarities.
    clean = []
    for text in records:
        if re.search(r"\b(secret|eternal|divine|secreto|eterno|divino)\b", text, re.I):
            clean.append(re.sub(r"\s+", " ", text).strip())
    return clean

def rarity_of(text):
    for key, label in RARITY_MAP.items():
        if re.search(rf"\b{re.escape(key)}\b", text, re.I):
            return label
    return "Raro"

def stable_id(text):
    import hashlib
    return hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:20]

async def main():
    if not WEBHOOK_URL:
        log("Configure DISCORD_WEBHOOK_URL antes de iniciar.")
        raise SystemExit(2)
    log(f"Iniciando consulta de {SITE_URL} a cada {POLL_SECONDS}s.")
    log("Atenção: são relatos comunitários, não confirmação do seu servidor Roblox.")
    seen = load_seen()
    first_success = True

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page(locale="en-US")
        while True:
            try:
                records = await read_records(page)
                if not records:
                    log("Nenhum registro reconhecido; tentando novamente no próximo ciclo.")
                else:
                    # On first run, baseline existing records silently so history
                    # doesn't trigger a flood of old alerts.
                    if first_success and not seen:
                        for record in records:
                            seen.add(stable_id(record))
                        save_seen(seen)
                        log(f"Histórico inicial salvo ({len(records)} registros); sem alertas antigos.")
                    else:
                        new_records = []
                        for record in records:
                            rid = stable_id(record)
                            if rid not in seen:
                                seen.add(rid)
                                new_records.append(record)
                        if new_records:
                            # Notify newest-first; limit burst if site returns many at once.
                            for record in new_records[:10]:
                                rarity = rarity_of(record)
                                mentions = " ".join(f"<@{uid}>" for uid in USER_IDS)
                                content = f"🥚 **Novo registro {rarity}!**\n{mentions}\n{record}\nFonte: {SITE_URL}"
                                ok = send_webhook(content)
                                log(f"{'Alerta enviado' if ok else 'Falha no alerta'}: {record[:160]}")
                                await asyncio.sleep(1)
                            if len(new_records) > 10:
                                log(f"{len(new_records)-10} registros extras foram ignorados para evitar spam.")
                            save_seen(seen)
                    first_success = False
            except Exception as exc:
                log(f"Falha ao consultar o site: {type(exc).__name__}: {exc}")
            await asyncio.sleep(POLL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
