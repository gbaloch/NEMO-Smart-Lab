"""Shared fixtures for the readers tests: temp-dir base case and synthetic log writers."""

import tempfile
import unittest
import uuid


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


def _write_heater_log(path, header_cols, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\t" + "\t".join(header_cols) + "\n")
        for row in rows:
            f.write("\t" + "\t".join(row) + "\n")


MVD_SUM_TEMPLATE = """[Recipe Status]
Recipe name = {recipe}
Recipe status = Recipe running
Completion status = Successfully completed

[Recipe Statistics]
Start time = 2026/01/01 00:00:00
End time = 2026/01/01 00:00:05
Run time = 5.0 sec

[heaters]
HTR6 = "EXHAUST TRAP",80,180,5,3,180,5.000,3.375,0.675,0,100
"""


MVD_DAT_HEADER = ["Time(sec)", "HTR6(C)", "HTR6(%)"]


def _new_guid_bytes():
    return uuid.uuid4().bytes_le


EVENTLOG_FIELDS = ["Date/Time", "Module", "Event", "Info"]
