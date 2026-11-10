#!/usr/bin/env python3
"""Jameson — bot de notícias em tempo real (editor-chefe de bolso).

Roda de tempos em tempos (cron), pega só o que é NOVO nos feeds, pede a um LLM para escolher o que
vale interromper o leitor agora, e manda cada notícia como uma mensagem no Telegram.
Modelo/provedor trocáveis por variável de ambiente (Pydantic AI) — ver .env.example."""
import argparse
import concurrent.futures
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
DATA = Path(os.environ.get("DATA_DIR") or BASE)  # onde ficam estado e logs (volume no Docker)
DATA.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- CONFIG ----
TZ = ZoneInfo("America/Sao_Paulo")
QUIET_HOURS = (21, 9)          # sem envio das 21h às 9h (a última rodada com envio é a das 20h)
MIN_SCORE = 8                  # o código só envia nota >= 8 (o modelo ainda devolve as 7, que ficam no picks.jsonl p/ calibrar)
ALWAYS_SCORE = 9               # nota >= 9 passa por cima do ritmo e dos tetos de categoria
MAX_PER_RUN = 7                # teto de mensagens por execução (evita enxurrada); execuções são de hora em hora
TOTAL_DAILY_CAP = 18           # teto diário para notas abaixo de ALWAYS_SCORE; nota 9-10 vai até HARD_DAILY_CAP
HARD_DAILY_CAP = 25
DAILY_CAPS = {"Brasil": 4, "Mundo": 4, "Política": 4, "Mercado": 3, "Tecnologia": 3, "Esportes": 3}
MORNING_ALLOWANCE = 0.3        # fração do dia já "liberada" às 9h (notícias da madrugada)
MAX_AGE_HOURS = 14             # ignora itens mais velhos que isso
FIRST_RUN_AGE_HOURS = 3        # no primeiro uso, só olha as últimas horas (evita flood)
PER_SOURCE_PER_RUN = 8
SNIPPET_CHARS = 280            # trecho enviado ao modelo (mais contexto = menos enchimento)
MAX_ITEMS_PER_RUN = 140
RECENT_SENT_HOURS = 48         # memória anti-repetição de assunto
RECENT_SENT_LIMIT = 60
FEEDBACK_BUTTONS = os.getenv("FEEDBACK_BUTTONS", "0") == "1"   # botões 👍/👎 sob cada mensagem (desligados por padrão)
ARTICLE_CHARS = 2500           # quanto da matéria o redator lê
LANG_OUT = "português do Brasil"

INTERESTS = """
Briefing equilibrado entre Brasil e mundo. Priorize fatos de grande impacto:
decisões de governo/Congresso/tribunais, eleições, conflitos e diplomacia, juros/inflação/câmbio/bolsa,
grandes movimentos de empresas, IA e tecnologia relevante, resultados e grandes eventos esportivos.
Ignore: celebridades, fofoca, clickbait, publieditorial, "dicas", promoções, opinião sem fato novo.
"""
CATEGORY_RULES = {
    "Brasil": "fatos nacionais de impacto (governo, Congresso, STF/tribunais, segurança, saúde pública, grandes eventos). Evite: serviço (horários, como chegar, regras de prova), explicativos atemporais, colunas de opinião, notas regionais pequenas e atos processuais ou administrativos de rotina (liminares e suspensões pontuais, nomeações, votações em comissão, pedidos de arquivamento, inclusão em listas) que não mudam a situação do país.",
    "Mundo": "política internacional, conflitos, diplomacia, eleições e decisões que afetam o cenário global. Acidentes, naufrágios, tiroteios e desastres só entram com número muito alto de vítimas ou efeito além do país; política interna de países pequenos e prisões ou processos de figuras estrangeiras sem efeito global ficam de fora.",
    "Política": "decisões e disputas de poder no Brasil e no exterior (eleições, Congresso, tribunais, governos); evite opinião sem fato novo.",
    "Mercado": "SOMENTE economia e mercados: juros, inflação, câmbio, bolsa, bancos centrais, política fiscal, commodities, resultados e movimentos de grandes empresas. Sem matérias de consumo, finanças pessoais, modismos ou interesse humano.",
    "Tecnologia": "IA, big techs, semicondutores, segurança digital, regulação e ciência/tecnologia relevantes; evite review de gadget e promoções.",
    "Esportes": "futebol é a prioridade (Brasil e grandes competições). Outros esportes só quando for grande acontecimento: finais e decisões de torneios importantes, F1, Copa do Mundo/Olimpíadas, Grand Slams, finais da NBA/NFL, xadrez de alto nível. Evite rodadas comuns de modalidades de nicho e semifinais de torneios pouco relevantes para o Brasil.",
}
# (categoria, região, nome, url) — só veículos estabelecidos
SOURCES = [
    ("Mundo", "Global", "BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Mundo", "Global", "The Guardian", "https://www.theguardian.com/world/rss"),
    ("Mundo", "Global", "Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("Mundo", "Global", "NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("Mundo", "BR", "G1 Mundo", "https://g1.globo.com/rss/g1/mundo/"),
    ("Mundo", "Global", "NYT World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("Mundo", "BR", "BBC Brasil", "https://feeds.bbci.co.uk/portuguese/rss.xml"),
    ("Mundo", "BR", "DW Brasil", "https://rss.dw.com/rdf/rss-br-all"),
    ("Brasil", "BR", "G1", "https://g1.globo.com/rss/g1/"),
    ("Brasil", "BR", "Folha", "https://feeds.folha.uol.com.br/emcimadahora/rss091.xml"),
    ("Brasil", "BR", "Agência Brasil", "https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml"),
    ("Brasil", "BR", "CNN Brasil", "https://www.cnnbrasil.com.br/feed/"),
    ("Política", "Global", "Guardian US Politics", "https://www.theguardian.com/us-news/us-politics/rss"),
    ("Política", "BR", "G1 Política", "https://g1.globo.com/rss/g1/politica/"),
    ("Política", "BR", "Folha Poder", "https://feeds.folha.uol.com.br/poder/rss091.xml"),
    ("Política", "BR", "Poder360", "https://www.poder360.com.br/feed/"),
    ("Política", "Global", "Washington Post", "https://feeds.washingtonpost.com/rss/world"),
    ("Mercado", "Global", "CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("Mercado", "Global", "MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Mercado", "BR", "Valor Econômico", "https://pox.globo.com/rss/valor"),
    ("Mercado", "BR", "InfoMoney", "https://www.infomoney.com.br/feed/"),
    ("Mercado", "BR", "Folha Mercado", "https://feeds.folha.uol.com.br/mercado/rss091.xml"),
    ("Mercado", "BR", "Exame", "https://exame.com/feed/"),
    ("Mercado", "Global", "NYT Business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
    ("Mercado", "Global", "Financial Times", "https://www.ft.com/rss/home"),
    ("Tecnologia", "Global", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Tecnologia", "Global", "The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Tecnologia", "Global", "TechCrunch", "https://techcrunch.com/feed/"),
    ("Tecnologia", "BR", "Tecnoblog", "https://tecnoblog.net/feed/"),
    ("Esportes", "Global", "BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
    ("Esportes", "Global", "Sky Sports", "https://www.skysports.com/rss/12040"),
    ("Esportes", "BR", "ge.globo", "https://ge.globo.com/rss/ge/"),
]
# --------------------------------------------------------------------------

STATE_FILE = DATA / "state.json"
UA = "Mozilla/5.0 (compatible; jameson-news-bot/1.0)"
log = logging.getLogger("jameson")


# ---------------------------------------------------------------- modelo ----
class Pick(BaseModel):
    id: int = Field(description="id do item na lista")
    categoria: str
    titulo: str = Field(description="primeiros 40 caracteres do título original, copiados exatamente")
    por_que: str = Field(default="", description="1 frase curta: o fato concreto e por que um leitor brasileiro precisa saber HOJE (se não consegue justificar, a nota deve ser baixa); não é enviada")
    nota: int = Field(ge=1, le=10, description="importância para o leitor, 1-10")
    manchete: str = Field(description="manchete curta (até 12 palavras) com verbo, só o fato principal")
    resumo: str = Field(default="", description="1 frase de contexto que complementa a manchete sem repeti-la; vazio se não houver nada a acrescentar")

    atualizacao: bool = Field(default=False, description="true se for desdobramento novo de uma história que já aparece em JÁ ENVIADAS")

    @property
    def texto(self):
        return f"{self.manchete}. {self.resumo}".strip() if self.resumo else self.manchete


class Selection(BaseModel):
    picks: list[Pick]


def make_model():
    """LLM_MODEL = 'provedor:modelo' (anthropic:claude-haiku-4-5, groq:..., deepseek:..., together:...,
    fireworks:..., openrouter:...). Para qualquer API compatível com OpenAI (ex.: Synthetic), defina
    também LLM_BASE_URL e LLM_API_KEY."""
    spec = os.getenv("LLM_MODEL", "anthropic:claude-haiku-4-5")
    base = os.getenv("LLM_BASE_URL")
    if base:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        return OpenAIChatModel(spec.split(":", 1)[-1],
                               provider=OpenAIProvider(base_url=base, api_key=os.environ["LLM_API_KEY"]))
    return spec


def norm(s):
    return re.sub(r"\W+", "", s.lower())


def build_instructions():
    rules = "\n".join(f"- {c}: {r}" for c, r in CATEGORY_RULES.items())
    return (
        "Você é o curador de um bot de notícias em tempo real. A cada execução recebe os itens NOVOS dos feeds e "
        "decide quais valem INTERROMPER o leitor agora.\n"
        f"Critérios do leitor:\n{INTERESTS}\nCategorias e critério de cada uma:\n{rules}\n\n"
        "Cada item vem como [id] [categoria|região|fonte] (idade) título — trecho. A categoria que você devolve "
        "deve seguir o CONTEÚDO da notícia, não a seção do feed.\n"
        "Dê a cada item uma nota de importância de 1 a 10 e DEVOLVA SOMENTE os itens com nota 7 ou mais. Se nada "
        "merece, devolva a lista vazia — enviar pouco é melhor que enviar ruído; na maioria das execuções 0 a 2 itens "
        "chegam a 7.\n"
        "Escala: 9-10 = imprescindível (raro, uns 2 a 5 por dia): ataque ou tragédia com várias mortes, queda/formação "
        "de governo ou troca de chefe de Estado, decisão de juros de banco central, grande movimento de mercado, final "
        "ou decisão de grande torneio, avanço de enorme impacto em IA/tecnologia. 7-8 = importante e novo: um fato "
        "concreto de impacto nacional ou global que o leitor ficaria mal-informado se perdesse hoje. 4-6 = "
        "interessante mas não vale interromper: análises, desdobramentos menores, notas regionais, rodadas comuns, "
        "serviço, previews de jogos e eventos, crises de países pequenos sem efeito fora deles, notícias de empresas "
        "ou pessoas sem grande impacto. 1-3 = ruído. "
        "Matérias analíticas, explicativas, comparativas, \"quem é\", colunas, opinião e balanços semanais NÃO são fato "
        "novo: nota 6 ou menos, a não ser que tragam um acontecimento concreto e recente. "
        "Seja criterioso e consistente: dê a mesma nota a notícias de peso parecido em qualquer hora do dia, sem se "
        "preocupar com quantas já foram enviadas (o sistema controla o volume).\n"
        "MESMA HISTÓRIA: se vários itens cobrem o mesmo fato, devolva só o melhor (um por fato). Se a história já "
        "aparece em JÁ ENVIADAS, NÃO devolva — nem versão de outra fonte, nem atualização de número (mortos, feridos, "
        "valores), nem reação de mercado ao mesmo fato, nem nova declaração dos mesmos personagens repetindo o que já foi "
        "dito, nem mais um passo burocrático do mesmo caso — a menos que haja um desdobramento realmente novo e grande "
        "(ex.: renúncia, prisão, acordo, mudança de patamar); nesse caso marque atualizacao=true e escreva só o fato novo. "
        "Compare pelo ASSUNTO (pessoas, empresas, país, caso), não pelas palavras do título: o mesmo assunto com outro "
        "título ou de outro veículo continua sendo a mesma história. Um dado que já estava previsto ou antecipado em JÁ "
        "ENVIADAS não é fato novo, mas o resultado oficial de um dado antecipado (ex.: o número da inflação depois da "
        "prévia) é.\n"
        f"Para cada item devolvido: primeiro preencha por_que (por que o leitor precisa saber HOJE; se for só curiosidade, "
        f"nota baixa), depois a manchete (até 12 palavras, com verbo) e, se houver, uma frase de contexto que NÃO repete a "
        f"manchete (deixe o resumo vazio se não houver nada novo a acrescentar), em {LANG_OUT}, usando "
        "SOMENTE o que está escrito no título e no trecho daquele item. NÃO use conhecimento próprio nem informação de "
        "outros itens: não acrescente nomes, cargos, títulos (ex.: \"presidente\", \"senador\", \"ex-\"), datas, causas, "
        "consequências, contexto, qualificativos (principal, maior, rival, recorde...) nem frases genéricas de "
        "enchimento (ex.: \"a crise envolve a Justiça e o sistema financeiro\"). Se o título ou o trecho não dizem o "
        "cargo de alguém, cite só o nome. Se o trecho termina cortado, NÃO complete nem deduza o que faltou: escreva "
        "só o que é certo. Se o item é opinião ou análise, não o devolva. Mantenha o grau de certeza e o tempo verbal da fonte (\"busca restringir\" não é \"restringe\"; \"supostamente\" continua suposto). Rótulos como VÍDEO, FOTOS, AO VIVO ou EXCLUSIVO no título não são fonte da informação: ignore-os. Abra pelo fato principal e inclua o país/cargo/nome-chave que o título ou o trecho trazem. "
        "Escreva a notícia direto, como uma manchete de telejornal: NUNCA cite o veículo (\"segundo o G1\") — a fonte já aparece no link — e NUNCA use \"segundo o título\", \"a "
        "matéria/reportagem explica\", \"foi anunciado que\" nem metalinguagem sobre o texto-fonte; comece pelo fato."
    )


def build_items_listing(items, now=None):
    now = now or datetime.now(timezone.utc)

    def age(it):
        if not it.get("time"):
            return "?"
        h = (now - it["time"]).total_seconds() / 3600
        return f"{h:.0f}h" if h >= 1 else "<1h"

    listing = "\n".join(
        f"[{i}] [{it['category']}|{it['region']}|{it['source']}] ({age(it)}) {it['title']} — {it['snippet']}"
        for i, it in enumerate(items)
    )
    return listing


def recent_line(title, resumo=""):
    return f"{title} — {resumo}" if resumo else title


def build_user_prompt(items, recent_sent, now=None):
    sent = "\n".join(f"- {t}" for t in recent_sent) or "(nenhuma)"
    return (f"JÁ ENVIADAS NAS ÚLTIMAS {RECENT_SENT_HOURS}H:\n{sent}\n\n"
            f"ITENS NOVOS:\n{build_items_listing(items, now)}")


REWRITE_INSTRUCTIONS = (
    f"Você é o redator de um bot de notícias. Para cada item recebe: título, trecho do feed, um RASCUNHO de manchete e "
    f"contexto, e o TEXTO DA MATÉRIA. Reescreva manchete e contexto em {LANG_OUT} usando SOMENTE o que está no texto da "
    "matéria, no trecho ou no título. Manchete: até 12 palavras, com verbo, o fato principal (quem fez o quê). "
    "Contexto: 1 a 2 frases curtas que ACRESCENTAM o que o leitor precisa para entender sem conhecimento prévio — quem é "
    "a pessoa ou empresa (cargo, nacionalidade), onde, o motivo ou a consequência, números relevantes. Se o texto da "
    "matéria trouxer detalhe que corrija o rascunho, siga o texto. NUNCA cite o veículo (\"segundo o G1\"), nunca use "
    "metatexto (\"não identificado no trecho\", \"a matéria diz\"), não invente, não use conhecimento próprio, não "
    "interprete nem dê juízo de valor. Mantenha o grau de certeza e o tempo verbal da fonte. Devolva um item por id."
)


class Rewrite(BaseModel):
    id: int
    manchete: str
    resumo: str = ""


class Rewrites(BaseModel):
    items: list[Rewrite]


def fetch_article(url):
    """Texto da matéria (og:description + primeiros parágrafos). Vazio se bloqueada/paywall/erro."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        r.raise_for_status()
        if "html" not in r.headers.get("content-type", "html"):
            return ""
        page = r.text[:600_000]
    except Exception:
        return ""
    junk = re.compile(r"(?i)cookie|assine|newsletter|todos os direitos|leia também|leia mais|publicidade|advertisement|subscribe|sign up")
    paras = [clean(m, 700) for m in re.findall(r"<p[^>]*>(.*?)</p>", page, re.S | re.I)]
    paras = [p for p in paras if len(p) >= 60 and not junk.search(p)]
    og = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]+content=["\']([^"\']+)', page, re.I)
    text = " ".join(([clean(og.group(1), 400)] if og else []) + paras)
    return text[:ARTICLE_CHARS]


def enrich(send, items, model=None):
    """2º passo: só para quem vai ser enviado, lê a matéria e reescreve manchete/contexto com o texto completo."""
    if not send:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        texts = list(ex.map(lambda p: fetch_article(items[p.id]["url"]), send))
    todo = [(p, t) for p, t in zip(send, texts) if len(t) >= 300]
    if not todo:
        log.info("redator: nenhuma matéria legível; mantendo rascunhos")
        return
    blocks = []
    for p, t in todo:
        it = items[p.id]
        blocks.append(f"[{p.id}] TÍTULO: {it['title']}\nTRECHO DO FEED: {it['snippet']}\n"
                      f"RASCUNHO: {p.manchete}. {p.resumo}\nTEXTO DA MATÉRIA: {t}")
    think = os.getenv("LLM_THINKING", "").strip().lower()
    settings = {"thinking": False if think == "off" else think} if think else None
    agent = Agent(model or make_model(), output_type=Rewrites, instructions=REWRITE_INSTRUCTIONS, retries=2,
                  model_settings=settings)
    allowed = {p.id for p, _ in todo}

    @agent.output_validator
    def _check(out: Rewrites) -> Rewrites:
        bad = [r.id for r in out.items if r.id not in allowed]
        if bad:
            raise ModelRetry(f"ids inválidos {bad}; use só {sorted(allowed)}")
        return out

    result = agent.run_sync("\n\n".join(blocks))
    u = result.usage
    log.info("redator: in=%s out=%s tokens, %d matérias lidas", u.input_tokens, u.output_tokens, len(todo))
    by_id = {r.id: r for r in result.output.items}
    for p, _ in todo:
        r = by_id.get(p.id)
        if r and r.manchete.strip():
            p.manchete, p.resumo = r.manchete.strip(), r.resumo.strip()


def entities(text):
    """Nomes próprios aproximados (palavras capitalizadas no meio da frase, sem acento/caixa)."""
    words = re.findall(r"\b[\wÀ-ÿ'-]+\b", text)
    out = set()
    for i, w in enumerate(words):
        if len(w) >= 4 and w[0].isupper() and i > 0 and not re.search(r"[.!?:]\s*$", " ".join(words[:i])):
            out.add(norm(unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode()))
    return out


def is_repeat(resumo, previous):
    """Quase-cópia de algo já enviado (mesma notícia de outra fonte)? Conservador de propósito: só >= 5 nomes
    próprios em comum cobrindo >= 70% do menor conjunto. Desdobramentos de uma história (ex.: novas etapas de
    uma crise) passam; quem decide se algo é fato novo é o modelo, que vê os resumos já enviados."""
    e = entities(resumo)
    for prev in previous:
        pe = entities(prev)
        common = len(e & pe)
        if common >= 5 and common >= 0.7 * min(len(e), len(pe)):
            return True
    return False


def drop_repeats(picks, recent_texts):
    """Descarta picks (nota < ALWAYS_SCORE) que repetem a história de algo já enviado ou de um pick melhor desta execução."""
    kept = []
    for p in sorted(picks, key=lambda p: -p.nota):
        if p.nota < ALWAYS_SCORE and is_repeat(p.texto, list(recent_texts) + [k.texto for k in kept]):
            continue
        kept.append(p)
    return kept


def validate_selection(items):
    """Checa id, categoria e se o título copiado bate com o item; se não, o Pydantic AI pede para o modelo corrigir."""
    def check(out: Selection) -> Selection:
        errs = []
        for p in out.picks:
            if not 0 <= p.id < len(items):
                errs.append(f"id {p.id} não existe (0..{len(items)-1})")
            elif p.categoria not in CATEGORY_RULES:
                errs.append(f"categoria '{p.categoria}' inválida; use uma de {list(CATEGORY_RULES)}")
            elif norm(p.titulo)[:25] not in norm(items[p.id]["title"]):
                errs.append(f"id {p.id}: o título copiado não bate com o item [{p.id}] da lista")
        if errs:
            raise ModelRetry("Corrija: " + "; ".join(errs))
        return out
    return check


def select_news(items, recent_sent, model=None):
    # LLM_THINKING: "off" desliga o raciocínio (mais barato/rápido); "low"/"medium"/"high" ajusta; vazio = padrão do modelo
    think = os.getenv("LLM_THINKING", "").strip().lower()
    settings = {"thinking": False if think == "off" else think} if think else None
    agent = Agent(model or make_model(), output_type=Selection, instructions=build_instructions(),
                  retries=2, model_settings=settings)
    agent.output_validator(validate_selection(items))
    result = agent.run_sync(build_user_prompt(items, recent_sent))
    u = result.usage  # propriedade no pydantic-ai >= 2
    log.info("LLM: in=%s out=%s tokens, %d picks", u.input_tokens, u.output_tokens, len(result.output.picks))
    return result.output.picks


# ---------------------------------------------------------------- coleta ----
def parse_time(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def clean(text, n):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).strip()[:n]


JUNK_TITLE = re.compile(r"(?i)horóscopo|mega-sena|lotofácil|\bquina\b|loteria|cupom|promoç|\bofertas?\b|onde assistir|palpites?|"
                        r"globoesporte\.com\s*$|\bao vivo\s*-")
LIVE_URL = re.compile(r"/ao-vivo/|/live/|/liveblog/")


def make_snippet(title, summary):
    text = clean(summary, 2000)
    if text.lower().startswith(title.lower()):          # G1 etc. repetem o título no começo do resumo
        text = text[len(title):].lstrip(" .:-—")
    return text[:SNIPPET_CHARS]


def fetch(cat, region, name, url, cutoff):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)   # feedparser sozinho não tem timeout
        r.raise_for_status()
        d = feedparser.parse(r.content)
    except Exception as ex:
        log.warning("%s falhou: %s", name, ex)
        return []
    if not d.entries:
        log.warning("%s: feed vazio", name)
    out = []
    for e in d.entries:
        t = parse_time(e)
        if t and t < cutoff:
            continue
        title = clean(e.get("title"), 200)
        if JUNK_TITLE.search(title) or LIVE_URL.search(e.get("link", "")):
            continue
        out.append({"category": cat, "region": region, "source": name, "title": title,
                    "url": e.get("link", ""), "snippet": make_snippet(title, e.get("summary")), "time": t})
    out.sort(key=lambda x: x["time"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:PER_SOURCE_PER_RUN]


def collect_new(seen, max_age_hours):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items, urls, titles = [], set(), set()
    for src in SOURCES:
        for it in fetch(*src, cutoff):
            tkey = norm(it["title"])[:60]
            if not it["url"] or not it["title"] or it["url"] in seen or it["url"] in urls or tkey in titles:
                continue
            urls.add(it["url"]); titles.add(tkey)
            items.append(it)
        time.sleep(0.2)
    items.sort(key=lambda x: x["time"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    log.info("%d itens novos", len(items))
    return items[:MAX_ITEMS_PER_RUN]


# ---------------------------------------------------------------- estado ----
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"seen": {}, "sent": [], "daily": {}, "last_error_alert": 0, "initialized": False}


def save_state(st):
    now = time.time()
    st["seen"] = {u: ts for u, ts in st["seen"].items() if ts > now - 7 * 86400}
    st["sent"] = [s for s in st["sent"] if s["ts"] > now - 2 * 86400]
    st["meta"] = {h: m for h, m in st.get("meta", {}).items() if m.get("ts", 0) > now - 30 * 86400}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def in_quiet_hours(now_local):
    a, b = QUIET_HOURS
    h = now_local.hour
    return (h >= a or h < b) if a > b else (a <= h < b)


def day_fraction(now_local):
    """0..1: quanto do período ativo do dia (entre o fim e o início do silêncio) já passou."""
    start, end = QUIET_HOURS[1], QUIET_HOURS[0]
    frac = (now_local.hour + now_local.minute / 60 - start) / (end - start)
    return min(1.0, max(MORNING_ALLOWANCE, frac))  # piso: a 1ª execução do dia carrega as notícias da madrugada


def apply_policy(picks, items, daily, day_frac=1.0):
    """Controle de volume feito em código (o modelo só dá a nota):
    - nota >= ALWAYS_SCORE (9-10): passa sempre (até o teto duro);
    - nota 7-8: cada categoria tem um orçamento que cresce ao longo do dia (ritmo = teto x fração do dia + 1),
      para nenhuma categoria (ex.: Mundo) gastar tudo cedo e para sobrar espaço a notícias que chegam depois.
      A nota 8 tem folga extra (+2 no ritmo, +3 no teto da categoria).
    Devolve (enviar, adiar)."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    ranked = sorted(picks, key=lambda p: (-p.nota, items[p.id].get("time") or epoch))
    send, defer = [], []
    count, total = dict(daily.get("count", {})), daily.get("total", 0)
    for p in ranked:
        if p.nota < MIN_SCORE:
            continue
        if p.nota >= ALWAYS_SCORE:
            if total >= HARD_DAILY_CAP:
                continue
        else:
            bonus = p.nota - MIN_SCORE
            cap = DAILY_CAPS[p.categoria]
            limit = min(math.ceil(cap * day_frac) + 1 + 2 * bonus, cap + 3 * bonus)
            if count.get(p.categoria, 0) >= limit or total >= TOTAL_DAILY_CAP + 2 * bonus:
                continue                  # fora do orçamento: descarta de vez
        if len(send) >= MAX_PER_RUN:
            defer.append(p)               # fica pra próxima execução
            continue
        send.append(p)
        count[p.categoria] = count.get(p.categoria, 0) + 1
        total += 1
    return send, defer


# -------------------------------------------------------------- telegram ----
def format_message(it, p):
    alert = "🔴 " if p.nota >= ALWAYS_SCORE else ""
    ctx = f"\n{html.escape(p.resumo)}" if p.resumo else ""
    tag = " · 🔁 atualização" if p.atualizacao else ""
    return (f"{alert}<b>{html.escape(p.categoria)}</b>{tag}\n<b>{html.escape(p.manchete)}</b>{ctx}\n"
            f"<a href=\"{html.escape(it['url'], quote=True)}\">{html.escape(it['source'])}</a>")


def tg(method, **payload):
    r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}", json=payload, timeout=20)
    if not r.ok:   # sem a URL na mensagem (ela contém o token do bot)
        raise requests.HTTPError(f"Telegram {method}: {r.status_code} {r.text[:200]}")
    return r.json()


def send_telegram(text, preview=False, chat_id=None, markup=None):
    extra = {"reply_markup": markup} if markup else {}
    tg("sendMessage", chat_id=chat_id or os.environ["TELEGRAM_CHAT_ID"], text=text, parse_mode="HTML",
       disable_web_page_preview=not preview, **extra)


def url_hash(url):
    return hashlib.sha1(url.encode()).hexdigest()[:10]


def feedback_markup(h):
    return {"inline_keyboard": [[{"text": "👍", "callback_data": f"v1:{h}"}, {"text": "👎", "callback_data": f"v0:{h}"}]]}


def handle_vote(st, cq):
    """Registra 👍/👎 em feedback.jsonl (base para calibrar o prompt) e troca os botões por uma confirmação."""
    data = cq.get("data", "")
    msg = cq.get("message") or {}
    try:
        if data.startswith(("v1:", "v0:")):
            vote, h = int(data[1]), data[3:]
            meta = st.get("meta", {}).get(h, {})
            vkey = f"{msg.get('chat', {}).get('id')}:{msg.get('message_id')}"
            voted = st.setdefault("voted", [])
            if vkey not in voted:          # clique repetido na mesma mensagem não conta duas vezes
                voted.append(vkey); del voted[:-500]
                with open(DATA / "feedback.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps({**meta, "ts_envio": meta.get("ts"), "ts": int(time.time()),
                                        "chat": msg.get("chat", {}).get("id"), "voto": vote}, ensure_ascii=False) + "\n")
            label = "👍 anotado" if vote else "👎 anotado"
            try:
                tg("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                   reply_markup={"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]})
            except requests.RequestException as ex:   # ex.: "message is not modified" em clique repetido
                log.info("botão não editado: %s", ex)
        tg("answerCallbackQuery", callback_query_id=cq["id"])
    except (requests.RequestException, KeyError) as ex:
        log.warning("voto: %s", ex)


def recipients(st):
    """Dono (TELEGRAM_CHAT_ID) + quem se cadastrou com /start CÓDIGO."""
    return list(dict.fromkeys([str(os.environ["TELEGRAM_CHAT_ID"])] + list(st.get("subscribers", {}))))


def process_updates(st):
    """Lê as mensagens novas do bot: /start CÓDIGO cadastra, /parar descadastra. Código em INVITE_CODE (.env);
    sem ele, ninguém além do dono consegue se cadastrar."""
    code = os.getenv("INVITE_CODE", "").strip()
    owner = str(os.environ["TELEGRAM_CHAT_ID"])
    subs = st.setdefault("subscribers", {})
    for u in tg("getUpdates", offset=st.get("tg_offset", 0), timeout=0, allowed_updates=["message", "callback_query"]).get("result", []):
        st["tg_offset"] = u["update_id"] + 1
        if u.get("callback_query"):
            handle_vote(st, u["callback_query"])
            continue
        m = u.get("message") or {}
        chat, text = m.get("chat", {}), (m.get("text") or "").strip()
        if chat.get("type") != "private" or not text.startswith("/"):
            continue
        cid = str(chat["id"])
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        if cmd == "/start":
            if cid == owner or cid in subs:
                reply = "Você já está inscrito. Mande /parar para sair."
            elif code and hmac.compare_digest(arg.encode(), code.encode()):
                subs[cid] = {"name": chat.get("first_name", ""), "since": time.time()}
                reply = "Pronto, você está inscrito! Vou mandar as notícias que importam, uma por mensagem. /parar para sair."
                log.info("novo assinante: %s (%s)", cid, chat.get("first_name", ""))
            else:
                reply = "Este bot é privado. Para entrar, mande: /start SEU_CÓDIGO"
        elif cmd in ("/parar", "/stop"):
            if subs.pop(cid, None) is not None:
                reply = "Cancelado. Para voltar, mande /start SEU_CÓDIGO."
                log.info("assinante saiu: %s", cid)
            else:
                continue
        else:
            continue
        try:
            send_telegram(reply, chat_id=cid)
        except requests.RequestException as ex:
            log.warning("resposta a %s falhou: %s", cid, ex)


def broadcast(msg, st, markup=None):
    """Envia para todos; quem bloqueou o bot (403) ou não existe mais (400) sai da lista. Falha do dono propaga."""
    owner = str(os.environ["TELEGRAM_CHAT_ID"])
    for cid in recipients(st):
        try:
            send_telegram(msg, chat_id=cid, markup=markup)
        except requests.HTTPError as ex:
            if cid == owner:
                raise
            if ex.response is not None and ex.response.status_code in (400, 403):
                st.get("subscribers", {}).pop(cid, None)
                log.info("assinante %s removido (%s)", cid, ex.response.status_code)
            else:
                log.warning("envio a %s falhou: %s", cid, ex)


# ------------------------------------------------------------------ main ----
def run(dry=False, force=False, model=None):
    st = load_state()
    now_local = datetime.now(TZ)
    if in_quiet_hours(now_local) and not force:
        log.info("Horário de silêncio (%s). Nada a fazer.", now_local.strftime("%H:%M"))
        return []
    if not dry:
        try:
            process_updates(st)
        except requests.RequestException as ex:
            log.warning("cadastro: %s", ex)
    today = now_local.strftime("%Y-%m-%d")
    if st["daily"].get("date") != today:
        st["daily"] = {"date": today, "count": {}, "total": 0}

    if st["daily"].get("total", 0) >= HARD_DAILY_CAP and not force:   # nada mais pode ser enviado hoje: poupa a chamada ao modelo
        log.info("Teto diário (%d) atingido; pulando a rodada.", HARD_DAILY_CAP)
        if not dry: save_state(st)
        return []
    first = not st.get("initialized")
    items = collect_new(st["seen"], FIRST_RUN_AGE_HOURS if first else MAX_AGE_HOURS)
    if not items:
        st["initialized"] = True
        if not dry: save_state(st)
        return []
    recent = [recent_line(s["title"], s.get("resumo", "")) for s in st["sent"] if s["ts"] > time.time() - RECENT_SENT_HOURS * 3600][-RECENT_SENT_LIMIT:]
    picks = drop_repeats(select_news(items, recent, model), recent)
    send, defer = apply_policy(picks, items, st["daily"], day_fraction(now_local))
    try:
        enrich(send, items, model)
    except Exception as ex:                # sem redator, vale o rascunho do 1º passo
        log.warning("redator falhou (%s); mantendo rascunhos", ex)

    if not dry:                            # diário de decisões: base para calibrar notas com dados reais
        sent_ids = {p.id for p in send}
        with open(DATA / "picks.jsonl", "a", encoding="utf-8") as f:
            for p in picks:
                f.write(json.dumps({"ts": int(time.time()), "fonte": items[p.id]["source"], "categoria": p.categoria,
                                    "nota": p.nota, "resumo": p.resumo, "enviada": p.id in sent_ids, "adiada": p in defer,
                                    "manchete": p.manchete, "url": items[p.id]["url"]}, ensure_ascii=False) + "\n")

    sent_msgs = []
    for p in send:
        it = items[p.id]
        msg = format_message(it, p)
        sent_msgs.append(msg)
        if dry:
            continue
        h = url_hash(it["url"])
        st.setdefault("meta", {})[h] = {"manchete": p.manchete, "url": it["url"], "categoria": p.categoria,
                                        "nota": p.nota, "fonte": it["source"], "ts": int(time.time())}
        broadcast(msg, st, feedback_markup(h) if FEEDBACK_BUTTONS else None)
        time.sleep(1)                      # limite do Telegram: ~1 msg/s por chat
        st["sent"].append({"title": it["title"], "resumo": p.texto, "url": it["url"], "ts": time.time()})
        st["daily"]["count"][p.categoria] = st["daily"]["count"].get(p.categoria, 0) + 1
        st["daily"]["total"] += 1
        save_state(st)                     # a cada envio: se cair no meio, não reenvia ao recomeçar
    if not dry:
        deferred_ids = {p.id for p in defer}
        now = time.time()
        for i, it in enumerate(items):    # tudo que foi avaliado vira "visto", menos o que ficou adiado
            if i not in deferred_ids:
                st["seen"][it["url"]] = now
        st["initialized"] = True
        save_state(st)
    log.info("avaliados=%d escolhidos=%d enviados=%d adiados=%d", len(items), len(picks), len(send), len(defer))
    return sent_msgs


BALANCE_ALERT_USD = float(os.environ.get("BALANCE_ALERT_USD", "0.25"))  # avisa o dono quando o saldo DeepSeek cair abaixo disso


def check_balance(st):
    """Avisa só o dono (no máx. 1x/dia) se o saldo da DeepSeek estiver baixo. Silencioso se não usar DeepSeek."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key or not os.environ.get("LLM_MODEL", "").startswith("deepseek"):
        return
    if time.time() - st.get("last_balance_alert", 0) < 22 * 3600:
        return
    try:
        r = requests.get("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"}, timeout=15)
        info = r.json()["balance_infos"][0]
        bal = float(info["total_balance"])
        usd = bal if info.get("currency") == "USD" else bal / 7.2
    except Exception as ex:
        log.warning("Não consegui checar o saldo: %s", ex)
        return
    if usd < BALANCE_ALERT_USD:
        send_telegram(f"⚠️ Saldo da DeepSeek baixo: ~US$ {usd:.2f} (alerta abaixo de US$ {BALANCE_ALERT_USD:.2f}). Recarregue para o Jameson não parar.")
        st["last_balance_alert"] = time.time(); save_state(st)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="imprime em vez de enviar; não grava estado")
    ap.add_argument("--force", action="store_true", help="ignora horário de silêncio")
    ap.add_argument("--register", action="store_true", help="só processa /start e /parar (sem LLM); rode a cada minuto")
    args = ap.parse_args()
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  RotatingFileHandler(DATA / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")])
    if args.register:
        st = load_state()
        process_updates(st)
        save_state(st)
        return
    if not args.dry_run:
        try:
            st0 = load_state(); check_balance(st0)
        except Exception:
            log.exception("check_balance")
    try:
        msgs = run(dry=args.dry_run, force=args.force)
        if args.dry_run:
            for m in msgs:
                print(re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"[\2](\1)", re.sub(r"</?b>", "**", m)), "\n")
    except Exception as ex:
        log.exception("Falha no bot")
        st = load_state()
        if not args.dry_run and time.time() - st.get("last_error_alert", 0) > 6 * 3600:  # no máx. 1 alerta / 6h
            try:
                send_telegram(f"⚠️ Jameson falhou: {html.escape(str(ex))[:400]}")
                st["last_error_alert"] = time.time(); save_state(st)
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
