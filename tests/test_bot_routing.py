#!/usr/bin/env python3
"""
Сквозная проверка маршрутизации бота: настоящие апдейты через Dispatcher,
с подменённым транспортом вместо Telegram.

Юнит-тесты проверяют функции поиска, а этот файл — что нажатие кнопки и
следующий за ним текст попадают в нужный обработчик. Именно так нашлась
дыра, которой юнит-тесты не видели: режим поиска студента снимается только
при выборе человека, и введённый после него номер группы уходил в поиск
студента вместо выбора группы.

Сеть и Telegram не нужны.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.database as db
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update, Message, Chat, User, CallbackQuery


class FakeSession(BaseSession):
    """Ловит исходящие вызовы вместо похода в Telegram."""

    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b''

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.sent.append(method)
        if name in ('SendMessage', 'EditMessageText'):
            return Message(message_id=len(self.sent) + 1000, date=datetime.now(),
                           chat=Chat(id=1, type='private'),
                           text=getattr(method, 'text', '') or '')
        return True


_DISPATCHER = None


def shared_dispatcher():
    """
    router в bot/main.py — модульный синглтон, и прицепить его можно только
    к одному Dispatcher. Поэтому диспетчер один на весь модуль, а изоляция
    тестов достигается своим chat_id на каждый тест: состояние FSM хранится
    по паре (чат, пользователь) и между тестами не протекает.
    """
    global _DISPATCHER
    if _DISPATCHER is None:
        import bot.main as bot_main
        _DISPATCHER = Dispatcher(storage=MemoryStorage())
        _DISPATCHER.include_router(bot_main.router)
    return _DISPATCHER


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    """Обвязка: своя временная база, подменённый транспорт, свой чат."""

    _chat_seq = 100

    @classmethod
    def setUpClass(cls):
        import bot.main as bot_main
        cls.bot_main = bot_main
        cls.dp = shared_dispatcher()

    def seed(self, conn):
        """Данные под конкретный тест-класс."""

    async def asyncSetUp(self):
        BotRoutingTest._chat_seq += 1
        self.chat = Chat(id=BotRoutingTest._chat_seq, type='private')
        self.user = User(id=BotRoutingTest._chat_seq, is_bot=False, first_name='Тест')

        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)

        # Боевую базу не трогаем ни при каких обстоятельствах
        self._saved_db_path = db.DB_PATH
        db.DB_PATH = Path(self.db_path)
        self.addCleanup(self._restore_db)

        from core.db_students import ensure_tables
        conn = db.get_connection()
        ensure_tables(conn)
        conn.execute("INSERT INTO faculties (id,code,name,domain) "
                     "VALUES (1,'socio','Соцфак','http://x')")
        conn.execute("INSERT INTO groups_ (id,faculty_id,code,department,program) "
                     "VALUES (1,1,'с403','Бакалавриат','Соц')")
        for site_id, name in [('101', 'Смирнов Алексей Петрович'),
                              ('102', 'Смирнова Дарья Ивановна')]:
            conn.execute("INSERT INTO students (group_id,site_id,full_name,short_name) "
                         "VALUES (1,?,?,?)", (site_id, name, name.split()[0]))
        conn.execute("""INSERT INTO lessons
                        (group_id,date,pair_number,time_start,time_end,subject,teacher)
                        VALUES (1,'2026-09-08',1,'09:00','10:30','Социология','Смирнов В.А.')""")
        self.seed(conn)
        conn.commit()
        conn.close()

        self.session = FakeSession()
        self.bot = Bot(token='123456789:AAFakeTokenForLocalRoutingTestOnly___',
                       session=self.session)
        self._update_id = 0

    def _restore_db(self):
        db.DB_PATH = self._saved_db_path
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    async def asyncTearDown(self):
        await self.bot.session.close()

    # --- отправка апдейтов ---

    def _next_id(self):
        self._update_id += 1
        return self._update_id

    async def send_text(self, text: str):
        uid = self._next_id()
        self.session.sent.clear()
        await self.dp.feed_update(self.bot, Update(update_id=uid, message=Message(
            message_id=uid, date=datetime.now(), chat=self.chat,
            from_user=self.user, text=text)))
        return self._collect()

    async def press(self, data: str):
        uid = self._next_id()
        self.session.sent.clear()
        await self.dp.feed_update(self.bot, Update(update_id=uid, callback_query=CallbackQuery(
            id=str(uid), from_user=self.user, chat_instance='x', data=data,
            message=Message(message_id=999, date=datetime.now(),
                            chat=self.chat, text='.'))))
        return self._collect()

    def _collect(self):
        texts, buttons = [], []
        for method in self.session.sent:
            t = getattr(method, 'text', None)
            if t:
                texts.append(t)
            mk = getattr(method, 'reply_markup', None)
            if mk is not None and getattr(mk, 'inline_keyboard', None):
                buttons += [b.text for row in mk.inline_keyboard for b in row]
        return ' | '.join(texts), buttons


class BotRoutingTest(BotTestCase):
    """Маршрутизация: кнопка и следующий за ней текст."""


    async def test_teacher_mode_returns_only_teachers(self):
        """Главный баг: однофамилец-студент заслонял преподавателя."""
        await self.send_text("👨‍🏫 Преподаватель")
        text, buttons = await self.send_text("Смирнов")

        self.assertIn('преподавател', text.lower())
        self.assertEqual(buttons, ['Смирнов В.А.'])

    async def test_lowercase_surname_is_found(self):
        """LOWER() в SQLite кириллицу не трогает — ловим регресс."""
        await self.send_text("👨‍🏫 Преподаватель")
        _, buttons = await self.send_text("смирнов")
        self.assertEqual(buttons, ['Смирнов В.А.'])

    async def test_main_button_clears_state(self):
        """Нажал «Преподаватель», передумал, нажал «Сегодня» — режим снят."""
        await self.send_text("👨‍🏫 Преподаватель")
        await self.send_text("📅 Сегодня")
        _, buttons = await self.send_text("Смирнов")

        self.assertIn('Смирнов В.А.', buttons)
        self.assertTrue(any('Смирнов Алексей' in b for b in buttons))

    async def test_without_state_shows_both(self):
        """Без режима не угадываем, а показываем и студентов, и преподавателей."""
        text, buttons = await self.send_text("Смирнов")

        self.assertIn('студентов: 2', text)
        self.assertIn('преподавателей: 1', text)
        self.assertEqual(len(buttons), 3)

    async def test_student_mode_returns_only_students(self):
        await self.press('find_by_name')
        _, buttons = await self.send_text("Смирнов")

        self.assertNotIn('Смирнов В.А.', buttons)
        self.assertEqual(len(buttons), 2)

    async def test_group_code_works_in_any_mode(self):
        """
        Режим поиска студента снимается только выбором человека. Набранный
        после него номер группы обязан всё равно выбрать группу: фамилий
        с цифрами не бывает.
        """
        await self.press('find_by_name')
        text, _ = await self.send_text("403")

        self.assertIn('с403', text)
        self.assertIn('Группа', text)

    async def test_unknown_query_explains_options(self):
        text, buttons = await self.send_text("Абырвалг")
        self.assertEqual(buttons, [])
        self.assertIn('ничего не нашлось', text)


class LanguagePickerTest(BotTestCase):
    """
    Выбор языкового потока. Хранится смыслом (предмет + преподаватель),
    а не номером потока: номер живёт один семестр.
    """

    def seed(self, conn):
        # Захарова ведёт два потока — как на живом сайте: там таких
        # сочетаний 18 из 50, и различить их можно только расписанием
        streams = [
            ('Английский язык', 'Рассошенко Ж.В.', 'с403-2', 2, '2099-01-15'),
            ('Английский язык', 'Захарова Д.С.', 'с403-3', 3, '2099-01-15'),
            ('Английский язык', 'Захарова Д.С.', 'с403-4', 4, '2099-01-16'),
            ('Немецкий язык', 'Шмидт А.А.', 'с403-7', 3, '2099-01-15'),
        ]
        for subject, teacher, subgroup, pair, day in streams:
            conn.execute(
                """INSERT INTO lessons
                   (group_id,date,pair_number,time_start,time_end,
                    subject,room,teacher,subgroup)
                   VALUES (1,?,?,'10:40','12:10',?,'320',?,?)""",
                (day, pair, subject, teacher, subgroup))

    async def pick_group(self):
        await self.send_text("403")

    async def test_offers_languages(self):
        await self.pick_group()
        text, buttons = await self.send_text("/язык")

        self.assertIn('Английский язык', buttons)
        self.assertIn('Немецкий язык', buttons)
        self.assertIn('Показывать все', buttons)

    async def test_single_teacher_language_saved_at_once(self):
        """У немецкого один преподаватель — второй шаг не нужен."""
        await self.pick_group()
        await self.send_text("/язык")
        text, _ = await self.press(f"lang:s:{self.bot_main.subject_hash('Немецкий язык')}")

        self.assertIn('Немецкий язык', text)
        self.assertIn('Твой язык', text)

    async def test_language_button_works_like_command(self):
        await self.pick_group()
        text, buttons = await self.send_text(self.bot_main.LANG_BUTTON)
        self.assertIn('Английский язык', buttons)

    async def test_lists_streams_not_just_teachers(self):
        await self.pick_group()
        await self.send_text("/язык")
        shash = self.bot_main.subject_hash('Английский язык')

        text, buttons = await self.press(f"lang:s:{shash}")

        self.assertEqual(len(buttons), 4, "три потока + «любой преподаватель»")
        self.assertIn('Любой преподаватель', buttons)
        # У Рассошенко один поток — расписание в кнопке лишнее
        self.assertIn('Рассошенко Ж.В.', buttons)
        # У Захаровой два — их надо различить временем
        zaharova = [b for b in buttons if b.startswith('Захарова')]
        self.assertEqual(len(zaharova), 2)
        for b in zaharova:
            self.assertIn('·', b, "у неоднозначного преподавателя показано время")
        self.assertNotEqual(zaharova[0], zaharova[1], "кнопки должны различаться")

    async def test_picking_one_of_two_streams_of_same_teacher(self):
        await self.pick_group()
        await self.send_text("/язык")
        shash = self.bot_main.subject_hash('Английский язык')
        await self.press(f"lang:s:{shash}")

        text, _ = await self.press(f"lang:v:{shash}:{self.bot_main.subject_hash('с403-4')}")
        self.assertIn('Захарова Д.С.', text)

        conn = db.get_connection()
        choice = db.resolve_user_stream(conn, self.chat.id, 1)
        conn.close()
        self.assertEqual(choice['subgroup'], 'с403-4')
        self.assertEqual(choice['teacher'], 'Захарова Д.С.')

    async def test_stale_stream_number_degrades_to_teacher(self):
        """
        Номер потока живёт один семестр. Когда он исчезает, выбор должен
        скатиться до преподавателя, а не пропасть.
        """
        await self.pick_group()
        conn = db.get_connection()
        db.set_user_stream(conn, self.chat.id, 1,
                           'Английский язык', 'Захарова Д.С.', 'с403-99')
        choice = db.resolve_user_stream(conn, self.chat.id, 1)
        conn.close()

        self.assertEqual(choice['subject'], 'Английский язык')
        self.assertEqual(choice['teacher'], 'Захарова Д.С.')
        self.assertEqual(choice['subgroup'], '', "исчезнувший номер отброшен")

    async def test_any_teacher_stores_subject_only(self):
        await self.pick_group()
        await self.send_text("/язык")
        shash = self.bot_main.subject_hash('Английский язык')
        await self.press(f"lang:s:{shash}")
        text, _ = await self.press(f"lang:any:{shash}")

        self.assertIn('любой преподаватель', text.lower())
        conn = db.get_connection()
        choice = db.resolve_user_stream(conn, self.chat.id, 1)
        conn.close()
        self.assertEqual(choice['teacher'], '')
        self.assertEqual(choice['subgroup'], '')

    async def test_choice_survives_and_filters(self):
        await self.pick_group()
        await self.send_text("/язык")
        shash = self.bot_main.subject_hash('Немецкий язык')
        await self.press(f"lang:s:{shash}")

        conn = db.get_connection()
        choice = db.resolve_user_stream(conn, self.chat.id, 1)
        conn.close()
        self.assertEqual(choice['subject'], 'Немецкий язык')
        self.assertEqual(choice['teacher'], 'Шмидт А.А.')

    async def test_show_all_clears_choice(self):
        await self.pick_group()
        await self.send_text("/язык")
        await self.press(f"lang:s:{self.bot_main.subject_hash('Немецкий язык')}")
        text, _ = await self.press("lang:all")

        self.assertIn('все потоки', text)
        conn = db.get_connection()
        self.assertIsNone(db.resolve_user_stream(conn, self.chat.id, 1))
        conn.close()

    async def test_guide_is_short_and_offers_sections(self):
        """
        Гайд одним куском был простынёй на две тысячи знаков. Теперь короткий
        экран с разделами по кнопкам.
        """
        await self.pick_group()
        text, buttons = await self.send_text("/помощь")

        self.assertLess(len(text), 400, "главный экран должен быть коротким")
        self.assertTrue(any('Предметы по выбору' in b for b in buttons))
        self.assertTrue(any('Языки' in b for b in buttons))

    async def test_keyboard_groups_personal_buttons(self):
        """Предметы, язык и помощь — в одном ряду: это то, что человек настраивает."""
        rows = [[b.text for b in row] for row in self.bot_main.LANG_KEYBOARD.keyboard]
        personal = next(r for r in rows if '📋 Предметы' in r)

        self.assertIn(self.bot_main.LANG_BUTTON, personal)
        self.assertIn(self.bot_main.HELP_BUTTON, personal)

        plain = [[b.text for b in row] for row in self.bot_main.MAIN_KEYBOARD.keyboard]
        personal_plain = next(r for r in plain if '📋 Предметы' in r)
        self.assertIn(self.bot_main.HELP_BUTTON, personal_plain)
        self.assertNotIn(self.bot_main.LANG_BUTTON, personal_plain)

    async def test_help_button_works_like_command(self):
        await self.pick_group()
        text, buttons = await self.send_text(self.bot_main.HELP_BUTTON)
        self.assertIn('Как пользоваться', text)
        self.assertTrue(buttons)

    async def test_sections_open_and_go_back(self):
        await self.pick_group()
        await self.send_text("/помощь")

        text, buttons = await self.press("help:lang")
        self.assertIn('не у всех', text.lower())
        self.assertIn('← Назад', buttons)

        text, buttons = await self.press("help:subjects")
        self.assertIn('на одну пару', text.lower())

        text, buttons = await self.press("help:back")
        self.assertIn('Как пользоваться', text)
        self.assertTrue(any('Предметы по выбору' in b for b in buttons))

    async def test_no_language_section_without_streams(self):
        """Кнопки про языки не должно быть у тех, у кого языков нет."""
        conn = db.get_connection()
        conn.execute("DELETE FROM lessons WHERE subgroup != ''")
        conn.commit()
        conn.close()

        await self.pick_group()
        text, buttons = await self.send_text("/помощь")
        self.assertFalse(any('Языки' in b for b in buttons))
        self.assertTrue(any('Предметы по выбору' in b for b in buttons))

    async def test_stale_choice_is_ignored(self):
        """
        Главное свойство: сменился семестр, языка больше нет — фильтр
        молча выключается, а не ломает расписание.
        """
        await self.pick_group()
        conn = db.get_connection()
        db.set_user_stream(conn, self.chat.id, 1, 'Испанский язык', 'Гарсиа К.А.')
        self.assertIsNone(db.resolve_user_stream(conn, self.chat.id, 1))
        conn.close()

    async def test_group_without_streams(self):
        conn = db.get_connection()
        conn.execute("DELETE FROM lessons WHERE subgroup != ''")
        conn.commit()
        conn.close()

        await self.pick_group()
        text, buttons = await self.send_text("/язык")
        self.assertIn('нет языковых потоков', text)
        self.assertEqual(buttons, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)


class ErrorHandlerTest(BotTestCase):
    """
    Немой отказ.

    В боте не было ни одного try/except и ни одного обработчика ошибок:
    исключение в хендлере означало, что человек просто не получает ответа,
    а владелец об этом не узнаёт. Проверяем, что теперь и отвечаем, и
    жалуемся админу — но не потоком одинаковых сообщений.
    """

    def seed(self, conn):
        db.set_user_group(conn, self.chat.id, 1)

    def break_handler(self):
        """Сломать то, на чём стоит «Сегодня»."""
        def boom(*a, **kw):
            raise RuntimeError("база отвалилась")
        saved = self.bot_main.get_schedule_for_date
        self.bot_main.get_schedule_for_date = boom
        self.addCleanup(lambda: setattr(
            self.bot_main, 'get_schedule_for_date', saved))

    def catch_alerts(self):
        """Перехватить алерты админу, чтобы не ходить в Telegram."""
        sent = []
        saved = self.bot_main.send_admin_alert
        self.bot_main.send_admin_alert = lambda text: sent.append(text) or True
        self.addCleanup(lambda: setattr(
            self.bot_main, 'send_admin_alert', saved))
        self.bot_main._last_alert.clear()
        self.addCleanup(self.bot_main._last_alert.clear)
        return sent

    async def test_user_gets_an_answer_instead_of_silence(self):
        """Главное: раньше здесь не приходило вообще ничего."""
        self.catch_alerts()
        self.break_handler()

        text, _ = await self.send_text("📅 Сегодня")

        self.assertTrue(text, "пользователь не получил ответа")
        self.assertIn('сломалось', text.lower())

    async def test_admin_is_told(self):
        sent = self.catch_alerts()
        self.break_handler()

        await self.send_text("📅 Сегодня")

        self.assertEqual(len(sent), 1, "владелец не узнал об ошибке")
        self.assertIn('RuntimeError', sent[0])
        self.assertIn('база отвалилась', sent[0])

    async def test_repeated_error_does_not_spam_admin(self):
        """Зациклившаяся ошибка не должна превращаться в поток сообщений."""
        sent = self.catch_alerts()
        self.break_handler()

        for _ in range(4):
            await self.send_text("📅 Сегодня")

        self.assertEqual(len(sent), 1, f"ушло {len(sent)} сообщений вместо одного")

    async def test_working_handler_is_untouched(self):
        """Обработчик ошибок не должен вмешиваться в нормальный ответ."""
        sent = self.catch_alerts()
        text, _ = await self.send_text("📅 Сегодня")

        self.assertNotIn('сломалось', text.lower())
        self.assertEqual(sent, [])


class GroupGoneNoteTest(unittest.TestCase):
    """Чистая функция: когда предупреждать, что группы больше нет."""

    @classmethod
    def setUpClass(cls):
        import bot.main as bot_main
        cls.note = staticmethod(bot_main.group_gone_note)

    def test_fresh_group_is_silent(self):
        self.assertEqual(self.note({'last_seen': date.today().isoformat()}), '')

    def test_short_gap_is_tolerated(self):
        """Сайт лежит несколько дней подряд — не повод хоронить группу."""
        recent = (date.today() - timedelta(days=3)).isoformat()
        self.assertEqual(self.note({'last_seen': recent}), '')

    def test_missing_mark_is_silent(self):
        """База до появления колонки: сказать «группы нет» тому, у кого она есть, хуже."""
        self.assertEqual(self.note({'last_seen': None}), '')
        self.assertEqual(self.note({}), '')

    def test_vanished_group_is_explained(self):
        old = (date.today() - timedelta(days=30)).isoformat()
        note = self.note({'last_seen': old})

        self.assertIn('больше нет', note)
        self.assertIn('Сменить группу', note)


class VanishedGroupTest(BotTestCase):
    """
    Сквозная проверка: подписчик пп402 видел «🎉 Нет занятий!» каждый день
    и никакого объяснения. Теперь ему говорят правду.
    """

    def seed(self, conn):
        db.set_user_group(conn, self.chat.id, 1)
        conn.execute("UPDATE groups_ SET last_seen = ? WHERE id = 1",
                     ((date.today() - timedelta(days=60)).isoformat(),))

    async def test_today_explains_that_group_is_gone(self):
        text, _ = await self.send_text("📅 Сегодня")

        self.assertIn('больше нет', text)
        self.assertIn('Сменить группу', text)

    async def test_week_explains_too(self):
        text, _ = await self.send_text("🗓 Неделя")
        self.assertIn('больше нет', text)


class LiveGroupIsNotWarnedTest(BotTestCase):
    """Обратная сторона: у живой группы предупреждения быть не должно."""

    def seed(self, conn):
        db.set_user_group(conn, self.chat.id, 1)
        conn.execute("UPDATE groups_ SET last_seen = ? WHERE id = 1",
                     (date.today().isoformat(),))

    async def test_no_warning_for_live_group(self):
        text, _ = await self.send_text("📅 Сегодня")
        self.assertNotIn('больше нет', text)

    async def test_no_warning_on_week(self):
        text, _ = await self.send_text("🗓 Неделя")
        self.assertNotIn('больше нет', text)


class FeedbackTest(BotTestCase):
    """
    Раздел «Связь»: бот принимает сообщение сам.

    Раньше здесь стоял чужой ник — человека отправляли писать в личку
    постороннему аккаунту, а бот в разговоре о собственной поломке
    не участвовал.
    """

    ADMIN = 777000

    def seed(self, conn):
        db.set_user_group(conn, self.chat.id, 1)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        m = self.bot_main
        self._saved = (m.ADMIN_CHAT_ID, m.AD_FULL_TEXT)
        m.ADMIN_CHAT_ID = self.ADMIN
        m.AD_FULL_TEXT = '💬 <b>Связь</b>'
        self.addCleanup(self._restore_ad)

    def _restore_ad(self):
        self.bot_main.ADMIN_CHAT_ID, self.bot_main.AD_FULL_TEXT = self._saved

    def to_admin(self):
        """Сообщения, ушедшие владельцу, а не автору запроса."""
        return [m for m in self.session.sent
                if str(getattr(m, 'chat_id', '')) == str(self.ADMIN)]

    async def open_section(self):
        return await self.send_text(self.bot_main.AD_BUTTON_LABEL)

    async def test_section_invites_to_write(self):
        text, _ = await self.open_section()
        self.assertIn('Напишите сообщение', text)

    async def test_section_does_not_name_anyone(self):
        """Главное в задаче: ник владельца не должен светиться."""
        text, _ = await self.open_section()
        self.assertNotIn('@', text)

    async def test_message_reaches_the_owner(self):
        await self.open_section()
        text, _ = await self.send_text("В среду пара стоит не в той аудитории")

        sent = self.to_admin()
        self.assertEqual(len(sent), 1, "владельцу ничего не ушло")
        self.assertIn('не в той аудитории', sent[0].text)
        self.assertIn('с403', sent[0].text, "группа должна быть видна")
        self.assertIn(str(self.chat.id), sent[0].text)
        self.assertIn('передал', text.lower())

    async def test_html_in_message_is_escaped(self):
        """Иначе угловые скобки в тексте человека не дадут сообщению уйти."""
        await self.open_section()
        await self.send_text("тут <b>сломалось</b> и <не работает>")

        sent = self.to_admin()
        self.assertEqual(len(sent), 1)
        self.assertIn('&lt;b&gt;', sent[0].text)
        self.assertNotIn('<b>сломалось', sent[0].text)

    async def test_message_is_kept_in_log(self):
        """Если отправка не пройдёт, написанное не должно пропасть."""
        await self.open_section()
        await self.send_text("расписание не открывается")

        conn = db.get_connection()
        row = conn.execute(
            "SELECT action, detail FROM activity_log WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT 1", (self.chat.id,)).fetchone()
        conn.close()
        self.assertEqual(row['action'], 'feedback')
        self.assertIn('не открывается', row['detail'])

    async def test_too_short_is_asked_to_expand(self):
        await self.open_section()
        text, _ = await self.send_text("аа")

        self.assertIn('подробнее', text.lower())
        self.assertEqual(self.to_admin(), [], "обрывок владельцу не нужен")

    async def test_main_button_cancels(self):
        """Передумал и нажал «Сегодня» — это расписание, а не сообщение."""
        await self.open_section()
        text, _ = await self.send_text("📅 Сегодня")

        self.assertEqual(self.to_admin(), [])
        self.assertIn('сентября', text)

    async def test_next_message_is_not_feedback_again(self):
        """
        Состояние снимается после первого сообщения: иначе человек, однажды
        написавший в «Связь», отправлял бы владельцу каждый свой поиск.
        """
        await self.open_section()
        await self.send_text("первое сообщение про ошибку")
        self.assertEqual(len(self.to_admin()), 1, "первое должно было уйти")

        # send_text чистит список отправленного, так что это уже про второе
        text, buttons = await self.send_text("Смирнов")

        self.assertEqual(self.to_admin(), [],
                         "второе сообщение ушло владельцу, хотя не должно")
        # фамилия уходит в кнопки результата, а не в текст сообщения
        self.assertTrue(any('Смирнов' in b for b in buttons),
                        "должен был отработать обычный поиск")
