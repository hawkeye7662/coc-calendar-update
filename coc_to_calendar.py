from __future__ import annotations
import os
import json
import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytz
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account

# Full calendar access
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Your timezone for events
LOCAL_TZ = pytz.timezone("Asia/Kolkata")

# Player tag to village name mapping
VILLAGE_NAMES = {
    "#YJP00J80": "Hawk Eye",
    "#GLRJV8JPC": "bruh",
    "#Y0CG8QJVL": "Valiant Warrior",
    "#L9RVVQ2QU": "abcjjska"
}

# Mapping from keys in your CoC JSON -> section names in static_data.json
SECTION_TO_STATIC = {
    "buildings": "buildings",
    "buildings2": "buildings",
    "traps": "traps",
    "traps2": "traps",
    "units": "troops",
    "units2": "troops",
    "siege_machines": "siege_machines",
    "heroes": "heroes",
    "heroes2": "heroes",
    "spells": "spells",
    "pets": "pets",
    "guardians": "guardians",
}


def get_calendar_service():
    """
    Authenticate using Workload Identity Federation for GitHub Actions,
    or service account file for local development.
    """
    # Check if running in GitHub Actions with Workload Identity Federation
    if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
        # Workload Identity Federation - uses default credentials
        from google.auth import default
        creds, _ = default(scopes=SCOPES)
    elif os.path.exists("service-account.json"):
        # Running locally with service account
        creds = service_account.Credentials.from_service_account_file(
            "service-account.json",
            scopes=SCOPES
        )
    else:
        raise FileNotFoundError(
            "No credentials found. "
            "Either set up Workload Identity Federation or provide service-account.json file."
        )

    return build("calendar", "v3", credentials=creds)


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_name_maps(static_data_path: str) -> Dict[str, Dict[int, str]]:
    """
    Load ID->name maps for each relevant static section.

    Returns a dict like:
    {
        "buildings": {1000000: "Army Camp", ...},
        "troops": { ... },
        ...
    }
    """
    data = load_json(static_data_path)
    name_maps: Dict[str, Dict[int, str]] = {}

    # We only care about the sections that SECTION_TO_STATIC might point to
    static_sections = set(SECTION_TO_STATIC.values())

    for section in static_sections:
        mapping: Dict[int, str] = {}
        for entry in data.get(section, []):
            _id = entry.get("_id")
            name = entry.get("name")
            if _id is not None and name:
                mapping[int(_id)] = str(name)
        name_maps[section] = mapping

    return name_maps


def make_label(
    json_section: str,
    item: Dict[str, Any],
    name_maps: Dict[str, Dict[int, str]],
) -> str:
    """
    Build a human-readable label like 'BT (lvl 13)'.
    """
    static_section = SECTION_TO_STATIC.get(json_section)
    id_map = name_maps.get(static_section, {}) if static_section else {}

    data_id = item.get("data")
    lvl = item.get("lvl")

    base_name = id_map.get(data_id)
    if base_name is None:
        # Fallback if not found in static_data.json
        base_name = f"{static_section or json_section} {data_id}"
    
    # Only replace names that contain "bomb" (case-insensitive)
    if "bomb" in base_name.lower():
        if base_name == "Bomb Tower":
            base_name = "BT"
        elif base_name == "Bomb":
            base_name = "B"

    if lvl is not None:
        return f"{base_name} (lvl {lvl})"
    return base_name


def should_add_alarm(finish_dt: datetime) -> bool:
    """
    Determine if 'clashofclans' keyword should be added based on:
    1. Time must be between 9 AM and 11:59 PM
    2. Not during work hours (11 AM - 4:30 PM on Tuesday/Wednesday)
    """
    hour = finish_dt.hour
    minute = finish_dt.minute
    weekday = finish_dt.weekday()  # 0=Monday, 1=Tuesday, 2=Wednesday, ...
    
    # Check if between 9 AM and 11:59 PM
    if not (9 <= hour <= 23):
        return False
    
    # Check if it's Tuesday (1) or Wednesday (2) during work hours
    if weekday in [1, 2]:  # Tuesday or Wednesday
        # Work hours: 11:00 AM to 4:30 PM
        if hour == 11 or hour == 12 or hour == 13 or hour == 14 or hour == 15:
            return False
        if hour == 16 and minute < 30:  # Before 4:30 PM
            return False
    
    return True


def delete_player_events(service, calendar_id: str, player_tag: str):
    """
    Delete all events for a specific player tag.
    Events are identified by having the player tag in the event ID or description.
    """
    try:
        # Get all events (we'll filter by our marker)
        events_result = service.events().list(
            calendarId=calendar_id,
            maxResults=2500,  # Adjust if you have more events
            singleEvents=True,
        ).execute()
        
        events = events_result.get('items', [])
        deleted_count = 0
        
        for event in events:
            event_id = event.get('id', '')
            summary = event.get('summary', '')
            
            # Check if this event belongs to this player
            # We'll mark events by including player tag in extended properties
            extended_props = event.get('extendedProperties', {}).get('private', {})
            if extended_props.get('player_tag') == player_tag:
                try:
                    service.events().delete(
                        calendarId=calendar_id,
                        eventId=event_id
                    ).execute()
                    deleted_count += 1
                except HttpError:
                    pass  # Event might already be deleted
        
        print(f"Deleted {deleted_count} events for player {player_tag}")
        
    except HttpError as e:
        print(f"Error deleting events: {e}")


def create_or_update_event(
    service,
    calendar_id: str,
    event_body: dict,
    event_label: str,
):
    """
    Create a new event.
    """
    try:
        service.events().insert(
            calendarId=calendar_id,
            body=event_body
        ).execute()
        print(f"Created: {event_label}")
    except HttpError as e:
        print(f"Error creating event {event_label}: {e}")


def create_upgrade_events(
    coc_path: str,
    static_data_path: str,
    calendar_id: str | None = None,
):
    if calendar_id is None:
        calendar_id = os.environ.get("CALENDAR_ID", "primary")
    state = load_json(coc_path)
    name_maps = load_name_maps(static_data_path)
    service = get_calendar_service()

    base_ts = state["timestamp"]
    player_tag = state.get("tag", "Unknown")
    
    # Get village name or fall back to player tag
    village_name = VILLAGE_NAMES.get(player_tag, player_tag)
    
    # Delete all existing events for this player
    print(f"Deleting existing events for {village_name}...")
    delete_player_events(service, calendar_id, player_tag)
    print(f"Creating new events for {village_name}...")

    for json_section in SECTION_TO_STATIC.keys():
        items: List[Dict[str, Any]] = state.get(json_section, [])
        if not items:
            continue

        for item in items:
            timer = item.get("timer")
            if not timer:
                # No ongoing upgrade / nothing to schedule
                continue

            finish_ts = int(base_ts) + int(timer)

            # Convert Unix timestamp -> local timezone-aware datetime
            finish_dt_local = (
                datetime.utcfromtimestamp(finish_ts)
                .replace(tzinfo=pytz.utc)
                .astimezone(LOCAL_TZ)
            )
            end_dt_local = finish_dt_local + timedelta(minutes=5)

            label = make_label(json_section, item, name_maps)
            
            summary = f"{village_name}: {label} upgrade finished"
            
            # Determine if we should add alarm keyword
            description = ""
            if should_add_alarm(finish_dt_local):
                description = "clashofclans"

            event = {
                "summary": summary,
                "description": description,
                "start": {
                    "dateTime": finish_dt_local.isoformat(),
                    "timeZone": "Asia/Kolkata",
                },
                "end": {
                    "dateTime": end_dt_local.isoformat(),
                    "timeZone": "Asia/Kolkata",
                },
                "reminders": {
                    "useDefault": False,
                    "overrides": []
                },
                "extendedProperties": {
                    "private": {
                        "player_tag": player_tag
                    }
                }
            }

            create_or_update_event(
                service,
                calendar_id,
                event,
                f"[{json_section}] {summary}"
            )
            
            # If upgrade finishes between 12 AM and 8:59 AM, create a 9 AM alarm event
            if 0 <= finish_dt_local.hour < 9:
                # Create 9 AM event on the same day
                alarm_dt = finish_dt_local.replace(hour=9, minute=0, second=0, microsecond=0)
                alarm_end_dt = alarm_dt + timedelta(minutes=5)
                
                # Check if 9 AM falls during work hours (Tuesday/Wednesday)
                if should_add_alarm(alarm_dt):
                    alarm_summary = f"{village_name}: {label} finished (alarm)"
                    
                    alarm_event = {
                        "summary": alarm_summary,
                        "description": "clashofclans",
                        "start": {
                            "dateTime": alarm_dt.isoformat(),
                            "timeZone": "Asia/Kolkata",
                        },
                        "end": {
                            "dateTime": alarm_end_dt.isoformat(),
                            "timeZone": "Asia/Kolkata",
                        },
                        "reminders": {
                            "useDefault": False,
                            "overrides": []
                        },
                        "extendedProperties": {
                            "private": {
                                "player_tag": player_tag
                            }
                        }
                    }
                    
                    create_or_update_event(
                        service,
                        calendar_id,
                        alarm_event,
                        f"[{json_section}] {alarm_summary}"
                    )


if __name__ == "__main__":
    cal_id = os.environ.get("CALENDAR_ID", "primary")
    create_upgrade_events(
        coc_path="coc_state.json",
        static_data_path="static_data.json",
        calendar_id=cal_id,
    )
