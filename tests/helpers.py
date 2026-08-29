"""Outils communs aux tests : import du paquet et etats injectes.

Les tests ne touchent ni au reseau ni au ~/.doublure de la machine. Les deux
seules sources exterieures du registre — le catalogue d'un fournisseur et la
sante de ses modeles — sont remplacees par des dictionnaires en memoire.
"""

import os
import sys
import unittest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import providers  # noqa: E402


class Fixture(unittest.TestCase):
    """Cas de test qui peut injecter un catalogue et une sante."""

    def fake(self, catalog=None, rel=None, seed=None):
        """Remplace models(), health() et SEED pour la duree du test."""
        catalog = dict(catalog or {})
        rel = dict(rel or {})

        self.patch(providers, "models",
                   lambda prov, refresh=False: list(catalog.get(prov, [])))
        self.patch(providers, "health",
                   lambda prov=None: dict(rel.get(prov, {})))
        if seed is not None:
            self.patch(providers, "SEED", dict(seed))
        return catalog, rel

    def patch(self, obj, name, value):
        """Remplace un attribut et le remet en place a la fin du test."""
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)
