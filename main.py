import asyncio
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import (
    MessageEntityTextUrl,
    InputMediaPhoto,
    InputMediaDocument,
    InputPhoto,
    InputDocument,
    InputSingleMedia,
)

load_dotenv()

# ================= 从 .env 读取配置 =================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "channel_copy")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "")
SEND_DELAY = float(os.getenv("SEND_DELAY", "1.0"))
TIMEZONE = os.getenv("TIMEZONE", "UTC")
CONTACT_LABEL = os.getenv("CONTACT_LABEL", "投稿联系")
CONTACT_URL = os.getenv("CONTACT_URL", "https://t.me/my_ccvj3mnxbot")
MESSAGE_IDS = os.getenv("MESSAGE_IDS", "").strip()

SEPARATOR_RE = re.compile(r'^[ \t]*[-=_*—–~·•▬]{3,}[ \t]*$', re.MULTILINE)
# =====================================================


def _utf16_len(s: str) -> int:
    return len(s.encode('utf-16-le')) // 2


def parse_time_range():
    tz = ZoneInfo(TIMEZONE)
    start_str = os.getenv("START_DATE", "").strip()
    end_str = os.getenv("END_DATE", "").strip()
    last_days_str = os.getenv("LAST_DAYS", "").strip()

    if start_str:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=tz)
        if end_str:
            end_dt = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)
        else:
            end_dt = start_dt + timedelta(days=1)
    elif last_days_str:
        end_dt = datetime.now(tz)
        start_dt = end_dt - timedelta(days=int(last_days_str))
    else:
        raise ValueError("请在 .env 中设置 START_DATE 或 LAST_DAYS")

    if end_dt <= start_dt:
        raise ValueError("结束时间必须晚于开始时间")
    return start_dt, end_dt


def trim_message(msg) -> bool:
    """找到第一条分割线，把分割线及其以下内容去掉，同步裁剪 entities。"""
    text = msg.message or ""
    m = SEPARATOR_RE.search(text)
    if not m:
        return False
    new_text = text[:m.start()].rstrip()
    if new_text == text:
        return False
    new_len = _utf16_len(new_text)
    msg.message = new_text
    if msg.entities:
        kept = []
        for e in msg.entities:
            if e.offset >= new_len:
                continue
            if e.offset + e.length > new_len:
                e.length = new_len - e.offset
            if e.length > 0:
                kept.append(e)
        msg.entities = kept or None
    return True


def merge_group_captions(group) -> None:
    """把相册里各成员的 caption 合并到第一个成员上。"""
    full_text = ""
    all_entities = []
    for g in group:
        t = g.message or ""
        if not t:
            continue
        if full_text:
            full_text += "\n\n"
        base = _utf16_len(full_text)
        full_text += t
        if g.entities:
            for e in g.entities:
                e.offset += base
                all_entities.append(e)
    if full_text:
        group[0].message = full_text
        group[0].entities = all_entities or None
    for g in group[1:]:
        g.message = ""
        g.entities = None


def append_contact_link(msg) -> None:
    """在消息末尾追加可点击的链接实体。"""
    text = msg.message or ""
    prefix = (text + "\n\n") if text else ""
    offset = _utf16_len(prefix)
    length = _utf16_len(CONTACT_LABEL)
    msg.message = prefix + CONTACT_LABEL
    entity = MessageEntityTextUrl(offset=offset, length=length, url=CONTACT_URL)
    if msg.entities:
        msg.entities = list(msg.entities) + [entity]
    else:
        msg.entities = [entity]


def build_input_media(group):
    """
    使用 InputSingleMedia 显式包装每个媒体和其对应的 caption。
    这是 Telethon 中为相册成员单独指定 caption 的正确方式。
    """
    media_list = []
    for idx, g in enumerate(group):
        cap = g.message if idx == 0 else ""
        cap_ent = g.entities if idx == 0 else None

        # 1. 先创建底层的 InputMedia 对象（不带 caption）
        if g.photo:
            inp = InputPhoto(
                id=g.photo.id,
                access_hash=g.photo.access_hash,
                file_reference=g.photo.file_reference,
            )
            base_media = InputMediaPhoto(inp)
        elif g.document:
            inp = InputDocument(
                id=g.document.id,
                access_hash=g.document.access_hash,
                file_reference=g.document.file_reference,
            )
            base_media = InputMediaDocument(inp)
        else:
            print(f"  警告：消息 ID={g.id} 无 photo/document，跳过")
            continue

        # 2. 用 InputSingleMedia 包装，将 caption 和 entities 绑定到媒体上
        single_media = InputSingleMedia(
            media=base_media,
            random_id=os.urandom(8),  # 需要随机 ID
            message=cap,
            entities=cap_ent,
        )
        media_list.append(single_media)

    return media_list


async def collect_album(client, source, msg):
    """给定相册中的一条消息，收集整个相册的所有成员。"""
    if not msg.grouped_id:
        return [msg]
    gid = msg.grouped_id
    result = {msg.id: msg}
    async for m in client.iter_messages(source, offset_id=msg.id, limit=20):
        if m.grouped_id != gid:
            break
        result[m.id] = m
    async for m in client.iter_messages(source, min_id=msg.id, limit=20):
        if m.grouped_id != gid:
            break
        result[m.id] = m
    return sorted(result.values(), key=lambda x: x.id)


async def collect_by_ids(client, source, ids):
    """按 ID 获取消息，并自动展开相册。"""
    msgs = await client.get_messages(source, ids=ids)
    all_msgs = {}
    for m in msgs:
        if m is None:
            print(f"  警告：ID 对应的消息不存在或不可见")
            continue
        for am in await collect_album(client, source, m):
            all_msgs[am.id] = am
    return sorted(all_msgs.values(), key=lambda x: x.id)


async def send_one(client, target, msg):
    trim_message(msg)
    if msg.media is None and not (msg.message or "").strip():
        print(f"  消息 ID={msg.id} 截断后为空，跳过")
        return
    append_contact_link(msg)
    try:
        await client.send_message(target, msg)
        print(f"  已发送消息 ID={msg.id}")
    except Exception as e:
        print(f"  消息发送失败 ID={msg.id}: {e}")


async def send_album(client, target, group):
    for g in group:
        trim_message(g)
    merge_group_captions(group)
    append_contact_link(group[0])

    if _utf16_len(group[0].message) > 1024:
        print(f"  警告：相册 caption 超过 1024，将被截断")
        group[0].message = group[0].message[:1024]
        group[0].entities = None

    try:
        media = build_input_media(group)
        if not media:
            print(f"  相册无有效媒体，跳过")
            return
        # 直接传入 InputSingleMedia 列表即可，无需再传 caption 参数
        await client.send_file(target, media)
        print(f"  已发送相册（{len(media)} 个媒体，caption 长度 {_utf16_len(group[0].message)}）")
    except Exception as e:
        print(f"  相册发送失败 (grouped_id={group[0].grouped_id}): {e}")


async def main():
    if not API_ID or not API_HASH:
        raise ValueError("请在 .env 中设置 API_ID 和 API_HASH")
    if not SOURCE_CHANNEL or not TARGET_CHANNEL:
        raise ValueError("请在 .env 中设置 SOURCE_CHANNEL 和 TARGET_CHANNEL")

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()

    source = await client.get_entity(SOURCE_CHANNEL)
    target = await client.get_entity(TARGET_CHANNEL)

    if MESSAGE_IDS:
        ids = [int(x.strip()) for x in MESSAGE_IDS.split(",") if x.strip()]
        print(f"指定 ID 模式：{ids}")
        messages = await collect_by_ids(client, source, ids)
        print(f"共收集到 {len(messages)} 条消息（含相册展开）")
    else:
        start_dt, end_dt = parse_time_range()
        print(f"时间范围（{TIMEZONE}）：{start_dt}  ->  {end_dt}")
        messages = []
        async for msg in client.iter_messages(source, offset_date=end_dt):
            if msg.date < start_dt:
                break
            messages.append(msg)
        messages.reverse()
        print(f"共收集到 {len(messages)} 条消息")

    print("开始复制...")
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.grouped_id:
            group = [msg]
            j = i + 1
            while j < len(messages) and messages[j].grouped_id == msg.grouped_id:
                group.append(messages[j])
                j += 1
            await send_album(client, target, group)
            i = j
        else:
            await send_one(client, target, msg)
            i += 1
        await asyncio.sleep(SEND_DELAY)

    await client.disconnect()
    print("复制完成。")


if __name__ == "__main__":
    asyncio.run(main())
