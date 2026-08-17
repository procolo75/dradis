"""
tests/test_car_mode.py
──────────────────────
Pins the Car Mode sanitiser: `car_mode.to_spoken`.

Run with:  cd dradis && python3 -m unittest discover tests

These tests exist because every failure mode here is SILENT. A half-converted
unit ("chilometri/h"), a compass point left as the bare letter "O", a stripped
emoji that took a whole line with it — none of them raise, none of them show up
in a log, and none of them are visible on screen. They only surface as a sentence
the car reads out wrong while the user is driving and cannot check.

The last class runs real alert layouts through the sanitiser rather than strings
invented for the test. That is where the ordering bugs actually live.
"""

import unittest

from dradis.car_mode import to_spoken


class MarkupAndLinksTest(unittest.TestCase):
    """Telegram HTML in, plain speech out."""

    def test_tags_are_removed_and_their_text_kept(self):
        self.assertEqual(
            to_spoken("<b>Temporale</b> a <i>12</i> <code>km</code>"),
            "Temporale a 12 chilometri.",
        )

    def test_a_link_goes_whole_label_included(self):
        # Every link DRADIS sends is a tap target. Spoken, the label is an
        # instruction the listener cannot follow — it sounds like information
        # and is not. v4.4.0 kept the label and alerts ended in a flat
        # "apri la mappa." that told a driver nothing.
        out = to_spoken('<a href="https://maps.google.com/?q=40.8,14.2">apri la mappa</a>')
        self.assertEqual(out, "")

    def test_a_link_in_a_sentence_leaves_the_sentence_intact(self):
        out = to_spoken('Evento <a href="https://ingv.it/e/1">Scheda evento</a> registrato')
        self.assertEqual(out, "Evento registrato.")
        self.assertNotIn("http", out)

    def test_a_bare_url_is_dropped(self):
        out = to_spoken("Dettagli su https://www.ingv.it/evento/12345 adesso")
        self.assertNotIn("http", out)
        self.assertIn("Dettagli su", out)

    def test_entities_resolve_to_their_character(self):
        self.assertEqual(to_spoken("pioggia &amp; vento"), "pioggia & vento.")

    def test_escaped_markup_does_not_survive_as_spoken_symbols(self):
        # "less than b greater than" is not worth reading aloud, and leaving it
        # would break idempotency — a second pass would strip what the first kept.
        self.assertEqual(to_spoken("&lt;b&gt;grassetto&lt;/b&gt;"), "grassetto.")


class TelemetryTest(unittest.TestCase):
    """Things no message should ever read aloud, wherever they come from.

    A net rather than the fix: the LINES carrying these are removed at the source
    by `snapshot.format_caption(voice=True)`. These rules exist so a monitor added
    later cannot quietly reintroduce them.
    """

    def test_a_coordinate_pair_is_dropped(self):
        # Five decimals, spoken digit by digit, twice — the worst single thing
        # on the list.
        out = to_spoken("<code>40.85123, 14.26891</code> qui")
        self.assertNotIn("40", out)
        self.assertEqual(out, "qui.")

    def test_negative_coordinates_too(self):
        self.assertEqual(to_spoken("pos -12.34567, -0.98765 qui"), "pos qui.")

    def test_an_ordinary_decimal_distance_survives(self):
        # The pair needs the comma; a lone number is a measurement, not a fix.
        self.assertIn("12.3456", to_spoken("Fronte a 12.3456 km"))

    def test_a_record_id_is_dropped(self):
        self.assertEqual(to_spoken("🕐 14:32 · <code>#31884</code>"), "14:32.")

    def test_a_short_hash_number_in_prose_survives(self):
        # "#2" means something in a sentence; "#31884" is INGV's bookkeeping.
        self.assertIn("#2", to_spoken("Anello #2 di 4"))

    def test_removing_something_mid_list_leaves_no_orphan_separator(self):
        self.assertEqual(
            to_spoken("alle 14:32 · <code>#31884</code> · magnitudo 2.1"),
            "alle 14:32, magnitudo 2.1.",
        )

    def test_a_trailing_separator_does_not_survive_the_removal(self):
        self.assertEqual(to_spoken('Testo · <a href="http://x">link</a>'), "Testo.")

    def test_a_leading_separator_does_not_survive_either(self):
        # The thunderstorm monitor writes "📍 40.7967, 14.0735 | Forecast 2 days";
        # taking the coordinates out used to leave the line starting on a pipe.
        self.assertEqual(
            to_spoken("\U0001F4CD 40.7967, 14.0735 | Forecast 2 days"),
            "Forecast 2 days.",
        )


class EmojiTest(unittest.TestCase):

    def test_leading_emoji_are_stripped_from_every_line(self):
        out = to_spoken("\U0001F4CD Fronte vicino\n\U0001F9ED Rotta costante")
        self.assertEqual(out, "Fronte vicino. Rotta costante.")

    def test_composed_emoji_leave_nothing_behind(self):
        # ⛈️ and ⏱️ are a base character plus U+FE0F; a regex that missed the
        # variation selector would leave an invisible orphan in the text.
        out = to_spoken("⛈️ Temporale\n⏱️ Da poco")
        self.assertEqual(out, "Temporale. Da poco.")

    def test_a_line_of_only_emoji_is_dropped_not_turned_into_a_full_stop(self):
        self.assertEqual(to_spoken("Testo\n\U0001F4A7\nAltro"), "Testo. Altro.")

    def test_degree_and_middot_are_not_treated_as_emoji(self):
        # Both live outside the pictographic ranges on purpose: they are words.
        self.assertIn("gradi", to_spoken("Rotta 45°"))
        self.assertNotIn("·", to_spoken("a · b"))


class UnitsTest(unittest.TestCase):

    def test_compound_units_are_converted_before_their_prefix(self):
        # The ordering bug this pins: `km` matched first leaves "chilometri/h".
        self.assertEqual(to_spoken("80 km/h"), "80 chilometri orari.")
        self.assertNotIn("/", to_spoken("80 km/h"))

    def test_plain_units(self):
        self.assertEqual(to_spoken("12 km in 18 min"), "12 chilometri in 18 minuti.")
        self.assertEqual(to_spoken("±12 m"), "più o meno 12 metri.")

    def test_a_ratio_between_digits_becomes_words(self):
        self.assertEqual(to_spoken("Anello 2/4"), "Anello 2 su 4.")

    def test_degrees_and_percent(self):
        self.assertEqual(to_spoken("45° e 40%"), "45 gradi e 40 per cento.")

    def test_millimetres_per_hour_is_not_mangled_into_millimetres(self):
        self.assertEqual(to_spoken("picco 24 mm/h"), "picco 24 millimetri all'ora.")


class CompassTest(unittest.TestCase):
    """`geo.direction_label` returns abbreviations; one of them is a disaster."""

    def test_west_in_italian_is_not_left_as_the_conjunction_o(self):
        self.assertEqual(to_spoken("a 12 km a O"), "a 12 chilometri a ovest.")

    def test_all_eight_italian_points(self):
        for abbr, word in [("N", "nord"), ("NE", "nord-est"), ("E", "est"),
                           ("SE", "sud-est"), ("S", "sud"), ("SO", "sud-ovest"),
                           ("O", "ovest"), ("NO", "nord-ovest")]:
            self.assertEqual(to_spoken(f"verso {abbr}"), f"verso {word}.")

    def test_english_points(self):
        self.assertEqual(to_spoken("to W", "en"), "to west.")
        self.assertEqual(to_spoken("to SW", "en"), "to southwest.")

    def test_a_lowercase_italian_conjunction_is_left_alone(self):
        self.assertEqual(to_spoken("pioggia e vento"), "pioggia e vento.")

    def test_the_hyphen_in_an_expanded_point_survives_the_separator_pass(self):
        self.assertIn("nord-est", to_spoken("verso NE"))


class SentenceTest(unittest.TestCase):

    def test_lines_become_sentences_joined_on_one_line(self):
        out = to_spoken("Prima riga\nSeconda riga")
        self.assertEqual(out, "Prima riga. Seconda riga.")
        self.assertNotIn("\n", out)

    def test_existing_terminal_punctuation_is_not_doubled(self):
        self.assertEqual(to_spoken("Domanda?\nRisposta."), "Domanda? Risposta.")

    def test_separators_become_pauses(self):
        self.assertEqual(to_spoken("Anello 2 · entro 20 km"),
                         "Anello 2, entro 20 chilometri.")
        self.assertEqual(to_spoken("Temporale — Casa"), "Temporale, Casa.")

    def test_empty_input(self):
        self.assertEqual(to_spoken(""), "")

    def test_input_that_reduces_to_nothing(self):
        self.assertEqual(to_spoken("\U0001F4A7\n\U0001F550"), "")


class IdempotenceTest(unittest.TestCase):
    """`send_telegram` applies the sanitiser on the way out. A caller that already
    sanitised its own text must not have the message damaged a second time."""

    CASES = [
        "⛈️ <b>Temporale — Casa</b>\n\U0001F4CD Fronte a <b>12 km</b> a O (270°)",
        '<code>40.85, 14.26</code> — <a href="https://maps.google.com/">apri la mappa</a>',
        "&lt;b&gt;non un tag&lt;/b&gt; &amp; simboli",
        "\U0001F3AF Anello 2/4 · entro 20 km\n\U0001F697 A 80 km/h verso NE",
    ]

    def test_a_second_pass_changes_nothing(self):
        for raw in self.CASES:
            once = to_spoken(raw)
            self.assertEqual(to_spoken(once), once, f"not idempotent: {raw!r}")


class RealAlertLayoutTest(unittest.TestCase):
    """The layouts the monitors actually emit, copied from their formatters."""

    STORM = (
        "⛈️ <b>Temporale nel raggio — Casa</b>\n"
        "\U0001F4CD Fronte a <b>12 km</b> a NE (45°)\n"
        "\U0001F3AF Anello 2/4 · entro 20 km\n"
        "\U0001F9ED Rotta costante: ti arriva addosso\n"
        "\U0001F697 In movimento a 80 km/h verso NE\n"
        "⏱️ Da 25 a 12 km in 18 min\n"
        "\U0001F522 47 fulmini in 30 min (settore NE)\n"
        "\U0001F550 14:32"
    )

    def test_a_storm_alert_reads_as_prose(self):
        out = to_spoken(self.STORM)
        self.assertEqual(
            out,
            "Temporale nel raggio, Casa. "
            "Fronte a 12 chilometri a nord-est (45 gradi). "
            "Anello 2 su 4, entro 20 chilometri. "
            "Rotta costante: ti arriva addosso. "
            "In movimento a 80 chilometri orari verso nord-est. "
            "Da 25 a 12 chilometri in 18 minuti. "
            "47 fulmini in 30 minuti (settore nord-est). "
            "14:32."
        )

    def test_a_storm_alert_keeps_every_number_that_matters(self):
        out = to_spoken(self.STORM)
        for value in ["12", "45", "20", "80", "18", "47", "14:32"]:
            self.assertIn(value, out)

    def test_a_snapshot_position_block_loses_the_coordinates_and_the_link(self):
        # v4.4.0 kept both and read them out. Coordinates spoken digit by digit
        # and a tap target with no link behind it are the two things a driver can
        # do least with. The LINE they sit on is removed at the source by
        # `format_caption(voice=True)`; this is the net under that.
        caption = (
            "\U0001F4CD <b>Posizione</b>\n"
            "<code>40.85123, 14.26891</code> — "
            '<a href="https://www.google.com/maps?q=40.85123,14.26891">apri la mappa</a>'
        )
        out = to_spoken(caption)
        self.assertEqual(out, "Posizione.")

    def test_an_english_rain_alert(self):
        out = to_spoken(
            "\U0001F327️ <b>Rain closing in — Home</b>\n"
            "\U0001F4CD Front at <b>8 km</b> to W (270°)\n"
            "\U0001F9ED On a collision course: reaches you in 12 min\n"
            "\U0001F4A7 Peak 24 mm/h · hail 40%",
            "en",
        )
        self.assertEqual(
            out,
            "Rain closing in, Home. "
            "Front at 8 kilometres to west (270 degrees). "
            "On a collision course: reaches you in 12 minutes. "
            "Peak 24 millimetres per hour, hail 40 percent."
        )

    def test_no_alert_layout_leaves_markup_or_icons_behind(self):
        for raw in [self.STORM]:
            out = to_spoken(raw)
            self.assertNotIn("<", out)
            self.assertNotIn("&", out)
            self.assertTrue(all(ord(ch) < 0x2000 or ch in "’" for ch in out),
                            f"non-speech character survived: {out!r}")


if __name__ == "__main__":
    unittest.main()
