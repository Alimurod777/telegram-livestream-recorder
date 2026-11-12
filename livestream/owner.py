"""Owner administration adapted to the recorder's Bot API and SQLite stack."""
import asyncio
from dataclasses import replace
import logging

from .chat_log import LogPolicy

logger = logging.getLogger(__name__)

OWNER_COMMANDS = {'/owner', '/ownerhelp', '/maintenance', '/stats', '/logchannel'}
SAFE_COMMANDS = {'/help', '/status', '/send', '/stop', '/cancel', '/logout'}
MAINTENANCE_NOTICE = '🔧 Botda texnik xizmat ketmoqda. Keyinroq urinib ko‘ring. /status, /send va /stop ishlaydi.'

OWNER_HELP = (
    '👑 Owner paneli\n'
    '/maintenance on|off|status — texnik xizmat rejimi\n'
    '/stats — umumiy statistika\n'
    '/logchannel set <ID yoki @username> — suhbat log manzili\n'
    '/logchannel status|on|off|remove|reset|test\n'
    '/logchannel exclude|include <user_id>\n'
    '/logchannel excluded — istisnolar\n'
    'Owner chati va login ma’lumotlari log qilinmaydi.\n'
    'Xato diagnostikasi LOG_CHANNEL_ID orqali alohida ishlaydi.'
)


class MaintenanceActive(RuntimeError):
    pass


class OwnerControls:
    def __init__(self, db, config, chat_logger):
        self.db, self.config, self.chat_logger = db, config, chat_logger
        self.owner_id = getattr(config, 'OWNER_ID', 0)
        self.maintenance = bool(getattr(config, 'MAINTENANCE_MODE', False))
        self.loaded = False
        self.lock = asyncio.Lock()

    def blocked(self, user_id):
        return self.maintenance and user_id != self.owner_id

    def env_policy(self):
        target = getattr(self.config, 'CHAT_LOG_CHANNEL_ID', 0)
        return LogPolicy(target, bool(target))

    async def load(self):
        async with self.lock:
            if self.loaded:
                return
            data = await asyncio.to_thread(self.db.get_owner_settings)
            self.maintenance = data.get('maintenance', self.maintenance)
            raw = data.get('chat_log')
            if raw:
                policy = (replace(self.env_policy(), excluded=tuple(raw['excluded'])) if raw.get('use_env')
                          else LogPolicy(raw['chat_id'], raw['enabled'], tuple(raw['excluded'])))
            else:
                policy = self.env_policy()
            await self.chat_logger.configure(policy)
            self.loaded = True

    async def save_policy(self, policy, *, reset=False):
        async with self.lock:
            # Persist before publishing: a failed write never claims success.
            raw = dict(use_env=reset, chat_id=policy.chat_id, enabled=policy.enabled, excluded=list(policy.excluded))
            await asyncio.to_thread(self.db.set_owner_setting, 'chat_log', raw)
            await self.chat_logger.configure(policy)

    async def rpc(self, api, method, data=None):
        result = await asyncio.to_thread(api, method, data or {}, timeout=10)
        if not result.get('ok'):
            raise ValueError('Telegram manzil yoki bot huquqlarini tasdiqlamadi.')
        return result.get('result')

    async def validate_chat(self, api, target):
        chat = await self.rpc(api, 'getChat', {'chat_id': target})
        if chat.get('type') not in {'channel', 'group', 'supergroup'}:
            raise ValueError('Log manzili kanal yoki guruh bo‘lishi kerak.')
        me = await self.rpc(api, 'getMe')
        member = await self.rpc(api, 'getChatMember', {'chat_id': chat['id'], 'user_id': me['id']})
        if member.get('status') not in {'administrator', 'creator'}:
            raise ValueError('Botni log kanal/guruhida admin qiling.')
        if chat['type'] == 'channel' and member['status'] != 'creator' and not member.get('can_post_messages'):
            raise ValueError('Botga kanalda xabar yuborish huquqini bering.')
        return int(chat['id'])

    async def handle(self, uid, text, api, send, recorder, waiting):
        parts = text.split()
        command = parts[0].split('@', 1)[0].lower()
        if command not in OWNER_COMMANDS:
            return False
        if uid != self.owner_id or not self.owner_id:
            await send(uid, '⛔ Bu komanda faqat owner uchun.')
            return True
        await self.load()
        try:
            if command in {'/owner', '/ownerhelp'}:
                await send(uid, OWNER_HELP)
            elif command == '/maintenance':
                action = parts[1].lower() if len(parts) == 2 else 'status' if len(parts) == 1 else ''
                if action not in {'on', 'off', 'status'}:
                    raise ValueError('/maintenance on|off|status')
                if action != 'status':
                    async with self.lock:
                        enabled = action == 'on'
                        await asyncio.to_thread(self.db.set_owner_setting, 'maintenance', enabled)
                        self.maintenance = enabled
                    for state in waiting.values():
                        state.setdefault('wake', asyncio.Event()).set()
                await send(uid, f"🔧 Maintenance: {'ON' if self.maintenance else 'OFF'}\n"
                           'Boshlangan yozuvlar va yuborishlar davom etadi. Sozlama SQLite’da saqlanadi.')
            elif command == '/stats':
                stats = await asyncio.to_thread(self.db.owner_stats)
                await send(uid, f"Faol yozuvlar: {len(recorder.active_sessions)}\n"
                           f"Kutishlar: {len(waiting)}\nSaqlangan loginlar: {stats['user_sessions']}\n"
                           f"Pool akkauntlar: {stats['accounts']}\nYozuvlar tarixi: {stats['recordings']}\n"
                           f"Maintenance: {'ON' if self.maintenance else 'OFF'}")
            else:
                await self.log_command(parts[1:], api, lambda text: send(uid, text))
        except ValueError as error:
            await send(uid, str(error))
        except Exception as error:
            logger.exception('Owner command failed: user=%s command=%s', uid, command)
            await send(uid, f'❌ Sozlama bajarilmadi: {type(error).__name__}. Manzil, bot huquqlari va diskni tekshiring.')
        return True

    async def log_command(self, parts, api, reply):
        action = parts[0].lower() if parts else 'status'
        policy = self.chat_logger.policy
        if action == 'status' and len(parts) <= 1:
            await reply(f"Log: {'ON' if policy.enabled else 'OFF'}\nManzil: {policy.chat_id or 'belgilanmagan'}\n"
                        f"Navbat: {self.chat_logger.queue.qsize()} | Yuborildi: {self.chat_logger.sent}\n"
                        f"Xato: {self.chat_logger.failed} | Tashlab ketildi: {self.chat_logger.dropped}\n"
                        'Owner doim istisno. Sozlamalar SQLite’da saqlanadi.\n/logchannel set <ID yoki @username>')
            return
        if action == 'excluded' and len(parts) == 1:
            ids = sorted(set(policy.excluded) | {self.owner_id})
            await reply('Log qilinmaydigan IDlar:\n' + '\n'.join(map(str, ids)))
            return
        if action in {'exclude', 'include'} and len(parts) == 2:
            uid = int(parts[1])
            if uid <= 0:
                raise ValueError('Musbat user ID kiriting.')
            excluded = set(policy.excluded)
            if action == 'exclude':
                excluded.add(uid)
            else:
                excluded.discard(uid)
            policy = replace(policy, excluded=tuple(sorted(excluded)))
        elif action == 'set' and len(parts) == 2:
            target = parts[1]
            if target.lstrip('-').isdigit():
                target = int(target)
            elif not target.startswith('@'):
                raise ValueError('Kanal/guruh ID yoki @username kiriting.')
            policy = replace(policy, chat_id=await self.validate_chat(api, target), enabled=True)
        elif len(parts) == 1 and action in {'on', 'off', 'remove', 'reset', 'test'}:
            if action == 'off':
                policy = replace(policy, enabled=False)
            elif action == 'remove':
                policy = replace(policy, chat_id=0, enabled=False)
            elif action == 'reset':
                policy = replace(self.env_policy(), excluded=policy.excluded)
                if policy.chat_id:
                    await self.validate_chat(api, policy.chat_id)
            else:
                if not policy.chat_id:
                    raise ValueError('Avval /logchannel set <ID yoki @username> ishlating.')
                await self.validate_chat(api, policy.chat_id)
                if action == 'test':
                    if not policy.enabled:
                        raise ValueError('Log o‘chiq. Avval /logchannel on ishlating.')
                    await self.rpc(api, 'sendMessage', {'chat_id': policy.chat_id, 'text': '✅ Log kanal testi.'})
                    await reply('✅ Test xabari yuborildi.')
                    return
                policy = replace(policy, enabled=True)
        else:
            raise ValueError('/logchannel set <ID yoki @username>\n/logchannel status|on|off|remove|reset|test\n'
                             '/logchannel exclude|include <user_id>\n/logchannel excluded')
        await self.save_policy(policy, reset=action == 'reset')
        await reply(f"✅ Log: {'ON' if policy.enabled else 'OFF'} | Manzil: {policy.chat_id or 'belgilanmagan'}. Saqlandi.")
