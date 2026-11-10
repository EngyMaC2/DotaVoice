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
    {"account_id": 101, "name": "Aegis", "hero_name": "npc_dota_hero_juggernaut", "hero_id": 8},
    {"account_id": 102, "name": "Frost", "hero_name": "npc_dota_hero_crystal_maiden", "hero_id": 5},
    {"account_id": 103, "name": "Ember", "hero_name": "npc_dota_hero_ember_spirit", "hero_id": 106},
]
DIRE = [
    {"account_id": 201, "name": "Bane", "hero_name": "npc_dota_hero_bane", "hero_id": 3},
    {"account_id": 202, "name": "Slark", "hero_name": "npc_dota_hero_slark", "hero_id": 93},
    {"account_id": 203, "name": "Zeus", "hero_name": "npc_dota_hero_zuus", "hero_id": 22},
]


def _allplayers(radiant, dire) -> dict:
    """A spectator-style ``allplayers``: flat, keyed by the global slot 0..9.

    Real spectator dumps carry a numeric ``hero_id`` and a ``player_slot``; the
    side is implied by the slot (0..4 Radiant, 5..9 Dire) even when
    ``team_name`` is absent. The simulator sends both, like the game does.
    """
    players: dict[str, dict] = {}
    slot = 0
    for p in radiant:
        players[str(slot)] = {
            "steamid": str(76561197960000000 + int(p["account_id"])),
            "account_id": p["account_id"],
            "name": p["name"],
            "hero_name": p["hero_name"],
            "hero_id": p.get("hero_id"),
            "team_name": "radiant",
            "team": "radiant",
            "player_slot": slot,
            "team_slot": slot,
        }
        slot += 1
    for p in dire:
        players[str(slot)] = {
            "steamid": str(76561197960000000 + int(p["account_id"])),
            "account_id": p["account_id"],
            "name": p["name"],
            "hero_name": p["hero_name"],
            "hero_id": p.get("hero_id"),
            "team_name": "dire",
            "team": "dire",
            "player_slot": slot,
            "team_slot": slot - 5,
        }
        slot += 1
    return players


def _draft(radiant, dire) -> dict:
    """The draft module: each team's picks, keyed as Dota sends them.

    ``pickN_class`` is the hero id in pick order; team 2 is Radiant, team 3 is
    Dire. Bans and un-made picks are empty strings.
    """
    def team_block(players):
        block = {}
        for index, p in enumerate(players, start=1):
            block[f"pick{index}_class"] = p.get("hero_name", "")
        return block

    return {"team2": team_block(radiant), "team3": team_block(dire)}


def payload(
    match_id: str,
    game_state: str = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
    radiant=None,
    dire=None,
    clock: int = 0,
    token: str | None = None,
    draft: bool = False,
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
            "steamid": str(76561197960000000 + int(radiant[0]["account_id"])),
            "name": radiant[0]["name"],
            "team_name": "radiant",
            "team_slot": 0,
        },
        "allplayers": _allplayers(radiant, dire),
        "hero": {"name": "npc_dota_hero_juggernaut", "level": 1},
    }
    if draft:
        body["draft"] = _draft(radiant, dire)
    if token:
        body["auth"] = {"token": token}
    return body


def payload_teams(
    match_id: str,
    game_state: str = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
    radiant=None,
    dire=None,
    clock: int = 0,
    token: str | None = None,
) -> dict:
    """A normal (non-spectating) client's payload.

    The local client reports only its own slot, and Dota splits the team into
    ``player.team2``/``player.team3`` with the heroes mirrored under
    ``hero.team2``/``hero.team3`` - the shape the app has to read to show all ten
    players. ``allplayers`` is empty here, exactly as the game sends it.
    """
    radiant = radiant if radiant is not None else RADIANT
    dire = dire if dire is not None else DIRE
    me = radiant[0]

    def team_block(players):
        block = {}
        for index, p in enumerate(players):
            block[f"player{index}"] = {
                "name": p["name"],
                "steamid": str(76561197960000000 + int(p["account_id"])),
                "team_slot": index,
            }
        return block

    def hero_block(players):
        return {f"player{index}": {"name": p["hero_name"]} for index, p in enumerate(players)}

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
            "steamid": str(76561197960000000 + int(me["account_id"])),
            "name": me["name"],
            "team_name": "radiant",
            "team_slot": 0,
            "team2": team_block(radiant),
            "team3": team_block(dire),
        },
        "allplayers": {},
        "hero": {
            "name": me["hero_name"],
            "team2": hero_block(radiant),
            "team3": hero_block(dire),
        },
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
