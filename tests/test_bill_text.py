import unittest

from evbudget import billmatch


def bill(id_, name, phrase=""):
    return {"id": id_, "name": name, "match_phrase": phrase}


class MatchTextTests(unittest.TestCase):
    def test_phrase_in_ignores_punctuation_case_and_trailing_ids(self):
        self.assertTrue(billmatch.phrase_in("Dicks Sanitation", "WCI*Dicks Sanitation payment1234"))
        self.assertTrue(billmatch.phrase_in("WCI*DICKS SANITATION", "wci  *dicks sanitation"))
        self.assertTrue(billmatch.phrase_in("AARP *MEMBERSHIP", "AARP MEMBERSHIP"))
        self.assertFalse(billmatch.phrase_in("DICKS SANITATION", "DICKS SPORTING GOODS"))
        self.assertFalse(billmatch.phrase_in("", "ANYTHING"))

    def test_a_renamed_payee_is_covered_by_a_bill_saved_with_the_old_text(self):
        b = bill(27, "WCI*DICKS SANITATION", "WCI*DICKS SANITATION")
        self.assertTrue(billmatch.bill_covers(b, ["DICKS SANITATION", "WCI*DICKS SANITATION"]))

    def test_a_payee_linked_to_the_bill_is_covered(self):
        self.assertTrue(billmatch.bill_covers(bill(5, "Gas", "XYZ"), ["CENTERPOINT"], [5]))
        self.assertFalse(billmatch.bill_covers(bill(5, "Gas", "XYZ"), ["CENTERPOINT"], [6]))

    def test_first_two_words_make_spotify_and_spotify_usa_the_same(self):
        self.assertTrue(billmatch.bill_covers(bill(1, "Spotify USA"), ["SPOTIFY"]))
        self.assertFalse(billmatch.bill_covers(bill(1, "Spotify USA"), ["NETFLIX"]))

    def test_suggested_text_drops_the_parts_that_change(self):
        self.assertEqual(billmatch.suggest_phrase(["AMAZON PRIME*B04RM9X43"]), "AMAZON PRIME")
        self.assertEqual(billmatch.suggest_phrase(["SCOTT COUNTY TRE Scott Coun 052626 Scott Reil"]), "SCOTT COUNTY TRE SCOTT COUN")
        self.assertEqual(billmatch.suggest_phrase(["DICKS SANITATION"]), "DICKS SANITATION")
        self.assertEqual(billmatch.suggest_phrase(["QUANTUM FIBER 12", "QUANTUM FIBER 98"]), "QUANTUM FIBER")
        self.assertEqual(billmatch.suggest_phrase(["NETFLIX A", "NETFLIX B"]), "NETFLIX")


if __name__ == "__main__":
    unittest.main()
