from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MatchSnapshot:
    event_id: str
    league: str
    home_name: str
    away_name: str
    home_score: int
    away_score: int
    status: str
    state: str = ""
    display_clock: str = ""
    start_time: datetime | None = None
    seen_incident_ids: set = field(default_factory=set)


def snapshot_from_scoreboard_event(event: dict, league: str) -> MatchSnapshot | None:
    try:
        competition = event["competitions"][0]
        competitors = competition["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")
        status_type = event["status"]["type"]
        status = status_type["name"]
        state = status_type.get("state", "")
        clock = event["status"].get("displayClock", "")

        start_time = None
        raw_date = event.get("date")
        if raw_date:
            try:
                start_time = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                start_time = None

        return MatchSnapshot(
            event_id=str(event["id"]),
            league=league,
            home_name=home["team"]["displayName"],
            away_name=away["team"]["displayName"],
            home_score=int(home.get("score", 0) or 0),
            away_score=int(away.get("score", 0) or 0),
            status=status,
            state=state,
            display_clock=clock,
            start_time=start_time,
        )
    except (KeyError, IndexError, StopIteration, ValueError):
        return None


EARLY_PAYOUT_GOAL_MARGIN = 2


def team_with_early_payout_lead(new: MatchSnapshot) -> tuple[str, int, int, str] | None:
    if new.home_score - new.away_score >= EARLY_PAYOUT_GOAL_MARGIN:
        return new.home_name, new.home_score, new.away_score, new.away_name
    if new.away_score - new.home_score >= EARLY_PAYOUT_GOAL_MARGIN:
        return new.away_name, new.away_score, new.home_score, new.home_name
    return None


def is_winning_leg(team_score: int, opp_score: int, locked_win: bool) -> bool:
    return locked_win or team_score > opp_score


def next_fixture_from_team_profile(payload: dict, team_name: str) -> tuple[datetime, str, bool] | None:
    try:
        next_events = payload.get("team", {}).get("nextEvent", [])
        if not next_events:
            return None
        competition = next_events[0]["competitions"][0]
        competitors = competition["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")
        is_home = home["team"]["displayName"] == team_name
        opponent = away["team"]["displayName"] if is_home else home["team"]["displayName"]
        kickoff = datetime.fromisoformat(next_events[0]["date"].replace("Z", "+00:00"))
        return kickoff, opponent, is_home
    except (KeyError, IndexError, StopIteration, ValueError):
        return None


def diff_snapshots(old: MatchSnapshot | None, new: MatchSnapshot) -> list[str]:
    if old is None:
        return []

    events = []

    if new.home_score != old.home_score or new.away_score != old.away_score:
        minute = f" {new.display_clock}" if new.display_clock else ""
        if new.home_score > old.home_score:
            home_label, away_label = f"**{new.home_name}**", new.away_name
        elif new.away_score > old.away_score:
            home_label, away_label = new.home_name, f"**{new.away_name}**"
        else:
            home_label, away_label = new.home_name, new.away_name
        events.append(
            f"\u26bd{minute} {home_label} {new.home_score}-{new.away_score} {away_label}"
        )

    if new.status != old.status:
        if new.status == "STATUS_FULL_TIME":
            events.append(
                f"\U0001F3C1 FT {new.home_name} {new.home_score}-{new.away_score} {new.away_name}"
            )
        elif new.state == "in" and old.status == "STATUS_SCHEDULED":
            events.append(f"\U0001F7E2 KO {new.home_name} v {new.away_name}")

    return events


def extract_new_card_events(summary_payload: dict, snapshot: MatchSnapshot) -> list[str]:
    events = []
    incidents = (
        summary_payload.get("details")
        or summary_payload.get("keyEvents")
        or []
    )
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        incident_id = str(incident.get("id", ""))
        if not incident_id or incident_id in snapshot.seen_incident_ids:
            continue

        incident_type = (incident.get("type", {}) or {}).get("text", "").lower()
        if "red card" not in incident_type:
            continue

        athlete = ""
        for participant in incident.get("athletesInvolved", []) or []:
            athlete = participant.get("displayName", "")
            break

        team_name = (incident.get("team", {}) or {}).get("displayName", "")
        clock = (incident.get("clock", {}) or {}).get("displayValue", "")

        who = f"{athlete} ({team_name})" if athlete else team_name or "a player"
        events.append(f"\U0001F7E5 **Red card** at {clock}: {who}")
        snapshot.seen_incident_ids.add(incident_id)

    return events
