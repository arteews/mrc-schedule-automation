"""Check half-length lessons, short lessons and practice reminders."""

import copy
from datetime import datetime
from zoneinfo import ZoneInfo

from worker import describe_changes, notification_signature, reminder_candidates


zone = ZoneInfo('Europe/Minsk')
halves = [
    {'period': 2, 'last_period': 2, 'half': 1, 'start': '09:50', 'end': '10:35', 'subject': 'Физ к и зд', 'teacher': 'Преподаватель А', 'room': 'с/з'},
    {'period': 2, 'last_period': 2, 'half': 2, 'start': '10:45', 'end': '11:30', 'subject': 'Русская литература', 'teacher': 'Преподаватель Б', 'room': '214'},
]
first = reminder_candidates(halves, datetime(2030, 10, 15, 10, 20, tzinfo=zone))
assert len(first) == 1 and '2-я половина 2-й пары' in first[0][1] and '214' in first[0][1]
last = reminder_candidates(halves, datetime(2030, 10, 15, 11, 15, tzinfo=zone))
assert len(last) == 1 and last[0][0] != first[0][0]

before = {'date': '2030-10-15', 'group': '1К0000', 'lessons': halves}
after = copy.deepcopy(before)
after['lessons'][1]['room'] = '216'
assert notification_signature(before) != notification_signature(after)
assert describe_changes(before, after) == ['2-я половина 2-й пары: кабинет: 214 → 216']

short = [{'period': 4, 'last_period': 4, 'half': 1, 'explicit_time': True, 'start': '13:40', 'end': '14:00',
          'subject': 'Информационный час', 'teacher': 'Преподаватель В', 'room': '404'}]
assert len(reminder_candidates(short, datetime(2030, 10, 15, 13, 45, tzinfo=zone))) == 1

practice = [{'period': 1, 'last_period': 4, 'half': None, 'start': '09:50', 'end': '15:20',
             'subject': 'Практика', 'teacher': 'Преподаватель Г', 'room': '404'}]
assert len(reminder_candidates(practice, datetime(2030, 10, 15, 15, 5, tzinfo=zone))) == 1

print('nonstandard reminders: ok')
