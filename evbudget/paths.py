"""File and folder locations used by the app."""

import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "DataFiles")
INPUT_DIR = os.path.join(DATA_DIR, "input")
LOCAL_STORE_DIR = os.path.join(DATA_DIR, "LocalStore")
BACKUP_DIR = os.path.join(LOCAL_STORE_DIR, "backups")
DB_PATH = os.path.join(LOCAL_STORE_DIR, "budget_store.db")
MASTER_CSV_PATH = os.path.join(LOCAL_STORE_DIR, "master_transactions.csv")
WEB_DIR = os.path.join(ROOT_DIR, "web")

# Pre-3.0 settings files. They are read once by the migration, then renamed.
LEGACY_ENVELOPES_PATH = os.path.join(DATA_DIR, "envelopes.json")
LEGACY_USER_RULES_PATH = os.path.join(DATA_DIR, "user_rules.json")
LEGACY_KNOWN_VENDORS_PATH = os.path.join(DATA_DIR, "known_vendors.json")
