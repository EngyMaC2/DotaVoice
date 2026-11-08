"""GSI simulator.

Feeds realistic Dota 2 Game State Integration payloads into a running bridge so
the whole pipeline (session binding, team split, team switch, mute, reconnect,
match end) can be exercised without the game.

Usage::

    python -m scripts.simulate --url http://127.0.0.1:12000 --match-id 7123456789
    python -m scripts.simulate --scenario reconnect
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

RADIANT = [
    {"account_id": 101, "name": "Aegis", "hero_name": "npc_dota_hero_juggernaut"},
    {"account_id": 102, "name": "Frost", "hero_name": "npc_dota_hero_crystal_maiden"},
    {"account_id": 103, "name": "Ember", "hero_name": "npc_dota_hero_ember_spirit"},
]
DIRE = [
    {"account_id": 201, "name": "Bane", "hero_name": "npc_dota_hero_bane"},
    {"account_id": 202, "name": "Slark", "hero_name": "npc_dota_hero_slark"},
    {"account_id": 203, "name": "Zeus", "hero_name": "npc_dota_hero_zuus"},
]


def _allplayers(radiant, dire) -> dict:
    players: dict[str, dict] = {}
    slot = 0
    for p in radiant:
        players[str(slot)] = {**p, "team_name": "radiant", "team": "radiant"}
        slot += 1
    for p in dire:
        players[str(slot)] = {**p, "team_name": "dire", "team": "dire"}
        slot += 1
    return players


def payload(
    match_id: str,
    game_state: str = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
    radiant=None,
    dire=None,
    clock: int = 0,
    token: str | None = None,
) -> dict:
    radiant = radiant if radiant is not None else RADIANT
    dire = dire if dire is not None else DIRE
    body = {
        "provider": {"name": "Dota 2", "appid": 570, "version": 40},
        "map": {
            "name": "start",
            "matchid": match_id,
            "game_state": game_state,
            "game_time": clock,
            "clock_time": clock,
            "radiant_score": 0,
            "dire_score": 0,
        },
        "player": {
            "steamid": "76561198000000101",
            "name": "Aegis",
            "team_name": "radiant",
            "team_slot": 0,
        },
        "allplayers": _allplayers(radiant, dire),
        "hero": {"name": "npc_dota_hero_juggernaut", "level": 1},
    }
    if token:
        body["auth"] = {"token": token}
    return body


def gsi_url(url: str) -> str:
    return url if url.rstrip("/").endswith("/gsi") else url.rstrip("/") + "/gsi"


def post(url: str, body: dict) -> dict | None:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"  ! HTTP {exc.code}: {exc.read().decode('utf-8')[:200]}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"  ! ошибка соединения: {exc}", file=sys.stderr)
    return None


def _log(label: str, result: dict | None) -> None:
    if result is None:
        return
    print(f"  -> {label}: {json.dumps(result, ensure_ascii=False)}")


def scenario_basic(url: str, match_id: str, token: str | None) -> None:
    print("[1] матч начался (подготовка)")
    _log("gsi", post(gsi_url(url), payload(match_id, "DOTA_GAMERULES_STATE_PRE_GAME", token=token)))
    time.sleep(1)

    print("[2] игра идёт")
    _log("gsi", post(gsi_url(url), payload(match_id, clock=60, token=token)))
    time.sleep(1)

    print("[3] повторное событие по тому же матчу: сторона игрока не меняется")
    _log("gsi", post(gsi_url(url), payload(match_id, clock=120, token=token)))
    time.sleep(1)

    print("[4] матч завершён")
    _log("gsi", post(gsi_url(url), payload(match_id, "DOTA_GAMERULES_STATE_POST_GAME", clock=2400, token=token)))


def scenario_reconnect(url: str, match_id: str, token: str | None) -> None:
    print("[1] матч начался")
    _log("gsi", post(gsi_url(url), payload(match_id, clock=10, token=token)))
    time.sleep(1)

    print("[2] имитируем перезапуск клиента: приходит тот же match_id")
    _log("gsi", post(gsi_url(url), payload(match_id, clock=11, token=token)))
    time.sleep(1)

    print("[3] и снова, чтобы убедиться, что каналы не дублируются")
    _log("gsi", post(gsi_url(url), payload(match_id, clock=12, token=token)))


def scenario_mute(url: str, match_id: str, token: str | None) -> None:
    print("[1] матч начался")
    _log("gsi", post(gsi_url(url), payload(match_id, token=token)))
    time.sleep(0.5)
    print("[2] мут на стороне сервера через API (сценарий push-to-talk)")
    mute_url = f"{url}/api/sessions/{match_id}/mute"
    _log("mute", post(mute_url, {"player_id": "101", "muted": True}))
    time.sleep(0.5)
    print("[3] включаем индикатор речи для игрока 202")
    _log("speaking", post(f"{url}/api/sessions/{match_id}/speaking",
                          {"player_id": "202", "speaking": True}))


SCENARIOS = {
    "basic": scenario_basic,
    "reconnect": scenario_reconnect,
    "mute": scenario_mute,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Симулятор GSI для Dota 2")
    parser.add_argument("--url", default="http://127.0.0.1:12000")
    parser.add_argument("--match-id", default="7123456789")
    parser.add_argument("--token", default=None)
    parser.add_argument("--scenario", default="basic", choices=sorted(SCENARIOS))
    args = parser.parse_args()

    print(f"Сценарий '{args.scenario}' против {args.url} (матч {args.match_id})")
    SCENARIOS[args.scenario](args.url, args.match_id, args.token)
    print("готово")


if __name__ == "__main__":
    main()
