"""Weekly chat notifications: training stats and an LLM post about where the club is swimming."""

import html
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import Callable

import pandas as pd
from telebot import types

import db
import geo_context
import llm_client
from constants import EMPTY_WEEK_TEXTS, LOCAL_TZ, ROUTE_FINISHED_TEXT
from map_generator import RouteFinishedError, SwimPosition, generate_map_png, get_swim_position
from settings import ADMIN_IDS, LOCATIONS_SHEET_NAME, NOTIFY_CHAT_ID, NOTIFY_THREAD_ID, WORKSHEET_NAME

SEND_WEEKDAY = 6  # воскресенье
SEND_TIME = dt_time(15, 0)
CHECK_INTERVAL_SECONDS = 5 * 60

LLM_ROUND_INTERVAL = timedelta(hours=3)
LLM_MAX_ROUNDS = 3
GENERATION_TRIES = 2
STATS_RETRY_INTERVAL = timedelta(minutes=30)
STATS_MAX_ATTEMPTS = 8
SEND_RETRY_INTERVAL = timedelta(minutes=10)
SEND_MAX_ATTEMPTS = 6

SUMMARIZE_AFTER_POSTS = 8
USED_SOURCES_POSTS = 26  # примерно полгода еженедельных постов
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
POST_MIN_CHARS = 300
EXHAUSTED_MIN_CHARS = 150
POST_MAX_CHARS = 1500
POST_FORBIDDEN_MARKERS = ("{", "}", '"text":', "<think>", "</think>", "```")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
NO_LINK_PREVIEW = types.LinkPreviewOptions(is_disabled=True)

KIND_STATS = "stats"
KIND_JOURNEY = "journey"

_SERVICE_SOURCES = ", ".join(f"«{source}»" for source in geo_context.PROMPT_SERVICE_SOURCES)
_JSON_FORMAT = (
    "- Ответь строго одним JSON-объектом без пояснений и без обрамления в ```:\n"
    '{"text": "текст поста", "facts": [{"fact": "факт 1", "source": "источник факта"}]}\n'
    "где facts — короткие формулировки фактов о местах, которые ты использовал в тексте "
    "(без пройденных километров и прочего прогресса заплыва), а source — откуда взят факт: "
    "заголовок статьи Википедии из справки дословно, без кавычек, или один из разделов справки: "
    f"{_SERVICE_SOURCES}."
)

JOURNEY_SYSTEM_PROMPT = (
    "Ты — бот клуба плавания SwimOcean. Участники клуба плавают в бассейне, а их суммарный "
    "метраж откладывается вдоль реального маршрута на карте: клуб «виртуально плывёт» по морям "
    "и океанам. Раз в неделю ты пишешь в общий чат короткий пост о том, где мы сейчас плывём, "
    "и рассказываешь 1–2 интересных факта об окружающей местности: природа, история, люди, "
    "необычные события.\n"
    "Правила:\n"
    "- Пиши по-русски, живо и дружелюбно, без пафоса и канцелярита. Статьи на английском "
    "пересказывай по-русски.\n"
    "- Объём 600–900 символов. Без Markdown и HTML, можно 1–3 эмодзи.\n"
    "- Бери факты только из справки в запросе. Не добавляй сведений, которых в справке нет, "
    "даже если уверен в них: ни дат, ни чисел, ни имён, ни событий.\n"
    "- Не повторяй факты из списка уже рассказанного, выбери что-то новое.\n"
    f"{_JSON_FORMAT} В facts 2–5 элементов."
)

EXHAUSTED_SYSTEM_PROMPT = (
    "Ты — бот клуба плавания SwimOcean. Участники клуба плавают в бассейне, а их суммарный "
    "метраж откладывается вдоль реального маршрута на карте: клуб «виртуально плывёт» по морям "
    "и океанам. Раз в неделю ты пишешь в общий чат пост о том, где мы сейчас плывём. "
    "На этой неделе новых материалов о местности нет, поэтому пост короткий: где мы "
    "(регион, акватория, ближайшие объекты), сколько осталось до конца отрезка и что впереди.\n"
    "Правила:\n"
    "- Пиши по-русски, живо и дружелюбно, без пафоса и канцелярита.\n"
    "- Объём 300–600 символов. Без Markdown и HTML, можно 1–2 эмодзи.\n"
    "- Не рассказывай исторических и прочих фактов: используй только названия, расстояния, "
    "глубину и описание отрезка из справки.\n"
    f"{_JSON_FORMAT} Если фактов о местах нет, facts — пустой список."
)

SUMMARY_SYSTEM_PROMPT = (
    "Ты ведёшь память бота, который раз в неделю рассказывает факты о местах на маршруте "
    "виртуального заплыва. Тебе дают текущую память и списки фактов из новых постов. "
    "Объедини всё в одну сжатую память: сгруппируй факты по местам, убери дубли, сохрани "
    "конкретику (названия, события), чтобы по памяти было понятно, о чём уже рассказано. "
    "Не больше 1500 символов. Ответь только текстом памяти, без вступлений."
)


FACT_CHECK_SYSTEM_PROMPT = (
    "Ты — строгий фактчекер постов бота клуба плавания SwimOcean. Клуб «виртуально плывёт» по "
    "реальному маршруту, и бот рассказывает факты о местах рядом с текущей точкой пловца.\n"
    "Тебе дают справку о местности (координаты пловца, регион, акваторию, объекты поблизости "
    "с расстояниями, статьи Википедии с расстоянием до пловца), готовый пост и список его фактов "
    "с источниками. Проверь каждое утверждение поста:\n"
    "1. Упомянутое место находится рядом с пловцом: в его регионе, акватории или среди объектов "
    "поблизости. Остерегайся тёзок: одноимённые бухты, мысы и города бывают в разных частях света.\n"
    "2. Утверждение подтверждается справкой. Всё, чего в справке нет, считай недостоверным, "
    "даже если это кажется общеизвестным: особенно даты, числа и имена.\n"
    "3. Если в запросе есть список фактов без подтверждённого источника, обязательно убери их из поста.\n"
    "Если всё верно, верни пост без изменений. Если есть недостоверные утверждения, удали или "
    "перепиши их, сохранив стиль, тон, эмодзи и примерный объём; не добавляй новых фактов, "
    "которых нет в справке.\n"
    "Ответь строго одним JSON-объектом без пояснений и без обрамления в ```:\n"
    '{"verdict": "ok" или "fixed", "issues": ["что было не так"], "text": "итоговый текст поста", '
    '"facts": [{"fact": "факт из итогового текста", "source": "источник факта"}]}'
)


@dataclass
class JourneyDraft:
    text: str
    facts: list[dict]  # [{"fact": ..., "source": ...}]
    position: SwimPosition
    articles: list[geo_context.WikiArticle] = field(default_factory=list)
    exhausted: bool = False
    issues: list[str] = field(default_factory=list)
    check_skipped: str = ""  # почему пост ушёл без проверки фактов

    @property
    def topics(self) -> list[str]:
        return [fact["fact"] for fact in self.facts]

    @property
    def sources(self) -> list[str]:
        return cited_titles(self.facts, self.articles)

    def message_html(self) -> str:
        """Текст для Telegram (parse_mode=HTML) со ссылками на использованные статьи."""
        message = html.escape(self.text, quote=False)
        cited = set(self.sources)
        links = [f'<a href="{html.escape(a.url)}">{html.escape(a.title, quote=False)}</a>'
                 for a in self.articles if a.title in cited]
        if links:
            message += "\n\nПо материалам Википедии: " + ", ".join(links)
        return message


# ---------------------------------------------------------------------------
# Даты и недели
# ---------------------------------------------------------------------------

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def format_time(moment: datetime) -> str:
    return moment.strftime(TIME_FORMAT)


def week_key_for(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(week_key: str) -> tuple[date, date]:
    year, week = week_key.split("-W")
    monday = date.fromisocalendar(int(year), int(week), 1)
    return monday, monday + timedelta(days=6)


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

def _plural_days(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "день"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def _format_meters(meters: float) -> str:
    return f"{int(meters):,}".replace(",", " ")


def build_weekly_stats_text(period_df: pd.DataFrame, monday: date, sunday: date) -> str:
    """period_df: index — даты, колонки — участники, значения — метры за день."""
    columns = [col for col in period_df.columns if str(col).strip()]
    table = pd.DataFrame({
        "days": (period_df[columns] > 0).sum(),
        "meters": period_df[columns].sum(),
    })
    table = table[table["days"] > 0]
    if table.empty:
        return random.choice(EMPTY_WEEK_TEXTS)

    table = table.sort_values(["meters", "days"], ascending=False)
    if len(table) > 3:
        top = table[table["meters"] >= table.iloc[2]["meters"]]
    else:
        top = table

    lines = [f"Итоги недели {monday:%d.%m} – {sunday:%d.%m}", "",
             "Больше всего на этой неделе проплыли:"]
    for name, row in top.iterrows():
        lines.append(f"• {name} — {_format_meters(row['meters'])} м")

    max_days = int(table["days"].max())
    if max_days > 1:
        leaders = table[table["days"] == max_days].index.tolist()
        days = f"{max_days} {_plural_days(max_days)}"
        lines.append("")
        if len(leaders) == 1:
            lines.append(f"Больше всего на этой неделе тренировался(-лась) {leaders[0]} — {days}")
        else:
            lines.append(f"Больше всего на этой неделе тренировались {_join_names(leaders)} — по {days}")
    return "\n".join(lines)


def _join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " и " + names[-1]


# ---------------------------------------------------------------------------
# Разбор и проверка ответа LLM
# ---------------------------------------------------------------------------

def _json_object(raw: str) -> dict | None:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_facts(items) -> list[dict]:
    """Список {"fact", "source"}; строки без источника тоже принимаются (source пустой)."""
    facts = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            fact, source = str(item.get("fact", "")).strip(), str(item.get("source", "")).strip()
        else:
            fact, source = str(item).strip(), ""
        if fact:
            facts.append({"fact": fact, "source": source})
    return facts


def parse_journey_response(raw: str) -> tuple[str, list[dict]] | None:
    """(text, facts) из JSON; None, если ответ не JSON или в нём нет текста."""
    data = _json_object(raw)
    if data is None:
        return None
    text = str(data.get("text", "")).strip()
    if not text:
        return None
    return text, _parse_facts(data.get("facts", data.get("topics")))


def parse_fact_check_response(raw: str) -> dict | None:
    """{"verdict", "issues", "text", "facts"} или None, если ответ не разобрался."""
    data = _json_object(raw)
    if data is None:
        return None
    verdict = str(data.get("verdict", "")).strip().lower()
    issues = data.get("issues", [])
    issues = [str(i).strip() for i in issues if str(i).strip()] if isinstance(issues, list) else []
    text = str(data.get("text", "")).strip()
    if verdict not in ("ok", "fixed") or (verdict == "fixed" and not text):
        return None
    return {"verdict": verdict, "issues": issues, "text": text,
            "facts": _parse_facts(data.get("facts", data.get("topics")))}


def validate_post_text(text: str, min_chars: int = POST_MIN_CHARS) -> str | None:
    """Причина, по которой текст нельзя отправлять в чат, или None."""
    if len(text) < min_chars:
        return f"слишком короткий текст ({len(text)} символов)"
    if len(text) > POST_MAX_CHARS:
        return f"слишком длинный текст ({len(text)} символов)"
    for marker in POST_FORBIDDEN_MARKERS:
        if marker in text:
            return f"в тексте служебная разметка «{marker}»"
    return None


def _normalize_source(source: str) -> str:
    return " ".join(re.sub(r"[«»\"'()\[\]]", " ", source).lower().replace("ё", "е").split())


def _match_title(source: str, articles: list[geo_context.WikiArticle]) -> str | None:
    normalized = _normalize_source(source)
    if not normalized:
        return None
    titles = [(article.title, _normalize_source(article.title)) for article in articles]
    exact = [title for title, norm in titles if norm == normalized]
    if exact:
        return exact[0]
    # «Находка (залив)» должен совпасть с этой статьёй, а не с более короткой «Находка»
    partial = [(len(norm), title) for title, norm in titles if len(norm) >= 4 and norm in normalized]
    return max(partial)[1] if partial else None


def find_unsupported_facts(facts: list[dict], articles: list[geo_context.WikiArticle]) -> list[dict]:
    """Факты, источник которых не статья из справки и не служебный раздел справки."""
    service = {_normalize_source(s) for s in geo_context.PROMPT_SERVICE_SOURCES}
    return [fact for fact in facts
            if _normalize_source(fact["source"]) not in service
            and _match_title(fact["source"], articles) is None]


def cited_titles(facts: list[dict], articles: list[geo_context.WikiArticle]) -> list[str]:
    titles = (_match_title(fact["source"], articles) for fact in facts)
    return list(dict.fromkeys(title for title in titles if title))


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Части не длиннее лимита Telegram, по возможности по границам абзацев."""
    chunks, current = [], ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > limit:
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Уведомления
# ---------------------------------------------------------------------------

class WeeklyNotifier:
    def __init__(
        self,
        bot,
        get_df: Callable[[str], pd.DataFrame],
        get_statistics_for_period: Callable,
        chromium_lock: threading.Lock,
    ):
        self.bot = bot
        self.get_df = get_df
        self.get_statistics_for_period = get_statistics_for_period
        self.chromium_lock = chromium_lock
        self.process_lock = threading.Lock()

    # --- отправка -----------------------------------------------------------

    def _target(self, chat_id=None) -> tuple[int, int | None]:
        if chat_id is not None:
            return chat_id, None
        if NOTIFY_CHAT_ID is None:
            raise RuntimeError("NOTIFY_CHAT_ID не задан")
        return NOTIFY_CHAT_ID, NOTIFY_THREAD_ID

    def send_text(self, text: str, chat_id=None, html_mode: bool = False):
        target, thread_id = self._target(chat_id)
        for chunk in split_message(text):
            self.bot.send_message(
                target, chunk, message_thread_id=thread_id,
                parse_mode="HTML" if html_mode else None, link_preview_options=NO_LINK_PREVIEW,
            )

    def send_photo(self, photo, caption: str, chat_id=None):
        target, thread_id = self._target(chat_id)
        if len(caption) <= CAPTION_LIMIT:
            self.bot.send_photo(target, photo=photo, caption=caption, message_thread_id=thread_id)
        else:
            self.bot.send_photo(target, photo=photo, message_thread_id=thread_id)
            self.send_text(caption, chat_id)

    def notify_admins(self, text: str):
        for admin_id in ADMIN_IDS:
            try:
                self.bot.send_message(admin_id, text)
            except Exception as e:
                logging.error(f"Не смог написать админу {admin_id}: {e}")

    # --- статистика ---------------------------------------------------------

    def make_stats_text(self, week_key: str) -> str:
        monday, sunday = week_bounds(week_key)
        period_df = self.get_statistics_for_period(
            start_date=pd.Timestamp(monday), end_date=pd.Timestamp(sunday)
        )
        return build_weekly_stats_text(period_df, monday, sunday)

    # --- пост о местности ---------------------------------------------------

    def load_route(self) -> tuple[SwimPosition, pd.DataFrame, pd.DataFrame]:
        meters_df = self.get_df(WORKSHEET_NAME)
        locations_df = self.get_df(LOCATIONS_SHEET_NAME)
        position = get_swim_position(meters_df, locations_df)
        return position, meters_df, locations_df

    @staticmethod
    def _memory_block() -> str:
        summary = db.get_latest_journey_memory()
        recent_topics = []
        for _, topics_json, _ in db.get_unsummarized_journey_posts():
            try:
                recent_topics += json.loads(topics_json or "[]")
            except json.JSONDecodeError:
                continue
        parts = []
        if summary:
            parts.append(summary)
        if recent_topics:
            parts.append("\n".join(f"- {topic}" for topic in recent_topics))
        return "\n".join(parts) if parts else "Пока ничего: это первый пост."

    @classmethod
    def build_journey_messages(cls, position: SwimPosition, context: geo_context.GeoContext,
                               articles: list[geo_context.WikiArticle],
                               exhausted: bool = False) -> list[dict]:
        user_prompt = (
            "Где мы сейчас:\n"
            f"{geo_context.describe_for_prompt(position, context, articles)}\n\n"
            "Уже рассказано в прошлых постах (не повторяй):\n"
            f"{cls._memory_block()}"
        )
        return [
            {"role": "system", "content": EXHAUSTED_SYSTEM_PROMPT if exhausted else JOURNEY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

    @staticmethod
    def build_fact_check_messages(draft: JourneyDraft, context: geo_context.GeoContext,
                                  unsupported: list[dict]) -> list[dict]:
        facts = "\n".join(f"- {f['fact']} (источник: {f['source'] or 'не указан'})" for f in draft.facts)
        user_prompt = (
            "Справка о местности:\n"
            f"{geo_context.describe_for_prompt(draft.position, context, draft.articles)}\n\n"
            f"Пост для проверки:\n{draft.text}\n\n"
            f"Факты из поста и их источники:\n{facts or '—'}"
        )
        if unsupported:
            user_prompt += (
                "\n\nФакты без подтверждённого источника, их нужно убрать из поста:\n"
                + "\n".join(f"- {f['fact']}" for f in unsupported)
            )
        return [
            {"role": "system", "content": FACT_CHECK_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

    @classmethod
    def fact_check(cls, draft: JourneyDraft, context: geo_context.GeoContext,
                   min_chars: int = POST_MIN_CHARS) -> JourneyDraft:
        """Если проверка не ответила, пост уходит как есть, но только когда у всех фактов есть
        источник из справки. Иначе, как и при негодном исправленном тексте, бросает LLMError."""
        unsupported = find_unsupported_facts(draft.facts, draft.articles)
        if unsupported:
            logging.info(f"Факты без источника: {[f['fact'] for f in unsupported]}")
        try:
            raw = llm_client.complete(cls.build_fact_check_messages(draft, context, unsupported),
                                      temperature=0.1, attempts=1)
            result = parse_fact_check_response(raw)
            skipped = "" if result else "проверка фактов вернула неразборчивый ответ"
        except llm_client.LLMError as e:
            result, skipped = None, f"проверка фактов недоступна: {e}"

        if result is None:
            if unsupported:
                raise llm_client.LLMError(f"{skipped}, а в посте есть факты без источника")
            logging.warning(f"{skipped}, пост уходит без проверки")
            draft.check_skipped = skipped
            return draft

        draft.issues = result["issues"]
        if result["verdict"] == "ok":
            if unsupported:
                raise llm_client.LLMError("проверка фактов оставила в посте факты без источника")
            logging.info("Проверка фактов: замечаний нет")
            return draft

        logging.info(f"Проверка фактов исправила пост: {result['issues']}")
        problem = validate_post_text(result["text"], min_chars)
        if problem:
            raise llm_client.LLMError(f"проверка фактов вернула негодный текст: {problem}")
        facts = result["facts"] or [f for f in draft.facts if f not in unsupported]
        still_unsupported = find_unsupported_facts(facts, draft.articles)
        return JourneyDraft(
            text=result["text"],
            facts=[f for f in facts if f not in still_unsupported],
            position=draft.position,
            articles=draft.articles,
            exhausted=draft.exhausted,
            issues=result["issues"],
        )

    def generate_journey(self, position: SwimPosition, context: geo_context.GeoContext,
                         attempts: int = llm_client.ATTEMPTS) -> JourneyDraft:
        articles = geo_context.select_articles(context, db.get_used_sources(USED_SOURCES_POSTS))
        exhausted = not articles
        if exhausted:
            logging.info("Неиспользованных статей о местности нет, пишу короткий пост о прогрессе")
        messages = self.build_journey_messages(position, context, articles, exhausted)
        min_chars = EXHAUSTED_MIN_CHARS if exhausted else POST_MIN_CHARS

        parsed, problem = None, ""
        for _ in range(GENERATION_TRIES):
            parsed = parse_journey_response(llm_client.complete(messages, attempts=attempts))
            problem = "ответ не JSON или в нём нет текста" if parsed is None \
                else validate_post_text(parsed[0], min_chars)
            if not problem:
                break
            logging.warning(f"LLM вернула негодный пост: {problem}")
        else:
            raise llm_client.LLMError(f"модель вернула негодный пост: {problem}")

        text, facts = parsed
        draft = JourneyDraft(text=text, facts=facts, position=position,
                             articles=articles, exhausted=exhausted)
        return self.fact_check(draft, context, min_chars)

    @staticmethod
    def save_journey(week_key: str, draft: JourneyDraft):
        db.add_journey_post(
            week_key,
            draft.position.leg_index,
            draft.position.lat,
            draft.position.lon,
            draft.text,
            json.dumps(draft.topics, ensure_ascii=False),
            json.dumps(draft.sources, ensure_ascii=False),
        )

    @staticmethod
    def maybe_summarize():
        """Сжимает topics накопившихся постов в память. Ошибки не мешают отправке постов."""
        posts = db.get_unsummarized_journey_posts()
        if len(posts) < SUMMARIZE_AFTER_POSTS:
            return
        topics = []
        for _, topics_json, text in posts:
            try:
                post_topics = json.loads(topics_json or "[]")
            except json.JSONDecodeError:
                post_topics = []
            # Если topics нет (короткий пост о прогрессе), отдаем на сжатие начало текста
            topics += post_topics or [text[:300]]
        user_prompt = (
            f"Текущая память:\n{db.get_latest_journey_memory() or 'пусто'}\n\n"
            "Факты из новых постов:\n" + "\n".join(f"- {t}" for t in topics)
        )
        try:
            summary = llm_client.complete(
                [
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
            )
        except llm_client.LLMError as e:
            logging.warning(f"Саммаризация памяти не удалась, попробую после следующего поста: {e}")
            return
        db.save_journey_memory(summary)
        db.mark_journey_posts_summarized([post_id for post_id, _, _ in posts])
        logging.info(f"Память постов обновлена, сжато {len(posts)} постов")

    @staticmethod
    def fallback_text(position: SwimPosition, context: geo_context.GeoContext) -> str:
        origin = (position.lat, position.lon)
        where = context.region or context.sea or context.nearest_name(origin)
        lines = ["Где мы плывём на этой неделе"]
        if where:
            lines.append(f"Мы в районе: {where}.")
        nearest = [obj for obj in context.nearby_from(origin) if not obj.is_sea][:1]
        if nearest:
            lines.append(f"Ближе всего к нам {nearest[0].describe(origin)}.")
        leg = "Отрезок маршрута"
        if position.start_name or position.finish_name:
            leg += f": {position.start_name or 'старт'} → {position.finish_name or 'финиш'}"
        lines.append(
            f"{leg}. Проплыто {position.leg_travelled / 1000:.1f} км из "
            f"{position.leg_total / 1000:.1f} км, до конца отрезка {position.leg_remaining / 1000:.1f} км."
        )
        if position.description:
            lines.append(position.description)
        return "\n".join(lines)

    def send_fallback(self, position, context, meters_df, locations_df, chat_id=None):
        text = self.fallback_text(position, context)
        try:
            with self.chromium_lock:
                img, _ = generate_map_png(meters_df, locations_df)
        except Exception as e:
            logging.error(f"Не удалось нарисовать карту для запасного поста: {e}")
            self.send_text(text, chat_id)
            return
        self.send_photo(img, text, chat_id)

    # --- планировщик --------------------------------------------------------

    def ensure_week_posts(self, moment: datetime):
        if moment.weekday() != SEND_WEEKDAY or moment.time() < SEND_TIME:
            return
        week_key = week_key_for(moment.date())
        for kind in (KIND_STATS, KIND_JOURNEY):
            if db.create_scheduled_post(kind, week_key, format_time(moment)):
                logging.info(f"Запланировано уведомление {kind} за {week_key}")

    def _send_saved_text(self, post: dict) -> bool:
        """Переотправка уже готового текста. True, если отправлено."""
        try:
            # Текст поста о местности хранится готовым для parse_mode=HTML
            self.send_text(post["text"], html_mode=post["kind"] == KIND_JOURNEY)
        except Exception as e:
            attempts = post["send_attempts"] + 1
            logging.error(f"Уведомление {post['kind']} {post['week_key']} не отправилось ({attempts}): {e}")
            if attempts >= SEND_MAX_ATTEMPTS:
                db.update_scheduled_post(post["id"], status="failed", send_attempts=attempts)
                self.notify_admins(
                    f"Не удалось отправить еженедельное уведомление {post['kind']} "
                    f"за {post['week_key']}: {e}"
                )
            else:
                db.update_scheduled_post(
                    post["id"], send_attempts=attempts,
                    next_attempt_at=format_time(now_local() + SEND_RETRY_INTERVAL),
                )
            return False
        db.update_scheduled_post(post["id"], status="sent", sent_at=format_time(now_local()))
        logging.info(f"Уведомление {post['kind']} за {post['week_key']} отправлено")
        return True

    def process_stats(self, post: dict):
        if not post["text"]:
            try:
                text = self.make_stats_text(post["week_key"])
            except Exception as e:
                rounds = post["rounds_done"] + 1
                logging.error(f"Не удалось посчитать недельную статистику ({rounds}): {e}")
                if rounds >= STATS_MAX_ATTEMPTS:
                    db.update_scheduled_post(post["id"], status="failed", rounds_done=rounds)
                    self.notify_admins(f"Не удалось посчитать недельную статистику: {e}")
                else:
                    db.update_scheduled_post(
                        post["id"], rounds_done=rounds,
                        next_attempt_at=format_time(now_local() + STATS_RETRY_INTERVAL),
                    )
                return
            db.update_scheduled_post(post["id"], text=text)
            post["text"] = text
        self._send_saved_text(post)

    def process_journey(self, post: dict):
        if post["text"]:
            self._send_saved_text(post)
            return

        round_started = now_local()
        rounds = post["rounds_done"] + 1
        try:
            position, meters_df, locations_df = self.load_route()
        except RouteFinishedError:
            text = html.escape(ROUTE_FINISHED_TEXT, quote=False)
            db.update_scheduled_post(post["id"], text=text)
            post["text"] = text
            self._send_saved_text(post)
            return
        except Exception as e:
            logging.error(f"Не удалось получить позицию на маршруте: {e}")
            self._schedule_next_round(post, rounds, round_started, reason=e)
            return

        context = geo_context.build_geo_context(position)
        if not context.is_usable() and rounds < LLM_MAX_ROUNDS:
            missing = ", ".join(context.failed) or "регион и акватория не найдены"
            logging.warning(f"Справка о местности неполная ({missing}), пост переносится на следующий раунд")
            self._schedule_next_round(post, rounds, round_started, reason=f"справка неполная: {missing}")
            return
        try:
            draft = self.generate_journey(position, context)
        except llm_client.LLMError as e:
            logging.error(f"Раунд {rounds}/{LLM_MAX_ROUNDS} генерации поста не удался: {e}")
            if rounds < LLM_MAX_ROUNDS:
                self._schedule_next_round(post, rounds, round_started, reason=e)
                return
            self._finish_with_fallback(post, rounds, position, context, meters_df, locations_df, e)
            return

        if draft.check_skipped:
            self.notify_admins(f"Пост о местности за {post['week_key']} уходит без проверки фактов: "
                               f"{draft.check_skipped}")
        self.save_journey(post["week_key"], draft)
        message = draft.message_html()
        db.update_scheduled_post(post["id"], text=message, rounds_done=rounds)
        post["text"] = message
        if self._send_saved_text(post):
            self.maybe_summarize()

    def _schedule_next_round(self, post, rounds, round_started, reason):
        if rounds >= LLM_MAX_ROUNDS:
            db.update_scheduled_post(post["id"], status="failed", rounds_done=rounds)
            self.notify_admins(f"Пост о местности за {post['week_key']} не отправлен: {reason}")
            return
        db.update_scheduled_post(
            post["id"], rounds_done=rounds,
            next_attempt_at=format_time(round_started + LLM_ROUND_INTERVAL),
        )

    def _finish_with_fallback(self, post, rounds, position, context, meters_df, locations_df, reason):
        try:
            self.send_fallback(position, context, meters_df, locations_df)
        except Exception as e:
            logging.error(f"Не удалось отправить запасной пост: {e}")
            db.update_scheduled_post(post["id"], status="failed", rounds_done=rounds)
            self.notify_admins(f"Пост о местности за {post['week_key']} не отправлен: {e}")
            return
        db.update_scheduled_post(
            post["id"], status="fallback", rounds_done=rounds, sent_at=format_time(now_local())
        )
        self.notify_admins(
            f"LLM не ответила за {LLM_MAX_ROUNDS} раунда, в чат ушёл запасной пост "
            f"с картой ({post['week_key']}). Последняя ошибка: {reason}"
        )

    def tick(self):
        with self.process_lock:
            moment = now_local()
            self.ensure_week_posts(moment)
            for post in db.get_due_scheduled_posts(format_time(moment)):
                if post["kind"] == KIND_STATS:
                    self.process_stats(post)
                elif post["kind"] == KIND_JOURNEY:
                    self.process_journey(post)

    def run_forever(self):
        if NOTIFY_CHAT_ID is None:
            logging.warning("NOTIFY_CHAT_ID не задан, поток еженедельных уведомлений не запущен")
            return
        time.sleep(30)  # ждем старта сети
        while True:
            try:
                self.tick()
            except Exception as e:
                logging.error(f"Ошибка в weekly_digest_job: {e}")
            time.sleep(CHECK_INTERVAL_SECONDS)

    # --- ручной запуск админом ----------------------------------------------

    def run_manual(self, admin_chat_id: int, publish: bool):
        """publish=False: оба текста уходят админу в личку, в память ничего не пишется.
        publish=True: отправка в тему группы прямо сейчас, пост попадает в память."""
        if not self.process_lock.acquire(blocking=False):
            self.bot.send_message(admin_chat_id, "Сейчас идёт плановая рассылка, попробуйте через несколько минут.")
            return
        try:
            self._run_manual(admin_chat_id, publish)
        finally:
            self.process_lock.release()

    def _run_manual(self, admin_chat_id: int, publish: bool):
        chat_id = None if publish else admin_chat_id
        week_key = week_key_for(now_local().date())

        try:
            self.send_text(self.make_stats_text(week_key), chat_id)
        except Exception as e:
            logging.error(f"Ручная статистика не отправлена: {e}")
            self.bot.send_message(admin_chat_id, f"Статистика не получилась: {e}")

        try:
            position, meters_df, locations_df = self.load_route()
        except RouteFinishedError:
            self.send_text(ROUTE_FINISHED_TEXT, chat_id)
            return
        except Exception as e:
            self.bot.send_message(admin_chat_id, f"Не удалось получить позицию на маршруте: {e}")
            return

        context = geo_context.build_geo_context(position)
        if not publish:
            articles = geo_context.select_articles(context, db.get_used_sources(USED_SOURCES_POSTS))
            failed = f"\n\nНе ответили: {', '.join(context.failed)}" if context.failed else ""
            self.send_text("Контекст для LLM:\n\n"
                           + geo_context.describe_for_prompt(position, context, articles) + failed,
                           admin_chat_id)
        try:
            draft = self.generate_journey(position, context, attempts=1)
        except llm_client.LLMError as e:
            self.bot.send_message(admin_chat_id, f"LLM не дала годного поста, отправляю запасной: {e}")
            self.send_fallback(position, context, meters_df, locations_df, chat_id)
            return

        self.send_text(draft.message_html(), chat_id, html_mode=True)
        if publish:
            self.save_journey(week_key, draft)
            self.maybe_summarize()
            if draft.check_skipped:
                self.bot.send_message(admin_chat_id, f"Пост ушёл без проверки фактов: {draft.check_skipped}")
        else:
            mode = "короткий пост о прогрессе (новых статей нет)\n" if draft.exhausted else ""
            facts = "\n".join(f"- {f['fact']} [{f['source'] or 'без источника'}]" for f in draft.facts) or "—"
            self.bot.send_message(admin_chat_id, f"{mode}Факты и источники:\n{facts}")
            issues = "\n".join(f"- {issue}" for issue in draft.issues) or "замечаний нет"
            if draft.check_skipped:
                issues = draft.check_skipped
            self.bot.send_message(admin_chat_id, f"Проверка фактов:\n{issues}")
