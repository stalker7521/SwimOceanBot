"""Build a Folium swim-route map and render it to PNG via headless Chromium."""

import math
import os
import time
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

import folium
import geopy.distance
import pandas as pd
import pytz
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

from settings import DEFAULT_FINISH_CAPTION, DEFAULT_START_CAPTION

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
SWIMMER_RIGHT = ASSETS_DIR / "swimmer_right.png"
SWIMMER_LEFT = ASSETS_DIR / "swimmer_left.png"

CHROMIUM_BINARIES = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)
CHROME_BINARIES = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)
CHROMEDRIVER_PATHS = (
    "/usr/bin/chromedriver",
    "/usr/bin/chromium-driver",
    "/usr/lib/chromium-browser/chromedriver",
)

MAP_TZ = pytz.timezone("Asia/Yekaterinburg")


class RouteFinishedError(Exception):
    """Raised when the club has already finished every planned leg."""


class MapBuildError(Exception):
    """Raised when metres or location data cannot be turned into a map."""


def calculate_initial_compass_bearing(start: tuple, end: tuple) -> float:
    if not isinstance(start, tuple) or not isinstance(end, tuple):
        raise TypeError("Only tuples are supported as arguments")
    lat1 = math.radians(start[0])
    lat2 = math.radians(end[0])
    diff_long = math.radians(end[1] - start[1])
    x = math.sin(diff_long) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (
        math.sin(lat1) * math.cos(lat2) * math.cos(diff_long)
    )
    initial_bearing = math.degrees(math.atan2(x, y))
    return (initial_bearing + 360) % 360


def distance_between(start, end):
    return geopy.distance.distance(start, end).km


def location_at_dist(
    start: tuple,
    end: tuple,
    distance_travelled,
    bearing: float | None = None,
) -> tuple[float, float]:
    if not bearing:
        bearing = calculate_initial_compass_bearing(start, end)
    point = geopy.distance.distance(kilometers=distance_travelled).destination(
        start,
        bearing=bearing,
    )
    return (point.latitude, point.longitude)


def get_points_list_along_path(start_point, finish_point, bearing=None):
    route_array = [tuple(start_point)]
    full_dist = distance_between(start_point, finish_point)
    for i in range(1, 100):
        dist = full_dist * i / 100
        route_array.append(
            location_at_dist(
                tuple(start_point),
                tuple(finish_point),
                dist,
                bearing,
            )
        )
    route_array.append(tuple(finish_point))
    return route_array


def draw_marker(coord, caption=""):
    return folium.CircleMarker(
        location=coord,
        radius=7,
        fill=True,
        popup=folium.Popup(caption),
        color="red",
    )


def _parse_coord(value) -> list[float]:
    return [float(part) for part in str(value).split(",")]


def _format_caption(raw_value, default: str) -> str:
    if raw_value:
        return f"{default}: {raw_value}"
    return default


def get_distance_at_day(meters_df: pd.DataFrame, day: pd.Timestamp) -> int:
    df = meters_df.copy()
    df["Cumulative_sum"] = df["Cumulative_sum"].replace("", pd.NA).ffill()
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True)
    match = df[df["Date"] == day]
    if match.empty:
        earlier = df[df["Date"] <= day].dropna(subset=["Cumulative_sum"])
        if earlier.empty:
            raise MapBuildError("Нет данных о проплытых метрах на эту дату")
        return int(float(earlier.iloc[-1]["Cumulative_sum"]))
    value = match["Cumulative_sum"].values[0]
    if pd.isna(value):
        raise MapBuildError("Нет данных о проплытых метрах на эту дату")
    return int(float(value))


def get_location(locations_df: pd.DataFrame, dist: int) -> tuple[pd.DataFrame, int]:
    df = locations_df.copy()
    df["Cumul_dist"] = df["Cumul_dist"].astype(int)
    next_df = df[df["Cumul_dist"] > dist]
    if next_df.empty:
        raise RouteFinishedError("Все отрезки маршрута уже проплыты")
    row_idx = next_df[next_df["Cumul_dist"] == next_df["Cumul_dist"].min()].index[0]
    return df, row_idx


def build_map(
    start_coord,
    finish_coord,
    start_caption,
    finish_caption,
    dist,
    distance_travelled,
    completed_legs=None,
):
    half_dist = distance_between(start_coord, finish_coord) / 2
    center_point = location_at_dist(tuple(start_coord), tuple(finish_coord), half_dist)

    world_map = folium.Map(
        location=center_point,
        zoom_start=8,
        tiles="OpenStreetMap",
        attributionControl=0,
        control_scale=True,
    )

    draw_marker(start_coord, start_caption).add_to(world_map)
    draw_marker(finish_coord, finish_caption).add_to(world_map)

    dist_on_map = distance_between(start_coord, finish_coord) * 1000
    portion_travelled = (distance_travelled / dist) if dist else 0
    graphical_distance = round(portion_travelled * dist_on_map)
    current_point = location_at_dist(
        tuple(start_coord),
        tuple(finish_coord),
        graphical_distance / 1000,
    )
    current_point = tuple(round(i, 3) for i in current_point)
    bearing = calculate_initial_compass_bearing(
        start=tuple(start_coord),
        end=tuple(finish_coord),
    )
    icon_image = str(SWIMMER_RIGHT if 0 <= bearing <= 180 else SWIMMER_LEFT)
    icon = folium.CustomIcon(icon_image, icon_size=(50, 50))
    remaining_dist = dist - distance_travelled
    folium.Marker(
        location=current_point,
        icon=icon,
        popup=folium.Popup(
            f"Мы тут! До конца этого заплыва {remaining_dist} м",
            parse_html=True,
            max_width=100,
        ),
    ).add_to(world_map)

    folium.PolyLine(
        locations=get_points_list_along_path(start_coord, current_point),
        color="#FF0000",
        weight=5,
        tooltip=f"Проплыто {distance_travelled} м",
    ).add_to(world_map)

    folium.PolyLine(
        locations=get_points_list_along_path(start_coord, finish_coord),
        color="#000980",
        weight=2,
        tooltip=f"Траектория заплыва, длина {dist} м",
        dash_array="5",
        opacity=0.3,
    ).add_to(world_map)

    world_map.fit_bounds(
        bounds=[start_coord, finish_coord],
        padding_top_left=start_coord,
        padding_bottom_right=finish_coord,
    )

    for start_point, finish_point in completed_legs or []:
        folium.PolyLine(
            locations=get_points_list_along_path(start_point, finish_point),
            color="#008000",
            weight=5,
            tooltip="Уже проплыто",
        ).add_to(world_map)

    return world_map


def build_map_for_day(
    meters_df: pd.DataFrame,
    locations_df: pd.DataFrame,
    day: pd.Timestamp | None = None,
) -> tuple[folium.Map, str]:
    if day is None:
        day = pd.to_datetime(datetime.now(MAP_TZ).date(), dayfirst=True)

    overall_distance = get_distance_at_day(meters_df, day)
    df, row_idx = get_location(locations_df, overall_distance)

    if row_idx == df.index.min():
        current_dist = overall_distance
    else:
        prev_idx = df.index[df.index.get_loc(row_idx) - 1]
        current_dist = overall_distance - int(df.loc[prev_idx, "Cumul_dist"])

    start_coord = _parse_coord(df.loc[row_idx, "Start_point"])
    finish_coord = _parse_coord(df.loc[row_idx, "Finish_point"])
    dist = int(df.loc[row_idx, "Distance"])
    start_caption = _format_caption(df.loc[row_idx, "Start_caption"], DEFAULT_START_CAPTION)
    finish_caption = _format_caption(df.loc[row_idx, "Finish_caption"], DEFAULT_FINISH_CAPTION)
    description = str(df.loc[row_idx, "Description"] or "").strip()

    completed_legs = []
    for _, row in df.loc[:row_idx].iloc[:-1].iterrows():
        completed_legs.append(
            (_parse_coord(row["Start_point"]), _parse_coord(row["Finish_point"]))
        )

    world_map = build_map(
        start_coord,
        finish_coord,
        start_caption,
        finish_caption,
        dist,
        distance_travelled=current_dist,
        completed_legs=completed_legs,
    )
    remaining = dist - current_dist
    caption_parts = [description] if description else []
    caption_parts.append(f"Проплыто {current_dist} м из {dist} м, осталось {remaining} м")
    return world_map, "\n".join(caption_parts)


def _first_existing(paths: tuple[str, ...]) -> str | None:
    for path in paths:
        if os.path.isfile(path):
            return path
    return None


def _browser_and_driver() -> tuple[str | None, str | None]:
    driver_path = _first_existing(CHROMEDRIVER_PATHS)
    chromium_path = _first_existing(CHROMIUM_BINARIES)
    chrome_path = _first_existing(CHROME_BINARIES)
    if driver_path and chromium_path:
        return chromium_path, driver_path
    if driver_path and chrome_path:
        return chrome_path, driver_path
    if chrome_path:
        return chrome_path, None
    return chromium_path, driver_path


def _chrome_options(window_size: tuple[int, int]) -> webdriver.ChromeOptions:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument(f"--window-size={window_size[0]},{window_size[1]}")
    binary, _ = _browser_and_driver()
    if binary:
        options.binary_location = binary
    return options


def _chrome_service() -> Service | None:
    _, driver_path = _browser_and_driver()
    return Service(driver_path) if driver_path else None


def save_map_as_png(
    map_object,
    window_size: tuple[int, int] = (800, 800),
    wait_seconds: float = 3,
) -> BytesIO:
    tmp_dir = TemporaryDirectory()
    html_path = Path(tmp_dir.name) / "temp_map.html"
    map_object.save(str(html_path))

    driver = None
    try:
        options = _chrome_options(window_size)
        service = _chrome_service()
        driver = (
            webdriver.Chrome(service=service, options=options)
            if service
            else webdriver.Chrome(options=options)
        )
        driver.get(f"file://{html_path.resolve()}")
        time.sleep(wait_seconds)
        png = BytesIO(driver.get_screenshot_as_png())
        png.seek(0)
        return png
    finally:
        if driver is not None:
            driver.quit()
        tmp_dir.cleanup()


def generate_map_png(
    meters_df: pd.DataFrame,
    locations_df: pd.DataFrame,
    day: pd.Timestamp | None = None,
) -> tuple[BytesIO, str]:
    world_map, caption = build_map_for_day(meters_df, locations_df, day)
    return save_map_as_png(world_map), caption
