from __future__ import annotations

from datetime import date, timedelta

import discord

WEEKDAYS = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")


def _safe(value: object, limit: int = 100) -> str:
    text = " ".join(str(value).split())[:limit]
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def _week_label(day: date, monday: date) -> str:
    offset = (day - monday).days // 7
    if offset == 0:
        return "이번 주"
    if offset == 1:
        return "다음 주"
    if offset == 2:
        return "다다음 주"
    return f"{offset}주 후" if offset > 2 else ""


def build_schedule_embed(
    config: dict,
    rows: list[dict],
    holidays: list[dict],
    today: date,
    names: dict[int, str],
    *,
    title: str = "🗓️ 세미나 일정",
) -> discord.Embed:
    """Render the supplied date range with this calendar week's speaker highlighted.

    Names remain plain display text. The caller separately controls the one
    presenter mention used in an automatic notification's message content.
    """
    monday = today - timedelta(days=today.weekday())
    start = _safe(config["seminar_time"]) if config.get("seminar_time") else ""
    items = {date.fromisoformat(row["seminar_date"]): row for row in rows}
    # A holiday takes precedence over a stale scheduled occurrence on that day.
    items.update({date.fromisoformat(row["seminar_date"]): None for row in holidays})
    lines = []
    for day, row in sorted(items.items()):
        week = _week_label(day, monday)
        if row is None:
            line = " · ".join(filter(None, (week, day.isoformat(), "💤 휴강")))
        else:
            presenter_id = row["presenter_id"]
            name = _safe(names.get(presenter_id) or f"발표자 {presenter_id} (이름 확인 불가)")
            if day < today:
                name += " (지난 발표)"
            line = " · ".join(
                filter(
                    None,
                    ((week + " 발표") if week == "이번 주" else week, day.isoformat(), start, name),
                )
            )
        if week == "이번 주":
            line = f"🔵 **{line}**"
        lines.append(line)

    # Keep complete Markdown lines inside Discord's embed description limit.
    selected = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > 4050:
            selected.append("… 이후 일정은 표시 범위를 줄여 확인해주세요.")
            break
        selected.append(line)
        used += len(line) + 1
    embed = discord.Embed(
        title=_safe(title, 256),
        description="\n".join(selected) or "아직 등록된 일정이 없습니다.",
        color=0x5B8DEF,
    )
    weekly = f"매주 {WEEKDAYS[config['weekday']]}"
    weekly += f" {start}" if start else " · 시작 시간 미설정"
    weekly += f" ({_safe(config['timezone'])})"
    stages = [f"일정 안내 {_safe(config['morning'])}"]
    if config.get("before_reminder"):
        before_minutes = config.get("before_minutes")
        before_label = f"시작 {before_minutes}분 전" if before_minutes is not None else "시작 전"
        stages.append(f"{before_label} {_safe(config['before_reminder'])}")
    after_minutes = config.get("after_minutes")
    after_label = f"시작 {after_minutes}분 후" if after_minutes is not None else "종료 후"
    stages.append(f"{after_label} {_safe(config['evening'])}")
    embed.set_footer(text=weekly + " · " + " / ".join(stages))
    return embed
