import datetime
import threading

import pytz
import requests
from flask import Flask, render_template, request, Response
from google.cloud import bigquery

import helpers

TABLE_NAMES = {
    'teams': 'commander.teams',
    'projections': 'commander.projections',
    'scores': 'commander.scores',
    'changes': 'commander.changes',
}

app = Flask(__name__)


@app.route("/update/all", methods=['GET'])
def update_all():
    """ Update all but live scores """

    responses = []

    responses.append(('projections', helpers.update_projections()))
    responses.append(('teams', helpers.update_teams()))
    update_scores()

    response = ', '.join(f"{key}: {value}" for key, value in responses)

    return Response(response, status=200 if False not in [r[1] for r in responses] else 500)


@app.route("/update/scores", methods=['GET'])
def update_scores():
    helpers.update_progress()
    helpers.update_all_scores()
    return Response('Success', 200)


@app.route("/changes", methods=['GET'])
def list_changes():

    changes = []

    for change in helpers.run_query(f"SELECT * FROM `{TABLE_NAMES.get('changes')}` ORDER BY updated DESC LIMIT 20", as_list=True):
        change = dict(change)
        change['diff'] = f"<span class='change-{'negative' if change.get('old') > change.get('new') else 'positive'}'>" \
                         f"{'-' if change.get('old') > change.get('new') else '+'}{abs(change.get('old') - change.get('new'))}</span>"
        changes.append(change)

    return render_template('changes.html', changes=changes)


@app.route("/records", methods=['GET'])
def records():

    leagues = []
    records = {}
    data = {}

    profiles = helpers.load_profiles()

    for league_list in profiles.values():
        
        for league in league_list:

            league_data = {
                'name': league.get('name'),
                'id': league.get('league_id'),
                'platform': league.get('platform'),
                'start': league.get('start_year')
            }
            
            if league_data not in leagues:
                leagues.append(league_data)

    threads = []

    for league in leagues:

        thread = threading.Thread(target=helpers.get_league_data, args=(data, league))
        thread.start()
        threads.append(thread)
    
    for thread in threads:
        thread.join()

    for league_name, league_data in data.items():

        if not league_data:
            continue

        records[league_name] = {
            'Highest Points (Week)': sorted(league_data, key=lambda x: x[4], reverse=True)[0:3],
            'Lowest Points (Week)': sorted(league_data, key=lambda x: x[4])[0:3],
            'Highest Projected (Week)': sorted(league_data, key=lambda x: x[5], reverse=True)[0:3],
            'Lowest Projected (Week)': sorted(league_data, key=lambda x: x[5])[0:3],
            'Best Outcome (Week)': sorted(league_data, key=lambda x: x[6], reverse=True)[0:3],
            'Worst Outcome (Week)': sorted(league_data, key=lambda x: x[6])[0:3],
        }

    return render_template('records.html', records=records)


_ESPN_PLAYER_GAME_LOG_CACHE = {}

def get_espn_player_game_log(season, name, team):
    cache_key = (int(season), str(name), str(team))

    if cache_key in _ESPN_PLAYER_GAME_LOG_CACHE:
        return list(_ESPN_PLAYER_GAME_LOG_CACHE[cache_key])

    """
    Return completed regular-season fantasy game logs for one NFL player.

    Fantasy PPG rules:
      - Weeks 1-17 only
      - Regular season only
      - Completed games only
      - Player must actually appear in the ESPN box score
      - Standard PPR scoring
    """
    from datetime import date, timedelta

    game_log = []

    try:
        all_events = {}

        schedule_start = date(int(season), 8, 1)
        schedule_end = date(int(season), 12, 31)
        current_date = schedule_start

        while current_date <= schedule_end:
            chunk_end = min(
                current_date + timedelta(days=30),
                schedule_end
            )

            scoreboard_url = (
                "https://site.api.espn.com/apis/site/v2/sports/"
                "football/nfl/scoreboard"
                f"?dates={current_date.strftime('%Y%m%d')}-"
                f"{chunk_end.strftime('%Y%m%d')}"
                "&limit=100"
            )

            try:
                scoreboard = requests.get(
                    scoreboard_url,
                    timeout=20
                ).json()

                for event in scoreboard.get("events", []):
                    event_id = event.get("id")
                    if event_id:
                        all_events[event_id] = event

            except Exception:
                pass

            current_date = chunk_end + timedelta(days=1)

        for event in all_events.values():
            week = (event.get("week") or {}).get("number")

            season_type = (
                (event.get("season") or {}).get("type")
            )

            if not isinstance(week, int) or not 1 <= week <= 17:
                continue

            if season_type != 2:
                continue

            competition = (event.get("competitions") or [{}])[0]

            status_name = (
                event.get("status", {})
                .get("type", {})
                .get("name")
            )

            if status_name != "STATUS_FINAL":
                continue

            competitors = competition.get("competitors") or []

            team_abbrs = {
                (c.get("team") or {}).get("abbreviation")
                for c in competitors
            }

            if team not in team_abbrs:
                continue

            event_id = event.get("id")

            try:
                summary_url = (
                    "https://site.api.espn.com/apis/site/v2/sports/"
                    f"football/nfl/summary?event={event_id}"
                )

                summary = requests.get(
                    summary_url,
                    timeout=20
                ).json()

            except Exception:
                continue

            player_stats = {
                "pass_att": 0,
                "pass_cmp": 0,
                "pass_yd": 0,
                "pass_td": 0,
                "pass_int": 0,
                "rush_att": 0,
                "rush_yd": 0,
                "rush_td": 0,
                "rec": 0,
                "rec_yd": 0,
                "rec_td": 0,
                "targets": 0,
                "fumbles_lost": 0,
            }

            found = False

            for team_box in summary.get("boxscore", {}).get("players", []):
                team_info = team_box.get("team") or {}

                if team_info.get("abbreviation") != team:
                    continue

                for stat_group in team_box.get("statistics", []):
                    group_name = stat_group.get("name")
                    athletes = stat_group.get("athletes") or []

                    for athlete in athletes:
                        athlete_info = athlete.get("athlete") or {}

                        if athlete_info.get("displayName") != name:
                            continue

                        found = True

                        labels = stat_group.get("labels") or []
                        values = athlete.get("stats") or []
                        stats_map = dict(zip(labels, values))

                        if group_name == "passing":
                            try:
                                cmp_att = str(
                                    stats_map.get("C/ATT", "0/0")
                                ).split("/")

                                if len(cmp_att) == 2:
                                    player_stats["pass_cmp"] = int(cmp_att[0])
                                    player_stats["pass_att"] = int(cmp_att[1])
                            except Exception:
                                pass

                            try:
                                player_stats["pass_yd"] = int(
                                    float(stats_map.get("YDS", 0))
                                )
                            except Exception:
                                pass

                            try:
                                player_stats["pass_td"] = int(
                                    float(stats_map.get("TD", 0))
                                )
                            except Exception:
                                pass

                            try:
                                player_stats["pass_int"] = int(
                                    float(stats_map.get("INT", 0))
                                )
                            except Exception:
                                pass

                        elif group_name == "rushing":
                            try:
                                player_stats["rush_att"] = int(
                                    float(stats_map.get("CAR", 0))
                                )
                            except Exception:
                                pass

                            try:
                                player_stats["rush_yd"] = int(
                                    float(stats_map.get("YDS", 0))
                                )
                            except Exception:
                                pass

                            try:
                                player_stats["rush_td"] = int(
                                    float(stats_map.get("TD", 0))
                                )
                            except Exception:
                                pass

                        elif group_name == "receiving":
                            try:
                                player_stats["rec"] = int(
                                    float(stats_map.get("REC", 0))
                                )
                            except Exception:
                                pass

                            try:
                                player_stats["rec_yd"] = int(
                                    float(stats_map.get("YDS", 0))
                                )
                            except Exception:
                                pass

                            try:
                                player_stats["rec_td"] = int(
                                    float(stats_map.get("TD", 0))
                                )
                            except Exception:
                                pass

                            try:
                                player_stats["targets"] = int(
                                    float(stats_map.get("TGTS", 0))
                                )
                            except Exception:
                                pass

                        elif group_name == "fumbles":
                            try:
                                player_stats["fumbles_lost"] = int(
                                    float(stats_map.get("LOST", 0))
                                )
                            except Exception:
                                pass

            if not found:
                continue

            points = (
                player_stats["pass_yd"] * 0.04
                + player_stats["pass_td"] * 4
                - player_stats["pass_int"] * 2
                + player_stats["rush_yd"] * 0.1
                + player_stats["rush_td"] * 6
                + player_stats["rec"]
                + player_stats["rec_yd"] * 0.1
                + player_stats["rec_td"] * 6
                - player_stats["fumbles_lost"] * 2
            )

            game_log.append({
                "week": week,
                "points": round(points, 2),
                "pass_cmp": player_stats["pass_cmp"],
                "pass_att": player_stats["pass_att"],
                "pass_yd": player_stats["pass_yd"],
                "pass_td": player_stats["pass_td"],
                "pass_int": player_stats["pass_int"],
                "rush_att": player_stats["rush_att"],
                "rush_yd": player_stats["rush_yd"],
                "rush_td": player_stats["rush_td"],
                "rec": player_stats["rec"],
                "targets": player_stats["targets"],
                "rec_yd": player_stats["rec_yd"],
                "rec_td": player_stats["rec_td"],
                "fumbles_lost": player_stats["fumbles_lost"],
            })

        game_log.sort(key=lambda row: int(row.get("week", 0)))

    except Exception as exc:
        print(
            f"ESPN game-log query failed for "
            f"{name} ({team}, {season}): {exc}"
        )

    _ESPN_PLAYER_GAME_LOG_CACHE[cache_key] = list(game_log)

    return game_log


@app.route("/api/player/<player_id>", methods=["GET"])
def player_api(player_id: str):
    player_id = str(player_id)
    requested_week = int(request.args.get("week", helpers.get_current_week()))

    profiles = helpers.load_profiles()

    # ------------------------------------------------------------
    # Sleeper player identity
    # ------------------------------------------------------------
    try:
        sleeper_players = requests.get(
            "https://api.sleeper.app/v1/players/nfl",
            timeout=20
        ).json()
    except Exception:
        return {"error": "Unable to load Sleeper players"}, 502

    sleeper_player = sleeper_players.get(player_id)

    if not sleeper_player:
        return {"error": "Player not found"}, 404

    name = (
        sleeper_player.get("full_name")
        or sleeper_player.get("last_name")
        or "Unknown Player"
    )

    positions = sleeper_player.get("fantasy_positions") or []
    position = positions[0] if positions else ""
    if position == "DEF":
        position = "DST"

    team = sleeper_player.get("team") or "FA"
    status = sleeper_player.get("injury_status") or "ACTIVE"

    # ------------------------------------------------------------
    # Current NFL season
    # ------------------------------------------------------------
    try:
        state = requests.get(
            "https://api.sleeper.app/v1/state/nfl",
            timeout=20
        ).json()
    except Exception:
        state = {}

    season = str(
    request.args.get("season")
    or state.get("season")
    or datetime.datetime.utcnow().year
)
    current_week = int(state.get("week") or requested_week)

    # ------------------------------------------------------------
    # Configured Sleeper leagues
    # ------------------------------------------------------------
    configured_leagues = []

    for profile_leagues in profiles.values():
        for league in profile_leagues:
            if league.get("platform") != "sleeper":
                continue

            league_id = str(league.get("league_id"))

            if any(x["id"] == league_id for x in configured_leagues):
                continue

            configured_leagues.append({
                "id": league_id,
                "name": league.get("name") or f"Sleeper League {league_id}",
                "season": league.get("start_year"),
                "team_id": league.get("team_id"),
                "scoring": league.get("scoring") or "ppr",
            })

    # ------------------------------------------------------------
    # YOUR ownership + YOUR lineup status
    #
    # commander.leagues already stores team_id for each configured
    # league. Only inspect that roster; never count other owners.
    # ------------------------------------------------------------
    leagues = []
    started = 0
    benched = 0

    for league in configured_leagues:
        league_id = league["id"]
        my_team_id = league.get("team_id")

        if my_team_id is None:
            continue

        try:
            rosters = requests.get(
                f"https://api.sleeper.app/v1/league/{league_id}/rosters",
                timeout=20
            ).json()

            my_roster = next(
                (
                    roster for roster in rosters
                    if str(roster.get("roster_id")) == str(my_team_id)
                ),
                None
            )

            if not my_roster:
                continue

            roster_players = [
                str(x) for x in (my_roster.get("players") or [])
            ]

            if player_id not in roster_players:
                continue

            # This is one of MY teams that owns the player.
            league_started = 0
            league_benched = 0
            week_slots = {}

            try:
                matchups = requests.get(
                    f"https://api.sleeper.app/v1/league/{league_id}/matchups/{requested_week}",
                    timeout=20
                ).json()

                my_matchup = next(
                    (
                        m for m in matchups
                        if str(m.get("roster_id")) == str(my_team_id)
                    ),
                    None
                )

                starters = [
                    str(x)
                    for x in ((my_matchup or {}).get("starters") or [])
                ]

                if player_id in starters:
                    league_started = 1
                    started += 1
                    week_slots[requested_week] = "STARTED"
                else:
                    league_benched = 1
                    benched += 1
                    week_slots[requested_week] = "BENCHED"

            except Exception:
                pass

            leagues.append({
                "id": league_id,
                "name": league["name"],
                "team": str(my_team_id),
                "scoring": league.get("scoring") or "ppr",
                "started": league_started,
                "benched": league_benched,
                "week_slots": week_slots,
            })

        except Exception:
            continue

    # ------------------------------------------------------------
    # ------------------------------------------------------------
    # Weekly NFL game log
    #
    # ESPN provides completed NFL box scores. Use the shared helper
    # so there is only one ESPN game-log implementation.
    # ------------------------------------------------------------
    game_log = get_espn_player_game_log(
        season,
        name,
        team,
    )

    played = [
        float(x["points"])
        for x in game_log
        if x.get("points") is not None
    ]

    ppg = (
        sum(played) / len(played)
        if played else None
    )

    # ------------------------------------------------------------
    # Current-week projection
    # ------------------------------------------------------------
    projection = None

    try:
        projection_rows = requests.get(
            f"https://api.sleeper.app/projections/nfl/{season}/{requested_week}"
            "?season_type=regular",
            timeout=20
        ).json()

        for row in projection_rows:
            if str(row.get("player_id")) == player_id:
                projection = (row.get("stats") or {}).get("pts_ppr")
                break

    except Exception:
        pass

    # ------------------------------------------------------------
    # ------------------------------------------------------------
    # 2026 NFL bye week
    #
    # Find the actual regular-season bye week for the player's team.
    # This is independent of the week currently being viewed.
    # ------------------------------------------------------------
    bye_week = False

    try:
        current_nfl_season = str(
            state.get("season") or datetime.datetime.utcnow().year
        )

        # We only calculate the bye for the current NFL season.
        if str(season) == current_nfl_season:
            for check_week in range(1, 19):
                schedule_url = (
                    "https://site.api.espn.com/apis/site/v2/sports/"
                    f"football/nfl/scoreboard?dates={season}"
                    f"&seasontype=2&week={check_week}&limit=100"
                )

                schedule = requests.get(
                    schedule_url,
                    timeout=20
                ).json()

                team_has_game = False

                for event in schedule.get("events", []):
                    event_week = (
                        (event.get("week") or {}).get("number")
                    )

                    if event_week != check_week:
                        continue

                    competition = (
                        event.get("competitions") or [{}]
                    )[0]

                    for competitor in competition.get(
                        "competitors", []
                    ):
                        competitor_team = (
                            competitor.get("team") or {}
                        ).get("abbreviation")

                        if competitor_team == team:
                            team_has_game = True
                            break

                    if team_has_game:
                        break

                if not team_has_game and schedule.get("events") is not None:
                    bye_week = check_week
                    break

    except Exception:
        bye_week = False

    # Prior NFL seasons
    # ------------------------------------------------------------
    # ------------------------------------------------------------
    # Current-season NFL schedule
    # ------------------------------------------------------------
    season_schedule = []

    try:
        if int(season) == int(state.get("season") or season):
            for schedule_week in range(1, 18):
                schedule_url = (
                    "https://site.api.espn.com/apis/site/v2/sports/"
                    f"football/nfl/scoreboard?dates={season}"
                    f"&seasontype=2&week={schedule_week}&limit=100"
                )

                schedule_data = requests.get(
                    schedule_url,
                    timeout=20
                ).json()

                team_game = None

                for event in schedule_data.get("events", []):
                    competition = (
                        event.get("competitions") or [{}]
                    )[0]

                    competitors = competition.get("competitors") or []

                    for competitor in competitors:
                        competitor_team = (
                            competitor.get("team") or {}
                        ).get("abbreviation")

                        if competitor_team != team:
                            continue

                        opponent = next(
                            (
                                (other.get("team") or {}).get(
                                    "abbreviation"
                                )
                                for other in competitors
                                if (other.get("team") or {}).get(
                                    "abbreviation"
                                ) != team
                            ),
                            None
                        )

                        team_game = {
                            "week": schedule_week,
                            "date": event.get("date"),
                            "opponent": opponent,
                            "home": (
                                competitor.get("homeAway") == "home"
                            ),
                        }
                        break

                    if team_game:
                        break

                if team_game:
                    season_schedule.append(team_game)
                else:
                    season_schedule.append({
                        "week": schedule_week,
                        "date": None,
                        "opponent": None,
                        "home": None,
                    })

    except Exception as exc:
        print(
            f"Current-season schedule query failed for "
            f"{name} ({team}, {season}): {exc}"
        )
        season_schedule = []

    prior_years = []

    try:
        current_season_year = int(season)

        # Check the previous three seasons.
        for prior_season in range(
            current_season_year - 1,
            current_season_year - 4,
            -1
        ):
            prior_log = get_espn_player_game_log(
                prior_season,
                name,
                team
            )

            if not prior_log:
                continue

            prior_points = [
                float(row["points"])
                for row in prior_log
                if row.get("points") is not None
            ]

            if not prior_points:
                continue

            prior_years.append({
                "season": prior_season,
                "games": len(prior_log),
                "ppg": sum(prior_points) / len(prior_points),
                "pass_yd": sum(
                    int(row.get("pass_yd") or 0)
                    for row in prior_log
                ),
                "pass_td": sum(
                    int(row.get("pass_td") or 0)
                    for row in prior_log
                ),
                "pass_int": sum(
                    int(row.get("pass_int") or 0)
                    for row in prior_log
                ),
                "rush_att": sum(
                    int(row.get("rush_att") or 0)
                    for row in prior_log
                ),
                "rush_yd": sum(
                    int(row.get("rush_yd") or 0)
                    for row in prior_log
                ),
                "rush_td": sum(
                    int(row.get("rush_td") or 0)
                    for row in prior_log
                ),
                "rec": sum(
                    int(row.get("rec") or 0)
                    for row in prior_log
                ),
                "targets": sum(
                    int(row.get("targets") or 0)
                    for row in prior_log
                ),
                "rec_yd": sum(
                    int(row.get("rec_yd") or 0)
                    for row in prior_log
                ),
                "rec_td": sum(
                    int(row.get("rec_td") or 0)
                    for row in prior_log
                ),
                "fumbles_lost": sum(
                    int(row.get("fumbles_lost") or 0)
                    for row in prior_log
                ),
            })

    except Exception as exc:
        print(
            f"Prior-season game-log query failed for "
            f"{player_id}: {exc}"
        )
        prior_years = []

    return {
        "player": {
            "id": player_id,
            "name": name,
            "position": position,
            "team": team,
            "status": status,
            "headshot": (
                f"https://sleepercdn.com/content/nfl/players/thumb/{player_id}.jpg"
            ),
            "projection": projection,
        },
        "owned_count": len(leagues),
        "leagues": leagues,
        "started": started,
        "benched": benched,
        "season": int(season),
        "ppg": ppg,
        "projection": projection,
        "bye_week": bye_week,
        "game_log": list(reversed(game_log)),
        "prior_years": prior_years,
        "season_schedule": season_schedule,
    }


@app.route("/", methods=['GET'])
def index():
    return index_profile("joeycapps")


@app.route("/<string:profile>/<string:mode>", methods=['GET'])
def index_mode(profile: str, mode: str):
    return index_profile(profile, mode)


@app.route("/<string:profile>/", methods=['GET'])
def index_profile(profile: str, mode: str = 'default'):

    week = int(request.args.get('week')) if 'week' in request.args.keys() else helpers.get_current_week()
    season = int(request.args.get('season')) if 'season' in request.args.keys() else 2026
    matchups = helpers.get_all_matchups(profile, week, mode)

    return render_template(
        'leagues.html',
        matchups=matchups,
        week=week,
        season=season
    )


if __name__ == '__main__':
    app.run()
