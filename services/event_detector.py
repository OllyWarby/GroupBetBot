"""Turns ESPN scoreboard/summary payloads into a normalized match state,
and diffs two states to produce a list of events worth posting to Discord.

Kept as pure functions (no I/O) so they're easy to reason about and test
independently of the network/poller/Discord layers.
"""

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
    # ESPN's specific phase name, e.g. STATUS_SCHEDULED / STATUS_FIRST_HALF /
    # STATUS_HALFTIME / STATUS_SECOND_HALF / STATUS_FULL_TIME. Note there is
    # no generic "STATUS_IN_PROGRESS" for soccer - see `state` below for that.
    status: str
    # ESPN's generic phase bucket: "pre" / "in" / "post". Use this (not
    # `status`) whenever you just need "is the match currently live" - it
    # covers first half, half-time and second half (and extra time/penalties
    # for cup fixtures) without having to enumerate every status name.
    state: str = ""
    display_clock: str = ""
    start_time: datetime | None = None  # scheduled kickoff, UTC, from the scoreboard payload
    # ids of card/goal incidents we've already announced, so summary polls
    # don't re-announce them
    seen_incident_ids: set = field(default_factory=set)


def snapshot_from_scoreboard_event(event: dict, league: str) -> MatchSnapshot | None:
    """Build a MatchSnapshot from one entry in a /scoreboard payload's `events` list."""
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


# Our bet provider auto-settles an accumulator leg as a winner - "early
# payout" - the moment a team goes this many goals clear, regardless of
# what happens for the rest of the match (even a later draw or loss).
EARLY_PAYOUT_GOAL_MARGIN = 2


def team_with_early_payout_lead(new: MatchSnapshot) -> tuple[str, int, int, str] | None:
    """Returns (team_name, team_score, opponent_score, opponent_name) for
    whichever side is currently leading by EARLY_PAYOUT_GOAL_MARGIN+ goals,
    or None if neither side currently qualifies.
    """
    if new.home_score - new.away_score >= EARLY_PAYOUT_GOAL_MARGIN:
        return new.home_name, new.home_score, new.away_score, new.away_name
    if new.away_score - new.home_score >= EARLY_PAYOUT_GOAL_MARGIN:
        return new.away_name, new.away_score, new.home_score, new.home_name
    return None


def next_fixture_from_team_profile(payload: dict, team_name: str) -> tuple[datetime, str, bool] | None:
    """Extract the next scheduled fixture (kickoff time, opponent name, is_home)
    from a `/teams/{id}` payload's `team.nextEvent` list. Returns None if there's
    no upcoming fixture or the payload doesn't match the expected shape.
    """
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
    """Compare the previous poll's snapshot to the new one and return a list
    of human-readable event strings to post. Returns [] if nothing notable
    changed (or this is the first time we've seen the match).
    """
    if old is None:
        return []  # first sighting - nothing to diff against yet

    events = []

    if new.home_score != old.home_score or new.away_score != old.away_score:
        minute = f" {new.display_clock}" if new.display_clock else ""
        # Bold whichever side's score went up so it's clear who the goal
        # was for without needing a separate scorer lookup.
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
        if new.status == "STATUS_HALFTIME":
            events.append(
                f"\U0001F3C1 HT {new.home_name} {new.home_score}-{new.away_score} {new.away_name}"
            )
        elif new.status == "STATUS_FULL_TIME":
            events.append(
                f"\U0001F3C1 FT {new.home_name} {new.home_score}-{new.away_score} {new.away_name}"
            )
        elif new.state == "in" and old.status == "STATUS_SCHEDULED":
            events.append(f"\U0001F7E2 KO {new.home_name} v {new.away_name}")

    return events


def extract_new_card_events(summary_payload: dict, snapshot: MatchSnapshot) -> list[str]:
    """Look at a /summary payload's incident feed for red cards not yet
    announced for this match (tracked via snapshot.seen_incident_ids).
    ESPN's exact key for this varies by competition/coverage level, so this
    defensively checks a couple of likely shapes and skips quietly if
    neither is present rather than raising.
    """
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
