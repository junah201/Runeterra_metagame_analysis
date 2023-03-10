import requests
import os
import models
import database
from sqlalchemy.sql import exists
from sqlalchemy import literal
from datetime import datetime
import gzip
from typing import Tuple, Dict, Optional, List
import boto3
import uuid

RIOT_API_KEY = os.environ.get("RIOT_API_KEY")
DISCORD_WEBHOOKS_CHECK_MATCH_LOG = os.environ.get(
    "DISCORD_WEBHOOKS_CHECK_MATCH_LOG")

header_row = [
    "data_version",
    "match_id",
    "game_mode",
    "game_type",
    "game_start_time_utc",
    "game_version",
    "win_user_puuid",
    "win_user_deck_id",
    "win_user_deck_code",
    "win_user_order_of_play",
    "loss_user_puuid",
    "loss_user_deck_id",
    "loss_user_deck_code",
    "loss_user_order_of_play",
    "total_turn_count",
]


def check_match() -> Tuple[str, Dict]:
    log = {
        "start": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total checked": 0,
        "new players": [],
        "updated players": [],
        "new ranked matches": 0,
        "end": None,
        "rate limit": False,
    }

    match_datas = ", ".join(header_row) + "\n"

    db = database.get_db()

    db_master_players_puuid: List[models.Player] = db.query(models.Player) \
        .filter(models.Player.is_master == True, models.Player.puuid != None) \
        .order_by(models.Player.last_checked_at.asc(), models.Player.last_matched_at.desc()).all()

    for db_master_player in db_master_players_puuid:
        log["total checked"] += 1
        print("start", db_master_player.puuid)
        res = requests.get(
            f"https://apac.api.riotgames.com/lor/match/v1/matches/by-puuid/{db_master_player.puuid}/ids",
            headers={
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Charset": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Riot-Token": RIOT_API_KEY,
            },
            timeout=3
        )

        if res.status_code != 200:
            print(res.text)
            if res.status_code == 429:
                print("Too many requests")
                log["rate limit"] = True
                return match_datas, log
            continue

        match_ids = res.json()

        new_last_match_id: Optional[str] = None
        new_last_matched_at = db_master_player.last_matched_at or datetime.min
        if match_ids:
            new_last_match_id = match_ids[0]

        for match_id in match_ids:
            if match_id == db_master_player.last_matched_game_id:
                print("Already checked")
                break

            print("match_id", match_id)

            match_res = requests.get(
                f"https://apac.api.riotgames.com/lor/match/v1/matches/{match_id}",
                headers={
                    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept-Charset": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Riot-Token": RIOT_API_KEY,
                },
                timeout=3
            )

            if match_res.status_code != 200:
                print(match_res.text)
                if match_res.status_code == 429:
                    print("Too many requests")
                    log["rate limit"] = True
                    return match_datas, log
                continue

            match_data = match_res.json()

            tmp = dict()
            tmp["data_version"] = match_data["metadata"]["data_version"]
            tmp["match_id"] = match_data["metadata"]["match_id"]
            tmp["game_mode"] = match_data["info"]["game_mode"]
            tmp["game_type"] = match_data["info"]["game_type"]
            tmp["game_start_time_utc"] = match_data["info"]["game_start_time_utc"]
            tmp["game_version"] = match_data["info"]["game_version"]

            for player in match_data["info"]["players"]:
                tmp[f"{player['game_outcome']}_user_puuid"] = player["puuid"]
                tmp[f"{player['game_outcome']}_user_deck_id"] = player["deck_id"]
                tmp[f"{player['game_outcome']}_user_deck_code"] = player["deck_code"]
                tmp[f"{player['game_outcome']}_user_order_of_play"] = player["order_of_play"]

            row = []
            for header in header_row:
                row.append(str(tmp.get(header, "")))
            tmp = ", ".join(row) + "\n"
            match_datas += tmp

            new_last_matched_at = max(new_last_matched_at, datetime.strptime(
                match_data["info"]["game_start_time_utc"][:26], "%Y-%m-%dT%H:%M:%S.%f"))

            if match_data["info"]["game_type"] != "Ranked":
                continue

            log["new ranked matches"] += 1

            for puuid in match_data["metadata"]["participants"]:
                if puuid == db_master_player.puuid:
                    continue

                # ?????? DB??? ????????? puuid?????? ??????
                is_collected = db.query(literal(True)).filter(db.query(models.Player).filter(
                    models.Player.puuid == puuid
                ).exists()).scalar()
                print(is_collected)
                if is_collected == True:
                    continue

                account_res = requests.get(
                    f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}",
                    headers={
                        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Accept-Charset": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Riot-Token": RIOT_API_KEY,
                    },
                    timeout=3
                )
                if account_res.status_code != 200:
                    print(account_res.text)
                    if account_res.status_code == 429:
                        print("Too many requests")
                        return match_datas, log
                    continue
                account_data = account_res.json()

                # ????????? ???????????????, DB??? ????????? puuid??? ????????? ????????? ?????? ????????? ????????? ????????? ??????
                db_new_player: models.Player = db.query(models.Player).filter(
                    models.Player.game_name == account_data["gameName"], models.Player.puuid == None).first()

                if db_new_player:
                    print("update", db_new_player.game_name, puuid)
                    log["updated players"].append(
                        f"{db_new_player.game_name}\n")

                    db_new_player.puuid = puuid
                    db_new_player.game_name = account_data["gameName"]
                    db_new_player.tag_line = account_data["tagLine"]

                    db.commit()
                    continue

                # ????????? DB??? ?????? ????????? (?????????)
                print("new", account_data["gameName"], puuid)
                log["new players"].append(
                    f"{account_data['gameName']}\n")
                db_new_player = models.Player(
                    puuid=puuid,
                    game_name=account_data["gameName"],
                    tag_line=account_data["tagLine"],
                    is_master=False
                )
                db.add(db_new_player)
                db.commit()

        db_master_player.last_matched_game_id = new_last_match_id
        db_master_player.last_matched_at = new_last_matched_at
        db_master_player.last_checked_at = datetime.utcnow()
        db.commit()
        print("end", db_master_player.puuid)

    db.commit()
    return match_datas, log


def lambda_handler(event, context):
    match_datas, log = check_match()
    log["end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    success_color = 0x2ECC71
    error_color = 0xE74C3C

    if log["rate limit"]:
        color = error_color
    else:
        color = success_color

    data = {
        "content": "",
        "embeds": [
            {
                "title": "?????? ?????? ??????",
                "description": f"""
                ??? `{log["total checked"]}`?????? ????????? ??????????????? ????????? ??????????????????.

                ????????? ???????????? `{len(log["new players"])}`?????? ??????????????????.
                {"```" + "".join([f"{player}" for player in log["new players"]]) + "```" if log["new players"] else ""}

                ?????? ????????? ???????????? `{len(log["updated players"])}`?????? puuid??? ??????????????????.
                {"```" + "".join([f"{player}" for player in log["updated players"]]) + "```" if log["updated players"] else ""}

                ????????? ?????? ?????? `{log["new ranked matches"]}`?????? ??????????????????.

                rate limit : `{log["rate limit"]}`

                ?????? : `{log["start"]}`
                ?????? : `{log["end"]}`
                """,
                "color": color,
                "footer": {
                    "text": str(log["start"])
            }
            }
        ]
    }

    s3 = boto3.client('s3')
    s3.put_object(
        Bucket="lor-match-data",
        Key=f"match_data-1-{datetime.utcnow().strftime('%Y/%m/%d/%H')}/{uuid.uuid4()}.csv.gz",
        Body=gzip.compress(match_datas.encode("utf-8"))
    )

    requests.post(
        DISCORD_WEBHOOKS_CHECK_MATCH_LOG,
        json=data
    )

    return log
