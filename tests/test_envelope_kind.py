from helpers import TempAppCase


class EnvelopeKindTests(TempAppCase):
    def kinds(self):
        return self.call(self.app.get_envelopes, method="GET")["kinds"]

    def test_new_envelope_uses_the_chosen_type_not_the_name(self):
        self.call(self.app.add_envelope, {"envelope": "Insurance: Boat"})
        self.call(self.app.add_envelope, {"envelope": "Vacation", "kind": "goal"})
        k = self.kinds()
        self.assertEqual(k["Insurance: Boat"]["kind"], "spending")
        self.assertEqual(k["Vacation"]["kind"], "goal")

    def test_old_savings_envelopes_become_goals(self):
        from evbudget import store
        c = self.conn()
        c.execute("INSERT INTO envelopes (name, kind, goal_cents, goal_date) VALUES ('Rainy day', 'savings', 100000, '2027-01-01')")
        c.commit()
        store.create_schema(c)
        row = c.execute("SELECT kind, goal_cents, goal_date FROM envelopes WHERE name = 'Rainy day'").fetchone()
        c.close()
        self.assertEqual(tuple(row), ("goal", 100000, None))

    def test_type_can_be_changed_and_survives_a_rename(self):
        self.call(self.app.add_envelope, {"envelope": "Water"})
        self.call(self.app.save_envelope_settings, {"id": self.kinds()["Water"]["id"], "kind": "bill"})
        self.call(self.app.rename_envelope, {"old_name": "Water", "new_name": "Water bill"})
        self.assertEqual(self.kinds()["Water bill"]["kind"], "bill")

    def test_unknown_type_is_refused(self):
        with self.assertRaises(Exception):
            self.call(self.app.add_envelope, {"envelope": "X", "kind": "nonsense"})


class EnvelopeOrderTests(TempAppCase):
    def names(self, kind):
        k = self.call(self.app.get_envelopes, method="GET")
        return [n for n in k["envelopes"] if k["kinds"][n]["kind"] == kind]

    def test_alphabetical_by_default_inside_each_type(self):
        for n in ("Zoo", "apple", "Mango"):
            self.call(self.app.add_envelope, {"envelope": n, "kind": "spending"})
        got = self.names("spending")
        self.assertEqual(got, sorted(got, key=str.lower))

    def test_reorder_one_type_and_reset_to_a_z(self):
        for n in ("Zoo", "apple", "Mango"):
            self.call(self.app.add_envelope, {"envelope": n, "kind": "goal"})
        a_z = self.names("goal")
        mine = list(reversed(a_z))
        res = self.call(self.app.reorder_envelopes, {"kind": "goal", "names": mine})
        self.assertEqual(self.names("goal"), mine)
        self.assertTrue(all(res["kinds"][n]["manual"] for n in mine))
        self.call(self.app.add_envelope, {"envelope": "Aaa", "kind": "goal"})   # joins the end of a hand-ordered type
        self.assertEqual(self.names("goal"), mine + ["Aaa"])
        self.call(self.app.reorder_envelopes, {"kind": "goal", "reset": True})
        got = self.names("goal")
        self.assertEqual(got, sorted(got, key=str.lower))

    def test_reorder_leaves_other_types_alone_and_rejects_strangers(self):
        before = self.names("spending")
        self.call(self.app.add_envelope, {"envelope": "Trip", "kind": "goal"})
        self.call(self.app.reorder_envelopes, {"kind": "goal", "names": ["Trip"]})
        self.assertEqual(self.names("spending"), before)
        with self.assertRaises(Exception):
            self.call(self.app.reorder_envelopes, {"kind": "goal", "names": ["Groceries"]})
