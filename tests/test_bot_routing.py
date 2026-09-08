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
from datetime import datetime
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

    async def test_guide_covers_both_features(self):
        """Гайд должен объяснять и предметы по выбору, и языки."""
        await self.pick_group()
        text, _ = await self.send_text("/помощь")

        self.assertIn('предметы по выбору', text.lower())
        self.assertIn('/язык', text)
        self.assertIn('не у всех', text.lower())
        self.assertNotIn('языковых потоков нет', text, "у этой группы потоки есть")

    async def test_guide_notes_when_group_has_no_streams(self):
        conn = db.get_connection()
        conn.execute("DELETE FROM lessons WHERE subgroup != ''")
        conn.commit()
        conn.close()

        await self.pick_group()
        text, _ = await self.send_text("/помощь")
        self.assertIn('языковых потоков нет', text)

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
