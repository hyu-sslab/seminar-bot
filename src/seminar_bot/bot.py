from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import discord
from discord import app_commands
from discord.ext import tasks

from .archive import extract_saved
from .command_sync import sync_commands
from .documents import MAX_FILE_BYTES, DocumentInfo
from .paper_metadata import PaperMetadataResolver
from .remote_parser import extract_remote
from .rotation import apply_rotation, read_rotations
from .schedule_display import build_schedule_embed
from .scheduling import due_reminders
from .settings import Settings
from .store import Store, UserError, local_now, parse_date
from .submission_access import SubmissionAccess, submission_link
from .submissions import submission_summary
from .web_api import SeminarAPI

log = logging.getLogger(__name__)
WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
KIND_LABELS = {"slides": "발표 자료", "paper": "논문 PDF", "unknown": "분류 미확정"}
MAX_CONCURRENT_UPLOADS = 4


def safe(value: str, limit: int = 300) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))[:limit]


def weekly_schedule(config: dict) -> str:
    start = config.get("seminar_time")
    when = f" {start}" if start else " · 시작 시간 미설정"
    return f"매주 {WEEKDAYS[config['weekday']]}{when} ({config['timezone']})"


class DocumentKindButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"seminar:kind:(?P<record_id>\d+):(?P<kind>slides|paper)",
):
    """Handle old archive buttons by directing users to the web submission page."""

    def __init__(self, record_id: int, kind: str, selected: bool = False):
        self.record_id = record_id
        self.kind = kind
        super().__init__(
            discord.ui.Button(
                label=KIND_LABELS[kind],
                style=discord.ButtonStyle.primary if selected else discord.ButtonStyle.secondary,
                custom_id=f"seminar:kind:{record_id}:{kind}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["record_id"]), match["kind"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await interaction.client.tree.interaction_check(interaction):
            return
        await interaction.response.send_message(
            "자료 수정은 `/제출`로 받은 본인 전용 웹페이지에서 진행해주세요.",
            ephemeral=True,
        )


def visible_channel_ids(interaction: discord.Interaction) -> list[int]:
    return [
        channel.id
        for channel in interaction.guild.text_channels
        if channel.permissions_for(interaction.user).view_channel
        and channel.permissions_for(interaction.user).read_message_history
    ]


class SeminarCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        target = self.client.settings.active_guild_id
        if target is not None and interaction.guild_id != target:
            await interaction.response.send_message(
                "이 봇은 설정된 세미나 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return False
        return True


class SeminarBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.message_content = False
        intents.typing = False
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.data_dir / "seminar.sqlite3")
        self.tree = SeminarCommandTree(self)
        self.schedule_lock = asyncio.Lock()
        self.upload_slots = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
        self.paper_metadata = PaperMetadataResolver()
        self.rotation_errors: dict[int | None, str] = {}
        self.api_runner = None
        self.relay_client = None
        self._register_commands()
        self.tree.error(self.on_command_error)
        self.add_dynamic_items(DocumentKindButton)

    async def setup_hook(self) -> None:
        await self.sync_commands()
        if self.settings.api_token:
            from aiohttp import web

            self.api_runner = web.AppRunner(
                SeminarAPI(self).app, access_log=None, shutdown_timeout=20
            )
            await self.api_runner.setup()
            try:
                site = web.TCPSite(self.api_runner, self.settings.api_host, self.settings.api_port)
                await site.start()
            except Exception:
                await self.api_runner.cleanup()
                self.api_runner = None
                raise
            log.info("Seminar web API listening on port %s", self.settings.api_port)
            if self.settings.api_transport == "relay":
                from .relay import RelayClient

                self.relay_client = RelayClient(self.settings)
                self.relay_client.start()
        self.reminders.start()

    async def sync_commands(self) -> None:
        await sync_commands(self)

    def web_submission_enabled(self, guild_id: int) -> bool:
        return bool(self.settings.api_token) and guild_id == self.settings.api_guild_id

    async def presenter_names(self, guild_id: int, presenter_ids: set[int]) -> dict[int, str]:
        file_names = {}
        try:
            rotations = await asyncio.to_thread(
                read_rotations, self.settings.rotation_file, default_guild_id=guild_id
            )
            for rotation in rotations:
                if rotation.guild_id == guild_id:
                    file_names = dict(zip(rotation.presenters, rotation.names, strict=True))
                    break
        except (UserError, OSError):
            pass
        guild = self.get_guild(guild_id)
        names = {}
        for user_id in sorted(presenter_ids):
            member = guild.get_member(user_id) if guild else None
            name = " ".join(member.display_name.split()) if member else ""
            if not name:
                name = " ".join(file_names.get(user_id, "").split())
            if not name and guild:
                try:
                    member = await guild.fetch_member(user_id)
                    name = " ".join(member.display_name.split())
                except (discord.HTTPException, OSError):
                    pass
            names[user_id] = name or f"발표자 {user_id} (이름 확인 불가)"
        return names

    async def on_ready(self) -> None:
        log.info("Connected as %s (%s)", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        self.reminders.cancel()
        reminder_task = self.reminders.get_task()
        if reminder_task:
            await asyncio.gather(reminder_task, return_exceptions=True)
        if self.relay_client:
            await self.relay_client.close()
            self.relay_client = None
        if self.api_runner:
            await self.api_runner.cleanup()
            self.api_runner = None
        await super().close()
        self.store.close()

    @tasks.loop(seconds=30)
    async def reminders(self) -> None:
        try:
            await self.reload_rotation_file()
        except Exception:
            log.exception("Presenter JSON reload failed; keeping existing schedule and retrying")
        for config in self.store.all_configs():
            if (
                self.settings.active_guild_id is not None
                and config["guild_id"] != self.settings.active_guild_id
            ):
                continue
            if self.get_guild(config["guild_id"]) is None:
                continue
            try:
                async with self.schedule_lock:
                    # Re-read inside the lock, after any settings mutation.
                    config = self.store.require_config(config["guild_id"])
                    if not config["notifications_enabled"]:
                        continue
                    due = due_reminders(self.store, config, datetime.now(UTC))
                    for session, kind in due:
                        channel = self.get_channel(config["channel_id"])
                        if channel is None:
                            channel = await self.fetch_channel(config["channel_id"])
                        if (
                            not isinstance(channel, discord.TextChannel)
                            or channel.guild.id != config["guild_id"]
                        ):
                            raise UserError("알림 채널을 확인해주세요.")
                        mention = f"<@{session['presenter_id']}>"
                        start = config.get("seminar_time")
                        when = f" {start} ({safe(config['timezone'])})" if start else ""
                        embed = None
                        status = self.store.submission(
                            config["guild_id"],
                            session["id"],
                            visible_channel_ids=[config["upload_channel_id"]],
                        )
                        if kind == "morning":
                            today = parse_date(session["seminar_date"])
                            through = (today + timedelta(weeks=3)).isoformat()
                            rows = [
                                dict(row)
                                for row in self.store.db.execute(
                                    "SELECT * FROM sessions WHERE guild_id=? AND seminar_date>=? "
                                    "AND seminar_date<=? ORDER BY seminar_date",
                                    (config["guild_id"], session["seminar_date"], through),
                                )
                            ]
                            holidays = [
                                row
                                for row in self.store.holidays(config["guild_id"], today)
                                if row["seminar_date"] <= through
                            ]
                            names = await self.presenter_names(
                                config["guild_id"], {row["presenter_id"] for row in rows}
                            )
                            embed = build_schedule_embed(
                                config,
                                rows,
                                holidays,
                                today,
                                names,
                                title="🗓️ 이번 주부터 4주 세미나 일정",
                            )
                            text = f"☀️ **이번 주 발표자: {mention}**\n오늘({session['seminar_date']}){when} 세미나입니다.\n"
                            text += (
                                "발표 자료 제출을 확인했습니다. 수정할 내용은 `/제출`에서 변경해주세요."
                                if status["complete"]
                                else "`/제출` 명령으로 본인 전용 임시 링크를 받아 발표 자료와 참고 논문을 등록해주세요."
                            )
                        else:
                            if kind == "before":
                                phase = "⏰ 시작 전"
                                if config.get("before_minutes") is not None:
                                    phase += f" ({config['before_minutes']}분 전)"
                            elif config.get("after_minutes") is not None:
                                phase = f"⏰ 시작 {config['after_minutes']}분 후"
                            else:
                                phase = "🌙 종료 후"
                            if self.web_submission_enabled(config["guild_id"]):
                                text = (
                                    f"{phase} 자료 확인 · {mention}\n"
                                    f"{session['seminar_date']}{when} 세미나 자료 제출이 아직 완료되지 않았습니다.\n"
                                    "`/제출` 명령을 실행해 본인 전용 임시 링크를 받은 뒤 웹페이지에서 등록해주세요.\n"
                                    "발표 자료 **PDF/PPT/PPTX 1개 이상** + 참고한 **서로 다른 논문 3편 이상**이 필요합니다.\n"
                                    "논문은 제출 페이지에 PDF를 올려 정보를 추출하거나 직접 입력할 수 있습니다.\n"
                                    "한 번에 올리거나 나누어 올려도 회차별로 누적합니다.\n"
                                    + submission_summary(status)
                                )
                            else:
                                text = (
                                    f"{phase} · {mention} {session['seminar_date']}{when} 세미나 자료 제출을 위한 웹 연동이 설정되지 않았습니다.\n"
                                    "서버 관리자에게 웹 제출 연동 설정을 요청해주세요.\n"
                                    "발표 자료는 웹 제출 페이지에서 등록합니다.\n"
                                    + submission_summary(status)
                                )
                        # Name/channel lookups may cross a stage boundary. Only
                        # send the currently due stage after those awaits.
                        if not any(
                            candidate["id"] == session["id"] and stage == kind
                            for candidate, stage in due_reminders(
                                self.store, config, datetime.now(UTC)
                            )
                        ):
                            continue
                        on_date = session["seminar_date"]
                        nonce = hashlib.sha256(
                            f"seminar-reminder:{config['guild_id']}:{session['id']}:{on_date}:{kind}".encode()
                        ).hexdigest()[:24]
                        send_options = {"embed": embed} if embed is not None else {}
                        sent = await channel.send(
                            text,
                            **send_options,
                            nonce=nonce,
                            allowed_mentions=discord.AllowedMentions(
                                users=[discord.Object(id=session["presenter_id"])],
                                roles=False,
                                everyone=False,
                                replied_user=False,
                            ),
                        )
                        self.store.mark_sent(session["id"], on_date, kind, sent.id)
            except Exception:
                # A forbidden/deleted channel or transient network error must not kill the loop.
                log.exception(
                    "Reminder failed for guild %s; will retry next tick", config["guild_id"]
                )

    async def notify_web_submission(self, session: dict, status: dict) -> int:
        config = self.store.require_config(session["guild_id"])
        if not config["notifications_enabled"]:
            raise UserError("알림이 비활성화되어 있습니다.")
        channel = self.get_channel(config["channel_id"])
        if channel is None:
            channel = await self.fetch_channel(config["channel_id"])
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != session["guild_id"]:
            raise UserError("자료 알림 채널을 확인해주세요.")
        message = await channel.send(
            f"📚 {session['seminar_date']} · <@{session['presenter_id']}> 발표 자료가 웹으로 제출되었습니다.\n"
            + submission_summary(status)
            + "\n"
            + self.settings.submission_url(session["id"]),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return message.id

    async def extract_document(
        self, path: Path, filename: str, data: bytes, *, kind: str = "paper"
    ):
        if len(data) > MAX_FILE_BYTES:
            return DocumentInfo(
                Path(filename.replace("\\", "/")).stem[:500] or "이름 없는 문서",
                "",
                None,
                None,
                hashlib.sha256(data).hexdigest(),
                "filename",
                "문서 분석 용량 제한을 넘어 파일명으로 기록했습니다.",
            )
        if self.settings.parser_url:
            info = await extract_remote(path, filename, data, self.settings.parser_url, kind=kind)
        else:
            info = await extract_saved(path, filename, data, kind=kind)
        return await self.paper_metadata.enrich(info) if kind == "paper" else info

    def source_url(self, record: dict) -> str:
        if record.get("source_type") == "import":
            if not self.settings.seminar_page_url:
                return ""
            page = urlsplit(self.settings.seminar_page_url)
            tab = "papers" if record.get("kind") == "paper" else "presentations"
            query = record["title"][:180].encode("utf-8")[:400].decode("utf-8", errors="ignore")
            return urlunsplit(
                (
                    page.scheme,
                    page.netloc,
                    f"/internal/seminars/{tab}/",
                    "q=" + quote(query),
                    "",
                )
            )
        if record.get("source_type") in {"web", "manual"} and record.get("session_id"):
            return self.settings.submission_url(record["session_id"]) or record["message_url"]
        return record["message_url"]

    async def reload_rotation_file(self) -> None:
        configs = self.store.all_configs()
        target = self.settings.active_guild_id
        if target is not None:
            configs = [config for config in configs if config["guild_id"] == target]
        default_guild_id = target or (configs[0]["guild_id"] if len(configs) == 1 else None)
        try:
            rotations = await asyncio.to_thread(
                read_rotations, self.settings.rotation_file, default_guild_id=default_guild_id
            )
        except (UserError, OSError) as exc:
            message = str(exc)
            if self.rotation_errors.get(None) != message:
                log.warning("Presenter JSON not applied; keeping existing schedule: %s", message)
                self.rotation_errors[None] = message
            return
        self.rotation_errors.pop(None, None)
        if target is not None:
            rotations = [rotation for rotation in rotations if rotation.guild_id == target]
            self.rotation_errors = {
                guild_id: message
                for guild_id, message in self.rotation_errors.items()
                if guild_id == target
            }
            if not configs:
                message = "설정된 세미나 서버에서 먼저 /설정을 실행해주세요."
                if self.rotation_errors.get(target) != message:
                    log.warning("Presenter JSON not applied for guild %s: %s", target, message)
                    self.rotation_errors[target] = message
                return
        file_guild_ids = {rotation.guild_id for rotation in rotations}
        for config in configs:
            guild_id = config["guild_id"]
            if guild_id not in file_guild_ids:
                message = (
                    f"config/rotation.json의 guilds에 이 서버({guild_id})의 발표자 명단이 없습니다."
                )
                if self.rotation_errors.get(guild_id) != message:
                    log.warning(
                        "Presenter JSON not applied for guild %s; keeping existing schedule: %s",
                        guild_id,
                        message,
                    )
                    self.rotation_errors[guild_id] = message
        for rotation in rotations:
            guild_id = rotation.guild_id
            if self.store.rotation_fingerprint(guild_id) == rotation.fingerprint:
                self.rotation_errors.pop(guild_id, None)
                continue
            guild = self.get_guild(guild_id)
            if guild is None:
                message = "JSON의 서버 ID를 확인하고 해당 서버에 봇을 초대해주세요."
                if self.rotation_errors.get(guild_id) != message:
                    log.warning("Presenter JSON not applied for guild %s: %s", guild_id, message)
                    self.rotation_errors[guild_id] = message
                continue
            try:
                self.store.require_config(guild_id)
                # Resolve only configured members; no privileged member-list intent.
                for user_id in rotation.presenters:
                    member = guild.get_member(user_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(user_id)
                        except discord.NotFound as exc:
                            raise UserError(
                                "발표자 user_id 중 서버에 없는 계정이 있습니다."
                            ) from exc
                    if member.bot:
                        raise UserError("봇 계정은 발표자로 등록할 수 없습니다.")
                async with self.schedule_lock:
                    start = apply_rotation(self.store, rotation, datetime.now(UTC))
                self.rotation_errors.pop(guild_id, None)
                if start is not None:
                    log.info("Presenter JSON applied for guild %s from %s", guild_id, start)
            except (UserError, discord.HTTPException) as exc:
                message = str(exc)
                if self.rotation_errors.get(guild_id) != message:
                    log.warning(
                        "Presenter JSON not applied for guild %s; keeping existing schedule: %s",
                        guild_id,
                        message,
                    )
                    self.rotation_errors[guild_id] = message

    @reminders.before_loop
    async def before_reminders(self) -> None:
        await self.wait_until_ready()

    async def on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, UserError):
            text = str(original)
        elif isinstance(original, app_commands.MissingPermissions):
            text = "일정과 설정 변경에는 서버 관리(Manage Server) 권한이 필요합니다."
        elif isinstance(original, app_commands.NoPrivateMessage):
            text = "서버 채널에서 사용해주세요."
        elif isinstance(original, app_commands.CommandOnCooldown):
            text = f"{original.retry_after:.0f}초 후 다시 시도해주세요."
        else:
            log.error("Command failed", exc_info=(type(original), original, original.__traceback__))
            text = "처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    def _register_commands(self) -> None:
        tree = self.tree

        @tree.command(
            name="설정",
            description="세미나 요일·필수 시작 시간, 알림 시간과 알림 채널을 설정합니다 (서버 관리자)",
        )
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.choices(
            weekday=[app_commands.Choice(name=name, value=i) for i, name in enumerate(WEEKDAYS)],
            notifications=[
                app_commands.Choice(name="끄기", value="off"),
                app_commands.Choice(name="켜기", value="on"),
            ],
        )
        @app_commands.rename(
            seminar_time="세미나시간",
            notifications="알림",
            reminder_start_date="알림시작일",
            morning="일정안내",
            before_minutes="시작전분",
            after_minutes="시작후분",
        )
        @app_commands.describe(
            weekday="세미나 요일",
            channel="알림을 보낼 채널",
            morning="당일 포함 4주 일정 안내 HH:MM (신규 기본 12:00, 생략 시 기존 유지)",
            before_minutes="시작 몇 분 전에 자료를 요청할지 (기본 15분, 생략 시 기존 분 설정 유지)",
            after_minutes="시작 몇 분 후에 자료를 요청할지 (기본 60분, 생략 시 기존 분 설정 유지)",
            timezone="시간대 (기본 Asia/Seoul)",
            seminar_time="매주 세미나 시작 시각 HH:MM (필수, 예: 16:00)",
            notifications="알림 끄기/켜기 (생략하면 기존 설정 유지, 최초 OFF)",
            reminder_start_date="처음 알림을 켤 때 기준으로 삼을 등록된 회차 날짜 YYYY-MM-DD",
        )
        async def configure(
            interaction: discord.Interaction,
            weekday: int,
            channel: discord.TextChannel,
            seminar_time: str,
            morning: str | None = None,
            timezone: str = "Asia/Seoul",
            notifications: str | None = None,
            reminder_start_date: str | None = None,
            before_minutes: app_commands.Range[int, 0, 1439] | None = None,
            after_minutes: app_commands.Range[int, 0, 1439] | None = None,
        ) -> None:
            if notifications is not None and notifications not in {"off", "on"}:
                raise UserError("알림은 끄기 또는 켜기를 선택해주세요.")
            start_date = (
                parse_date(reminder_start_date) if reminder_start_date is not None else None
            )
            if channel.guild.id != interaction.guild_id:
                raise UserError("현재 서버의 채널을 선택해주세요.")
            permissions = channel.permissions_for(interaction.guild.me)
            if not (
                permissions.view_channel
                and permissions.send_messages
                and permissions.embed_links
                and permissions.read_message_history
            ):
                raise UserError(
                    f"{channel.mention}에서 봇에게 채널 보기·메시지 보내기·링크 임베드·기록 보기 권한을 주세요."
                )
            await interaction.response.defer(ephemeral=True)
            async with self.schedule_lock:
                previous = self.store.config(interaction.guild_id)
                upload_channel_id = previous["upload_channel_id"] if previous else channel.id
                before = before_minutes
                after = after_minutes
                if before is None and previous:
                    before = previous.get("before_minutes")
                if after is None and previous:
                    after = previous.get("after_minutes")
                delta = self.store.configure(
                    interaction.guild_id,
                    channel.id,
                    upload_channel_id,
                    weekday,
                    morning
                    if morning is not None
                    else previous["morning"]
                    if previous
                    else "12:00",
                    previous["evening"] if previous else "17:00",
                    timezone,
                    seminar_time=seminar_time,
                    notifications_enabled=notifications == "on"
                    if notifications is not None
                    else None,
                    reminder_start_date=start_date,
                    before_minutes=15 if before is None else before,
                    after_minutes=60 if after is None else after,
                )
                config = self.store.require_config(interaction.guild_id)
            text = (
                f"{safe(weekly_schedule(config))}\n"
                f"알림: 일정 안내 {config['morning']}"
                f" / 시작 {config['before_minutes']}분 전 {config['before_reminder']}"
                f" / 시작 {config['after_minutes']}분 후 {config['evening']}\n"
                f"알림 상태: {'ON' if config['notifications_enabled'] else 'OFF'} · "
                f"알림 시작일: {config['reminder_start_date'] or '미설정'}\n"
                f"알림 채널: {channel.mention}"
            )
            if delta:
                text += f"\n기존 미래 일정도 새 요일에 맞춰 {delta}일 뒤로 이동했습니다."
            await self.reload_rotation_file()
            text += "\n발표 순서는 NAS의 config/rotation.json에서 관리합니다. 파일 저장 후 30초 안에 반영됩니다."
            error = self.rotation_errors.get(None) or self.rotation_errors.get(interaction.guild_id)
            if error:
                text += f"\n⚠️ 발표 순서 파일 적용 오류: {safe(error, 800)}\n현재 서버 ID: `{interaction.guild_id}`"
            await interaction.followup.send(text, ephemeral=True)

        @tree.command(
            name="일정",
            description="이번 주 발표를 강조하고 최근·다가오는 발표자와 시간·휴강일을 확인합니다",
        )
        @app_commands.guild_only()
        async def schedule(
            interaction: discord.Interaction, weeks: app_commands.Range[int, 1, 20] = 8
        ) -> None:
            await interaction.response.defer()
            await self.reload_rotation_file()
            config = self.store.require_config(interaction.guild_id)
            today = local_now(config).date()
            async with self.schedule_lock:
                rows = self.store.upcoming(interaction.guild_id, today, weeks)
                previous = self.store.recent_session(
                    interaction.guild_id, today - timedelta(days=1)
                )
                if previous and previous["seminar_date"] >= (today - timedelta(days=7)).isoformat():
                    rows.insert(0, previous)
                holidays = self.store.holidays(interaction.guild_id, today)
            if not rows:
                error = self.rotation_errors.get(None) or self.rotation_errors.get(
                    interaction.guild_id
                )
                message = f"현재 서버 ID: `{interaction.guild_id}`\n"
                if error:
                    message += f"발표 순서 파일 적용 오류: {safe(error, 800)}"
                else:
                    message += "아직 발표 순서가 없습니다. config/rotation.json의 presenters에 발표자를 등록해주세요."
                raise UserError(message)
            names = await self.presenter_names(
                interaction.guild_id, {row["presenter_id"] for row in rows}
            )
            embed = build_schedule_embed(
                config,
                rows,
                [row for row in holidays if row["seminar_date"] <= rows[-1]["seminar_date"]],
                today,
                names,
            )
            if self.rotation_errors.get(None) or self.rotation_errors.get(interaction.guild_id):
                embed.set_footer(
                    text=embed.footer.text + " · 발표 순서 파일 적용 오류: NAS 로그 확인"
                )
            await interaction.followup.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )

        @tree.command(name="스왑", description="두 날짜의 발표자를 교환합니다 (해당 회차만)")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def swap(interaction: discord.Interaction, first_date: str, second_date: str) -> None:
            await interaction.response.defer()
            async with self.schedule_lock:
                config = self.store.require_config(interaction.guild_id)
                today = local_now(config).date()
                self.store.ensure_horizon(interaction.guild_id, today, 20)
                self.store.swap(
                    interaction.guild_id, parse_date(first_date), parse_date(second_date), today
                )
            await interaction.followup.send(
                f"{safe(first_date)} ↔ {safe(second_date)} 발표자를 교환했습니다. 연결된 자료도 발표자를 따라 이동합니다."
            )

        @tree.command(
            name="연기", description="해당 주를 휴강하고 모든 후속 발표를 1주씩 연기합니다"
        )
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.describe(from_date="휴강할 발표일 YYYY-MM-DD (생략: 다음 발표)")
        async def postpone(interaction: discord.Interaction, from_date: str | None = None) -> None:
            await interaction.response.defer()
            async with self.schedule_lock:
                config = self.store.require_config(interaction.guild_id)
                start, _ = self.store.postpone(
                    interaction.guild_id,
                    local_now(config).date(),
                    parse_date(from_date) if from_date else None,
                )
            await interaction.followup.send(
                f"💤 {start.isoformat()}은 휴강입니다. 해당 회차부터 이후 모든 발표와 알림을 7일씩 뒤로 옮겼습니다."
            )

        @tree.command(
            name="검색",
            description="등록된 논문·발표 자료를 제목, 학회, 연도, DOI 등으로 검색합니다",
        )
        @app_commands.guild_only()
        @app_commands.describe(
            query="논문 제목 일부 / 파일명 / DOI / arXiv ID",
            page="결과 페이지",
            kind="검색할 자료 종류 (기본: 논문)",
        )
        @app_commands.choices(
            kind=[
                app_commands.Choice(name="논문", value="paper"),
                app_commands.Choice(name="발표 자료", value="slides"),
                app_commands.Choice(name="분류 미확정", value="unknown"),
                app_commands.Choice(name="전체", value="all"),
            ]
        )
        async def search(
            interaction: discord.Interaction,
            query: str,
            page: app_commands.Range[int, 1, 10000] = 1,
            kind: str = "paper",
        ) -> None:
            config = self.store.require_config(interaction.guild_id)
            rows, count = self.store.search(
                interaction.guild_id,
                query,
                page,
                visible_channel_ids=visible_channel_ids(interaction),
                kind=None if kind == "all" else kind,
            )
            if not count:
                await interaction.response.send_message(
                    "일치하는 자료가 없습니다. 제목 일부·파일명으로 검색하거나 kind:전체 를 선택해보세요.",
                    ephemeral=True,
                )
                return
            if not rows:
                raise UserError(f"검색 결과는 {(count + 4) // 5}페이지까지 있습니다.")
            embed = discord.Embed(
                title="📚 자료 검색",
                description=f"검색어: {safe(query, 100)} · 총 {count}개",
                color=0x5B8DEF,
            )
            today = local_now(config).date().isoformat()
            for row in rows:
                archived = row.get("source_type") == "import"
                if archived:
                    timing = row["seminar_date"] or f"{row['seminar_year']}년 · 날짜 미상"
                    if row.get("archive_kind") == "unassigned":
                        timing = f"{row['seminar_year']}년 · 회차 미상"
                    historical_presenter = safe(row.get("presenter_name_snapshot") or "", 100) or (
                        f"<@{row['presenter_id']}>" if row.get("presenter_id") else "발표자 미상"
                    )
                    linked = f"과거 발표: {timing} · {historical_presenter}"
                elif row["seminar_date"]:
                    status = "발표 예정/당일" if row["seminar_date"] >= today else "발표일"
                    linked = f"{status}: {row['seminar_date']} · <@{row['presenter_id']}>"
                else:
                    linked = "발표 회차 미연결"
                author = (
                    "회차 발표자"
                    if row.get("source_type") in {"web", "manual", "import"}
                    else "업로더"
                )
                manual = row.get("source_type") == "manual" or (
                    archived and row["kind"] == "paper" and not row["sha256"]
                )
                source = (
                    "발표자료에서 확인한 논문 정보"
                    if manual and archived
                    else "직접 입력한 논문 정보"
                    if manual
                    else f"파일: {safe(row['filename'], 160)}"
                )
                source_url = self.source_url(row)
                link_label = "자료 조회" if archived else "발표 회차"
                source_link = f"[{link_label}]({source_url}) · " if source_url else ""
                owner = historical_presenter if archived else f"<@{row['uploader_id']}>"
                value = f"구분: {'논문 정보' if manual else KIND_LABELS[row['kind']]}\n{linked}\n{source}\n{source_link}{author} {owner}"
                if row.get("venue") or row.get("year"):
                    value += "\n" + safe(
                        " · ".join(str(row[key]) for key in ("venue", "year") if row.get(key)), 220
                    )
                if row["doi"]:
                    value += f"\nDOI: {safe(row['doi'], 255)}"
                if row["arxiv_id"]:
                    value += f"\narXiv: {safe(row['arxiv_id'])}"
                embed.add_field(
                    name=f"#{row['id']} {safe(row['title'], 190)}", value=value[:1024], inline=False
                )
            embed.set_footer(text=f"{page}/{(count + 4) // 5} 페이지 · /검색 query:... page:2")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @tree.command(
            name="제출",
            description="본인 발표 회차를 선택해 자료를 제출·수정하는 임시 링크를 받습니다",
        )
        @app_commands.guild_only()
        @app_commands.checks.cooldown(
            1, 10, key=lambda interaction: (interaction.guild_id, interaction.user.id)
        )
        @app_commands.describe(
            seminar_date="본인 발표 날짜 YYYY-MM-DD (생략: 최근 미완료 또는 다음 발표)"
        )
        async def submit_link(
            interaction: discord.Interaction, seminar_date: str | None = None
        ) -> None:
            if (
                not self.settings.api_token
                or interaction.guild_id != self.settings.api_guild_id
                or not self.settings.seminar_page_url
            ):
                raise UserError(
                    "이 서버의 웹 자료 제출이 설정되지 않았습니다. 서버 관리자에게 문의해주세요."
                )
            submission_link(self.settings.seminar_page_url, "x" * 43)
            await interaction.response.defer(ephemeral=True)
            await self.reload_rotation_file()
            async with self.schedule_lock:
                config = self.store.require_config(interaction.guild_id)
                access = SubmissionAccess(self.store)
                session = access.select_session(
                    interaction.guild_id,
                    interaction.user.id,
                    local_now(config).date(),
                    parse_date(seminar_date) if seminar_date else None,
                )
                token, _ = access.issue(
                    interaction.guild_id, session["id"], interaction.user.id, scope="presenter"
                )
            view = discord.ui.View()
            view.add_item(
                discord.ui.Button(
                    label="내 발표 자료 제출하기",
                    url=submission_link(self.settings.seminar_page_url, token),
                )
            )
            await interaction.followup.send(
                f"**{session['seminar_date']} 발표 자료 제출**\n"
                "아래 버튼으로 본인 발표 회차를 선택하고 자료를 제출·수정할 수 있습니다.\n"
                "링크는 15분 안에 한 번만 열 수 있고, 열린 페이지는 1시간 동안 사용할 수 있습니다.\n"
                "새 링크를 발급하면 이전 링크와 제출 페이지는 만료됩니다. 링크를 다른 사람에게 공유하지 마세요.",
                view=view,
                ephemeral=True,
            )

        @tree.command(
            name="제출현황",
            description="발표 자료 1개와 서로 다른 논문 3편 이상이 제출됐는지 확인합니다",
        )
        @app_commands.guild_only()
        @app_commands.describe(seminar_date="발표 날짜 YYYY-MM-DD (생략: 오늘 또는 최근/다음 회차)")
        async def submission_status(
            interaction: discord.Interaction, seminar_date: str | None = None
        ) -> None:
            config = self.store.require_config(interaction.guild_id)
            today = local_now(config).date()
            async with self.schedule_lock:
                upcoming = self.store.upcoming(interaction.guild_id, today)
                if seminar_date:
                    session = self.store.session_on(interaction.guild_id, parse_date(seminar_date))
                else:
                    session = self.store.session_on(interaction.guild_id, today)
                    if not session:
                        session = self.store.recent_session(interaction.guild_id, today) or (
                            upcoming[0] if upcoming else None
                        )
                if not session:
                    raise UserError("해당 발표 회차가 없습니다. /일정 에서 날짜를 확인해주세요.")
                status = self.store.submission(
                    interaction.guild_id, session["id"], visible_channel_ids(interaction)
                )
            embed = discord.Embed(
                title="📋 발표 자료 제출 현황",
                description=f"{session['seminar_date']} · <@{session['presenter_id']}>\n\n"
                + submission_summary(status),
                color=0x5B8DEF,
            )
            # Keep the response bounded even when more than three papers are submitted.
            rows = status["documents"]
            for record in rows[:15]:
                embed.add_field(
                    name=f"#{record['id']} · {KIND_LABELS[record['kind']]}",
                    value=f"{safe(record['title'], 160)} · [업로드]({self.source_url(record)})",
                    inline=False,
                )
            embed.set_footer(
                text=f"열람 가능한 자료 기준 · 전체 {len(rows)}개 중 {min(15, len(rows))}개 표시 · 같은 논문 재업로드는 중복 제외"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @tree.command(name="도움말", description="세미나 봇의 설정 방법과 명령어를 안내합니다")
        @app_commands.guild_only()
        async def help_command(interaction: discord.Interaction) -> None:
            text = (
                "**발표 자료 제출·수정**\n"
                "`/제출` → 본인만 보이는 임시 링크 → 본인의 지난·예정 발표 회차 선택\n"
                "같은 페이지에서 자료를 새로 제출하거나 기존 자료를 수정·교체·삭제할 수 있습니다.\n"
                "발표 자료 **PDF/PPT/PPTX 1개 이상** + **서로 다른 논문 3편 이상**이 필요합니다.\n"
                "논문은 웹에 PDF를 올려 제목·DOI를 자동 추출하거나 직접 입력할 수 있습니다.\n"
                "직접 입력은 **제목만 필수**, 학회·연도·DOI·링크는 선택입니다.\n"
                "새 논문 PDF는 분석 후 원본을 NAS에 보관하지 않고 메타정보만 저장합니다.\n"
                "임시 링크는 15분 안에 한 번 열 수 있고 페이지는 1시간 동안 사용할 수 있습니다.\n\n"
                "**일정·자료 조회**\n"
                "`/일정` 발표자·세미나 시작 시간·휴강일 확인\n"
                "`/제출현황` 회차별 제출 자료와 부족한 항목 확인\n"
                "연구실 사이트 **Internal → 세미나 자료 조회**에서 최신순 열람·논문 검색·발표 자료 다운로드\n"
                "`/검색`으로도 논문 검색 가능하며 종류 옵션에서 발표 자료나 전체를 선택할 수 있습니다.\n\n"
                "**서버 관리자**\n"
                "`/설정` 요일·**세미나시간**(매주 고정 시작 시각, 필수)·알림 채널 지정\n"
                "`일정안내`는 시각을 지정하며 신규 기본값은 12:00입니다.\n"
                "자료 요청은 기본 **시작 15분 전 / 시작 60분 후**이며, `시작전분`·`시작후분`으로 변경할 수 있습니다.\n"
                "세미나 시작 시각을 바꾸면 자료 요청 시각도 자동으로 변경됩니다.\n"
                "일정 안내는 당일 포함 4주 일정을 보내고 이번 주 발표자만 멘션합니다.\n"
                "알림은 기본 OFF. `/설정`의 `알림`(켜기/끄기)으로 변경하며 생략하면 기존 상태를 유지합니다.\n"
                "처음 켤 때 `알림시작일`에 등록된 회차 날짜를 지정하세요.\n"
                "시작 전·시작 후 요청은 시작일 이후 당일 발표의 자료가 미완료일 때 각 한 번만 보냅니다.\n"
                "지난 회차는 요청하지 않으며 재시작 때 지난 알림 시간을 몰아 보내지 않습니다.\n"
                "발표 순서는 NAS의 `config/rotation.json`의 `presenters`에서 편집 (서버 한 곳이면 서버 ID 불필요)\n"
                "파일 저장 후 30초 안에 적용하며 `/일정`을 열 때도 확인합니다.\n"
                "`/스왑` 두 날짜의 발표자 교환 · `/연기` 해당 주 휴강 + 이후 전체 1주 연기"
            )
            if not self.web_submission_enabled(interaction.guild_id):
                text += "\n\n웹 제출이 설정되지 않았습니다. 서버 관리자에게 웹 연동 설정을 요청해주세요."
            await interaction.response.send_message(text, ephemeral=True)
