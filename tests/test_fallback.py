"""L'etat et la CLI : noms courts, chaine de repli, surcharge de modele."""

import json
import os
import tempfile
import unittest

from helpers import Fixture, providers
import fallback

P = "testprov"
BIG = "meta/llama-3.1-405b-instruct"
SMALL = "meta/llama-3.3-70b-instruct"


class Etat(Fixture):
    """Chaque test travaille sur son propre state.json."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dbl-test-")
        self.addCleanup(self._wipe)
        self.patch(fallback, "STATE_FILE", os.path.join(self.dir, "state.json"))
        self.patch(fallback, "LEGACY_STATE", ())

    def _wipe(self):
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)


class Alias(unittest.TestCase):

    def test_noms_courts_d_avant_le_registre(self):
        # `dbl on zen` et un state.json ancien doivent continuer de marcher.
        self.assertEqual(fallback.resolve("zen"), "opencode_zen")
        self.assertEqual(fallback.resolve("go"), "opencode_go")
        self.assertEqual(fallback.resolve("nim"), "nvidia_nim")
        self.assertEqual(fallback.resolve("openrouter"), "or")

    def test_id_du_registre_inchange(self):
        self.assertEqual(fallback.resolve("groq"), "groq")
        self.assertIsNone(fallback.resolve(None))

    def test_open_router_va_vers_le_natif(self):
        # OpenRouter parle l'API Anthropic : passer par la traduction OpenAI
        # serait moins fidele, donc « or » et non « open_router ».
        self.assertEqual(fallback.resolve("open_router"), "or")
        self.assertEqual(fallback.reg("or"), "open_router")
        self.assertEqual(fallback.reg("groq"), "groq")

    def test_usable(self):
        self.assertTrue(fallback.usable("fcc"))
        self.assertTrue(fallback.usable("or"))
        self.assertTrue(fallback.usable("nvidia_nim"))
        self.assertFalse(fallback.usable("pas-un-fournisseur"))

    def test_label(self):
        self.assertEqual(fallback.label("fcc"), fallback.FCC_LABEL)
        self.assertEqual(fallback.label("or"), providers.label("open_router"))

    def test_base_non_vide(self):
        for pid in ("fcc", "or", "groq"):
            self.assertTrue(fallback.base(pid), pid)


class NeedsKey(Fixture):

    def test_cle_manquante(self):
        self.patch(providers, "key", lambda prov: None)
        self.assertTrue(fallback.needs_key("groq"))

    def test_cle_posee(self):
        self.patch(providers, "key", lambda prov: "x")
        self.assertFalse(fallback.needs_key("groq"))

    def test_dispenses(self):
        self.patch(providers, "key", lambda prov: None)
        for pid in ("fcc", "opencode_zen", "kilo", "ollama"):
            self.assertFalse(fallback.needs_key(pid), pid)


class Chaine(Etat):

    def setUp(self):
        super().setUp()
        self.patch(fallback, "fcc_up", lambda ttl=30: False)
        self.patch(providers, "configured",
                   lambda include_local=True: ["nvidia_nim", "groq",
                                               "opencode_zen"])
        self.patch(providers, "key", lambda prov: "x")

    def test_deduite_du_registre(self):
        self.assertEqual(fallback.chain(),
                         ["nvidia_nim", "groq", "opencode_zen"])

    def test_fcc_ferme_la_marche(self):
        self.patch(fallback, "fcc_up", lambda ttl=30: True)
        self.assertEqual(fallback.chain()[-1], "fcc")

    def test_fcc_absent_s_il_n_ecoute_pas(self):
        self.assertNotIn("fcc", fallback.chain())

    def test_open_router_sans_cle_est_ecarte(self):
        # « or » et « open_router » sont le meme service : sans cle, l'essayer
        # depenserait un aller-retour pour un 401.
        self.patch(providers, "configured",
                   lambda include_local=True: ["open_router", "groq"])
        self.patch(providers, "key", lambda prov: None)
        self.assertEqual(fallback.chain(), ["groq"])

    def test_pas_de_doublon_apres_resolution(self):
        self.patch(providers, "configured",
                   lambda include_local=True: ["open_router", "or", "groq"])
        self.assertEqual(fallback.chain(), ["or", "groq"])

    def test_ordre_personnalise_en_tete(self):
        fallback.set_state(chain=["opencode_zen", "groq"])
        self.assertEqual(fallback.chain(),
                         ["opencode_zen", "groq", "nvidia_nim"])

    def test_ordre_personnalise_traduit_les_noms_courts(self):
        fallback.set_state(chain=["zen"])
        self.assertEqual(fallback.chain()[0], "opencode_zen")

    def test_ordre_personnalise_hors_sujet_ignore(self):
        # Un fournisseur sans cle listee a la main ne doit pas passer devant :
        # sinon chaque repli commencerait par un 401.
        fallback.set_state(chain=["cerebras", "pas-un-fournisseur"])
        self.assertEqual(fallback.chain(),
                         ["nvidia_nim", "groq", "opencode_zen"])

    def test_jamais_vide(self):
        self.patch(providers, "configured", lambda include_local=True: [])
        self.assertEqual(fallback.chain(), ["opencode_zen"])


class Catalogue(Fixture):

    def test_bruit_ecarte_et_tri_par_nom(self):
        self.patch(providers, "models",
                   lambda prov, refresh=False: ["z/text-embedding-3", BIG,
                                                "a/sdxl-turbo", SMALL])
        ids = [e[0] for e in fallback.catalog("nvidia_nim")]
        self.assertEqual(ids, [BIG, SMALL])

    def test_gratuit_seulement(self):
        self.patch(providers, "models",
                   lambda prov, refresh=False: ["z/m-70b-instruct",
                                                "z/m-70b-instruct:free"])
        ids = [e[0] for e in fallback.catalog("open_router")]
        self.assertEqual(ids, ["z/m-70b-instruct:free"])

    def test_fcc_filtre_pareil(self):
        # FCC prefixe ses modeles ; la note se lit quand meme sur l'id.
        self.patch(fallback, "_fetch_fcc",
                   lambda: [("nim/" + BIG, "gros", ""),
                            ("nim/arctic-embed-l", "plongement", "")])
        ids = [e[0] for e in fallback.catalog("fcc")]
        self.assertEqual(ids, ["nim/" + BIG])

    def test_fournisseur_inconnu(self):
        self.assertEqual(fallback.catalog("pas-un-fournisseur"), [])


class Surcharge(Etat):

    def setUp(self):
        super().setUp()
        self.patch(providers, "models",
                   lambda prov, refresh=False: [BIG, SMALL])

    def test_pose_puis_annule(self):
        ok, _note = fallback.set_model("nim", "opus", SMALL)
        self.assertTrue(ok)
        over = fallback.state()["models"]
        self.assertEqual(over["nvidia_nim"]["opus"], SMALL)
        self.assertEqual(providers.tiers("nvidia_nim",
                                        over["nvidia_nim"])["opus"], SMALL)

        ok, _note = fallback.reset_models("nim")
        self.assertTrue(ok)
        self.assertNotIn("nvidia_nim", fallback.state()["models"])

    def test_annuler_sans_surcharge(self):
        ok, note = fallback.reset_models("nim")
        self.assertFalse(ok)
        self.assertIn("aucune surcharge", note)

    def test_les_autres_paliers_survivent(self):
        fallback.set_model("nim", "opus", SMALL)
        fallback.set_model("nim", "haiku", BIG)
        tbl = fallback.state()["models"]["nvidia_nim"]
        self.assertEqual(tbl, {"opus": SMALL, "haiku": BIG})

    def test_les_autres_fournisseurs_survivent(self):
        fallback.set_model("nim", "opus", SMALL)
        fallback.set_model("groq", "opus", BIG)
        fallback.reset_models("nim")
        self.assertEqual(fallback.state()["models"], {"groq": {"opus": BIG}})

    def test_refus_hors_catalogue(self):
        ok, note = fallback.set_model("nim", "opus", "z/invente")
        self.assertFalse(ok)
        self.assertIn("hors catalogue", note)

    def test_refus_palier_inconnu(self):
        ok, note = fallback.set_model("nim", "turbo", BIG)
        self.assertFalse(ok)
        self.assertIn("palier inconnu", note)

    def test_refus_fournisseur_inconnu(self):
        for pid in ("pas-un-fournisseur", "fcc"):
            ok, note = fallback.set_model(pid, "opus", BIG)
            self.assertFalse(ok, pid)
            self.assertIn("fournisseur inconnu", note)


class EtatFichier(Etat):

    def test_defauts_sans_fichier(self):
        st = fallback.state()
        self.assertEqual(st, dict(fallback.DEFAULT_STATE))

    def test_ecriture_champ_par_champ(self):
        fallback.set_state(mode="groq")
        fallback.set_state(chain=["groq"])
        st = fallback.state()
        self.assertEqual(st["mode"], "groq")
        self.assertEqual(st["chain"], ["groq"])

    def test_fichier_illisible_rend_les_defauts(self):
        with open(fallback.STATE_FILE, "w") as fh:
            fh.write("{pas du json")
        self.assertEqual(fallback.state()["mode"],
                         fallback.DEFAULT_STATE["mode"])


if __name__ == "__main__":
    unittest.main()
