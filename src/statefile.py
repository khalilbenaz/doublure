#!/usr/bin/env python3
"""Ecriture partagee de l'etat : le routeur et la CLI touchent les memes fichiers.

Deux ecrivains, dans deux processus, sur `state.json` et `accounts.json`. Sans
coordination, deux defauts silencieux :

  - le fichier temporaire portait le meme nom des deux cotes ; deux ecritures
    simultanees se tronquent l'une l'autre avant `os.replace`, qui publie alors
    un JSON coupe en deux ;
  - chacun lit tout, modifie un champ, reecrit tout — le second efface la
    modification du premier. `dbl on zen` annulait une bascule du routeur, et
    un changement de compte annulait un changement de mode.

D'ou un verrou de fichier tenu pendant la lecture *et* l'ecriture, et un
temporaire unique par processus et par thread.
"""

import fcntl
import json
import os
import threading

HOME = os.path.expanduser("~")
DBL_DIR = os.path.join(HOME, ".doublure")
LOCK_FILE = os.path.join(DBL_DIR, "state.lock")

_held = {"depth": 0, "fh": None}
_reentry = threading.RLock()


class file_lock:
    """Verrou exclusif inter-processus, reentrant dans un meme processus.

    Un flock est attache au descripteur, pas au thread : reouvrir le fichier
    alors qu'on tient deja le verrou attendrait sa propre liberation —
    interblocage. D'ou le compteur de profondeur, et le RLock qui range les
    threads du processus derriere une seule prise.

    Si le verrou ne peut pas etre pris (systeme de fichiers sans flock), on
    ecrit quand meme : une coordination absente vaut mieux qu'un outil bloque.
    """

    def __enter__(self):
        _reentry.acquire()
        if _held["depth"] == 0:
            try:
                os.makedirs(DBL_DIR, exist_ok=True)
                fh = open(LOCK_FILE, "a+")
                fcntl.flock(fh, fcntl.LOCK_EX)
                _held["fh"] = fh
            except OSError:
                _held["fh"] = None
        _held["depth"] += 1
        return self

    def __exit__(self, *exc):
        _held["depth"] -= 1
        if _held["depth"] == 0 and _held["fh"]:
            try:
                fcntl.flock(_held["fh"], fcntl.LOCK_UN)
            except OSError:
                pass
            _held["fh"].close()
            _held["fh"] = None
        _reentry.release()
        return False


def write_json(path, data, indent=1):
    """Ecrit un JSON en place, atomiquement. Rend False si ca n'a pas pu."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=indent, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
