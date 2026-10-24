"""Budget profiles: one folder per budget, so several people can keep separate books.

Each profile is DataFiles/Profiles/<name>/ with its own database, input folder, backups and CSV copy.
Rules that everyone shares live in one separate database (see store.ensure_shared).

A PIN is optional per profile. It keeps a casual look at someone else's budget from happening by accident;
it does not encrypt the database file. Forgot it? Delete pin.json in the profile folder.
"""

import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
from typing import Any, Dict, List, Optional

from . import paths, store

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{0,39}$")
RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
MIN_PIN_LENGTH = 4
PIN_ITERATIONS = 200_000


class ProfileError(Exception):
    """Something the user can fix. The message is shown to them."""


def _hash_pin(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PIN_ITERATIONS).hex()


class Profiles:
    def __init__(
        self,
        profiles_dir: Optional[str] = None,
        shared_db_path: Optional[str] = None,
        legacy_db_path: Optional[str] = None,
        legacy_input_dir: Optional[str] = None,
    ):
        self.profiles_dir = profiles_dir or paths.PROFILES_DIR
        self.shared_db_path = shared_db_path or paths.SHARED_DB_PATH
        self.legacy_db_path = legacy_db_path or paths.DB_PATH
        self.legacy_input_dir = legacy_input_dir or paths.INPUT_DIR

    # ------------------------------------------------------------------ names and folders

    def clean_name(self, name: str) -> str:
        name = " ".join(str(name or "").split())
        if not NAME_RE.match(name) or name.lower() in RESERVED_NAMES:
            raise ProfileError("Use 1 to 40 letters, numbers, spaces, dashes or underscores for the budget name.")
        return name

    def _find(self, name: str) -> Optional[str]:
        """The stored spelling of a profile, ignoring case."""
        wanted = " ".join(str(name or "").split()).lower()
        for existing in self.names():
            if existing.lower() == wanted:
                return existing
        return None

    def names(self) -> List[str]:
        if not os.path.isdir(self.profiles_dir):
            return []
        return sorted(
            (n for n in os.listdir(self.profiles_dir) if os.path.isfile(os.path.join(self.profiles_dir, n, "budget_store.db"))),
            key=str.lower,
        )

    def profile_paths(self, name: str) -> Dict[str, str]:
        found = self._find(name)
        if found is None:
            raise ProfileError(f"There is no budget called '{name}'.")
        folder = os.path.join(self.profiles_dir, found)
        return {
            "name": found, "dir": folder, "db": os.path.join(folder, "budget_store.db"),
            "input": os.path.join(folder, "input"), "csv": os.path.join(folder, "master_transactions.csv"),
        }

    def listing(self) -> List[Dict[str, Any]]:
        last = self.last_used()
        return [{"name": n, "has_pin": self.has_pin(n), "last_used": n == last} for n in self.names()]

    def last_used(self) -> str:
        try:
            with open(os.path.join(self.profiles_dir, "last_used.txt"), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def set_last_used(self, name: str) -> None:
        try:
            os.makedirs(self.profiles_dir, exist_ok=True)
            with open(os.path.join(self.profiles_dir, "last_used.txt"), "w", encoding="utf-8") as f:
                f.write(name)
        except OSError:
            pass

    # ------------------------------------------------------------------ creating

    def create(self, name: str, pin: str = "") -> Dict[str, str]:
        """Make an empty budget. It starts with the default envelopes and the shared rules, and no people or income."""
        name = self.clean_name(name)
        if self._find(name):
            raise ProfileError(f"A budget called '{self._find(name)}' already exists.")
        if pin and len(pin) < MIN_PIN_LENGTH:
            raise ProfileError(f"A PIN needs at least {MIN_PIN_LENGTH} characters.")
        folder = os.path.join(self.profiles_dir, name)
        os.makedirs(os.path.join(folder, "input"), exist_ok=True)
        try:
            store.ensure_schema(os.path.join(folder, "budget_store.db"), self.shared_db_path, fresh=True)
            if pin:
                self._write_pin(name, pin)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return self.profile_paths(name)

    # ------------------------------------------------------------------ PIN

    def _pin_file(self, name: str) -> str:
        return os.path.join(self.profile_paths(name)["dir"], "pin.json")

    def has_pin(self, name: str) -> bool:
        return os.path.isfile(self._pin_file(name))

    def _write_pin(self, name: str, pin: str) -> None:
        salt = secrets.token_bytes(16)
        with open(self._pin_file(name), "w", encoding="utf-8") as f:
            json.dump({"salt": salt.hex(), "hash": _hash_pin(pin, salt), "iterations": PIN_ITERATIONS}, f)

    def check_pin(self, name: str, pin: str) -> bool:
        """True if the profile has no PIN, or this is it."""
        if not self.has_pin(name):
            return True
        try:
            with open(self._pin_file(name), encoding="utf-8") as f:
                saved = json.load(f)
            return hmac.compare_digest(_hash_pin(str(pin or ""), bytes.fromhex(saved["salt"])), saved["hash"])
        except (OSError, ValueError, KeyError):
            return False

    def set_pin(self, name: str, current_pin: str, new_pin: str) -> bool:
        """Set, change or (with an empty new_pin) remove the PIN. Returns whether the profile has a PIN now."""
        if not self.check_pin(name, current_pin):
            raise ProfileError("That is not the current PIN.")
        if not new_pin:
            if self.has_pin(name):
                os.remove(self._pin_file(name))
            return False
        if len(new_pin) < MIN_PIN_LENGTH:
            raise ProfileError(f"A PIN needs at least {MIN_PIN_LENGTH} characters.")
        self._write_pin(name, new_pin)
        return True

    # ------------------------------------------------------------------ moving a single-budget install into a profile

    def legacy_found(self) -> bool:
        return os.path.isfile(self.legacy_db_path)

    def migrate_legacy(self, name: str) -> Dict[str, Any]:
        """Turn the budget from the single-budget layout into a profile.

        The database is copied first and checked; only then are the other files moved and the old database
        renamed (budget_store.db.migrated, kept as a backup). If anything fails before that, nothing has moved.
        Every rule the old budget had becomes a shared rule, so it keeps applying to everyone.
        """
        name = self.clean_name(name)
        if self._find(name):
            raise ProfileError(f"A budget called '{self._find(name)}' already exists.")
        if not self.legacy_found():
            raise ProfileError("There is no earlier budget to move.")
        folder = os.path.join(self.profiles_dir, name)
        db = os.path.join(folder, "budget_store.db")
        os.makedirs(os.path.join(folder, "input"), exist_ok=True)
        try:
            store.backup_db(self.legacy_db_path)
            store.backup_db(self.legacy_db_path, backup_dir=folder, prefix="budget_store")  # a consistent copy, even if a journal is pending
            os.replace(max(glob.glob(os.path.join(folder, "budget_store_*.db"))), db)
            store.ensure_schema(db, self.shared_db_path)
            conn = store.connect(db, shared_path=self.shared_db_path)
            try:
                moved_rules = store.move_rules_to_shared(conn)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

        warnings: List[str] = []
        legacy_store = os.path.dirname(self.legacy_db_path)
        moves = [(os.path.join(legacy_store, "master_transactions.csv"), os.path.join(folder, "master_transactions.csv"))]
        for src_dir, dst_dir in ((os.path.join(legacy_store, "backups"), os.path.join(folder, "backups")),
                                 (self.legacy_input_dir, os.path.join(folder, "input"))):
            if os.path.isdir(src_dir):
                os.makedirs(dst_dir, exist_ok=True)
                moves += [(os.path.join(src_dir, f), os.path.join(dst_dir, f)) for f in os.listdir(src_dir)]
        for src, dst in moves:
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    shutil.move(src, dst)
                except OSError as exc:
                    warnings.append(f"Could not move {os.path.basename(src)}: {exc}")
        os.replace(self.legacy_db_path, self.legacy_db_path + ".migrated")
        self.set_last_used(name)
        return {"name": name, "rules_shared": moved_rules, "warnings": warnings}
