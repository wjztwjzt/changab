import asyncio
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageEntityTextUrl

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
CONTACT_URL = os.getenv("CONTACT_URL", "https://t.em/my_ccvj3mnxbot")

# 匹配“分割线”行：单独一行，由 3 个以上 - = _ * — – ~ · • ▬ 组成
SEPARATOR_RE = re.compile(r'^[ \t]*[-=_*—–~·•▬]{3,}[ \t]*$', re.MULTILINE)


def _utf16_len(s: str) -> int:
    """Telegram 的 entity 偏移按 UTF-16 计算"""
    return len(s.encode('utf-16-le')) // 2


def parse_time_range():
    """根据 .env 计算 [start, end) 区间，返回两个 aware datetime。"""
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


START_DT, END_DT = parse_time_range()


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
    """
    把相册里每个成员的 caption 合并到第一个成员的 caption 上。
    Telegram 相册只显示第一个媒体的 caption，其余会隐藏；
    合并后所有文字都能保留在 B 频道。
    """
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
    """
    在消息末尾追加链接实体（客户端显示为可点击文字，视觉效果等同 markdown 链接）。
    注意：Telegram 客户端渲染的是实体，不会显示 [label](url) 这样的原始 markdown 源码。
    """
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


async def copy_channel_by_date():
    if not API_ID or not API_HASH:
        raise ValueError("请在 .env 中设置 API_ID 和 API_HASH")
    if not SOURCE_CHANNEL or not TARGET_CHANNEL:
        raise ValueError("请在 .env 中设置 SOURCE_CHANNEL 和 TARGET_CHANNEL")

    print(f"时间范围（{TIMEZONE}）：{START_DT}  ->  {END_DT}")

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()

    source = await client.get_entity(SOURCE_CHANNEL)
    target = await client.get_entity(TARGET_CHANNEL)

    # 1. 从新到旧收集时间范围内的消息
    print("正在收集消息...")
    messages = []
    async for msg in client.iter_messages(source, offset_date=END_DT):
        if msg.date < START_DT:
            break
        messages.append(msg)

    # 2. 反转成从旧到新
    messages.reverse()
    print(f"共收集到 {len(messages)} 条消息，开始复制...")

    # 3. 按顺序发送，合并相册，追加联系链接
    i = 0
    while i < len(messages):
        msg = messages[i]

        if msg.grouped_id:
            # 收集同一个相册的所有消息
            group = [msg]
            j = i + 1
            while j < len(messages) and messages[j].grouped_id == msg.grouped_id:
                group.append(messages[j])
                j += 1

            # 先逐条裁剪，再合并 caption，最后给第一个成员追加链接
            for g in group:
                trim_message(g)
            merge_group_captions(group)
            append_contact_link(group[0])

            try:
                await client.send_file(target, group)
                print(f"  已发送相册（{len(group)} 个媒体）")
            except Exception as e:
                print(f"  相册发送失败 (grouped_id={msg.grouped_id}): {e}")

            i = j

        else:
            trim_message(msg)

            # 纯文本消息截断后为空则跳过（Telegram 不允许发空文本）
            if msg.media is None and not (msg.message or "").strip():
                print(f"  消息 ID={msg.id} 截断后为空，跳过")
                i += 1
                await asyncio.sleep(SEND_DELAY)
                continue

            append_contact_link(msg)

            try:
                await client.send_message(target, msg)
                print(f"  已发送消息 ID={msg.id}")
            except Exception as e:
                print(f"  消息发送失败 ID={msg.id}: {e}")

            i += 1

        await asyncio.sleep(SEND_DELAY)

    await client.disconnect()
    print("复制完成。")


if __name__ == "__main__":
    asyncio.run(copy_channel_by_date())
