"""Weekly email digest, sent through SNS on an EventBridge cron schedule."""

from datetime import datetime, timedelta, timezone

from shared.common import episodes_by_status, get_budget, notify


def lambda_handler(event, context):
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    ready = [
        e for e in episodes_by_status("READY", limit=100)
        if e.get("updatedAt", "") >= cutoff.isoformat(timespec="seconds")
    ]

    if not ready:
        print("nothing new this week; skipping the digest")
        return {"sent": False}

    lines = [f"{len(ready)} episode(s) transcribed in the last 7 days.", ""]
    for ep in ready:
        lines.append(f"- {ep.get('feedTitle', '')}: {ep.get('title', '')}")
        topics = ep.get("topics") or []
        if topics:
            lines.append(f"    topics: {', '.join(topics[:8])}")
        lines.append("")

    budget = get_budget()
    used_min = budget["usedSeconds"] // 60
    cap_min = budget["capSeconds"] // 60
    lines.append(f"Transcribe usage this month: {used_min}/{cap_min} minutes.")

    notify(f"PodcastLens weekly digest - {len(ready)} new", "\n".join(lines))
    return {"sent": True, "episodes": len(ready)}
