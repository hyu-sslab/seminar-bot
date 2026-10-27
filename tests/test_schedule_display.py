from __future__ import annotations

import unittest
from datetime import date

from seminar_bot.schedule_display import build_schedule_embed

CONFIG = {
    "weekday": 3,
    "seminar_time": "16:00",
    "timezone": "Asia/Seoul",
    "morning": "12:00",
    "before_reminder": "15:45",
    "evening": "17:00",
    "before_minutes": 15,
    "after_minutes": 60,
}


def session(day: str, presenter: int = 101) -> dict:
    return {"seminar_date": day, "presenter_id": presenter}


class ScheduleDisplayTests(unittest.TestCase):
    def render(self, rows, *, today=date(2026, 10, 27), holidays=None, names=None, **kwargs):
        return build_schedule_embed(
            CONFIG,
            rows,
            holidays or [],
            today,
            names or {101: "발표자 하나", 102: "발표자 둘"},
            **kwargs,
        )

    def test_four_calendar_weeks_emphasize_only_the_current_speaker(self):
        embed = self.render(
            [
                session("2026-11-19"),
                session("2026-10-29"),
                session("2026-11-12"),
                session("2026-11-05"),
            ]
        )
        lines = embed.description.splitlines()
        self.assertEqual(lines[0], "🔵 **이번 주 발표 · 2026-10-29 · 16:00 · 발표자 하나**")
        self.assertIn("다음 주 · 2026-11-05", lines[1])
        self.assertIn("다다음 주 · 2026-11-12", lines[2])
        self.assertIn("3주 후 · 2026-11-19", lines[3])
        self.assertEqual(sum(line.startswith("🔵 **") for line in lines), 1)

    def test_week_boundaries_follow_monday_and_keep_a_past_current_week_distinct(self):
        rows = [session("2026-10-29"), session("2026-11-05", 102)]
        sunday = self.render(rows, today=date(2026, 11, 1)).description.splitlines()
        self.assertEqual(
            sunday[0], "🔵 **이번 주 발표 · 2026-10-29 · 16:00 · 발표자 하나 (지난 발표)**"
        )
        self.assertIn("다음 주", sunday[1])
        monday = self.render(rows, today=date(2026, 11, 2)).description.splitlines()
        self.assertNotIn("이번 주", monday[0])
        self.assertIn("지난 발표", monday[0])
        self.assertEqual(monday[1], "🔵 **이번 주 발표 · 2026-11-05 · 16:00 · 발표자 둘**")

    def test_today_is_current_without_being_marked_as_a_past_talk(self):
        text = self.render([session("2026-10-29")], today=date(2026, 10, 29)).description
        self.assertIn("이번 주 발표", text)
        self.assertNotIn("지난 발표", text)

    def test_holiday_overrides_a_stale_presenter_without_losing_other_weeks(self):
        embed = self.render(
            [session("2026-10-29"), session("2026-11-05", 102)],
            holidays=[{"seminar_date": "2026-10-29"}],
        )
        lines = embed.description.splitlines()
        self.assertEqual(lines[0], "🔵 **이번 주 · 2026-10-29 · 💤 휴강**")
        self.assertNotIn("발표자 하나", embed.description)
        self.assertIn("발표자 둘", lines[1])
        self.assertEqual(embed.description.count("2026-10-29"), 1)

    def test_untrusted_display_names_cannot_add_schedule_lines_or_live_mentions(self):
        name = "**Injected**\n@everyone <@123456789012345678> [link](https://example.test)"
        embed = self.render([session("2026-10-29")], names={101: name})
        self.assertEqual(len(embed.description.splitlines()), 1)
        self.assertIn(r"\*\*Injected\*\*", embed.description)
        self.assertNotIn("@everyone", embed.description)
        self.assertNotIn("<@123456789012345678>", embed.description)
        self.assertNotRegex(embed.description, r"(?<!\\)\[link\]\(")

    def test_footer_has_fixed_seminar_time_and_all_three_notification_times(self):
        embed = self.render([session("2026-10-29")], title="📢 앞으로 4주 세미나 일정")
        self.assertEqual(embed.title, "📢 앞으로 4주 세미나 일정")
        self.assertEqual(
            embed.footer.text,
            "매주 목요일 16:00 (Asia/Seoul) · 일정 안내 12:00 / 시작 15분 전 15:45 / 시작 60분 후 17:00",
        )
        legacy = build_schedule_embed(
            {**CONFIG, "before_reminder": None, "before_minutes": None, "after_minutes": None},
            [],
            [],
            date(2026, 10, 27),
            {},
        )
        self.assertNotIn("시작 전", legacy.footer.text)
        self.assertNotIn("None", legacy.footer.text)
        self.assertIn("종료 후 17:00", legacy.footer.text)

    def test_custom_minute_offsets_show_the_resolved_times_after_a_start_time_change(self):
        changed = {
            **CONFIG,
            "seminar_time": "14:30",
            "before_minutes": 30,
            "after_minutes": 90,
            "before_reminder": "14:00",
            "evening": "16:00",
        }
        embed = build_schedule_embed(
            changed, [session("2026-10-29")], [], date(2026, 10, 27), {101: "발표자 하나"}
        )
        self.assertIn("14:30 · 발표자 하나", embed.description)
        self.assertEqual(
            embed.footer.text,
            "매주 목요일 14:30 (Asia/Seoul) · 일정 안내 12:00 / 시작 30분 전 14:00 / 시작 90분 후 16:00",
        )
        self.assertNotIn("종료 후", embed.footer.text)

    def test_legacy_absolute_alarm_times_remain_visible_without_minute_offsets(self):
        config = {**CONFIG, "before_minutes": None, "after_minutes": None}
        embed = build_schedule_embed(config, [], [], date(2026, 10, 27), {})
        self.assertIn("시작 전 15:45 / 종료 후 17:00", embed.footer.text)
        self.assertNotIn("None분", embed.footer.text)

    def test_missing_name_is_plain_text_and_input_collections_are_not_mutated(self):
        rows = [session("2026-10-29", 103)]
        holidays = [{"seminar_date": "2026-11-05"}]
        original = repr((CONFIG, rows, holidays))
        embed = self.render(rows, holidays=holidays)
        self.assertIn("발표자 103 (이름 확인 불가)", embed.description)
        self.assertNotIn("<@", embed.description)
        self.assertEqual(repr((CONFIG, rows, holidays)), original)

    def test_long_display_text_stays_within_discord_description_limit_without_partial_rows(self):
        rows = [session(f"2026-11-{day:02}") for day in range(1, 29)]
        embed = self.render(rows, names={101: "*" * 100})
        self.assertLessEqual(len(embed.description), 4096)
        self.assertTrue(embed.description.endswith("… 이후 일정은 표시 범위를 줄여 확인해주세요."))
        self.assertTrue(
            all(
                line.endswith("**")
                for line in embed.description.splitlines()
                if line.startswith("🔵")
            )
        )


if __name__ == "__main__":
    unittest.main()
