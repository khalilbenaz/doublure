"""Le registre : notation, classement, paliers, dialectes, cles."""

import json
import os
import stat
import tempfile
import time
import unittest

from helpers import Fixture, providers

P = "testprov"      # fournisseur imaginaire : aucun test ne sort du process
BIG = "meta/llama-3.1-405b-instruct"
MID = "openai/gpt-oss-120b"
SMALL = "meta/llama-3.3-70b-instruct"
NANO = "nano-1b-instruct"


class Score(unittest.TestCase):

    def test_non_chat_rejete(self):
        # Un catalogue reel melange plongements, rerank, image et audio aux
        # modeles de dialogue. Une note negative les ecarte partout.
        for name in ("text-embedding-3-large", "nvidia/nv-rerankqa-1b",
                     "whisper-large-v3", "flux.1-dev", "sdxl-turbo",
                     "llama-guard-3-8b", "bge-m3", "kokoro-tts",
                     "ocr-2b", "wan-video-1.3b"):
            self.assertLess(providers._score(name), 0, name)

    def test_taille_ordonne(self):
        self.assertGreater(providers._score(BIG), providers._score(MID))
        self.assertGreater(providers._score(MID), providers._score(SMALL))
        self.assertGreater(providers._score(SMALL), providers._score(NANO))

    def test_sans_taille_reste_derriere(self):
        # Un nom de code opaque ne se classe pas : plancher a 1, donc
        # derriere le moindre modele qui annonce sa taille.
        self.assertLess(providers._score("mystery-alpha"),
                        providers._score("tiny-3b-instruct"))
        self.assertGreater(providers._score("mystery-alpha"), 0)

    def test_bonus_usage_et_famille(self):
        self.assertGreater(providers._score("acme-70b-instruct"),
                           providers._score("acme-70b"))
        self.assertGreater(providers._score("qwen-70b"),
                           providers._score("acme-70b"))

    def test_note_sur_le_total_pas_sur_l_actif(self):
        # « 235b-a22b » : 235 est la capacite, 22 les parametres actifs.
        self.assertGreater(providers._score("qwen/qwen3-235b-a22b"),
                           providers._score("acme-120b"))


class FreeOnly(unittest.TestCase):

    def test_le_gratuit_evince_le_payant(self):
        pool = ["z/model-a", "z/model-a:free", "z/model-b:free"]
        self.assertEqual(providers._free_only(pool),
                         ["z/model-a:free", "z/model-b:free"])

    def test_rien_de_gratuit_ne_vide_pas_la_liste(self):
        pool = ["z/model-a", "z/model-b"]
        self.assertEqual(providers._free_only(pool), pool)


class Small(unittest.TestCase):

    def test_pas_le_plus_petit_mais_le_plus_petit_utile(self):
        # NANO est le plus petit du lot et n'est pas retenu : sous 15 B la
        # boucle a outils decroche.
        self.assertEqual(providers._small([BIG, MID, NANO, SMALL]), SMALL)

    def test_fenetre_elargie_quand_rien_ne_convient(self):
        # Aucun marqueur d'usage : la troisieme fenetre laisse passer.
        self.assertEqual(providers._small(["acme-70b"]), "acme-70b")

    def test_dernier_recours(self):
        # Trop petit pour toutes les fenetres utiles : on rend quand meme
        # quelque chose plutot qu'un palier vide.
        self.assertEqual(providers._small(["tiny-3b-instruct"]),
                         "tiny-3b-instruct")


class KnownGood(Fixture):
    """La sante sert a ecarter, pas a promouvoir."""

    def test_sonde_bon_ne_depasse_pas_un_inconnu_plus_gros(self):
        now = time.time()
        self.fake(rel={P: {NANO: {"ok": True, "tools": True, "at": now}}})
        self.assertEqual(providers._known_good(P, [NANO, BIG])[0], BIG)

    def test_verifie_casse_passe_en_queue(self):
        now = time.time()
        self.fake(rel={P: {BIG: {"ok": False, "at": now}}})
        self.assertEqual(providers._known_good(P, [NANO, BIG]),
                         [NANO, BIG])

    def test_sourd_aux_outils_entre_les_deux(self):
        now = time.time()
        self.fake(rel={P: {BIG: {"ok": True, "tools": False, "at": now},
                           MID: {"ok": False, "at": now}}})
        self.assertEqual(providers._known_good(P, [BIG, MID, SMALL]),
                         [SMALL, BIG, MID])

    def test_verdict_perime_vaut_inconnu(self):
        old = time.time() - providers.HEALTH_TTL_BAD - 60
        self.fake(rel={P: {BIG: {"ok": False, "at": old}}})
        self.assertEqual(providers._known_good(P, [NANO, BIG])[0], BIG)


class Tiers(Fixture):

    def test_deduction_de_base(self):
        self.fake({P: [BIG, MID, SMALL, NANO]}, seed={})
        t = providers.tiers(P)
        self.assertEqual(t["opus"], BIG)
        self.assertEqual(t["sonnet"], BIG)
        self.assertEqual(t["haiku"], SMALL)
        self.assertEqual(t["fable"], MID)
        self.assertEqual(sorted(t), sorted(providers.TIERS))

    def test_le_bruit_du_catalogue_est_ecarte(self):
        self.fake({P: ["text-embedding-3-large", BIG, "sdxl-turbo"]}, seed={})
        self.assertEqual(set(providers.tiers(P).values()), {BIG})

    def test_modele_casse_ne_devient_pas_un_palier(self):
        now = time.time()
        self.fake({P: [BIG, SMALL]},
                  rel={P: {BIG: {"ok": False, "at": now}}}, seed={})
        self.assertNotIn(BIG, providers.tiers(P).values())

    def test_tout_casse_rend_quand_meme_une_table(self):
        now = time.time()
        self.fake({P: [BIG, SMALL]},
                  rel={P: {BIG: {"ok": False, "at": now},
                           SMALL: {"ok": False, "at": now}}}, seed={})
        self.assertEqual(providers.tiers(P)["opus"], BIG)

    def test_seed_gagne_sur_la_deduction(self):
        self.fake({P: [BIG, SMALL]}, seed={P: {"opus": SMALL}})
        self.assertEqual(providers.tiers(P)["opus"], SMALL)

    def test_seed_ignore_si_plus_servi(self):
        # Un modele sonde a la main puis retire du catalogue rendrait un 404.
        self.fake({P: [BIG, SMALL]}, seed={P: {"opus": "parti/en-fumee"}})
        self.assertEqual(providers.tiers(P)["opus"], BIG)

    def test_seed_seul_quand_le_catalogue_est_muet(self):
        # Certaines passerelles ne publient pas /models : la sonde manuelle
        # est alors la seule source.
        self.fake({P: []}, seed={P: {"sonnet": "code-supernova"}})
        t = providers.tiers(P)
        self.assertEqual(set(t.values()), {"code-supernova"})

    def test_sans_catalogue_ni_seed(self):
        self.fake({P: []}, seed={})
        self.assertEqual(providers.tiers(P), {})

    def test_surcharge_utilisateur_a_le_dernier_mot(self):
        self.fake({P: [BIG, SMALL]}, seed={P: {"opus": SMALL}})
        t = providers.tiers(P, {"opus": "a/moi", "haiku": "  ",
                                "inconnu": "x", "sonnet": None})
        self.assertEqual(t["opus"], "a/moi")
        self.assertEqual(t["haiku"], SMALL)      # blanc : ignore
        self.assertEqual(t["sonnet"], BIG)       # non-texte : ignore
        self.assertNotIn("inconnu", t)


class Pick(Fixture):

    def test_nom_deja_au_catalogue_passe_tel_quel(self):
        self.fake({P: [BIG, SMALL]}, seed={})
        self.assertEqual(providers.pick(P, BIG), BIG)

    def test_palier_traduit(self):
        self.fake({P: [BIG, MID, SMALL, NANO]}, seed={})
        self.assertEqual(providers.pick(P, "claude-opus-4-6-20260101"), BIG)
        self.assertEqual(providers.pick(P, "claude-3-5-haiku-latest"), SMALL)

    def test_nom_inconnu_tombe_sur_sonnet(self):
        self.fake({P: [BIG, SMALL]}, seed={})
        self.assertEqual(providers.pick(P, "gpt-9-turbo"), BIG)

    def test_sans_catalogue(self):
        self.fake({P: []}, seed={})
        self.assertIsNone(providers.pick(P, "claude-opus-4-6"))


class ChatBody(unittest.TestCase):

    def body(self, prov, data):
        out = providers.chat_body(prov, json.dumps(data).encode())
        return json.loads(out)

    def test_champ_de_longueur_renomme(self):
        out = self.body("cerebras", {"max_tokens": 64})
        self.assertEqual(out, {"max_completion_tokens": 64})

    def test_plancher_quand_l_amont_l_exige(self):
        self.assertEqual(self.body("xai", {})["max_tokens"], 32000)
        self.assertEqual(self.body("xai", {"max_tokens": 8})["max_tokens"], 8)

    def test_plancher_sur_le_champ_renomme(self):
        out = self.body("zenmux", {})
        self.assertEqual(out["max_completion_tokens"], 32000)
        self.assertNotIn("max_tokens", out)

    def test_champs_refuses_retires(self):
        out = self.body("cohere", {"messages": [{"role": "user", "name": "x",
                                                 "content": "."}],
                                   "frequency_penalty": 1, "n": 2,
                                   "temperature": 0.5})
        self.assertNotIn("frequency_penalty", out)
        self.assertNotIn("n", out)
        self.assertEqual(out["temperature"], 0.5)
        self.assertNotIn("name", out["messages"][0])

    def test_fournisseur_sans_ecart(self):
        self.assertEqual(self.body("nvidia_nim", {"max_tokens": 5}),
                         {"max_tokens": 5})

    def test_corps_intact_si_non_json(self):
        for raw in (b"", None, b"pas du json", b"[1, 2]"):
            self.assertEqual(providers.chat_body("cohere", raw), raw)


class Endpoint(unittest.TestCase):

    def test_hote_port_tls_prefixe(self):
        host, port, tls, prefix = providers.endpoint("nvidia_nim")
        self.assertEqual(host, "integrate.api.nvidia.com")
        self.assertEqual(port, 443)
        self.assertTrue(tls)
        self.assertTrue(prefix.startswith("/"))

    def test_local_en_clair(self):
        host, port, tls, _p = providers.endpoint("ollama")
        self.assertIn(host, ("localhost", "127.0.0.1"))
        self.assertFalse(tls)

    def test_chemin_de_completion(self):
        self.assertTrue(providers.chat_path("nvidia_nim").endswith(
            "/chat/completions"))


class Registre(unittest.TestCase):
    """Coherence du registre lui-meme : c'est lui qui pilote tout le reste."""

    def test_preference_couvre_le_catalogue(self):
        self.assertEqual(sorted(providers.PREFERENCE),
                         sorted(p for p in providers.CATALOG
                                if providers.usable(p)))

    def test_pas_de_doublon_de_preference(self):
        self.assertEqual(len(set(providers.PREFERENCE)),
                         len(providers.PREFERENCE))

    def test_chaque_fournisseur_a_une_base_et_un_nom(self):
        for prov, cfg in providers.CATALOG.items():
            if not providers.usable(prov):
                continue
            self.assertTrue(cfg.get("base"), prov)
            self.assertTrue(providers.label(prov), prov)

    def test_une_cle_ou_une_dispense(self):
        # Sans nom de variable, sans « keyless » et sans « local », rien ne
        # pourrait jamais authentifier l'appel.
        for prov, cfg in providers.CATALOG.items():
            if not providers.usable(prov):
                continue
            self.assertTrue(cfg.get("env") or cfg.get("static")
                            or cfg.get("keyless") or cfg.get("local"), prov)

    def test_seed_ne_parle_que_de_paliers_connus(self):
        for prov, table in providers.SEED.items():
            self.assertIn(prov, providers.CATALOG, prov)
            for tier in table:
                self.assertIn(tier, providers.TIERS, (prov, tier))


class Keys(Fixture):
    """Ou va la cle, et ce qui n'en sort jamais."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dbl-test-")
        self.addCleanup(self._wipe)
        self.patch(providers, "DBL_DIR", self.dir)
        self.patch(providers, "ENV_FILE", os.path.join(self.dir, ".env"))
        self.patch(providers, "FCC_ENV", os.path.join(self.dir, "fcc.env"))
        providers.forget_env()
        self.addCleanup(providers.forget_env)
        # L'environnement du testeur ne doit pas decider du resultat.
        for name in ("NVIDIA_NIM_API_KEY", "OPENROUTER_API_KEY",
                     "GROQ_API_KEY"):
            if name in os.environ:
                old = os.environ.pop(name)
                self.addCleanup(os.environ.__setitem__, name, old)

    def _wipe(self):
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def test_ecriture_puis_lecture(self):
        providers.set_key("groq", "gsk_test")
        self.assertEqual(providers.key("groq"), "gsk_test")

    def test_fichier_illisible_par_les_autres(self):
        providers.set_key("groq", "gsk_test")
        mode = stat.S_IMODE(os.stat(providers.ENV_FILE).st_mode)
        self.assertEqual(mode, 0o600)

    def test_l_environnement_gagne_sur_le_fichier(self):
        # `GROQ_API_KEY=... dbl probe groq` doit tester sans rien ecrire.
        providers.set_key("groq", "du_disque")
        os.environ["GROQ_API_KEY"] = "de_l_env"
        self.addCleanup(os.environ.pop, "GROQ_API_KEY", None)
        self.assertEqual(providers.key("groq"), "de_l_env")

    def test_les_autres_cles_survivent_a_une_ecriture(self):
        providers.set_key("groq", "un")
        providers.set_key("cerebras", "deux")
        providers.set_key("groq", "trois")
        self.assertEqual(providers.key("cerebras"), "deux")
        self.assertEqual(providers.key("groq"), "trois")

    def test_retrait(self):
        providers.set_key("groq", "un")
        providers.set_key("groq", "")
        self.assertIsNone(providers.key("groq"))

    def test_commentaires_preserves(self):
        with open(providers.ENV_FILE, "w") as fh:
            fh.write("# mes notes\nAUTRE=1\n")
        providers.set_key("groq", "un")
        with open(providers.ENV_FILE) as fh:
            body = fh.read()
        self.assertIn("# mes notes", body)
        self.assertIn("AUTRE=1", body)

    def test_import_fcc_ne_touche_pas_au_fichier_de_fcc(self):
        with open(providers.FCC_ENV, "w") as fh:
            fh.write("GROQ_API_KEY=de_fcc\nCEREBRAS_API_KEY=aussi\n")
        before = open(providers.FCC_ENV).read()
        taken = providers.import_fcc_keys()
        self.assertIn("GROQ_API_KEY", taken)
        self.assertEqual(providers.key("groq"), "de_fcc")
        self.assertEqual(open(providers.FCC_ENV).read(), before)

    def test_import_fcc_n_ecrase_pas_une_cle_a_nous(self):
        providers.set_key("groq", "a_moi")
        with open(providers.FCC_ENV, "w") as fh:
            fh.write("GROQ_API_KEY=de_fcc\n")
        providers.import_fcc_keys()
        self.assertEqual(providers.key("groq"), "a_moi")

    def test_cle_absente(self):
        self.assertIsNone(providers.key("groq"))

    def test_sans_cle_assumee(self):
        # Trois fournisseurs servent sans authentification : leur absence de
        # cle ne doit pas les faire passer pour mal configures.
        for prov in ("kilo", "opencode_go", "opencode_zen"):
            self.assertTrue(providers.keyless(prov), prov)

    def test_cle_facultative_reste_ecrivable(self):
        # « keyless » veut dire « sert sans cle », pas « refuse une cle » :
        # Kilo accepte un compte, et le poser doit marcher.
        providers.set_key("kilo", "kilo_test")
        self.assertEqual(providers.key("kilo"), "kilo_test")

    def test_serveur_local_refuse_l_ecriture(self):
        # Un serveur local n'a pas de variable d'environnement : ecrire la
        # cle quelque part serait une illusion silencieuse.
        with self.assertRaises(ValueError):
            providers.set_key("ollama", "x")


if __name__ == "__main__":
    unittest.main()
