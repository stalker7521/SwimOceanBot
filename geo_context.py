"""Turn the swimmer's coordinates into a text description for the LLM prompt.

Sources: OpenStreetMap Overpass (nearby places and natural features), Nominatim
(region), Marine Regions (IHO sea area), GEBCO via Open Topo Data (depth) and Wikipedia.
Articles come, in order of trust, from the wikipedia/wikidata tags of OSM objects, from
Wikipedia GeoSearch, from a search by name filtered by the article's distance to the
swimmer, and from the sea area and region.
Every source is optional: failures are logged and the context is built from what is left.
The cache stores raw coordinates, so distances are always computed from the current point.
"""

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import quote

import geopy.distance
import requests
from geopy.geocoders import Nominatim

import db
from map_generator import SwimPosition, calculate_initial_compass_bearing

USER_AGENT = "SwimOceanBot/1.0 (+https://github.com/stalker7521/SwimOceanBot)"
HTTP_TIMEOUT = (15, 90)

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
OVERPASS_RADII_M = (50_000, 150_000)
OVERPASS_VILLAGE_RADIUS_M = 20_000
OVERPASS_SEA_RADIUS_M = 500_000
NEARBY_STORE_LIMIT = 15
NEARBY_LIMIT = 8

NOMINATIM_ZOOMS = (10, 8, 5, 3)

MARINE_REGIONS_API = "https://www.marineregions.org/rest/getGazetteerRecordsByLatLong.json/{lat}/{lon}/"
DEPTH_API = "https://api.opentopodata.org/v1/gebco2020"

WIKI_API = "https://{lang}.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKI_GEOSEARCH_RADIUS_M = 10_000  # максимум, который разрешает API
WIKI_GEOSEARCH_LIMIT = 20
WIKI_BATCH = 20  # столько вступлений API отдаёт за один запрос
WIKI_CANDIDATES_LIMIT = 10
PROMPT_ARTICLES_LIMIT = 3
WIKI_EXTRACT_CHARS = 2500
WIKI_MIN_INTRO_CHARS = 200
WIKI_MAX_DISTANCE_KM = 200
WIKI_REGION_MAX_DISTANCE_KM = 1500
WIKI_BUSY_RETRIES = 2
WIKI_BUSY_PAUSE_SECONDS = 3

SOURCE_OSM = "osm"
SOURCE_GEOSEARCH = "geosearch"
SOURCE_SEARCH = "search"
SOURCE_SEA = "sea"
SOURCE_REGION = "region"

# Служебные источники в справке: на них модель ссылается в facts[*].source
PROMPT_SOURCE_LEG = "описание отрезка"
PROMPT_SOURCE_REGION = "регион"
PROMPT_SOURCE_SEA = "акватория"
PROMPT_SOURCE_NEARBY = "объекты поблизости"
PROMPT_SERVICE_SOURCES = (PROMPT_SOURCE_LEG, PROMPT_SOURCE_REGION, PROMPT_SOURCE_SEA, PROMPT_SOURCE_NEARBY)

FAIL_OVERPASS = "overpass"
FAIL_NOMINATIM = "nominatim"
FAIL_SEA = "marineregions"
FAIL_WIKI = "wikipedia"

CACHE_VERSION = "v3"
CACHE_GRID_DEG = 0.05

SETTLEMENT_KINDS = {
    "city": "город",
    "town": "город",
    "village": "село",
}
ISLAND_KINDS = {
    "island": "остров",
    "islet": "островок",
    "archipelago": "архипелаг",
}
NATURAL_KINDS = {
    "cape": "мыс",
    "bay": "залив",
    "peninsula": "полуостров",
    "strait": "пролив",
    "volcano": "вулкан",
    "reef": "риф",
}
SEA_KINDS = {
    "sea": "море",
    "ocean": "океан",
}
PLACE_KINDS = {**SETTLEMENT_KINDS, **ISLAND_KINDS, **SEA_KINDS}
COMPASS = ("С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ")


GEO_WORDS = (set(PLACE_KINDS.values()) | set(NATURAL_KINDS.values())
             | {"бухта", "губа", "лагуна", "гавань"})
GEO_WORDS_EN = {"cape", "bay", "island", "islands", "isle", "islet", "strait", "gulf", "sea", "ocean",
                "peninsula", "volcano", "reef", "mount", "point", "harbor", "harbour", "lagoon",
                "of", "the"}
CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
TAIL_SECTION = re.compile(
    r"^=+\s*(Примечания|Ссылки|Литература|Источники|См\. также|Галерея|References|Notes|"
    r"External links|Further reading|See also|Bibliography|Sources|Gallery)\s*=+\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _is_cyrillic(text: str) -> bool:
    return bool(CYRILLIC.search(text))


def _km(km: float) -> str:
    return f"{km:.1f}".replace(".", ",")


def _direction(origin: tuple, target: tuple) -> str:
    bearing = calculate_initial_compass_bearing(tuple(origin), tuple(target))
    return COMPASS[int((bearing + 22.5) // 45) % 8]


@dataclass
class NearbyObject:
    name: str
    kind: str
    lat: float
    lon: float
    bbox: list[float] | None = None  # [minlat, minlon, maxlat, maxlon] у линий и отношений
    name_en: str = ""
    wikipedia: str = ""  # тег OSM вида "ru:Название"
    wikidata: str = ""   # тег OSM вида "Q12345"

    @property
    def label(self) -> str:
        """«остров Русский», но «мыс Бугристый», а не «мыс мыс Бугристый»."""
        if (GEO_WORDS | GEO_WORDS_EN) & set(self.name.lower().split()):
            return self.name
        return f"{self.kind} {self.name}"

    @property
    def is_sea(self) -> bool:
        return self.kind in SEA_KINDS.values()

    def nearest_point(self, origin: tuple) -> tuple[float, float]:
        """Ближайшая к пловцу точка рамки объекта; у точечных объектов — сама точка."""
        if not self.bbox:
            return self.lat, self.lon
        min_lat, min_lon, max_lat, max_lon = self.bbox
        return min(max(origin[0], min_lat), max_lat), min(max(origin[1], min_lon), max_lon)

    def surrounds(self, origin: tuple) -> bool:
        """Пловец внутри рамки протяжённого объекта (залив, остров, архипелаг)."""
        return bool(self.bbox) and self.nearest_point(origin) == (origin[0], origin[1])

    def distance_km(self, origin: tuple) -> float:
        return geopy.distance.distance(origin, self.nearest_point(origin)).km

    def describe(self, origin: tuple) -> str:
        if self.is_sea:
            return f"{self.label} (акватория)"
        if self.surrounds(origin):
            return f"{self.label} (протяжённый объект, мы рядом)"
        point = self.nearest_point(origin)
        return f"{self.label} ({_km(self.distance_km(origin))} км, {_direction(origin, point)})"


@dataclass
class WikiArticle:
    title: str
    text: str
    source: str  # SOURCE_OSM | SOURCE_GEOSEARCH | SOURCE_SEARCH | SOURCE_SEA | SOURCE_REGION
    lang: str = "ru"
    coords: list[float] | None = None
    osm_label: str = ""  # для SOURCE_OSM: объект OSM, о котором статья

    @property
    def url(self) -> str:
        return f"https://{self.lang}.wikipedia.org/wiki/{quote(self.title.replace(' ', '_'))}"


@dataclass
class SeaArea:
    name: str
    wiki_title: str = ""
    wiki_lang: str = ""


@dataclass
class GeoContext:
    region: str = ""
    sea: str = ""
    ocean: str = ""
    depth_m: int | None = None
    nearby: list[NearbyObject] = field(default_factory=list)
    wiki: list[WikiArticle] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # источники, которые не ответили

    def is_empty(self) -> bool:
        return not (self.region or self.sea or self.nearby or self.wiki)

    def is_usable(self) -> bool:
        """Достаточно для поста: Overpass ответил и известно, где мы."""
        return FAIL_OVERPASS not in self.failed and bool(self.region or self.sea)

    def is_complete(self) -> bool:
        """Можно кешировать: все источники ответили."""
        return not self.failed and bool(self.region or self.sea)

    def nearby_from(self, origin: tuple, limit: int = NEARBY_LIMIT) -> list[NearbyObject]:
        """Ближайшие объекты от текущей точки; акватории в конце."""
        return sorted(self.nearby, key=lambda o: (o.is_sea, o.distance_km(origin)))[:limit]

    def nearest_name(self, origin: tuple) -> str:
        nearby = self.nearby_from(origin, limit=1)
        return nearby[0].label if nearby else ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> "GeoContext":
        data = json.loads(payload)
        return cls(
            region=data.get("region", ""),
            sea=data.get("sea", ""),
            ocean=data.get("ocean", ""),
            depth_m=data.get("depth_m"),
            nearby=[NearbyObject(**item) for item in data.get("nearby", [])],
            wiki=[WikiArticle(**item) for item in data.get("wiki", [])],
            failed=data.get("failed", []),
        )


def _session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


# ---------------------------------------------------------------------------
# Overpass
# ---------------------------------------------------------------------------

def _overpass_query(lat: float, lon: float, radius_m: int) -> str:
    naturals = "|".join(NATURAL_KINDS)
    islands = "|".join(ISLAND_KINDS)
    around = f"around:{radius_m},{lat},{lon}"
    village_radius = min(radius_m, OVERPASS_VILLAGE_RADIUS_M)
    return f"""
[out:json][timeout:60];
(
  nwr({around})["natural"~"^({naturals})$"]["name"];
  nwr({around})["place"~"^({islands})$"]["name"];
  nwr({around})["place"~"^(city|town)$"]["name"];
  nwr(around:{village_radius},{lat},{lon})["place"="village"]["name"];
);
out tags bb;
"""


def _overpass_sea_query(lat: float, lon: float) -> str:
    seas = "|".join(SEA_KINDS)
    return f"""
[out:json][timeout:60];
node(around:{OVERPASS_SEA_RADIUS_M},{lat},{lon})["place"~"^({seas})$"]["name"];
out tags bb;
"""


def _overpass_post(session: requests.Session, query: str, what: str) -> list[dict] | None:
    for url in OVERPASS_URLS:
        try:
            response = session.post(url, data={"data": query}, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            return response.json().get("elements", [])
        except Exception as e:
            logging.warning(f"Overpass {url} ({what}) не ответил: {e}")
    return None


def _element_geometry(element: dict) -> tuple[tuple[float, float], list[float] | None] | None:
    """Точка объекта и рамка (у линий и отношений)."""
    if "lat" in element and "lon" in element:
        return (element["lat"], element["lon"]), None
    bounds = element.get("bounds")
    if bounds:
        bbox = [bounds["minlat"], bounds["minlon"], bounds["maxlat"], bounds["maxlon"]]
        return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2), bbox
    center = element.get("center")
    if center:
        return (center["lat"], center["lon"]), None
    return None


def _names(tags: dict) -> tuple[str, str]:
    """(название для справки, английское название для поиска)."""
    name = tags.get("name", "")
    name_ru = tags.get("name:ru") or (name if _is_cyrillic(name) else "")
    name_en = tags.get("name:en") or (name if name and not _is_cyrillic(name) else "")
    return name_ru or tags.get("name:en") or name, name_en


def _parse_objects(elements: list[dict], origin: tuple) -> list[NearbyObject]:
    objects: dict[tuple, tuple[float, NearbyObject]] = {}
    for element in elements:
        tags = element.get("tags", {})
        geometry = _element_geometry(element)
        name, name_en = _names(tags)
        kind = PLACE_KINDS.get(tags.get("place")) or NATURAL_KINDS.get(tags.get("natural"))
        if not (geometry and name and kind):
            continue
        (lat, lon), bbox = geometry
        obj = NearbyObject(
            name=name,
            kind=kind,
            lat=lat,
            lon=lon,
            bbox=bbox,
            name_en=name_en,
            wikipedia=tags.get("wikipedia", ""),
            wikidata=tags.get("wikidata", ""),
        )
        distance_km = obj.distance_km(origin)
        key = (kind, name)
        if key not in objects or objects[key][0] > distance_km:
            objects[key] = (distance_km, obj)
    return [obj for _, obj in sorted(objects.values(), key=lambda item: item[0])]


def fetch_nearby_objects(session: requests.Session, lat: float, lon: float) -> list[NearbyObject] | None:
    """None означает, что Overpass недоступен; пустой список — что рядом ничего нет."""
    origin = (lat, lon)
    objects: list[NearbyObject] = []
    for radius in OVERPASS_RADII_M:
        elements = _overpass_post(session, _overpass_query(lat, lon, radius), f"радиус {radius} м")
        if elements is None:
            return None
        objects = _parse_objects(elements, origin)
        if objects:
            break
    objects = objects[:NEARBY_STORE_LIMIT]

    # Вдали от берега добавляем подписи морей и океанов
    if not objects or objects[0].distance_km(origin) > OVERPASS_RADII_M[0] / 1000:
        elements = _overpass_post(session, _overpass_sea_query(lat, lon), "моря и океаны")
        if elements:
            objects += _parse_objects(elements, origin)[:3]
    return objects


# ---------------------------------------------------------------------------
# Регион, акватория, глубина
# ---------------------------------------------------------------------------

def fetch_region(lat: float, lon: float) -> str | None:
    """None — Nominatim недоступен; пустая строка — адреса нет (открытое море)."""
    geolocator = Nominatim(user_agent=USER_AGENT, timeout=HTTP_TIMEOUT[1])
    for zoom in NOMINATIM_ZOOMS:
        try:
            location = geolocator.reverse((lat, lon), zoom=zoom, language="ru")
        except Exception as e:
            logging.warning(f"Nominatim (zoom {zoom}) не ответил: {e}")
            return None
        finally:
            time.sleep(1)  # правила Nominatim: не больше 1 запроса в секунду
        if location and location.address:
            return location.address
    return ""


def _wikidata_entities(session: requests.Session, ids: list[str], labels: bool = False) -> dict:
    if not ids:
        return {}
    params = {
        "action": "wbgetentities",
        "ids": "|".join(dict.fromkeys(ids)),
        "props": "sitelinks|labels" if labels else "sitelinks",
        "sitefilter": "ruwiki|enwiki",
        "format": "json",
    }
    if labels:
        params["languages"] = "ru"
    response = session.get(WIKIDATA_API, params=params, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json().get("entities", {})


def _sitelink(entity: dict) -> tuple[str, str] | None:
    """(заголовок, язык): русская статья, если есть, иначе английская."""
    sitelinks = entity.get("sitelinks", {})
    for lang in ("ru", "en"):
        title = sitelinks.get(f"{lang}wiki", {}).get("title")
        if title:
            return title, lang
    return None


def _wikidata_by_mrgid(session: requests.Session, mrgid: int) -> str | None:
    """Идентификатор Wikidata по свойству P3006 (Marine Regions Geographic ID)."""
    response = session.get(
        WIKIDATA_API,
        params={
            "action": "query",
            "list": "search",
            "srsearch": f"haswbstatement:P3006={mrgid}",
            "srlimit": 1,
            "format": "json",
        },
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    hits = response.json().get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


def _pick_sea_areas(records: list[dict]) -> tuple[tuple[int, str] | None, tuple[int, str] | None]:
    """(море, океан) из ответа Marine Regions: (MRGID, английское название)."""
    seas, iho_oceans, oceans = {}, {}, {}
    for record in records:
        name = record.get("preferredGazetteerName") or ""
        mrgid = record.get("MRGID")
        place_type = record.get("placeType")
        if not (name and mrgid):
            continue
        if place_type == "IHO Sea Area":
            target = iho_oceans if "ocean" in name.lower() else seas
        elif place_type == "Ocean":
            target = oceans
        else:
            continue
        target.setdefault(mrgid, name)
    seas, iho_oceans, oceans = (list(d.items()) for d in (seas, iho_oceans, oceans))
    sea = seas[0] if seas else (iho_oceans[0] if iho_oceans else None)
    ocean = oceans[0] if oceans else (iho_oceans[0] if seas and iho_oceans else None)
    return sea, ocean


def fetch_sea_areas(session: requests.Session, lat: float, lon: float,
                    errors: list[str] | None = None) -> tuple[SeaArea | None, SeaArea | None] | None:
    """(море, океан) по классификации IHO. None — Marine Regions недоступен.
    Если не ответила Wikidata, названия остаются английскими, а в `errors` пишется сбой."""
    try:
        response = session.get(MARINE_REGIONS_API.format(lat=lat, lon=lon), timeout=HTTP_TIMEOUT)
        if response.status_code in (204, 404):
            return None, None
        response.raise_for_status()
        records = response.json() if response.text.strip() else []
    except Exception as e:
        logging.warning(f"Marine Regions не ответил: {e}")
        return None
    picked = list(_pick_sea_areas(records))

    entities = {}
    qids = {}
    try:
        for area in picked:
            if area:
                qid = _wikidata_by_mrgid(session, area[0])
                if qid:
                    qids[area[0]] = qid
        entities = _wikidata_entities(session, list(qids.values()), labels=True)
    except Exception as e:
        logging.warning(f"Не удалось получить названия акваторий из Wikidata: {e}")
        if errors is not None:
            errors.append("wikidata")

    result = []
    for area in picked:
        if not area:
            result.append(None)
            continue
        mrgid, name = area
        entity = entities.get(qids.get(mrgid, ""), {})
        label = entity.get("labels", {}).get("ru", {}).get("value")
        link = _sitelink(entity)
        result.append(SeaArea(name=label or name, wiki_title=link[0] if link else "",
                              wiki_lang=link[1] if link else ""))
    return result[0], result[1]


def fetch_depth(session: requests.Session, lat: float, lon: float) -> int | None:
    """Глубина в метрах по GEBCO; None на суше или если сервис не ответил."""
    try:
        response = session.get(DEPTH_API, params={"locations": f"{lat},{lon}"}, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        elevation = response.json()["results"][0]["elevation"]
    except Exception as e:
        logging.warning(f"Open Topo Data (глубина) не ответил: {e}")
        return None
    if elevation is None or elevation >= 0:
        return None
    return round(-elevation)


# ---------------------------------------------------------------------------
# Википедия
# ---------------------------------------------------------------------------

def _wiki_get(session: requests.Session, lang: str = "ru", **params) -> dict:
    params.update({"format": "json", "formatversion": 2})
    for attempt in range(WIKI_BUSY_RETRIES + 1):
        response = session.get(WIKI_API.format(lang=lang), params=params, timeout=HTTP_TIMEOUT)
        busy = response.status_code in (429, 503)
        if not busy:
            response.raise_for_status()
            data = response.json()
            error = data.get("error")
            if not error:
                return data
            busy = "too-busy" in str(error.get("code", ""))
            if not busy:
                raise RuntimeError(error.get("info") or error)
        if attempt < WIKI_BUSY_RETRIES:
            time.sleep(WIKI_BUSY_PAUSE_SECONDS * (attempt + 1))
    raise RuntimeError(f"Википедия ({lang}) перегружена")


@dataclass
class WikiPage:
    title: str
    text: str
    coords: tuple[float, float] | None
    length: int = 0


def _parse_pages(query: dict) -> dict[str, WikiPage]:
    pages = {}
    for page in query.get("pages", []):
        text = (page.get("extract") or "").strip()
        if not text:
            continue
        coords = page.get("coordinates") or []
        pages[page["title"]] = WikiPage(
            title=page["title"],
            text=text,
            coords=(coords[0]["lat"], coords[0]["lon"]) if coords else None,
            length=page.get("length", 0),
        )
    return pages


def _wiki_pages(session: requests.Session, titles: list[str], lang: str = "ru") -> dict[str, WikiPage]:
    """Вступление, координаты и длина статей. Ключ — запрошенный заголовок (до редиректов)."""
    result = {}
    for start in range(0, len(titles), WIKI_BATCH):
        chunk = titles[start:start + WIKI_BATCH]
        data = _wiki_get(
            session,
            lang,
            action="query",
            prop="extracts|coordinates|info",
            exintro=1,
            explaintext=1,
            exlimit="max",
            colimit="max",
            redirects=1,
            titles="|".join(chunk),
        )
        query = data.get("query", {})
        renamed = {item["from"]: item["to"] for item in query.get("normalized", []) + query.get("redirects", [])}
        pages = _parse_pages(query)
        for title in chunk:
            final = title
            seen = set()
            while final in renamed and final not in seen:
                seen.add(final)
                final = renamed[final]
            if final in pages:
                result[title] = pages[final]
    return result


def _wiki_full_text(session: requests.Session, title: str, lang: str = "ru") -> str:
    """Полный текст статьи; API отдаёт его только по одной статье за запрос."""
    data = _wiki_get(session, lang, action="query", prop="extracts", explaintext=1,
                     redirects=1, titles=title)
    pages = data.get("query", {}).get("pages", [])
    return (pages[0].get("extract") or "").strip() if pages else ""


def _strip_tail_sections(text: str) -> str:
    """Убирает служебные разделы в конце статьи (примечания, ссылки, литература)."""
    match = TAIL_SECTION.search(text)
    text = text[:match.start()] if match else text
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _truncate(text: str, limit: int) -> str:
    """Обрезка по границе абзаца или предложения."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for separator in ("\n", ". "):
        position = cut.rfind(separator)
        if position > limit // 2:
            return cut[:position + 1].strip()
    return cut.strip()


def _wiki_geosearch(session: requests.Session, lat: float, lon: float) -> list[WikiPage]:
    """Статьи в радиусе 10 км; сначала длинные (обычно это самые содержательные)."""
    data = _wiki_get(
        session,
        generator="geosearch",
        ggscoord=f"{lat}|{lon}",
        ggsradius=WIKI_GEOSEARCH_RADIUS_M,
        ggslimit=WIKI_GEOSEARCH_LIMIT,
        action="query",
        prop="extracts|coordinates|info",
        exintro=1,
        explaintext=1,
        exlimit="max",
        colimit="max",
    )
    pages = [page for page in _parse_pages(data.get("query", {})).values()
             if len(page.text) >= WIKI_MIN_INTRO_CHARS]
    return sorted(pages, key=lambda page: page.length, reverse=True)


def _titles_from_osm(session: requests.Session, nearby: list[NearbyObject],
                     origin: tuple) -> list[tuple[str, str, NearbyObject]]:
    """(заголовок, язык, объект) по тегам wikipedia/wikidata объектов OSM, от ближних к дальним."""
    found = []
    via_wikidata = []
    for obj in nearby:
        lang, _, title = obj.wikipedia.partition(":")
        title = title.strip()
        if lang == "ru" and title:
            found.append((title, "ru", obj))
        elif obj.wikidata:
            via_wikidata.append(obj)
        elif lang == "en" and title:
            found.append((title, "en", obj))

    entities = _wikidata_entities(session, [obj.wikidata for obj in via_wikidata])
    for obj in via_wikidata:
        link = _sitelink(entities.get(obj.wikidata, {}))
        if link:
            found.append((link[0], link[1], obj))
        else:
            lang, _, title = obj.wikipedia.partition(":")
            if lang == "en" and title.strip():
                found.append((title.strip(), "en", obj))

    found.sort(key=lambda item: item[2].distance_km(origin))
    unique = {}
    for title, lang, obj in found:
        unique.setdefault((title, lang), obj)
    return [(title, lang, obj) for (title, lang), obj in unique.items()]


def _title_matches(query: str, title: str) -> bool:
    """Полнотекстовый поиск часто ставит первой постороннюю статью («мыс Огородный» → «Украина»),
    поэтому заголовок должен содержать основу самого длинного слова названия."""
    words = [w for w in query.split() if w.lower() not in GEO_WORDS | GEO_WORDS_EN]
    if not words:
        return True
    stem = max(words, key=len).lower()[:5]
    return stem in title.lower()


def _wiki_search_title(session: requests.Session, query: str, lang: str = "ru") -> str | None:
    data = _wiki_get(session, lang, action="query", list="search", srsearch=query, srlimit=5)
    for result in data.get("query", {}).get("search", []):
        if _title_matches(query, result["title"]):
            return result["title"]
    return None


def _search_queries(nearby: list[NearbyObject], region: str,
                    position: SwimPosition) -> list[tuple[str, str, bool]]:
    """Запросы для поиска по названию: (запрос, язык Википедии, это запрос по региону)."""
    queries = []
    for obj in [o for o in nearby if not o.is_sea][:4]:
        if _is_cyrillic(obj.name):
            queries.append((obj.label, "ru"))
        elif obj.name_en:
            queries.append((obj.name_en, "en"))
    queries += [(name, "ru") for name in (position.finish_name, position.start_name) if name]
    queries = list(dict.fromkeys(queries))
    result = [(query, lang, False) for query, lang in queries]
    # Первая часть адреса Nominatim — самая конкретная (район, регион или море);
    # округ и страна дают слишком общие статьи
    first_part = region.split(",")[0].strip()
    if first_part and (first_part, "ru") not in queries:
        result.append((first_part, "ru", True))
    return result


def _page_distance_km(origin: tuple[float, float], page: WikiPage) -> float | None:
    if not page.coords:
        return None
    return round(geopy.distance.distance(origin, page.coords).km, 1)


def fetch_wiki(session: requests.Session, position: SwimPosition, nearby: list[NearbyObject],
               region: str, sea_areas: list[SeaArea] | None = None,
               rejected: list[tuple[str, str]] | None = None,
               errors: list[str] | None = None) -> list[WikiArticle]:
    """До WIKI_CANDIDATES_LIMIT статей. В `rejected` складываются (статья, причина) отброшенных,
    в `errors` — описания сбоев (если они были, контекст не кешируется)."""
    origin = (position.lat, position.lon)
    articles: list[WikiArticle] = []

    def full() -> bool:
        return len(articles) >= WIKI_CANDIDATES_LIMIT

    def add(page: WikiPage, source: str, lang: str, osm_label: str = "") -> None:
        if full() or any(a.title == page.title and a.lang == lang for a in articles):
            return
        articles.append(WikiArticle(page.title, page.text, source, lang,
                                    list(page.coords) if page.coords else None, osm_label))

    def reject(title: str, reason: str) -> None:
        logging.info(f"Статья «{title}» отброшена: {reason}")
        if rejected is not None:
            rejected.append((title, reason))

    def failed(what: str, e: Exception) -> None:
        logging.warning(f"{what}: {e}")
        if errors is not None:
            errors.append(what)

    # 1. Точные ссылки из тегов OSM
    try:
        osm_titles = _titles_from_osm(session, nearby, origin)
        pages_by_lang = {
            lang: _wiki_pages(session, [t for t, l, _ in osm_titles if l == lang], lang)
            for lang in dict.fromkeys(l for _, l, _ in osm_titles)
        }
        for title, lang, obj in osm_titles:
            page = pages_by_lang[lang].get(title)
            if page:
                add(page, SOURCE_OSM, lang, obj.label)
    except Exception as e:
        failed("Не удалось получить статьи по ссылкам OSM", e)

    # 2. GeoSearch: статьи с координатами в радиусе 10 км
    if not full():
        try:
            for page in _wiki_geosearch(session, position.lat, position.lon):
                add(page, SOURCE_GEOSEARCH, "ru")
        except Exception as e:
            failed("Wikipedia GeoSearch не ответил", e)

    # 3. Поиск по названию с проверкой расстояния
    queries = _search_queries(nearby, region, position)

    def search(query: str, lang: str, is_region: bool) -> None:
        try:
            title = _wiki_search_title(session, query, lang)
            page = _wiki_pages(session, [title], lang).get(title) if title else None
        except Exception as e:
            failed(f"Wikipedia search '{query}' не ответил", e)
            return
        if not page:
            return
        distance_km = _page_distance_km(origin, page)
        if is_region:
            # У крупных объектов координаты — это центр, поэтому порог большой
            if distance_km is not None and distance_km > WIKI_REGION_MAX_DISTANCE_KM:
                reject(page.title, f"{distance_km:.0f} км от пловца (запрос по региону «{query}»)")
            else:
                add(page, SOURCE_REGION, lang)
        elif distance_km is None:
            reject(page.title, f"нет координат (запрос «{query}»)")
        elif distance_km > WIKI_MAX_DISTANCE_KM:
            reject(page.title, f"{distance_km:.0f} км от пловца (запрос «{query}»)")
        else:
            add(page, SOURCE_SEARCH, lang)

    for query, lang, is_region in queries:
        if not is_region and not full():
            search(query, lang, is_region)

    # 4. Статьи об акватории (море, затем океан)
    for area in sea_areas or []:
        if full() or not area.wiki_title:
            continue
        try:
            page = _wiki_pages(session, [area.wiki_title], area.wiki_lang).get(area.wiki_title)
        except Exception as e:
            failed(f"Не удалось получить статью об акватории «{area.wiki_title}»", e)
            continue
        if page:
            add(page, SOURCE_SEA, area.wiki_lang)

    # 5. Статья о регионе
    for query, lang, is_region in queries:
        if is_region and not full():
            search(query, lang, is_region)

    # Полный текст вместо вступления: в разделах вроде «История» больше интересного
    for article in articles:
        try:
            text = _wiki_full_text(session, article.title, article.lang)
        except Exception as e:
            failed(f"Не удалось получить полный текст «{article.title}»", e)
            text = ""
        article.text = _truncate(_strip_tail_sections(text or article.text), WIKI_EXTRACT_CHARS)
    return articles


# ---------------------------------------------------------------------------
# Сборка и текст для промпта
# ---------------------------------------------------------------------------

def _cache_key(position: SwimPosition) -> str:
    lat = round(position.lat / CACHE_GRID_DEG) * CACHE_GRID_DEG
    lon = round(position.lon / CACHE_GRID_DEG) * CACHE_GRID_DEG
    return f"{CACHE_VERSION}:{position.leg_index}:{lat:.2f}:{lon:.2f}"


def build_geo_context(position: SwimPosition) -> GeoContext:
    """Never raises: on network errors returns whatever could be collected."""
    key = _cache_key(position)
    cached = db.get_geo_cache(key)
    if cached:
        try:
            return GeoContext.from_json(cached)
        except Exception as e:
            logging.warning(f"Битый кеш геоконтекста {key}: {e}")

    session = _session()
    failed = []
    try:
        nearby = fetch_nearby_objects(session, position.lat, position.lon)
    except Exception as e:
        logging.warning(f"Ошибка поиска объектов рядом: {e}")
        nearby = None
    if nearby is None:
        failed.append(FAIL_OVERPASS)

    region = fetch_region(position.lat, position.lon)
    if region is None:
        failed.append(FAIL_NOMINATIM)

    sea_errors: list[str] = []
    sea_result = fetch_sea_areas(session, position.lat, position.lon, errors=sea_errors)
    if sea_result is None or sea_errors:
        failed.append(FAIL_SEA)
    sea, ocean = sea_result or (None, None)

    depth = fetch_depth(session, position.lat, position.lon)

    wiki_errors: list[str] = []
    wiki = fetch_wiki(session, position, nearby or [], region or "",
                      [area for area in (sea, ocean) if area], errors=wiki_errors)
    if wiki_errors:
        failed.append(FAIL_WIKI)

    context = GeoContext(
        region=region or "",
        sea=sea.name if sea else "",
        ocean=ocean.name if ocean and (not sea or ocean.name != sea.name) else "",
        depth_m=depth,
        nearby=nearby or [],
        wiki=wiki,
        failed=failed,
    )
    # Неполный контекст не кешируем, чтобы в следующий раз запросить источники заново
    if context.is_complete():
        db.set_geo_cache(key, context.to_json())
    else:
        logging.info(f"Геоконтекст {key} не кеширован, не ответили: {failed or 'регион и акватория пусты'}")
    return context


def select_articles(context: GeoContext, used_titles: set[str],
                    limit: int = PROMPT_ARTICLES_LIMIT) -> list[WikiArticle]:
    """Статьи для промпта: те, на которые ещё не опирались прошлые посты."""
    return [article for article in context.wiki if article.title not in used_titles][:limit]


def _article_note(article: WikiArticle, context: GeoContext, origin: tuple) -> str:
    notes = []
    if article.source == SOURCE_REGION:
        notes.append("статья о регионе")
    elif article.source == SOURCE_SEA:
        notes.append("статья об акватории")
    else:
        obj = next((o for o in context.nearby if o.label == article.osm_label), None) if article.osm_label else None
        if obj and obj.surrounds(origin):
            notes.append("мы рядом")
        elif obj:
            notes.append(f"{_km(obj.distance_km(origin))} км от пловца")
        elif article.coords:
            notes.append(f"{_km(geopy.distance.distance(origin, tuple(article.coords)).km)} км от пловца")
    if article.lang != "ru":
        notes.append("на английском")
    return "; ".join(notes)


def describe_for_prompt(position: SwimPosition, context: GeoContext,
                        articles: list[WikiArticle] | None = None) -> str:
    """Справка для LLM. Строки начинаются с названий служебных источников (PROMPT_SERVICE_SOURCES)."""
    origin = (position.lat, position.lon)
    if articles is None:
        articles = context.wiki[:PROMPT_ARTICLES_LIMIT]

    lines = [f"Координаты пловца: {position.lat:.3f}, {position.lon:.3f}."]
    leg = "Отрезок маршрута"
    if position.start_name or position.finish_name:
        leg += f": от «{position.start_name or 'старт'}» к «{position.finish_name or 'финиш'}»"
    percent = round(100 * position.leg_travelled / position.leg_total) if position.leg_total else 0
    remaining_km = geopy.distance.distance(origin, tuple(position.finish_coord)).km
    target = f"«{position.finish_name}»" if position.finish_name else "конца отрезка"
    lines.append(
        f"{leg}, пройдено {percent}% отрезка, до {target} осталось {_km(remaining_km)} км по прямой, "
        f"направление движения: {COMPASS[int((position.bearing + 22.5) // 45) % 8]}."
    )
    if position.description:
        lines.append(f"Описание отрезка (от организаторов): {position.description}")
    if context.region:
        lines.append(f"Регион (по данным OpenStreetMap): {context.region}.")
    if context.sea or context.ocean or context.depth_m:
        names = [name for name in (context.sea, context.ocean) if name]
        parts = [", ".join(names) + "."] if names else []
        if context.depth_m:
            parts.append(f"Глубина в точке: около {context.depth_m} м.")
        lines.append("Акватория: " + " ".join(parts))
    nearby = context.nearby_from(origin)
    if nearby:
        lines.append("Объекты поблизости: " + "; ".join(obj.describe(origin) for obj in nearby) + ".")
    if articles:
        lines.append("Статьи Википедии (источник — заголовок статьи):")
        for article in articles:
            note = _article_note(article, context, origin)
            title = f"«{article.title}» ({note})" if note else f"«{article.title}»"
            lines.append(f"- {title}: {article.text}")
    else:
        lines.append("Статей Википедии нет.")
    return "\n".join(lines)
