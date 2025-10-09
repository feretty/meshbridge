import os
import asyncio
import logging
import time
import nest_asyncio
import json
import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from meshtastic.serial_interface import SerialInterface
from pubsub import pub


nest_asyncio.apply()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("meshbridge.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger("meshtastic").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)

interface = None
application = None
CHANNEL_TO_CHAT = {}
MAIN_LOOP = None
ADMIN_USER_ID = None
NODE_NAME_CACHE = {}
NODE_NAME_FILE = "node_names.json"
FAVORITES_FILE = "favorites.json"
START_TIME = time.time()
MESSAGE_STATS = {"mesh_to_tg": 0, "tg_to_mesh": 0}
NODE_MESSAGE_COUNT = {}
NODE_MESSAGE_HISTORY = {}
MAX_HISTORY_DAYS = 7
SEEN_NODES = set()
BATTERY_VOLTAGE_HISTORY = {}
BATTERY_LOW_NOTIFIED = set()
BATTERY_LOW_THRESHOLD = 3.5

def get_node_suffix(node_id):
    if isinstance(node_id, str) and node_id.startswith('!'):
        try:
            num_id = int(node_id[1:], 16)
        except:
            return None
    else:
        num_id = node_id
    return f"{num_id & 0xFFFFFF:06X}"

def load_favorites():
    try:
        with open(FAVORITES_FILE, "r") as f:
            return set(json.load(f))
    except Exception as e:
        logger.warning(f"Не удалось загрузить {FAVORITES_FILE}: {e}")
        return set()

def save_favorites(favorites):
    try:
        with open(FAVORITES_FILE, "w") as f:
            json.dump(list(favorites), f, indent=2)
        logger.info(f"Сохранено {len(favorites)} избранных нод")
    except Exception as e:
        logger.error(f"Ошибка сохранения {FAVORITES_FILE}: {e}")

def load_node_name_cache():
    global NODE_NAME_CACHE, SEEN_NODES
    try:
        with open(NODE_NAME_FILE, "r", encoding="utf-8") as f:
            NODE_NAME_CACHE = json.load(f)
        SEEN_NODES = set(NODE_NAME_CACHE.keys())
        logger.info(f"📂 Кэш имён загружен из файла ({len(NODE_NAME_CACHE)} нод)")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось загрузить кэш имён: {e}")
        NODE_NAME_CACHE = {}
        SEEN_NODES = set()

def save_node_name_cache():
    try:
        with open(NODE_NAME_FILE, "w", encoding="utf-8") as f:
            json.dump(NODE_NAME_CACHE, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 Кэш имён сохранён в файл ({len(NODE_NAME_CACHE)} нод)")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось сохранить кэш имён: {e}")

def update_node_name_cache():
    global NODE_NAME_CACHE
    updated = 0
    added = 0
    if not interface or not hasattr(interface, 'nodes'):
        logger.warning("Нет данных о нодах Meshtastic для обновления кэша.")
        save_node_name_cache()
        return {"total": len(NODE_NAME_CACHE), "added": 0, "updated": 0}

    for node_id, node in interface.nodes.items():
        suffix = get_node_suffix(node_id)
        if suffix is None:
            continue
        user = node.get('user', {})
        name = user.get('shortName') or user.get('longName') or suffix

        if suffix in NODE_NAME_CACHE:
            if NODE_NAME_CACHE[suffix] != name:
                logger.info(f"✏️ Имя ноды {suffix} изменено: {NODE_NAME_CACHE[suffix]} → {name}")
                NODE_NAME_CACHE[suffix] = name
                updated += 1
        else:
            NODE_NAME_CACHE[suffix] = name
            added += 1

    total = len(NODE_NAME_CACHE)
    logger.info(f"🔄 Кэш имён обновлён: добавлено {added}, обновлено {updated}, всего {len(NODE_NAME_CACHE)} нод")
    save_node_name_cache()
    return {"total": total, "added": added, "updated": updated}

async def daily_reboot_task():
    """Ежедневная перезагрузка в 00:15"""
    while True:
        now = datetime.datetime.now()
        # Следующая 00:15
        next_reboot = now.replace(hour=0, minute=15, second=0, microsecond=0)
        if now >= next_reboot:
            next_reboot += datetime.timedelta(days=1)
        sleep_seconds = (next_reboot - now).total_seconds()
        logger.info(f"💤 Следующая перезагрузка: {next_reboot}")
        await asyncio.sleep(sleep_seconds)
        if interface:
            logger.info("🔄 Запуск ежедневной перезагрузки")
            interface.localNode.reboot()
            if ADMIN_USER_ID and application:
                await application.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text="🔄 Ежедневная перезагрузка выполнена"
                )

async def notify_new_nodes():
    global SEEN_NODES
    while True:
        try:
            if interface and hasattr(interface, 'nodes'):
                current_nodes = set()
                for node_id in interface.nodes:
                    suffix = get_node_suffix(node_id)
                    if suffix:
                        current_nodes.add(suffix)

                new_nodes = current_nodes - SEEN_NODES
                if new_nodes:
                    SEEN_NODES.update(new_nodes)
                    names = [NODE_NAME_CACHE.get(s, s) for s in new_nodes]
                    message = "🆕 Обнаружены новые ноды:\n" + "\n".join(names)
                    if ADMIN_USER_ID and application:
                        await application.bot.send_message(chat_id=ADMIN_USER_ID, text=message)
                        logger.info(f"🆕 Уведомление о новых нодах отправлено: {len(new_nodes)}")
            await asyncio.sleep(60)
        except Exception as e:
            logger.warning(f"Ошибка уведомления о новых нодах: {e}")
            await asyncio.sleep(60)

async def monitor_favorite_battery():
    while True:
        try:
            favorites = load_favorites()
            if interface and hasattr(interface, 'nodes'):
                for node_id, node in interface.nodes.items():
                    suffix = get_node_suffix(node_id)
                    if suffix in favorites:
                        metrics = node.get('deviceMetrics', {})
                        voltage = metrics.get('voltage')
                        if voltage is not None and voltage > 0:
                            
                            BATTERY_VOLTAGE_HISTORY[suffix] = voltage

                            if voltage < BATTERY_LOW_THRESHOLD and suffix not in BATTERY_LOW_NOTIFIED:
                                name = NODE_NAME_CACHE.get(suffix, suffix)
                                message = f"⚠️ Низкое напряжение на {name}: {voltage:.2f}V"
                                if ADMIN_USER_ID and application:
                                    await application.bot.send_message(chat_id=ADMIN_USER_ID, text=message)
                                    logger.info(f"🔋 Уведомление о низком заряде {name}: {voltage:.2f}V")
                                BATTERY_LOW_NOTIFIED.add(suffix)

                            elif voltage >= BATTERY_LOW_THRESHOLD and suffix in BATTERY_LOW_NOTIFIED:
                                BATTERY_LOW_NOTIFIED.discard(suffix)
                                logger.info(f"🔋 Заряд {suffix} восстановлен: {voltage:.2f}V")

            await asyncio.sleep(60)
        except Exception as e:
            logger.warning(f"Ошибка мониторинга избранных нод: {e}")
            await asyncio.sleep(60)

async def auto_update_names():
    while True:
        try:
            update_node_name_cache()
        except Exception as e:
            logger.warning(f"Ошибка автообновления кэша: {e}")
        await asyncio.sleep(1800)

async def monitor_meshtastic():
    last_warned = False
    while True:
        try:
            if not interface or not hasattr(interface, 'nodes') or not interface.nodes:
                if ADMIN_USER_ID and not last_warned and application:
                    await application.bot.send_message(
                        chat_id=ADMIN_USER_ID,
                        text="⚠️ Потеряна связь с Meshtastic!"
                    )
                    last_warned = True
            else:
                last_warned = False
            await asyncio.sleep(300)
        except Exception as e:
            logger.warning(f"Ошибка мониторинга Meshtastic: {e}")
            await asyncio.sleep(60)

def split_message(text, max_length=80):
    if len(text) <= max_length:
        return [text]
    words = text.split()
    parts = []
    current = []
    for word in words:
        if len(' '.join(current + [word])) <= max_length:
            current.append(word)
        else:
            parts.append(' '.join(current))
            current = [word]
    if current:
        parts.append(' '.join(current))
    return parts

def on_meshtastic_message(packet, interface):
    logger.debug(f"📥 Получено: {packet}")
    try:
        from_id = packet.get('from')
        to_id = packet.get('to', 0)
        if from_id is None:
            return

        my_node_id = interface.myInfo.my_node_num if interface and hasattr(interface, 'myInfo') else None
        is_direct = (to_id == my_node_id)

        decoded = packet.get('decoded', {})
        if 'user' in decoded:
            user = decoded['user']
            name = user.get('shortName') or user.get('longName')
            if name:
                suffix = f"{from_id & 0xFFFFFF:06X}"
                old_name = NODE_NAME_CACHE.get(suffix)
                if old_name != name:
                    logger.info(f"✏️ Имя ноды {suffix} изменено: {old_name} → {name}")
                NODE_NAME_CACHE[suffix] = name
                save_node_name_cache()

        suffix = f"{from_id & 0xFFFFFF:06X}"
        sender_name = NODE_NAME_CACHE.get(suffix, suffix)

        MESSAGE_STATS["mesh_to_tg"] += 1
        NODE_MESSAGE_COUNT[suffix] = NODE_MESSAGE_COUNT.get(suffix, 0) + 1
        today = time.strftime("%Y-%m-%d")
        if suffix not in NODE_MESSAGE_HISTORY:
            NODE_MESSAGE_HISTORY[suffix] = {}
        NODE_MESSAGE_HISTORY[suffix][today] = NODE_MESSAGE_HISTORY[suffix].get(today, 0) + 1

        if len(NODE_MESSAGE_HISTORY[suffix]) > MAX_HISTORY_DAYS:
            sorted_days = sorted(NODE_MESSAGE_HISTORY[suffix].keys())
            for day in sorted_days[:-MAX_HISTORY_DAYS]:
                del NODE_MESSAGE_HISTORY[suffix][day]

        if 'decoded' in packet and 'text' in packet['decoded']:
            text = packet['decoded']['text']
            channel = packet.get('channel', 0)
            message = f"[{sender_name}]: {text}"

            if is_direct:
                if ADMIN_USER_ID and application:
                    logger.info(f"🔐 Приватное сообщение → TG: {message}")
                    asyncio.run_coroutine_threadsafe(
                        application.bot.send_message(chat_id=ADMIN_USER_ID, text=message),
                        MAIN_LOOP
                    )
            else:
                chat_id = CHANNEL_TO_CHAT.get(channel)
                if chat_id and application:
                    logger.info(f"→ TG (ch{channel}): {message}")
                    asyncio.run_coroutine_threadsafe(
                        application.bot.send_message(chat_id=chat_id, text=message),
                        MAIN_LOOP
                    )
                else:
                    logger.warning(f"Сообщение в неизвестном канале: {channel}")
    except Exception as e:
        logger.exception("Ошибка в обработчике Meshtastic")

async def telegram_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    logger.info(f"📩 Получено из Telegram: chat_id={chat_id}, text='{text}'")

    display_name = user.full_name or user.username or f"tg_{user.id}"

    channel = None
    for ch_index, ch_chat_id in CHANNEL_TO_CHAT.items():
        if ch_chat_id == chat_id:
            channel = ch_index
            break

    enriched_text = f"[TG: {display_name}] {text}"
    parts = split_message(enriched_text, max_length=80)

    for i, part in enumerate(parts):
        try:
            if len(parts) > 1:
                part = f"{part} ({i+1}/{len(parts)})"
            logger.info(f"→ Mesh (ch{channel}): {part}")
            if interface:
                interface.sendText(part, channelIndex=channel)
                MESSAGE_STATS["tg_to_mesh"] += 1
            if i < len(parts) - 1:
                await asyncio.sleep(0.8)
        except Exception as e:
            logger.exception(f"Ошибка отправки части {i+1} в Meshtastic")

async def command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    try:
        raw_text = update.message.text.strip()
        if not raw_text:
            return

        if raw_text.startswith("@"):
            parts_at = raw_text[1:].split(maxsplit=1)
            if len(parts_at) < 2:
                await update.message.reply_text("❌ Формат: @Имя сообщение")
                return
            target_name = parts_at[0]
            message_text = parts_at[1]

            target_suffix = None
            for suffix, name in NODE_NAME_CACHE.items():
                if name == target_name or suffix == target_name.upper():
                    target_suffix = suffix
                    break

            if not target_suffix:
                await update.message.reply_text(f"❌ Нода '{target_name}' не найдена в кэше")
                return

            target_id = None
            for node_id in getattr(interface, 'nodes', {}):
                suffix = get_node_suffix(node_id)
                if suffix == target_suffix:
                    target_id = node_id
                    break

            if not target_id:
                await update.message.reply_text(f"❌ Нода '{target_name}' не в сети")
                return

            try:
                interface.sendText(message_text, destinationId=target_id, channelIndex=0)
                await update.message.reply_text(f"📨 Отправлено {target_name}: {message_text}")
            except Exception as e:
                await update.message.reply_text(f"⚠️ Ошибка: {e}")
            return

        if raw_text.startswith("/"):
            parts = raw_text[1:].split()
            if not parts:
                await update.message.reply_text("❓ Команда пустая")
                return

            cmd = parts[0].lower()
            args = parts[1:]

            if cmd == "help":
                help_text = (
                    "📊 Команды статистики:\n"
                    "/stats — полная статистика\n"
                    "/direct — прямые соседи\n"
                    "/battery — заряд батареи\n"
                    "/lastseen — последний контакт\n"
                    "\n🛠️ Команды управления:\n"
                    "/reload_names — обновить кэш имён\n"
                    "/dump_cache — показать кэш\n"
                    "/reset_cache — очистить кэш\n"
                    "/reset_nodedb — сбросить базу нод\n"
                    "/reboot — перезагрузка\n"
                    "/pos — отправить позицию\n"
                    "/ble true|false — управление BLE\n"
                    "/set <param> <value> — настройки\n"
                    "\n⭐ Управление избранными:\n"
                    "/fav_add <имя или суффикс> — добавить\n"
                    "/fav_del <имя или суффикс> — удалить\n"
                    "/fav_list — список избранных"
                )
                await update.message.reply_text(help_text)
                return

            if cmd == "fav_add" and args:
                target = args[0].upper()
                target_suffix = None
                for suffix, name in NODE_NAME_CACHE.items():
                    if name == target or suffix == target:
                        target_suffix = suffix
                        break
                if not target_suffix:
                    if len(target) == 6 and all(c in "0123456789ABCDEF" for c in target):
                        target_suffix = target
                    else:
                        await update.message.reply_text("❌ Нода не найдена и не похожа на суффикс (6 hex)")
                        return

                favorites = load_favorites()
                favorites.add(target_suffix)
                save_favorites(favorites)
                name = NODE_NAME_CACHE.get(target_suffix, target_suffix)
                await update.message.reply_text(f"✅ {name} добавлена в избранные")
                return

            if cmd == "fav_del" and args:
                target = args[0].upper()
                favorites = load_favorites()
                removed = False
                for suffix in list(favorites):
                    name = NODE_NAME_CACHE.get(suffix, suffix)
                    if suffix == target or name == target:
                        favorites.remove(suffix)
                        removed = True
                        break
                if removed:
                    save_favorites(favorites)
                    await update.message.reply_text("🗑 Удалена из избранных")
                else:
                    await update.message.reply_text("❌ Нода не найдена в избранных")
                return

            if cmd == "fav_list":
                favorites = load_favorites()
                if not favorites:
                    await update.message.reply_text("📭 Список избранных пуст")
                else:
                    lines = []
                    for suffix in sorted(favorites):
                        name = NODE_NAME_CACHE.get(suffix, suffix)
                        lines.append(f"{name} ({suffix})")
                    await update.message.reply_text("⭐ Избранные ноды:\n" + "\n".join(lines))
                return

            if cmd == "uptime":
                bot_uptime = int(time.time() - START_TIME)
                node_uptime = None
                if interface and hasattr(interface, 'myInfo'):
                    node_uptime = getattr(interface.myInfo, 'uptime', None)
                reply = f"⏱ Uptime бота: {bot_uptime//3600}ч {(bot_uptime%3600)//60}м"
                if node_uptime:
                    reply += f"\n⏱ Uptime устройства: {node_uptime//3600}ч {(node_uptime%3600)//60}м"
                await update.message.reply_text(reply)
                return

            if cmd == "nodeinfo" and args:
                suffix = args[0].upper()
                found = False
                for node_id, node in getattr(interface, 'nodes', {}).items():
                    if isinstance(node_id, str) and node_id.startswith('!'):
                        num_id = int(node_id[1:], 16)
                    else:
                        num_id = node_id
                    node_suffix = f"{num_id & 0xFFFFFF:06X}"
                    if node_suffix == suffix:
                        found = True
                        name = NODE_NAME_CACHE.get(node_suffix, node_suffix)
                        snr = node.get('snr', 'N/A')
                        last_heard = node.get('lastHeard', 0)
                        voltage = node.get('deviceMetrics', {}).get('voltage', 'N/A')
                        reply = (
                            f"ℹ️ Нода {name} ({node_suffix}):\n"
                            f"SNR: {snr}\n"
                            f"Батарея: {voltage}\n"
                            f"Последний контакт: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_heard))}"
                        )
                        await update.message.reply_text(reply)
                        break
                if not found:
                    await update.message.reply_text(f"❌ Нода {suffix} не найдена")
                return

            if cmd == "topnodes":
                if NODE_MESSAGE_COUNT:
                    top = sorted(NODE_MESSAGE_COUNT.items(), key=lambda x: x[1], reverse=True)[:5]
                    reply = "🏆 Топ-5 нод по сообщениям:\n" + "\n".join([f"{NODE_NAME_CACHE.get(s, s)}: {c}" for s, c in top])
                else:
                    reply = "Нет данных по активности нод."
                await update.message.reply_text(reply)
                return

            if cmd == "lastseen":
                lines = []
                now = time.time()
                for node_id, node in getattr(interface, 'nodes', {}).items():
                    if isinstance(node_id, str) and node_id.startswith('!'):
                        num_id = int(node_id[1:], 16)
                    else:
                        num_id = node_id
                    suffix = f"{num_id & 0xFFFFFF:06X}"
                    name = NODE_NAME_CACHE.get(suffix, suffix)
                    last_heard = node.get('lastHeard', None)
                    if last_heard is not None:
                        ago = int(now - last_heard)
                        lines.append(f"{name}: {ago//60} мин назад")
                    else:
                        lines.append(f"{name}: нет данных")
                lines = lines[:10]
                reply = "⏰ Последний контакт:\n" + "\n".join(lines) if lines else "Нет данных."
                await update.message.reply_text(reply)
                return

            if cmd == "battery_low":
                low_nodes = []
                for node_id, node in getattr(interface, 'nodes', {}).items():
                    metrics = node.get('deviceMetrics', {})
                    voltage = metrics.get('voltage')
                    if voltage is not None and voltage > 0 and voltage < 3.5:
                        if isinstance(node_id, str) and node_id.startswith('!'):
                            num_id = int(node_id[1:], 16)
                        else:
                            num_id = node_id
                        suffix = f"{num_id & 0xFFFFFF:06X}"
                        name = NODE_NAME_CACHE.get(suffix, suffix)
                        low_nodes.append(f"{name}: {voltage:.2f}V")
                reply = "🔋 Низкий заряд:\n" + "\n".join(low_nodes) if low_nodes else "Нет нод с низким зарядом."
                await update.message.reply_text(reply)
                return

            if cmd == "stats_today":
                today = time.strftime("%Y-%m-%d")
                lines = []
                for suffix, days in NODE_MESSAGE_HISTORY.items():
                    count = days.get(today, 0)
                    if count > 0:
                        lines.append(f"{NODE_NAME_CACHE.get(suffix, suffix)}: {count}")
                reply = "📈 Сообщения за сегодня:\n" + "\n".join(lines) if lines else "Нет сообщений за сегодня."
                await update.message.reply_text(reply)
                return

            if cmd == "snr_stats":
                snrs = []
                for node_id, node in getattr(interface, 'nodes', {}).items():
                    snr = node.get('snr')
                    if snr is not None:
                        snrs.append(snr)
                if snrs:
                    reply = f"SNR: min={min(snrs):.1f}, max={max(snrs):.1f}, avg={sum(snrs)/len(snrs):.1f}"
                else:
                    reply = "Нет данных по SNR."
                await update.message.reply_text(reply)
                return

            if cmd == "setname" and len(args) >= 2:
                suffix = args[0].upper()
                new_name = " ".join(args[1:])
                NODE_NAME_CACHE[suffix] = new_name
                save_node_name_cache()
                await update.message.reply_text(f"✅ Имя для {suffix} установлено: {new_name}")
                return

            if cmd == "reset_cache":
                NODE_NAME_CACHE.clear()
                SEEN_NODES.clear()
                save_node_name_cache()
                await update.message.reply_text("🗑 Кэш имён очищен.")
                return

            if cmd == "reset_nodedb":
                if interface:
                    interface.localNode.resetNodeDb()
                    await update.message.reply_text("🗑 База нод сброшена. Перезагрузка...")
                    await asyncio.sleep(5)
                    update_node_name_cache()
                else:
                    await update.message.reply_text("❌ Нет подключения к Meshtastic")
                return

            if cmd == "reload_names":
                stats = update_node_name_cache()
                reply = (
                    f"🔄 Кэш имён обновлён:\n"
                    f"Добавлено: {stats['added']}\n"
                    f"Обновлено: {stats['updated']}\n"
                    f"Всего: {stats['total']} нод"
                )
                await update.message.reply_text(reply)
                return

            if cmd == "dump_cache":
                if NODE_NAME_CACHE:
                    cache_lines = [f"{k}: {v}" for k, v in NODE_NAME_CACHE.items()]
                    reply = "Кэш имён:\n" + "\n".join(cache_lines)
                else:
                    reply = "Кэш пуст"
                await update.message.reply_text(reply)
                return

            if cmd == "reboot":
                if interface:
                    interface.localNode.reboot()
                    await update.message.reply_text("🔄 Перезагрузка запущена")
                    if ADMIN_USER_ID and application:
                        await application.bot.send_message(chat_id=ADMIN_USER_ID, text="🔄 Meshtastic перезагружается!")
                else:
                    await update.message.reply_text("❌ Нет подключения к Meshtastic")
                return

            if cmd == "stats":
                bot_uptime = int(time.time() - START_TIME)
                node_uptime = None
                if interface and hasattr(interface, 'myInfo'):
                    node_uptime = getattr(interface.myInfo, 'uptime', None)

                snrs = []
                for node_id, node in getattr(interface, 'nodes', {}).items():
                    snr = node.get('snr')
                    if snr is not None:
                        snrs.append(snr)

                top_nodes = []
                if NODE_MESSAGE_COUNT:
                    top = sorted(NODE_MESSAGE_COUNT.items(), key=lambda x: x[1], reverse=True)[:5]
                    top_nodes = [f"{NODE_NAME_CACHE.get(s, s)}: {c}" for s, c in top]

                today = time.strftime("%Y-%m-%d")
                today_stats = []
                for suffix, days in NODE_MESSAGE_HISTORY.items():
                    count = days.get(today, 0)
                    if count > 0:
                        today_stats.append(f"{NODE_NAME_CACHE.get(suffix, suffix)}: {count}")

                snr_today = []
                now = time.time()
                for node_id, node in getattr(interface, 'nodes', {}).items():
                    last_heard = node.get('lastHeard', 0)
                    snr = node.get('snr')
                    if snr is not None and now - last_heard <= 86400:
                        suffix = get_node_suffix(node_id)
                        if suffix:
                            name = NODE_NAME_CACHE.get(suffix, suffix)
                            snr_today.append((snr, name))

                reply_parts = [
                    "📊 Полная статистика:",
                    f"\n⏱ Uptime бота: {bot_uptime//3600}ч {(bot_uptime%3600)//60}м"
                ]
                if node_uptime:
                    reply_parts.append(f"⏱ Uptime устройства: {node_uptime//3600}ч {(node_uptime%3600)//60}м")

                if snrs:
                    reply_parts.append(f"\n📡 SNR: min={min(snrs):.1f}, max={max(snrs):.1f}, avg={sum(snrs)/len(snrs):.1f}")

                if snr_today:
                    snr_today.sort(key=lambda x: x[0], reverse=True)
                    top_snr = snr_today[:5]
                    snr_lines = [f"{name} (SNR: {snr:.1f})" for snr, name in top_snr]
                    reply_parts.append("\n🏆 Топ-5 нод по SNR за сегодня:")
                    reply_parts.extend(snr_lines)

                if top_nodes:
                    reply_parts.append("\n🏆 Топ-5 нод по сообщениям:")
                    reply_parts.extend(top_nodes)

                if today_stats:
                    reply_parts.append("\n📈 Сообщения за сегодня:")
                    reply_parts.extend(today_stats)

                await update.message.reply_text("\n".join(reply_parts))
                return

            if cmd == "pos":
                if not interface:
                    await update.message.reply_text("❌ Нет подключения к Meshtastic")
                    return
                try:
                    interface.sendPosition()
                    await update.message.reply_text("📍 Позиция отправлена")
                except Exception as e:
                    await update.message.reply_text(f"⚠️ Ошибка отправки позиции: {e}")
                return

            if cmd == "ble" and args:
                try:
                    enabled = args[0].lower() in ("true", "on", "1")
                    if interface:
                        prefs = interface.localNode.localConfig
                        prefs.bluetooth.enabled = enabled
                        interface.localNode.writeConfig("bluetooth")
                        await update.message.reply_text(f"✅ BLE {'включён' if enabled else 'выключен'}")
                    else:
                        await update.message.reply_text("❌ Нет подключения к Meshtastic")
                except Exception as e:
                    await update.message.reply_text(f"⚠️ Ошибка BLE: {e}")
                return

            if cmd == "set" and len(args) >= 2:
                param = args[0]
                value_str = " ".join(args[1:])
                if value_str.lower() in ("true", "false"):
                    value = value_str.lower() == "true"
                elif value_str.isdigit():
                    value = int(value_str)
                else:
                    value = value_str

                try:
                    if interface:
                        prefs = interface.localNode.localConfig
                        keys = param.split(".")
                        obj = prefs
                        for key in keys[:-1]:
                            obj = getattr(obj, key)
                        setattr(obj, keys[-1], value)
                        config_part = keys[0]
                        interface.localNode.writeConfig(config_part)
                        await update.message.reply_text(f"✅ {param} = {value}")
                    else:
                        await update.message.reply_text("❌ Нет подключения к Meshtastic")
                except Exception as e:
                    await update.message.reply_text(f"⚠️ Ошибка: {e}")
                return

            if cmd == "direct":
                direct_nodes = []
                cutoff_snr = 0.0
                now = time.time()

                if not interface or not hasattr(interface, 'nodes'):
                    await update.message.reply_text("❌ Нет подключения к Meshtastic")
                    return

                for node_id_str, node in interface.nodes.items():
                    if node_id_str == interface.myInfo.my_node_num:
                        continue

                    if isinstance(node_id_str, str) and node_id_str.startswith('!'):
                        try:
                            num_id = int(node_id_str[1:], 16)
                        except:
                            continue
                    else:
                        num_id = node_id_str

                    suffix = f"{num_id & 0xFFFFFF:06X}"
                    name = NODE_NAME_CACHE.get(suffix, suffix)
                    last_heard = node.get('lastHeard', None)
                    snr = node.get('snr')
                    if snr is None:
                        snr = -99.0

                    if last_heard is not None and now - last_heard <= 300 and snr >= cutoff_snr:
                        direct_nodes.append(f"{name} (SNR:{snr:.1f})")
                    elif last_heard is None and snr >= cutoff_snr:
                        direct_nodes.append(f"{name} (SNR:{snr:.1f}, время неизвестно)")

                if not direct_nodes:
                    reply = "📡 Прямых соседей не обнаружено"
                else:
                    reply = f"📡 Прямых соседей ({len(direct_nodes)}):\n" + "\n".join(direct_nodes)
                await update.message.reply_text(reply)
                return

            if cmd == "battery":
                battery_info = []
                if not interface or not hasattr(interface, 'nodes'):
                    await update.message.reply_text("❌ Нет подключения к Meshtastic")
                    return

                for node_id_str, node in interface.nodes.items():
                    if node_id_str == interface.myInfo.my_node_num:
                        continue

                    if isinstance(node_id_str, str) and node_id_str.startswith('!'):
                        try:
                            num_id = int(node_id_str[1:], 16)
                        except:
                            continue
                    else:
                        num_id = node_id_str

                    metrics = node.get('deviceMetrics', {})
                    voltage = metrics.get('voltage')
                    if voltage is not None and voltage > 0:
                        suffix = f"{num_id & 0xFFFFFF:06X}"
                        name = NODE_NAME_CACHE.get(suffix, suffix)
                        battery_info.append((voltage, f"{name}: {voltage:.2f}V"))

                battery_info.sort(key=lambda x: x[0])
                low_battery = [line for _, line in battery_info[:10]]

                if low_battery:
                    reply = "🔋 Напряжение батареи (ТОП-10 низких):\n" + "\n".join(low_battery)
                else:
                    reply = "🔋 Данные о батарее не получены"
                await update.message.reply_text(reply)
                return

            await update.message.reply_text("❓ Неизвестная команда. Используй /help")

        else:
            return

    except Exception as e:
        logger.exception("Ошибка в command_handler")
        await update.message.reply_text(f"💥 {e}")

async def connect_meshtastic():
    global interface
    while True:
        try:
            logger.info("Подключение к Meshtastic через /dev/ttyACM0...")
            interface = SerialInterface(devPath="/dev/ttyACM0")
            time.sleep(2)
            pub.subscribe(on_meshtastic_message, "meshtastic.receive.text")
            update_node_name_cache()
            logger.info("✅ Подключено к Meshtastic")
            return
        except Exception as e:
            logger.critical(f"❌ Не удалось подключиться к Meshtastic: {e}")
            if ADMIN_USER_ID and application:
                await application.bot.send_message(chat_id=ADMIN_USER_ID, text=f"❌ Ошибка подключения к Meshtastic: {e}\nПробую переподключиться через 30 секунд...")
            await asyncio.sleep(30)

async def main():
    global interface, application, CHANNEL_TO_CHAT, MAIN_LOOP, ADMIN_USER_ID

    MAIN_LOOP = asyncio.get_running_loop()

    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    CHAT_ID_PUBLIC_RAW = os.getenv("CHAT_ID_PUBLIC")
    CHAT_ID_PRIVATE_RAW = os.getenv("CHAT_ID_PRIVATE")
    MESH_CHANNEL_PUBLIC_RAW = os.getenv("MESH_CHANNEL_PUBLIC")
    MESH_CHANNEL_PRIVATE_RAW = os.getenv("MESH_CHANNEL_PRIVATE")
    ADMIN_USER_ID_RAW = os.getenv("ADMIN_USER_ID")

    if not all([BOT_TOKEN, CHAT_ID_PUBLIC_RAW, CHAT_ID_PRIVATE_RAW, 
                MESH_CHANNEL_PUBLIC_RAW, MESH_CHANNEL_PRIVATE_RAW]):
        logger.critical("❌ Отсутствуют обязательные переменные в .env")
        return

    try:
        CHAT_ID_PUBLIC = int(CHAT_ID_PUBLIC_RAW)
        CHAT_ID_PRIVATE = int(CHAT_ID_PRIVATE_RAW)
        MESH_CHANNEL_PUBLIC = int(MESH_CHANNEL_PUBLIC_RAW)
        MESH_CHANNEL_PRIVATE = int(MESH_CHANNEL_PRIVATE_RAW)
        ADMIN_USER_ID = int(ADMIN_USER_ID_RAW)
    except ValueError as e:
        logger.critical(f"❌ Некорректный формат ID: {e}")
        return

    CHANNEL_TO_CHAT = {
        MESH_CHANNEL_PUBLIC: CHAT_ID_PUBLIC,
        MESH_CHANNEL_PRIVATE: CHAT_ID_PRIVATE
    }
    logger.info(f"✅ Загружены настройки каналов: {CHANNEL_TO_CHAT}")

    load_node_name_cache()

    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        telegram_handler
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE,
        command_handler
    ))

    await connect_meshtastic()
    asyncio.create_task(auto_update_names())
    asyncio.create_task(monitor_meshtastic())
    asyncio.create_task(notify_new_nodes())
    asyncio.create_task(monitor_favorite_battery())
    asyncio.create_task(daily_reboot_task())

    logger.info("✅ Telegram бот запущен. Ожидание сообщений...")
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
