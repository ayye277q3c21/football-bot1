"""One scheduled run: fetch RSS, filter by football topics, post to the Telegram channel, save state."""
import asyncio
import html
import json
import os
import re
from pathlib import Path

import feedparser
import httpx
from telegram import Bot
from telegram.error import BadRequest, RetryAfter

BASE_DIR = Path(__file__).resolve().parent
STATE = BASE_DIR / 'published.json'
FEED = 'https://www.championat.com/rss/news/football/'
CHANNEL = os.getenv('CHANNEL_USERNAME', '@zafootball_news')
MAX_PER_RUN = 5
CAPTION_LIMIT = 1024  # Telegram limit for photo captions
STATE_LIMIT = 3000
FOOTER = '\n\n<a href="https://t.me/zafootball_news">Футбол за кадром</a>'

# ---------------------------------------------------------------------------
# Topic filter
# Keywords are regex word *beginnings*: "спартак" matches "Спартака", "спартаковцы".
# Short/ambiguous names get an exact-ending pattern so "реал" won't catch "реально".
# Edit freely. A news item is posted only if it matches at least one ENABLED topic.
# ---------------------------------------------------------------------------
TOPICS: dict[str, dict] = {
    'rpl': {
        'tag': '#РПЛ',
        'keywords': ['рпл', 'премьер-лиг', 'зенит', 'спартак', 'цска', 'локомотив', 'динамо',
                     'краснодар', 'ростов', 'рубин', 'ахмат', 'крылья совет', 'акрон',
                     'оренбург', 'факел', 'пари нн', 'нижний новгород', 'химки', 'сочи',
                     'кубок росси', 'сборная росси', 'карпин', 'семак'],
    },
    'epl': {
        'tag': '#АПЛ',
        'keywords': ['апл', 'английск', 'манчестер', 'ман сити', 'ман юнайтед', 'ливерпул',
                     'арсенал', 'челси', 'тоттенхэм', 'ньюкасл', 'астон вилл', 'вест хэм'],
    },
    'laliga': {
        'tag': '#ЛаЛига',
        'keywords': ['ла лиг', 'лалиг', 'испанск', r'реал(?:а|у|ом|е)?(?!\w)', 'барселон', 'атлетико', 'севиль',
                     'вильярреал', 'бетис', 'мбаппе', 'ямаль'],
    },
    'seriea': {
        'tag': '#СерияА',
        'keywords': ['серия а', 'серии а', 'итальянск', r'интер(?:а|у|ом|е)?(?!\w)', 'милан', 'ювентус', 'наполи',
                     r'ром[аеуы](?!\w)', 'лацио', 'аталант'],
    },
    'bundesliga': {
        'tag': '#Бундеслига',
        'keywords': ['бундеслиг', 'немецк', 'бавари', 'боруссия', 'байер', 'лейпциг', 'вердер',
                     'айнтрахт', 'штутгарт', 'вольфсбург', 'фрайбург', 'унион', 'хоффенхайм'],
    },
    'ligue1': {
        'tag': '#Лига1',
        'keywords': [r'лиг\w* 1(?!\d)', 'французск', 'псж', 'пари сен-жермен', 'марсел', 'монако', r'лион(?:а|у|ом|е)?(?!\w)'],
    },
    'eurocups': {
        'tag': '#Еврокубки',
        'keywords': [r'лиг\w* чемпионов', r'лч(?!\w)', r'лиг\w* европы', r'лиг\w* конференций', 'уефа'],
    },
    'national': {
        'tag': '#Сборные',
        'keywords': ['сборн', r'чемпионат\w* мира', 'чм-', r'чемпионат\w* европы', 'евро-', 'фифа',
                     'отборочн'],
    },
    'transfers': {
        'tag': '#Трансферы',
        'keywords': ['трансфер', 'переход', 'подписал контракт', 'контракт', 'аренд', 'перешел', 'перешёл', 'слух'],
    },
    'stars': {
        'tag': '#Звёзды',
        'keywords': ['месси', 'роналду', 'холанд', 'салах', 'винисиус', 'кейн', 'беллингем',
                     'де брюйне', 'левандовск', 'кварацхелия', 'кубарси',
                     'золотой мяч', 'золотого мяча', 'golden boy', 'ballon d'],
    },
}

# Which topics are on. Override without touching code via env, e.g. TOPICS=rpl,eurocups,transfers
# Empty / unset = all topics on.
ENABLED_TOPICS = [t.strip() for t in os.getenv('TOPICS', '').split(',') if t.strip()] or list(TOPICS)

# Anything matching these is never posted (ads, betting, non-football noise).
STOP_WORDS = ['ставк', 'букмекер', 'коэффициент', 'промокод', 'фрибет', 'бонус',
              'реклама', 'erid', 'мини-футбол', 'пляжн', 'футзал', 'киберспорт', 'ufc', r'мма(?!\w)', 'бокс', 'хокке', 'теннис', r'fifa 2\d', 'ea fc']


def _compile(stems: list[str]) -> re.Pattern:
    # (?<!\w) = must start at a word beginning, so "лч" won't match inside "молча".
    return re.compile(r'(?<!\w)(?:' + '|'.join(stems) + ')', re.IGNORECASE)


TOPIC_PATTERNS = {name: _compile(TOPICS[name]['keywords']) for name in ENABLED_TOPICS if name in TOPICS}
STOP_PATTERN = _compile(STOP_WORDS)


def match_topics(text: str) -> list[str]:
    """Return matching topic names, or [] if the item should be skipped."""
    text = text.replace('ё', 'е').replace('Ё', 'Е')
    if STOP_PATTERN.search(text):
        return []
    return [name for name, pattern in TOPIC_PATTERNS.items() if pattern.search(text)]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_seen() -> list[str]:
    if not STATE.exists():
        return []
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        print('State file broken, starting fresh:', exc)
        return []


def save_seen(seen: list[str]) -> None:
    STATE.write_text(json.dumps(seen[-STATE_LIMIT:], ensure_ascii=False), encoding='utf-8')


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def clean_summary(raw: str) -> str:
    text = html.unescape(re.sub(r'<[^>]+>', '', raw or ''))
    return ' '.join(text.split())


# ---------------------------------------------------------------------------
# Post templates
# Checked top to bottom; the first match wins. No match = 'regular'.
# 'patterns' are regex word beginnings (same rules as topic keywords).
# 'header' goes above the title, 'lead'/'rest' are emoji before the two text blocks.
# 'tags' = whether to show topic hashtags.
# ---------------------------------------------------------------------------
TEMPLATES: list[dict] = [
    {
        'name': 'mourning',
        'patterns': ['скончал', 'умер', r'смерт', r'погиб', 'траур', 'минутой молчания', 'соболезнован'],
        'header': '🕯',
        'title_prefix': '',
        'lead': '',
        'rest': '',
        'tags': False,
    },
    {
        'name': 'breaking',
        'patterns': ['срочно', 'молния', 'сенсаци', r'экстренн'],
        'header': '🚨 <b>СРОЧНО</b>',
        'title_prefix': '',
        'lead': '⚡️',
        'rest': '📝',
        'tags': True,
    },
    {
        'name': 'coach',
        'patterns': [r'(?:отставк|уволен|уволил|покину|покидает|расторг).{0,60}тренер',
                     r'тренер.{0,60}(?:отставк|уволен|покину|покидает|расторг)',
                     r'назначен\w* главн', r'нов\w+ главн\w* тренер', r'возглави'],
        'header': '🚪 <b>ТРЕНЕРСКАЯ ОТСТАВКА / НАЗНАЧЕНИЕ</b>',
        'title_prefix': '',
        'lead': '🧠',
        'rest': '📝',
        'tags': True,
    },
    {
        'name': 'official_transfer',
        'patterns': [r'официальн\w*.{0,60}(?:переш|подписа|аренд|трансфер|контракт)',
                     r'(?:переш|подписа|аренд|трансфер|контракт).{0,80}официальн',
                     r'объявил\w* о (?:переход|подписани|возвращени)'],
        'header': '✍️ <b>ОФИЦИАЛЬНО: ТРАНСФЕР</b>',
        'title_prefix': '',
        'lead': '🔄',
        'rest': '💰',
        'tags': True,
    },
    {
        'name': 'injury',
        'patterns': [r'травм', r'выбыл\w* на', r'пропустит\w* (?:матч|игр|месяц|недел|сезон|остаток)',
                     r'разрыв', r'перелом', r'операци'],
        'header': '🚑 <b>ТРАВМА</b>',
        'title_prefix': '',
        'lead': '🩹',
        'rest': '📝',
        'tags': True,
    },
    {
        'name': 'disqualification',
        'patterns': [r'дисквалифи', r'отстранен', r'красн\w* карточк', r'санкци', r'кдк'],
        'header': '🟥 <b>ДИСКВАЛИФИКАЦИЯ</b>',
        'title_prefix': '',
        'lead': '⚖️',
        'rest': '📝',
        'tags': True,
    },
    {
        'name': 'match_result',
        'patterns': [r'\d{1,2}:\d{1,2}(?!\d)', r'обыгра', r'победил', r'разгроми', r'сыграл\w* вничью',
                     r'уступил', r'одержал\w* победу'],
        'header': '⚽️ <b>РЕЗУЛЬТАТ</b>',
        'title_prefix': '',
        'lead': '📊',
        'rest': '🎙',
        'tags': True,
    },
]

REGULAR_TEMPLATE = {
    'name': 'regular',
    'header': '',
    'title_prefix': '❗️ ',
    'lead': '⚽️',
    'rest': '📝',
    'tags': True,
}

_TEMPLATE_PATTERNS = [(t, _compile(t['patterns'])) for t in TEMPLATES]


def pick_template(title: str, text: str) -> dict:
    """Match the title first (most reliable), then the whole text."""
    for source in (title, text):
        source = source.replace('ё', 'е').replace('Ё', 'Е')
        for template, pattern in _TEMPLATE_PATTERNS:
            if pattern.search(source):
                return template
    return REGULAR_TEMPLATE


def build_caption(title: str, summary: str, link: str, hashtags: str = '',
                  template: dict | None = None) -> str:
    t = template or REGULAR_TEMPLATE
    header = f"{t['header']}\n\n" if t['header'] else ''
    head = f"{header}{t['title_prefix']}<b>{html.escape(title[:200])}</b>"
    tags = f'\n\n{html.escape(hashtags)}' if hashtags and t['tags'] else ''
    tail = tags + FOOTER

    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', summary) if s.strip()]
    lead = sentences[0] if sentences else ''
    rest = ' '.join(sentences[1:])

    def block(emoji: str, text: str) -> str:
        return '\n\n' + (f'{emoji} ' if emoji else '') + html.escape(text)

    def compose(lead_text: str, rest_text: str) -> str:
        body = ''
        if lead_text:
            body += block(t['lead'], lead_text)
        if rest_text:
            body += block(t['rest'], rest_text)
        return head + body + tail

    caption = compose(lead, rest)
    if len(caption) <= CAPTION_LIMIT:
        return caption

    # Trim the second block first, then the lead, until it fits.
    while rest and len(compose(lead, rest + '…')) > CAPTION_LIMIT:
        rest = rest[: max(0, len(rest) - 50)].rsplit(' ', 1)[0]
    if rest:
        return compose(lead, rest + '…')
    while lead and len(compose(lead + '…', '')) > CAPTION_LIMIT:
        lead = lead[: max(0, len(lead) - 50)].rsplit(' ', 1)[0]
    return compose(lead + '…' if lead else '', '')


def find_photo(entry) -> str | None:
    return next(
        (e.get('href') for e in entry.get('enclosures', [])
         if e.get('type', '').startswith('image/') and e.get('href')),
        None,
    )


def entry_text(entry) -> str:
    rss_tags = ' '.join(t.get('term', '') for t in entry.get('tags', []) or [])
    return ' '.join([entry.get('title', ''), clean_summary(entry.get('summary', '')), rss_tags])


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
async def send(bot: Bot, photo: str, caption: str) -> None:
    try:
        await bot.send_photo(CHANNEL, photo, caption=caption, parse_mode='HTML')
    except RetryAfter as exc:
        wait = exc.retry_after if isinstance(exc.retry_after, (int, float)) else exc.retry_after.total_seconds()
        print(f'Flood limit, waiting {wait}s')
        await asyncio.sleep(wait + 1)
        await bot.send_photo(CHANNEL, photo, caption=caption, parse_mode='HTML')


async def main() -> None:
    seen = load_seen()
    seen_set = set(seen)

    def mark(link: str) -> None:
        seen.append(link)
        seen_set.add(link)
        save_seen(seen)

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(FEED)
        response.raise_for_status()
    entries = feedparser.parse(response.content).entries

    # RSS is newest-first; post oldest-first so the channel reads chronologically.
    fresh = [e for e in reversed(entries) if e.get('link') and e.get('link') not in seen_set]
    sent = filtered = 0
    print('Enabled topics:', ', '.join(TOPIC_PATTERNS) or 'none')

    async with Bot(os.environ['BOT_TOKEN']) as bot:
        for entry in fresh:
            if sent >= MAX_PER_RUN:
                break
            link = entry['link']

            topics = match_topics(entry_text(entry))
            if not topics:
                print('Filtered out:', entry.get('title', link))
                filtered += 1
                mark(link)  # don't re-check it every run
                continue

            photo = find_photo(entry)
            if not photo:
                print('No photo, skipping for good:', link)
                mark(link)
                continue

            hashtags = ' '.join(TOPICS[t]['tag'] for t in topics[:3])
            title = entry.get('title', '')
            template = pick_template(title, entry_text(entry))
            print(f"Template: {template['name']} :: {title}")
            caption = build_caption(title, clean_summary(entry.get('summary', '')), link, hashtags, template)
            try:
                await send(bot, photo, caption)
            except BadRequest as exc:
                # Permanent problem (bad image, bad markup): don't retry it forever.
                print(f'BadRequest, skipping for good: {link} :: {exc}')
                mark(link)
                continue
            except Exception as exc:
                # Temporary problem (network etc.): leave unseen, retry next run.
                print(f'Temporary error, will retry: {link} :: {type(exc).__name__}: {exc}')
                continue

            mark(link)
            sent += 1
            await asyncio.sleep(1)

    print(f'Published: {sent}, filtered out: {filtered}')


if __name__ == '__main__':
    asyncio.run(main())
