"""File and folder locations used by the app."""

import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "DataFiles")
WEB_DIR = os.path.join(ROOT_DIR, "web")

# Each budget is a profile: DataFiles/Profiles/<name>/ holds its database, input folder, backups and CSV copy.
# Rules everyone shares live in one database beside the profiles.
PROFILES_DIR = os.path.join(DATA_DIR, "Profiles")
SHARED_DIR = os.path.join(DATA_DIR, "Shared")
SHARED_DB_PATH = os.path.join(SHARED_DIR, "shared_rules.db")

# Where the single-budget versions kept everything. Read once to move it into a profile.
INPUT_DIR = os.path.join(DATA_DIR, "input")
LOCAL_STORE_DIR = os.path.join(DATA_DIR, "LocalStore")
BACKUP_DIR = os.path.join(LOCAL_STORE_DIR, "backups")
DB_PATH = os.path.join(LOCAL_STORE_DIR, "budget_store.db")
MASTER_CSV_PATH = os.path.join(LOCAL_STORE_DIR, "master_transactions.csv")

# Pre-3.0 settings files. They are read once by the migration, then renamed.
LEGACY_ENVELOPES_PATH = os.path.join(DATA_DIR, "envelopes.json")
LEGACY_USER_RULES_PATH = os.path.join(DATA_DIR, "user_rules.json")
LEGACY_KNOWN_VENDORS_PATH = os.path.join(DATA_DIR, "known_vendors.json")
