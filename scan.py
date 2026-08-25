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
import time
import uuid
import requests
from datetime import datetime, timezone

STATE_FILE = "data/state.json"
RESULTS_FILE = "data/results.json"

# --- Tune these to match your team's criteria ---
CCU_MIN = 3                # just a sanity floor to skip dead/empty games
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


def get_with_retry(url, params, max_retries=3):
    """GET with backoff on 429s — flat delays weren't enough since Roblox's
    limit here is stricter than expected, especially from shared CI IPs."""
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            if r.status_code == 429:
                wait = 8 * (attempt + 1)
                print(f"    rate limited, waiting {wait}s before retry {attempt + 1}/{max_retries}")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError:
            if attempt == max_retries - 1:
                raise
    raise Exception("exhausted retries")
    """Recursively pull any universeId values out of an arbitrarily-shaped
    JSON response. Used because these endpoints are undocumented and their
    exact structure can vary between categories/search results."""
    if found is None:
        found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == "universeid" and isinstance(v, int):
                found.add(v)
            else:
                extract_universe_ids(v, found)
    elif isinstance(obj, list):
        for item in obj:
            extract_universe_ids(item, found)
    return found


GENRE_SEARCH_TERMS = ["simulator", "tycoon", "obby"]


def get_seed_universe_ids():
    """Pull candidate universe IDs from every Discover category, plus
    genre-targeted searches for simulator/tycoon-style games."""
    ids = set()
    session_id = str(uuid.uuid4())

    # --- 1. Every Discover category, not just "new"-sounding ones ---
    try:
        r = requests.get(
            "https://apis.roblox.com/explore-api/v1/get-sorts",
            params={"sessionId": session_id},
            headers=HEADERS, timeout=15,
        )
        r.raise_for_status()
        sorts = r.json().get("sorts", [])
        print(f"Available sort names: {[s.get('sortDisplayName') for s in sorts]}")

        for s in sorts:
            sort_id = s.get("sortId")
            if not sort_id:
                continue
            try:
                r2 = requests.get(
                    "https://apis.roblox.com/explore-api/v1/get-sort-content",
                    params={"sessionId": session_id, "sortId": sort_id},
                    headers=HEADERS, timeout=15,
                )
                r2.raise_for_status()
                found = extract_universe_ids(r2.json())
                print(f"  sort '{s.get('sortDisplayName')}': {len(found)} ids")
                ids |= found
            except Exception as e:
                print(f"  sort '{s.get('sortDisplayName')}' failed (non-fatal): {e}")
            time.sleep(1)
    except Exception as e:
        print(f"Sort-based seed fetch failed entirely this run (non-fatal): {e}")

    # --- 2. Genre-targeted search, since discover categories alone won't
    #        reliably surface simulator/tycoon games specifically ---
    for term in GENRE_SEARCH_TERMS:
        try:
            r3 = get_with_retry(
                "https://apis.roblox.com/search-api/omni-search",
                params={"SearchQuery": term, "SessionId": session_id},
            )
            found = extract_universe_ids(r3.json())
            print(f"  search '{term}': {len(found)} ids")
            ids |= found
        except Exception as e:
            print(f"  search '{term}' failed (non-fatal): {e}")
        time.sleep(3)

    print(f"Seeded {len(ids)} total candidate universe IDs this run")
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
    if ccu < CCU_MIN:
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


def compute_trend(history):
    """Score the overall shape of a game's CCU history — is this a real
    sustained/accelerating climb (like the Rotrends example), flat, or
    declining? Uses the whole curve, not just two endpoints, so a single
    noisy snapshot doesn't swing the result.

    This isn't a real AI judgment call — it's a transparent heuristic
    standing in for one, since running an actual model per-game would cost
    real money via the Anthropic API. It targets the same thing though:
    curve shape, not a fixed CCU number.
    """
    MIN_POINTS = 4
    ccus = [h["ccu"] for h in history]
    n = len(ccus)

    if n < MIN_POINTS:
        return {"score": None, "label": "insufficient_data", "growth_ratio": None}

    # Overall slope via simple linear regression (no numpy dependency)
    xs = list(range(n))
    xbar = sum(xs) / n
    ybar = sum(ccus) / n
    num = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ccus))
    den = sum((x - xbar) ** 2 for x in xs) or 1
    slope = num / den                    # CCU change per snapshot
    relative_slope = slope / max(ybar, 1)  # normalized so it's comparable across game sizes

    # Compare the average of the first half vs second half — smoother
    # than just comparing endpoints, less swayed by one lucky/unlucky snapshot
    half = n // 2
    early_avg = sum(ccus[:half]) / half
    late_avg = sum(ccus[half:]) / (n - half)
    growth_ratio = late_avg / max(early_avg, 1)

    # Combine both signals into a 0-100 score, centered at 50 (flat)
    score = 50
    score += min(max(relative_slope * 500, -40), 40)
    score += min(max((growth_ratio - 1) * 40, -30), 40)
    score = round(max(0, min(100, score)), 1)

    if score >= 70:
        label = "strong_upward"
    elif score >= 55:
        label = "mild_upward"
    elif score <= 30:
        label = "declining"
    else:
        label = "flat_or_mixed"

    return {"score": score, "label": label, "growth_ratio": round(growth_ratio, 2)}


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
        entry["last_seen"] = now_iso

        trend = compute_trend(entry["history"])
        entry["trend_score"] = trend["score"]
        entry["trend_label"] = trend["label"]
        entry["growth_ratio"] = trend["growth_ratio"]

        known_games[uid] = entry
        matched += 1

    state["known_games"] = known_games
    save_state(state)

    # Flat results file for the dashboard to consume — best trend shape first,
    # with undetermined (not enough history yet) games sorted after scored ones
    results_list = sorted(
        known_games.values(),
        key=lambda g: (g.get("trend_score") is not None, g.get("trend_score") or 0),
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
