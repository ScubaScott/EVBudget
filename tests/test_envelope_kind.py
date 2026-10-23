from helpers import TempAppCase


class EnvelopeKindTests(TempAppCase):
    def kinds(self):
        return self.call(self.app.get_envelopes, method="GET")["kinds"]

    def test_new_envelope_uses_the_chosen_type_not_the_name(self):
        self.call(self.app.add_envelope, {"envelope": "Insurance: Boat"})
        self.call(self.app.add_envelope, {"envelope": "Vacation", "kind": "savings"})
        k = self.kinds()
        self.assertEqual(k["Insurance: Boat"]["kind"], "spending")
        self.assertEqual(k["Vacation"]["kind"], "savings")

    def test_type_can_be_changed_and_survives_a_rename(self):
        self.call(self.app.add_envelope, {"envelope": "Water"})
        self.call(self.app.save_envelope_settings, {"id": self.kinds()["Water"]["id"], "kind": "bill"})
        self.call(self.app.rename_envelope, {"old_name": "Water", "new_name": "Water bill"})
        self.assertEqual(self.kinds()["Water bill"]["kind"], "bill")

    def test_unknown_type_is_refused(self):
        with self.assertRaises(Exception):
            self.call(self.app.add_envelope, {"envelope": "X", "kind": "nonsense"})
