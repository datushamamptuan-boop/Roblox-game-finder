"""
Roblox Game Scout — finds new, small, "pre-algorithm" games matching a CCU/visits window.

How it works:
1. Pulls a batch of freshly-created games from Roblox's chronological "New" feed
   (this is a feed sorted by creation date, not popularity, so genuinely new/small
   games show up here even with almost no players).
2. Looks up live stats (CCU, visits, description, creation date) for each one.
3. Filters to your target window and saves matches, tracking CCU/visits over time
   so you can see growth trend across runs.

Notes:
- Roblox's "New" feed endpoint is undocumented and could change or occasionally
  return nothing — that's expected and non-fatal, the script just skips that round.
- Results accumulate in data/results.json across every run, so history builds up
  over hours/days even though each individual run is a small snapshot.
"""

import json
import os
import uuid
import requests
from datetime import datetime, timezone

STATE_FILE = "data/state.json"
RESULTS_FILE = "data/results.json"

# --- Tune these to match your team's criteria ---
CCU_MIN = 10
CCU_MAX_HARD = 2000        # absolute ceiling (early-algorithm allowance)
CCU_SMALL_CEILING = 200    # "small, pre-algorithm" ceiling
VISITS_MAX = 200_000
MAX_AGE_DAYS = 60          # "less than 2 months old"
HISTORY_CAP = 300          # max snapshots kept per game

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"known_games": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_seed_universe_ids():
    """Pull candidate universe IDs from Roblox's chronological 'New' discovery feed."""
    ids = set()
    session_id = str(uuid.uuid4())
    try:
        r = requests.get(
            "https://apis.roblox.com/explore-api/v1/get-sorts",
            params={"sessionId": session_id},
            headers=HEADERS, timeout=15,
        )
        r.raise_for_status()
        sorts = r.json().get("sorts", [])
        print(f"Available sort names: {[s.get('sortDisplayName') for s in sorts]}")

        # Match against the real category names Roblox uses
        candidate_sorts = [
            s for s in sorts
            if any(kw in (s.get("sortDisplayName") or "").lower() for kw in ["up-and-coming", "up and coming", "trending"])
        ]

        for s in candidate_sorts:
            try:
                r2 = requests.get(
                    "https://apis.roblox.com/explore-api/v1/get-sort-content",
                    params={"sessionId": session_id, "sortId": s["sortId"]},
                    headers=HEADERS, timeout=15,
                )
                r2.raise_for_status()
                for game in r2.json().get("games", []):
                    uid = game.get("universeId")
                    if uid:
                        ids.add(uid)
            except Exception as e:
                print(f"  sort '{s.get('sortDisplayName')}' failed (non-fatal): {e}")

    except Exception as e:
        print(f"Seed fetch failed entirely this run (non-fatal): {e}")

    print(f"Seeded {len(ids)} candidate universe IDs this run")
    return ids


def get_games_batch(universe_ids):
    """Roblox lets you batch multiple universeIds into one stats call."""
    if not universe_ids:
        return []
    results = []
    ids_list = list(universe_ids)
    # Batch in chunks of 50 to stay well under any implicit limits
    for i in range(0, len(ids_list), 50):
        chunk = ids_list[i:i + 50]
        ids_str = ",".join(str(x) for x in chunk)
        try:
            r = requests.get(
                "https://games.roblox.com/v1/games",
                params={"universeIds": ids_str},
                headers=HEADERS, timeout=15,
            )
            r.raise_for_status()
            results.extend(r.json().get("data", []))
        except Exception as e:
            print(f"  batch fetch failed for chunk starting at {i} (non-fatal): {e}")
    return results


def passes_filters(game):
    ccu = game.get("playing", 0) or 0
    visits = game.get("visits", 0) or 0

    if visits > VISITS_MAX:
        return False
    if not (CCU_MIN <= ccu <= CCU_MAX_HARD):
        return False

    created = game.get("created")
    if created:
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created_dt).days
            if age_days > MAX_AGE_DAYS:
                return False
        except ValueError:
            pass  # if we can't parse the date, don't filter it out on that basis

    return True


def main():
    state = load_state()
    known_games = state.get("known_games", {})

    seed_ids = get_seed_universe_ids()
    games = get_games_batch(seed_ids)

    now_iso = datetime.now(timezone.utc).isoformat()
    matched = 0

    for game in games:
        if not passes_filters(game):
            continue

        uid = str(game.get("id"))
        ccu = game.get("playing", 0) or 0
        visits = game.get("visits", 0) or 0

        entry = known_games.get(uid, {
            "name": game.get("name"),
            "universeId": uid,
            "rootPlaceId": game.get("rootPlaceId"),
            "description": (game.get("description") or "")[:400],
            "genre": game.get("genre"),
            "created": game.get("created"),
            "creatorName": (game.get("creator") or {}).get("name"),
            "creatorType": (game.get("creator") or {}).get("type"),
            "history": [],
        })

        entry["history"].append({"t": now_iso, "ccu": ccu, "visits": visits})
        entry["history"] = entry["history"][-HISTORY_CAP:]
        entry["latest_ccu"] = ccu
        entry["latest_visits"] = visits
        entry["small_pre_algorithm"] = ccu <= CCU_SMALL_CEILING
        entry["last_seen"] = now_iso

        known_games[uid] = entry
        matched += 1

    state["known_games"] = known_games
    save_state(state)

    # Flat results file for the dashboard to consume, newest-matched first
    results_list = sorted(
        known_games.values(),
        key=lambda g: g.get("last_seen", ""),
        reverse=True,
    )
    os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump({
            "generated_at": now_iso,
            "count": len(results_list),
            "games": results_list,
        }, f, indent=2)

    print(f"Run complete. {matched} games matched this round. "
          f"{len(results_list)} total tracked games.")


if __name__ == "__main__":
    main()
