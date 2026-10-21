from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from .archive import extract_saved
from .rotation import apply_rotation, read_rotations
from .scheduling import due_reminders
from .settings import Settings
from .store import Store, UserError, local_now, parse_date
from .submissions import classify_document, submission_summary

log = logging.getLogger(__name__)
WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
KIND_LABELS = {"slides": "발표 자료", "paper": "논문 PDF", "unknown": "분류 미확정"}


def safe(value: str, limit: int = 300) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))[:limit]


def is_manager(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    )


class DocumentKindButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"seminar:kind:(?P<record_id>\d+):(?P<kind>slides|paper)",
):
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
        bot = interaction.client
        try:
            if interaction.guild_id is None:
                raise UserError("서버 채널에서 사용해주세요.")
            await interaction.response.defer(ephemeral=True)
            async with bot.schedule_lock:
                record = bot.store.classify(
                    interaction.guild_id,
                    self.record_id,
                    self.kind,
                    interaction.user.id,
                    is_manager(interaction),
                )
                status = (
                    bot.store.submission(interaction.guild_id, record["session_id"])
                    if record["session_id"]
                    else None
                )
            # Keep the public archive acknowledgment in sync with the selected type.
            if interaction.message and interaction.message.embeds:
                embed = interaction.message.embeds[0].copy()
                add_submission_fields(embed, record, status)
                try:
                    await interaction.message.edit(embed=embed, view=classification_view(record))
                except discord.HTTPException:
                    log.warning(
                        "Could not refresh classification message for record %s", self.record_id
                    )
            text = f"#{self.record_id} 자료를 **{KIND_LABELS[self.kind]}**로 구분했습니다."
            if status:
                text += "\n" + submission_summary(status)
            await interaction.followup.send(text, ephemeral=True)
        except Exception as exc:
            await bot.on_command_error(interaction, exc)


def classification_view(record: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for kind in ("slides", "paper"):
        if kind == "paper" and Path(record["filename"]).suffix.casefold() != ".pdf":
            continue
        view.add_item(DocumentKindButton(record["id"], kind, selected=record["kind"] == kind))
    return view


def add_submission_fields(embed: discord.Embed, record: dict, status: dict | None) -> None:
    embed.description = safe(record["title"], 450)
    fields = {
        "발표 회차": record["seminar_date"] or "미연결 · /자료연결 로 지정",
        "자료 구분": f"{KIND_LABELS[record['kind']]} · {safe(record['classification_reason'], 200)}",
        "제출 현황": submission_summary(status)
        if status
        else "회차 미연결 · /자료연결 로 발표 날짜를 지정해주세요.",
    }
    for name, value in fields.items():
        index = next((i for i, field in enumerate(embed.fields) if field.name == name), None)
        if index is None:
            embed.add_field(name=name, value=value, inline=False)
        else:
            embed.set_field_at(index, name=name, value=value, inline=False)


def visible_channel_ids(interaction: discord.Interaction) -> list[int]:
    return [
        channel.id
        for channel in interaction.guild.text_channels
        if channel.permissions_for(interaction.user).view_channel
        and channel.permissions_for(interaction.user).read_message_history
    ]


class SeminarBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.typing = False
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.data_dir / "seminar.sqlite3")
        self.tree = app_commands.CommandTree(self)
        self.schedule_lock = asyncio.Lock()
        self.upload_slots = asyncio.Semaphore(2)
        self.rotation_errors: dict[int | None, str] = {}
        self._register_commands()
        self.tree.error(self.on_command_error)
        self.add_dynamic_items(DocumentKindButton)

    async def setup_hook(self) -> None:
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Registered %s commands", len(synced))
        self.reminders.start()

    async def on_ready(self) -> None:
        log.info("Connected as %s (%s)", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        self.reminders.cancel()
        reminder_task = self.reminders.get_task()
        if reminder_task:
            await asyncio.gather(reminder_task, return_exceptions=True)
        await super().close()
        self.store.close()

    @tasks.loop(seconds=30)
    async def reminders(self) -> None:
        try:
            await self.reload_rotation_file()
        except Exception:
            log.exception("Presenter JSON reload failed; keeping existing schedule and retrying")
        for config in self.store.all_configs():
            if self.get_guild(config["guild_id"]) is None:
                continue
            try:
                async with self.schedule_lock:
                    # Re-read inside the lock, after any settings mutation.
                    config = self.store.require_config(config["guild_id"])
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
                        if kind == "morning":
                            text = f"☀️ 오늘({session['seminar_date']}) 세미나 발표자는 {mention}입니다. 발표 준비 부탁드려요!"
                        else:
                            status = self.store.submission(config["guild_id"], session["id"])
                            text = (
                                f"🌙 {mention} 오늘 발표에 사용한 자료를 <#{config['upload_channel_id']}>에 공유해주세요.\n"
                                "발표 자료 **PDF/PPT/PPTX 1개 이상** + 참고한 **서로 다른 논문 PDF 3개 이상**이 필요합니다.\n"
                                "한 번에 올리거나 나누어 올려도 회차별로 누적합니다.\n"
                                + submission_summary(status)
                            )
                        sent = await channel.send(
                            text,
                            allowed_mentions=discord.AllowedMentions(
                                users=[discord.Object(id=session["presenter_id"])],
                                roles=False,
                                everyone=False,
                                replied_user=False,
                            ),
                        )
                        self.store.mark_sent(session["id"], session["seminar_date"], kind, sent.id)
            except Exception:
                # A forbidden/deleted channel or transient network error must not kill the loop.
                log.exception(
                    "Reminder failed for guild %s; will retry next tick", config["guild_id"]
                )

    async def reload_rotation_file(self) -> None:
        try:
            rotations = await asyncio.to_thread(read_rotations, self.settings.rotation_file)
        except (UserError, OSError) as exc:
            message = str(exc)
            if self.rotation_errors.get(None) != message:
                log.warning("Presenter JSON not applied; keeping existing schedule: %s", message)
                self.rotation_errors[None] = message
            return
        self.rotation_errors.pop(None, None)
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

    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or not message.attachments:
            return
        config = self.store.config(message.guild.id)
        if not config or message.channel.id != config["upload_channel_id"]:
            return
        for attachment in message.attachments:
            if Path(attachment.filename).suffix.casefold() not in {".pdf", ".ppt", ".pptx"}:
                continue
            try:
                async with self.upload_slots:
                    await self.archive_attachment(message, attachment)
            except Exception:
                log.exception("Could not archive attachment %s", attachment.id)
                try:
                    await message.reply(
                        f"`{safe(attachment.filename, 100)}` 저장에 실패했습니다. 잠시 후 파일을 다시 올려주세요.",
                        mention_author=False,
                    )
                except discord.HTTPException:
                    pass

    async def archive_attachment(
        self, message: discord.Message, attachment: discord.Attachment
    ) -> None:
        guild_id = message.guild.id
        if self.store.has_attachment(guild_id, attachment.id):
            return
        limit = self.settings.max_file_mb * 1024 * 1024
        if attachment.size > limit:
            await message.reply(
                f"파일당 {self.settings.max_file_mb}MB까지 기록할 수 있습니다.",
                mention_author=False,
            )
            return
        # Attachment.read uses Discord's CDN URL; never fetch URLs from message text.
        data = await attachment.read()
        if len(data) > limit:
            raise UserError("파일 크기 제한을 초과했습니다.")
        directory = self.settings.data_dir / "files" / str(guild_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{attachment.id}{Path(attachment.filename).suffix.casefold()}"
        await asyncio.to_thread(path.write_bytes, data)
        info = await extract_saved(path, attachment.filename, data)
        classification = classify_document(
            attachment.filename, info, getattr(message, "content", "")
        )
        # Replayed events can run concurrently while the worker is parsing.
        if self.store.has_attachment(guild_id, attachment.id):
            return
        async with self.schedule_lock:
            if self.store.has_attachment(guild_id, attachment.id):
                return
            record_id, duplicates = self.store.add_document(
                guild_id,
                attachment.id,
                message.author.id,
                info,
                attachment.filename,
                str(path.relative_to(self.settings.data_dir)),
                message.jump_url,
                message.created_at,
                source_channel_id=message.channel.id,
                kind=classification.kind,
                classification_reason=classification.reason,
            )
            record = self.store.document(guild_id, record_id)
            status = (
                self.store.submission(guild_id, record["session_id"])
                if record["session_id"]
                else None
            )
        embed = discord.Embed(
            title="📚 자료를 기록했어요", description=safe(info.title, 450), color=0x5B8DEF
        )
        embed.add_field(name="자료 번호", value=f"#{record_id}")
        embed.add_field(
            name="발표 회차",
            value=record["seminar_date"] or "미연결 · /자료연결 로 지정",
            inline=True,
        )
        if info.doi:
            embed.add_field(name="DOI", value=safe(info.doi, 255), inline=False)
        if info.arxiv_id:
            embed.add_field(name="arXiv", value=safe(info.arxiv_id), inline=True)
        if info.warning:
            embed.add_field(name="추출 결과", value=safe(info.warning, 500), inline=False)
        if duplicates:
            previous = []
            for old in duplicates:
                when = old["seminar_date"] or "발표 회차 미연결"
                previous.append(f"#{old['id']} · {when} · [기존 업로드]({old['message_url']})")
            embed.add_field(
                name="같은 논문 또는 파일로 보이는 기존 기록",
                value="\n".join(previous),
                inline=False,
            )
        add_submission_fields(embed, record, status)
        embed.set_footer(text="아래 버튼으로 자료 구분 확인/수정 · /제출현황 으로 최신 현황 확인")
        try:
            await message.reply(embed=embed, view=classification_view(record), mention_author=False)
        except discord.HTTPException:
            # The archive already committed; a failed acknowledgment is not a failed save.
            log.warning("Archived record %s but could not send acknowledgment", record_id)

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
            name="설정", description="세미나 요일, 알림 시간, 채널을 설정합니다 (서버 관리자)"
        )
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.choices(
            weekday=[app_commands.Choice(name=name, value=i) for i, name in enumerate(WEEKDAYS)]
        )
        @app_commands.describe(
            weekday="세미나 요일",
            channel="알림을 보낼 채널",
            upload_channel="자료 파일을 올릴 채널",
            morning="아침 알림 HH:MM",
            evening="저녁 자료 요청 HH:MM",
            timezone="시간대 (기본 Asia/Seoul)",
        )
        async def configure(
            interaction: discord.Interaction,
            weekday: int,
            channel: discord.TextChannel,
            upload_channel: discord.TextChannel,
            morning: str = "09:00",
            evening: str = "18:00",
            timezone: str = "Asia/Seoul",
        ) -> None:
            for target in (channel, upload_channel):
                member = interaction.guild.me
                permissions = target.permissions_for(member)
                if not (
                    permissions.view_channel
                    and permissions.send_messages
                    and permissions.embed_links
                    and permissions.read_message_history
                ):
                    raise UserError(
                        f"{target.mention}에서 봇에게 채널 보기·메시지 보내기·링크 임베드·기록 보기 권한을 주세요."
                    )
                if target.guild.id != interaction.guild_id:
                    raise UserError("현재 서버의 채널을 선택해주세요.")
            await interaction.response.defer(ephemeral=True)
            async with self.schedule_lock:
                delta = self.store.configure(
                    interaction.guild_id,
                    channel.id,
                    upload_channel.id,
                    weekday,
                    morning,
                    evening,
                    timezone,
                )
            text = f"{WEEKDAYS[weekday]} · {morning}/{evening} ({safe(timezone)})\n알림: {channel.mention} · 자료: {upload_channel.mention}"
            if delta:
                text += f"\n기존 미래 일정도 새 요일에 맞춰 {delta}일 뒤로 이동했습니다."
            await self.reload_rotation_file()
            text += "\n발표 순서는 NAS의 config/rotation.json에서 관리합니다. 파일 저장 후 30초 안에 반영됩니다."
            if self.rotation_errors.get(None) or self.rotation_errors.get(interaction.guild_id):
                text += "\n⚠️ 발표 순서 파일을 적용하지 못했습니다. NAS의 봇 로그를 확인해주세요."
            await interaction.followup.send(text, ephemeral=True)

        @tree.command(name="일정", description="다가오는 발표자와 휴강일을 확인합니다")
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
                holidays = self.store.holidays(interaction.guild_id, today)
            if not rows:
                raise UserError(
                    "아직 발표 순서가 없습니다. 서버의 config/rotation.json에 발표자를 등록해주세요."
                )
            items = [(row["seminar_date"], f"<@{row['presenter_id']}>") for row in rows]
            items.extend(
                (row["seminar_date"], "💤 휴강")
                for row in holidays
                if row["seminar_date"] <= rows[-1]["seminar_date"]
            )
            text = "\n".join(f"**{day}** · {speaker}" for day, speaker in sorted(items))
            embed = discord.Embed(title="🗓️ 세미나 일정", description=text, color=0x5B8DEF)
            embed.set_footer(
                text=f"{config['timezone']} · 아침 {config['morning']} / 저녁 {config['evening']}"
                + (
                    " · 발표 순서 파일 적용 오류: NAS 로그 확인"
                    if self.rotation_errors.get(None)
                    or self.rotation_errors.get(interaction.guild_id)
                    else ""
                )
            )
            await interaction.followup.send(embed=embed)

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
            description="업로드한 논문·발표 자료를 제목, 파일명, DOI, arXiv ID로 검색합니다",
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
                if row["seminar_date"]:
                    status = "발표 예정/당일" if row["seminar_date"] >= today else "발표일"
                    linked = f"{status}: {row['seminar_date']} · <@{row['presenter_id']}>"
                else:
                    linked = "발표 회차 미연결"
                value = f"구분: {KIND_LABELS[row['kind']]}\n{linked}\n파일: {safe(row['filename'], 160)}\n[원본 업로드]({row['message_url']}) · 업로더 <@{row['uploader_id']}>"
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
            name="자료수정", description="자동 인식한 자료 제목을 수정합니다 (업로더 또는 관리자)"
        )
        @app_commands.guild_only()
        async def rename(interaction: discord.Interaction, record_id: int, title: str) -> None:
            self.store.rename_document(
                interaction.guild_id, record_id, title, interaction.user.id, is_manager(interaction)
            )
            await interaction.response.send_message(
                f"#{record_id} 제목을 수정했습니다.", ephemeral=True
            )

        @tree.command(
            name="자료연결", description="자료를 발표 날짜에 연결합니다 (업로더 또는 관리자)"
        )
        @app_commands.guild_only()
        async def link(interaction: discord.Interaction, record_id: int, seminar_date: str) -> None:
            async with self.schedule_lock:
                self.store.link_document(
                    interaction.guild_id,
                    record_id,
                    parse_date(seminar_date),
                    interaction.user.id,
                    is_manager(interaction),
                )
                record = self.store.document(interaction.guild_id, record_id)
                status = self.store.submission(interaction.guild_id, record["session_id"])
            await interaction.response.send_message(
                f"#{record_id} 자료를 {safe(seminar_date)} 발표에 연결했습니다.\n"
                + submission_summary(status),
                ephemeral=True,
            )

        @tree.command(
            name="자료분류",
            description="자료를 발표 자료 또는 논문 PDF로 구분합니다 (버튼으로도 가능)",
        )
        @app_commands.guild_only()
        @app_commands.choices(
            kind=[
                app_commands.Choice(name=label, value=value) for value, label in KIND_LABELS.items()
            ]
        )
        async def classify(interaction: discord.Interaction, record_id: int, kind: str) -> None:
            async with self.schedule_lock:
                record = self.store.classify(
                    interaction.guild_id,
                    record_id,
                    kind,
                    interaction.user.id,
                    is_manager(interaction),
                )
                status = (
                    self.store.submission(interaction.guild_id, record["session_id"])
                    if record["session_id"]
                    else None
                )
            text = f"#{record_id} 자료를 **{KIND_LABELS[kind]}**로 구분했습니다."
            if status:
                text += "\n" + submission_summary(status)
            await interaction.response.send_message(text, ephemeral=True)

        @tree.command(
            name="제출현황", description="발표 자료 1개와 논문 PDF 3개 이상이 제출됐는지 확인합니다"
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
                    value=f"{safe(record['title'], 160)} · [업로드]({record['message_url']})",
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
                "**서버 관리자**\n"
                "`/설정` 요일·시간·알림/자료 채널 지정\n"
                "발표 순서는 NAS의 `config/rotation.json`에서 편집 (저장 후 30초 안에 적용)\n"
                "`/스왑` 두 날짜의 발표자 교환\n"
                "`/연기` 해당 주 휴강 + 이후 전체 1주 연기\n\n"
                "**모든 멤버**\n"
                "`/일정` 발표자와 휴강일 확인\n"
                "자료 채널에 **발표 자료 PDF/PPT/PPTX + 논문 PDF 3개 이상** 업로드 → 회차별 누적 기록\n"
                "구분이 애매한 PDF는 업로드 아래 버튼으로 **발표 자료 / 논문 PDF** 선택\n"
                "`/제출현황` 부족한 자료 확인 · `/자료분류` 구분 수정\n"
                "`/검색` 논문 검색 (kind:전체 로 발표 자료도 검색)\n"
                "`/자료수정` 제목 수정 · `/자료연결` 발표 회차 지정\n\n"
                "일정·자료는 재시작해도 유지됩니다. 스캔 PDF는 OCR 없이 파일명으로 기록됩니다."
            )
            await interaction.response.send_message(text, ephemeral=True)
