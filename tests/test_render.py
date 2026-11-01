"""Synthetic-only checks for the public, count-free activity artifact."""

from datetime import date, timedelta
import importlib.util
from pathlib import Path
import unittest
from xml.etree import ElementTree


_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/token_activity/render.py"
_SPEC = importlib.util.spec_from_file_location("token_activity_render", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
render_svg = _MODULE.render_svg
_NS = {"svg": "http://www.w3.org/2000/svg"}


def snapshot(rows=None, **changes):
    result = {
        "source": "codex-account-usage",
        "fetched_at": "2026-11-01T12:34:56Z",
        "daily": rows if rows is not None else [{"date": "2026-11-01", "tokens": 987654321}],
    }
    result.update(changes)
    return result


def day_cells(svg):
    root = ElementTree.fromstring(svg)
    return {
        cell.attrib["aria-label"].split(":", 1)[0]: cell
        for cell in root.findall("svg:g/svg:rect", _NS)
    }


class RenderTests(unittest.TestCase):
    today = date(2026, 11, 1)

    def test_counts_never_reach_public_text_attributes_or_metadata(self):
        values = [918273645, 987654321, 112233445566]
        rows = [
            {"date": (self.today - timedelta(days=index)).isoformat(), "tokens": value}
            for index, value in enumerate(values)
        ]
        svg = render_svg(snapshot(rows), self.today)
        root = ElementTree.fromstring(svg)
        for value in values + [sum(values)]:
            self.assertNotIn(str(value), svg)
            self.assertNotIn(f"{value:,}", svg)
        self.assertNotIn("<metadata", svg)
        self.assertNotIn("<script", svg)
        self.assertNotIn("threshold", svg)
        self.assertIn("ChatGPT Token Activity", " ".join(root.itertext()))
        self.assertIn("Account activity via Codex", " ".join(root.itertext()))
        for day, cell in day_cells(svg).items():
            expected = f"{day}: "
            self.assertTrue(cell.attrib["aria-label"].startswith(expected))
            self.assertEqual(cell.find("svg:title", _NS).text, cell.attrib["aria-label"])
            self.assertRegex(cell.attrib["aria-label"], r"^\d{4}-\d{2}-\d{2}: (?:No data|No activity|Low activity|Moderate activity|High activity|Very high activity)$")

    def test_unknown_is_distinct_from_explicit_zero_and_future_is_blank(self):
        rows = [
            {"date": "2026-10-31", "tokens": 0},
            {"date": "2026-11-01", "tokens": 123},
            {"date": "2026-11-02", "tokens": 999999999},
        ]
        cells = day_cells(render_svg(snapshot(rows), self.today))
        self.assertEqual(cells["2026-10-30"].attrib["class"], "cell unknown")
        self.assertEqual(cells["2026-10-31"].attrib["class"], "cell none")
        self.assertEqual(cells["2026-11-01"].attrib["class"], "cell peak")
        self.assertNotIn("2026-11-02", cells)

    def test_current_week_starts_on_sunday_and_history_has_fifty_three_columns(self):
        cells = day_cells(render_svg(snapshot(), self.today))
        earliest = min(cells)
        self.assertEqual(date.fromisoformat(earliest).weekday(), 6)
        self.assertEqual(len({cell.attrib["x"] for cell in cells.values()}), 53)
        self.assertEqual(cells["2026-11-01"].attrib["y"], "80")
        self.assertEqual(cells["2026-10-31"].attrib["y"], str(80 + 6 * 14))
        self.assertEqual(len(cells), 52 * 7 + 1)

    def test_partial_week_and_leap_day_render_real_calendar_dates(self):
        today = date(2024, 3, 2)
        rows = [{"date": "2024-02-29", "tokens": 3}]
        cells = day_cells(render_svg(snapshot(rows), today))
        self.assertIn("2024-02-29", cells)
        self.assertEqual(cells["2024-02-29"].attrib["y"], str(80 + 4 * 14))
        self.assertNotIn("2024-03-03", cells)
        self.assertEqual(len(cells), 53 * 7)

    def test_levels_are_qualitative_and_old_or_future_outliers_do_not_change_them(self):
        values = [0, 100, 400, 700, 1000]
        rows = [
            {"date": (self.today - timedelta(days=index)).isoformat(), "tokens": value}
            for index, value in enumerate(values)
        ]
        rows.extend([
            {"date": "2020-01-01", "tokens": 10**1000},
            {"date": "2026-11-02", "tokens": 10**1000},
        ])
        cells = day_cells(render_svg(snapshot(rows), self.today))
        for index, level in enumerate(("none", "low", "moderate", "high", "peak")):
            day = (self.today - timedelta(days=index)).isoformat()
            self.assertEqual(cells[day].attrib["class"], f"cell {level}")

    def test_all_reported_zero_days_are_not_made_unknown(self):
        rows = [{"date": "2026-11-01", "tokens": 0}]
        cells = day_cells(render_svg(snapshot(rows), self.today))
        self.assertEqual(cells["2026-11-01"].attrib["class"], "cell none")

    def test_missing_or_empty_daily_data_fails_instead_of_creating_zeros(self):
        for daily in (None, [], {}, "unavailable"):
            with self.subTest(daily=daily), self.assertRaises(ValueError):
                render_svg(snapshot(daily=daily), self.today)
        value = snapshot()
        del value["daily"]
        with self.assertRaises(ValueError):
            render_svg(value, self.today)
        with self.assertRaises(ValueError):
            render_svg(snapshot([{"date": "2020-01-01", "tokens": 1}]), self.today)

    def test_invalid_dates_values_duplicates_and_source_fail(self):
        invalid_rows = [
            [{"date": "2026-02-30", "tokens": 1}],
            [{"date": "20261101", "tokens": 1}],
            [{"date": "2026-11-01<script>", "tokens": 1}],
            [{"date": "2026-11-01", "tokens": 1}, {"date": "2026-11-01", "tokens": 2}],
            ["not a row"],
        ]
        invalid_rows.extend([[{"date": "2026-11-01", "tokens": value}] for value in (-1, 1.0, True, "12", None)])
        for rows in invalid_rows:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                render_svg(snapshot(rows), self.today)
        with self.assertRaises(ValueError):
            render_svg(snapshot(source="untrusted-source"), self.today)

    def test_fetched_time_must_be_utc_and_extra_input_is_not_serialized(self):
        for stamp in (None, "bad", "2026-11-01", "2026-11-01T12:00:00+01:00"):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                render_svg(snapshot(fetched_at=stamp), self.today)
        svg = render_svg(snapshot(fetched_at="2026-11-01T12:00:00+00:00", extra="SYNTHETIC_PRIVATE_SENTINEL"), self.today)
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", svg)

    def test_explicit_today_makes_output_deterministic_and_valid_svg(self):
        first = render_svg(snapshot(), self.today)
        self.assertEqual(first, render_svg(snapshot(), self.today))
        root = ElementTree.fromstring(first)
        self.assertEqual(root.attrib["viewBox"], "0 0 830 218")
        self.assertEqual(root.attrib["role"], "img")
        self.assertLess(len(first), 150000)


if __name__ == "__main__":
    unittest.main()
