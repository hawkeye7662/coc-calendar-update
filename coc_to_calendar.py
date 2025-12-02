from __future__ import annotations
import os
import json
import pickle
import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytz
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# Full calendar access
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Your timezone for events
LOCAL_TZ = pytz.timezone("Asia/Kolkata")

# Player tag to village name mapping
VILLAGE_NAMES = {
    "#YJP00J80": "Hawk Eye",
    "#GLRJV8JPC": "bruh",
    "#Y0CG8QJVL": "Valiant Warrior",
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
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open("token.pickle", "wb") as token:
            pickle.dump(creds, token)

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


def make_event_id_base(
    player_tag: str,
    json_section: str,
    item: Dict[str, Any],
    suffix: str = "",
) -> str:
    """
    Deterministic event ID based on upgrade identity (not timestamp).
    This allows us to update the same event when timers change.
    """
    raw = f"{player_tag}-{json_section}-{item.get('data')}-{item.get('lvl')}{suffix}"
    return hashlib.md5(raw.encode()).hexdigest()


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


def create_or_update_event(
    service,
    calendar_id: str,
    event_id: str,
    event_body: dict,
    event_label: str,
):
    """
    Try to update an existing event, or create it if it doesn't exist.
    """
    try:
        # Try to get the existing event
        existing = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        
        # Update the existing event
        service.events().update(
            calendarId=calendar_id,
            eventId=event_id,
            body=event_body
        ).execute()
        print(f"Updated: {event_label}")
        
    except HttpError as e:
        if e.resp.status == 404:
            # Event doesn't exist, create it
            try:
                service.events().insert(
                    calendarId=calendar_id,
                    body=event_body
                ).execute()
                print(f"Created: {event_label}")
            except HttpError as insert_error:
                if insert_error.resp.status == 409:
                    print(f"Already exists: {event_label}")
                else:
                    raise
        else:
            raise


def create_upgrade_events(
    coc_path: str,
    static_data_path: str,
    calendar_id: str = "primary",
):
    state = load_json(coc_path)
    name_maps = load_name_maps(static_data_path)
    service = get_calendar_service()

    base_ts = state["timestamp"]
    player_tag = state.get("tag", "Unknown")
    
    # Get village name or fall back to player tag
    village_name = VILLAGE_NAMES.get(player_tag, player_tag)

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
            
            # Main event - ID based on upgrade identity (not timestamp)
            event_id = make_event_id_base(player_tag, json_section, item)
            summary = f"{village_name}: {label} upgrade finished"
            
            # Determine if we should add alarm keyword
            description = ""
            if should_add_alarm(finish_dt_local):
                description = "clashofclans"

            event = {
                "id": event_id,
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
            }

            create_or_update_event(
                service,
                calendar_id,
                event_id,
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
                    alarm_event_id = make_event_id_base(player_tag, json_section, item, "-9am")
                    alarm_summary = f"{village_name}: {label} finished (alarm)"
                    
                    alarm_event = {
                        "id": alarm_event_id,
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
                    }
                    
                    create_or_update_event(
                        service,
                        calendar_id,
                        alarm_event_id,
                        alarm_event,
                        f"[{json_section}] {alarm_summary}"
                    )


if __name__ == "__main__":
    create_upgrade_events(
        coc_path="coc_state.json",
        static_data_path="static_data.json",
        calendar_id="primary",
    )