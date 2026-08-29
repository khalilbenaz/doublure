"""Le routeur : modes acceptes, chaine de repli, reecriture du corps."""

import json
import os
import tempfile
import unittest

from helpers import Fixture, providers
import fallback
import router

BIG = "meta/llama-3.1-405b-instruct"
SMALL = "meta/llama-3.3-70b-instruct"


class Modes(unittest.TestCase):

    def test_la_table_des_noms_courts_est_la_meme_des_deux_cotes(self):
        # Le routeur et la CLI lisent le meme state.json : deux tables qui
        # divergent, et `dbl on zen` designerait un autre fournisseur que
        # celui que le routeur appellerait.
        self.assertEqual(router.ALIASES, fallback.ALIAS_SHORT)

    def test_modes_acceptes(self):
        for mode in ("native", "fcc", "or", "groq", "nvidia_nim",
                     "zen", "nim", "openrouter", "open_router"):
            self.assertTrue(router.mode_ok(mode), mode)

    def test_modes_refuses(self):
        for mode in ("pas-un-fournisseur", "", None, 3, {"mode": "or"}):
            self.assertFalse(router.mode_ok(mode), mode)

    def test_tout_le_registre_est_un_mode(self):
        for prov in providers.PREFERENCE:
            self.assertTrue(router.mode_ok(prov), prov)

    def test_traduction_ou_pas(self):
        # « or » parle l'API Anthropic : pas de traduction. Les autres si.
        self.assertFalse(router.is_bridged("or"))
        self.assertFalse(router.is_bridged("fcc"))
        self.assertFalse(router.is_bridged("native"))
        self.assertTrue(router.is_bridged("groq"))

    def test_ou_joindre_une_passerelle(self):
        cfg = router.bridge_cfg("groq")
        self.assertEqual(set(cfg), {"label", "host", "port", "tls", "path"})
        self.assertTrue(cfg["host"])
        self.assertTrue(cfg["path"].endswith("/chat/completions"))


class Chaine(Fixture):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dbl-test-")
        self.addCleanup(self._wipe)
        self.patch(router, "STATE", os.path.join(self.dir, "state.json"))
        self.patch(router, "LEGACY_STATE", ())
        self.patch(fallback, "STATE_FILE",
                   os.path.join(self.dir, "cli-state.json"))
        self.patch(fallback, "LEGACY_STATE", ())
        self.patch(router, "fcc_up", lambda: False)
        self.patch(router, "or_key", lambda: None)
        self.patch(providers, "key", lambda prov: "x")
        self.patch(providers, "configured",
                   lambda include_local=True: ["nvidia_nim", "groq"])

    def _wipe(self):
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def _state(self, **kw):
        with open(router.STATE, "w") as fh:
            json.dump(kw, fh)

    def test_deduite_du_registre(self):
        self.assertEqual(router.chain(), ["nvidia_nim", "groq"])

    def test_meme_chaine_que_la_cli(self):
        # Le CLI annonce l'ordre a l'utilisateur ; le routeur l'applique. Les
        # deux doivent lire le registre de la meme facon.
        self.patch(fallback, "fcc_up", lambda ttl=30: False)
        self.assertEqual(router.chain(), fallback.chain())

    def test_sans_cle_ecarte(self):
        self.patch(providers, "key", lambda prov: None)
        self.assertEqual(router.chain(), ["opencode_zen"])

    def test_sans_cle_mais_sans_cle_requise(self):
        self.patch(providers, "key", lambda prov: None)
        self.patch(providers, "configured",
                   lambda include_local=True: ["opencode_zen", "groq"])
        self.assertEqual(router.chain(), ["opencode_zen"])

    def test_fcc_seulement_s_il_ecoute(self):
        self.assertNotIn("fcc", router.chain())
        self.patch(router, "fcc_up", lambda: True)
        self.assertEqual(router.chain()[-1], "fcc")

    def test_or_seulement_avec_sa_cle(self):
        self._state(chain=["or", "groq"])
        self.assertEqual(router.chain(), ["groq"])
        self.patch(router, "or_key", lambda: "sk-or-x")
        self.assertEqual(router.chain(), ["or", "groq"])

    def test_natif_jamais_dans_la_chaine(self):
        # Le natif est ce qu'on quitte : le remettre dans la chaine ferait
        # boucler le repli sur le 429 qui l'a declenche.
        self._state(chain=["native", "groq"])
        self.assertEqual(router.chain(), ["groq"])

    def test_doublons_ecartes(self):
        self._state(chain=["nim", "nvidia_nim", "groq"])
        self.assertEqual(router.chain(), ["nvidia_nim", "groq"])

    def test_jamais_vide(self):
        self._state(chain=["pas-un-fournisseur"])
        self.assertEqual(router.chain(), ["opencode_zen"])


class Reecriture(Fixture):
    """Le nom de modele et les champs propres a Anthropic."""

    def setUp(self):
        self.patch(router, "overrides", lambda: {})
        self.patch(providers, "models",
                   lambda prov, refresh=False: [BIG, SMALL])
        self.patch(providers, "health", lambda prov=None: {})

    def test_alias_traduit_pour_openrouter(self):
        # Sans ca, OpenRouter sert le vrai Anthropic et facture le repli.
        self.assertEqual(router.or_model("claude-opus-4-6-20260101"), BIG)
        self.assertEqual(router.or_model("claude-3-5-haiku-latest"), SMALL)

    def test_identifiant_explicite_intact(self):
        self.assertEqual(router.or_model("z/mon-modele:free"),
                         "z/mon-modele:free")

    def test_nom_non_texte_intact(self):
        self.assertIsNone(router.or_model(None))

    def test_surcharge_utilisateur_suivie(self):
        self.patch(router, "overrides", lambda: {"or": {"opus": SMALL}})
        self.assertEqual(router.or_model("claude-opus-4-6"), SMALL)

    def test_passerelle_traduite(self):
        self.assertEqual(router.bridge_model("groq", "claude-opus-4-6"), BIG)

    def test_passerelle_vide_rend_none(self):
        # Serveur local allume mais sans modele installe : l'appelant doit
        # passer au suivant, pas envoyer un nom vide en amont.
        self.patch(providers, "models", lambda prov, refresh=False: [])
        self.assertIsNone(router.bridge_model("ollama", "claude-opus-4-6"))

    def body(self, data):
        return json.loads(router.or_body(json.dumps(data).encode()))

    def test_cache_control_retire_partout(self):
        out = self.body({
            "model": "claude-opus-4-6",
            "system": [{"type": "text", "text": ".",
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": ".",
                 "cache_control": {"type": "ephemeral"}}]}],
            "tools": [{"name": "lire",
                       "cache_control": {"type": "ephemeral"}}],
        })
        self.assertEqual(out["model"], BIG)
        self.assertNotIn("cache_control", out["system"][0])
        self.assertNotIn("cache_control", out["messages"][0]["content"][0])
        self.assertNotIn("cache_control", out["tools"][0])

    def test_le_reste_du_corps_passe_tel_quel(self):
        out = self.body({"model": "claude-opus-4-6", "max_tokens": 7,
                         "stream": True, "temperature": 0.2})
        self.assertEqual(out["max_tokens"], 7)
        self.assertTrue(out["stream"])
        self.assertEqual(out["temperature"], 0.2)

    def test_corps_intact_si_non_json(self):
        for raw in (b"", None, b"pas du json", b"[1, 2]"):
            self.assertEqual(router.or_body(raw), raw)

    def test_sans_modele_rien_a_traduire(self):
        self.assertEqual(self.body({"max_tokens": 1}), {"max_tokens": 1})


if __name__ == "__main__":
    unittest.main()
