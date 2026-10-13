from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from bs4 import BeautifulSoup

from radio1_bot.errors import ServiceUnavailable
from radio1_bot.models import ContentKind, RadioSnapshot, RadioSong
from radio1_bot.radio import BUDAPEST, Radio, classify, parse_playlist, parse_time

NOW = datetime(2026, 10, 4, 10, tzinfo=UTC)


def row(
    artist: str = "Artist",
    title: str = "Song",
    source_id: str = "1",
    stamp: str = "2026-10-04T09:59:00+00:00",
    *,
    live: bool = False,
) -> str:
    css = "yt-row live-link" if live else "yt-row"
    return (
        f'<div class="{css}"><span data-id="{source_id}"></span>'
        f'<span itemprop="byArtist">{artist}</span><span itemprop="name">{title}</span>'
        f'<time itemprop="startDate" content="{stamp}"></time></div>'
    )


@pytest.mark.parametrize(
    ("artist", "title", "kind"),
    [
        ("Rádió 1", "Reklám blokk", ContentKind.BREAK),
        ("R1", "Station ID", ContentKind.BREAK),
        ("", "Hírek", ContentKind.BREAK),
        ("Közlekedés", "Traffic", ContentKind.BREAK),
        ("DJ", "World Is Mine Radio Show", ContentKind.SHOW),
        ("Rádió 1", "Balázsék", ContentKind.SHOW),
        ("DJ", "Disco's Hit", ContentKind.SHOW),
        ("Cooky", "Cooky Weekend", ContentKind.SHOW),
        ("Rádió 1", "Top 50", ContentKind.SHOW),
        ("Radiohead", "Creep", ContentKind.MUSIC),
        ("R3HAB", "All Around the World (La La La)", ContentKind.MUSIC),
        ("Queen", "The Show Must Go On", ContentKind.MUSIC),
        ("Coldplay", "Adventure of a Lifetime", ContentKind.MUSIC),
        ("Commercially Successful", "Life", ContentKind.MUSIC),
        ("Dua Lipa", "Break My Heart", ContentKind.MUSIC),
        ("DJ Snake", "Let Me Love You (Live)", ContentKind.MUSIC),
        ("Artist", "Top 5000", ContentKind.MUSIC),
    ],
)
def test_classify_uses_station_labels_and_show_names_without_broad_substrings(
    artist: str, title: str, kind: ContentKind
) -> None:
    assert classify(artist, title) == kind


def test_parse_playlist_prioritizes_live_row_and_keeps_show_metadata() -> None:
    html = (
        row(source_id="3")
        + row("Rádió 1", "Balázsék", "4", "2026-10-04T08:00:00+00:00", live=True)
        + row(source_id="2")
    )
    result = parse_playlist(html, NOW)
    assert [entry.source_id for entry in result.entries] == ["4", "3", "2"]
    assert result.current.kind == ContentKind.SHOW
    assert not result.stale
    assert [entry.source_id for entry in result.music] == ["3", "2"]
    assert result.fetched_at == NOW


def test_parse_playlist_ignores_broken_rows_and_preserves_text_and_ids() -> None:
    html = (
        '<div class="yt-row"><span itemprop="name">Missing artist</span></div>'
        '<div class="yt-row"><span itemprop="byArtist"></span>'
        '<span itemprop="name">Empty artist</span></div>'
        + row("Ákos &amp; Friends", "A <b>song</b>", "123", live=True)
    )
    result = parse_playlist(html, NOW)
    assert len(result.entries) == 1
    assert result.current.artist == "Ákos & Friends"
    assert result.current.title == "A song"
    assert result.current.key == "id:123"
    assert result.current.played_at == datetime(2026, 10, 4, 9, 59, tzinfo=UTC)


def test_time_text_fallback_from_station_column_without_metadata() -> None:
    html = (
        '<div class="yt-row"><span itemprop="byArtist">Artist</span>'
        '<span itemprop="name">Song</span><div class="txt2 mcolumn">10.04. 11:58</div>'
        "</div>"
    )
    result = parse_playlist(html, NOW)
    assert result.current.played_at == datetime(2026, 10, 4, 11, 58, tzinfo=BUDAPEST)


@pytest.mark.parametrize("html", ["", "<html>Maintenance</html>", '<div class="yt-row"></div>'])
def test_parse_playlist_raises_on_empty_or_changed_source(html: str) -> None:
    with pytest.raises(ServiceUnavailable, match="playlist"):
        parse_playlist(html, NOW)


@pytest.mark.parametrize(
    ("artist", "title", "stamp", "stale"),
    [
        ("Artist", "Song", NOW - timedelta(minutes=12), False),
        ("Artist", "Song", NOW - timedelta(minutes=12, seconds=1), True),
        ("Rádió 1", "Reklám", NOW - timedelta(minutes=30), False),
        ("DJ", "Daily Mix", NOW - timedelta(hours=2), False),
    ],
)
def test_stale_music_is_reported_without_inventing_a_break(
    artist: str, title: str, stamp: datetime, stale: bool
) -> None:
    result = parse_playlist(row(artist, title, stamp=stamp.isoformat()), NOW)
    assert result.stale is stale
    if artist == "Artist":
        assert result.current.kind == ContentKind.MUSIC


@pytest.mark.parametrize(
    ("markup", "now", "expected"),
    [
        (
            "<time>23:58</time>",
            datetime(2026, 10, 4, 0, 2, tzinfo=BUDAPEST),
            datetime(2026, 10, 3, 23, 58, tzinfo=BUDAPEST),
        ),
        (
            "<time>12.31. 23:58</time>",
            datetime(2026, 1, 1, 0, 2, tzinfo=BUDAPEST),
            datetime(2025, 12, 31, 23, 58, tzinfo=BUDAPEST),
        ),
        (
            "<time>10.04. 11:58</time>",
            NOW,
            datetime(2026, 10, 4, 11, 58, tzinfo=BUDAPEST),
        ),
        (
            '<time content="2026-10-04T09:58:00Z">Ignored</time>',
            NOW,
            datetime(2026, 10, 4, 9, 58, tzinfo=UTC),
        ),
        (
            '<time content="2026-10-04T11:58:00">Ignored</time>',
            NOW,
            datetime(2026, 10, 4, 11, 58, tzinfo=BUDAPEST),
        ),
        (
            '<time content="invalid">11:58</time>',
            NOW,
            datetime(2026, 10, 4, 11, 58, tzinfo=BUDAPEST),
        ),
        ("<time>13.40. 99:99</time>", NOW, None),
        ("<time>Just now</time>", NOW, None),
    ],
)
def test_parse_time_handles_iso_local_midnight_and_year_rollover(
    markup: str, now: datetime, expected: datetime | None
) -> None:
    element = BeautifulSoup(markup, "html.parser").find("time")
    assert parse_time(element, now) == expected


def test_missing_time_is_allowed_and_parser_limits_history() -> None:
    assert parse_time(None, NOW) is None
    result = parse_playlist("".join(row(source_id=str(index)) for index in range(100)), NOW)
    assert len(result.entries) == 60


async def test_concurrent_snapshot_calls_share_one_http_request() -> None:
    requests = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0)
        return httpx.Response(200, text=row(stamp=datetime.now(UTC).isoformat()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        radio = Radio(client, ttl=60)
        results = await asyncio.gather(*(radio.snapshot() for _ in range(10)))
        assert requests == 1
        assert all(result is results[0] for result in results)
        assert radio.cached is results[0]


async def test_http_failure_reuses_recent_cache_as_stale_and_honors_retry_interval() -> None:
    requests = 0

    def unavailable(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503)

    cached = RadioSnapshot((RadioSong("Artist", "Song"),), datetime.now(UTC))
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        radio = Radio(client, ttl=60)
        radio._snapshot = cached
        result = await radio.snapshot()
        assert result.stale
        assert result.entries == cached.entries
        assert result.fetched_at == cached.fetched_at
        assert await radio.snapshot() is result
        assert requests == 1


async def test_old_cache_is_discarded_and_failed_calls_are_negative_cached() -> None:
    requests = 0

    def unavailable(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        radio = Radio(client, ttl=60)
        radio._snapshot = RadioSnapshot(
            (RadioSong("Artist", "Song"),), datetime.now(UTC) - timedelta(minutes=3)
        )
        for _ in range(2):
            with pytest.raises(ServiceUnavailable):
                await radio.snapshot()
        assert radio.cached is None
        assert requests == 1
        radio._expires = 0
        with pytest.raises(ServiceUnavailable):
            await radio.snapshot()
        assert requests == 2


async def test_changed_html_uses_the_same_failure_policy_as_network_errors() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="Maintenance"))
    ) as client:
        radio = Radio(client)
        with pytest.raises(ServiceUnavailable):
            await radio.snapshot()
        assert radio.cached is None
