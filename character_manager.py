#!/usr/bin/env python3
"""
character_manager.py — Multi-character support.

Supported folder structures (auto-detected):
  character/          <- singular  (legacy / your current structure)
    liana/
      character.json
      banter.json
      scenarios.json
  characters/         <- plural
    liana/
      character.json
      ...

Usage:
  mgr = CharacterManager()
  mgr.list_characters()        # ['alfa', 'liana']
  mgr.load("liana")            # loads all 3 files
  mgr.character                # dict
  mgr.banter                   # list
  mgr.scenarios                # dict
  mgr.banter_path              # path string  (pass to BanterManager)
  mgr.scenario_path            # path string
"""

import json
import os
from typing import Dict, List, Optional

_BASE = os.path.dirname(os.path.abspath(__file__))


def _find_character_dir() -> str:
    """
    Auto-detect which folder holds the characters.
    Priority: character/ -> characters/ -> fallback create characters/
    Validates by checking for at least one sub-folder with character.json.
    """
    for candidate in ("character", "characters"):
        path = os.path.join(_BASE, candidate)
        if not os.path.isdir(path):
            continue
        for entry in os.listdir(path):
            sub = os.path.join(path, entry)
            if os.path.isdir(sub) and os.path.isfile(
                os.path.join(sub, "character.json")
            ):
                print(f"[CHAR] Auto-detected folder: {path}")
                return path
    # Fallback
    return os.path.join(_BASE, "characters")


class CharacterManager:
    """Load and expose one character at a time."""

    def __init__(self, characters_dir: str = None):
        self.dir = characters_dir if characters_dir else _find_character_dir()
        os.makedirs(self.dir, exist_ok=True)

        self._name:      Optional[str] = None
        self._character: Dict          = {}
        self._banter:    List          = []
        self._scenarios: Dict          = {}

    # -------------------------------------------------------------------------
    # Discovery
    # -------------------------------------------------------------------------

    def list_characters(self) -> List[str]:
        """Return sorted list of character names (directories with character.json)."""
        if not os.path.isdir(self.dir):
            return []
        return sorted(
            d for d in os.listdir(self.dir)
            if os.path.isdir(os.path.join(self.dir, d))
            and os.path.isfile(os.path.join(self.dir, d, "character.json"))
        )

    # -------------------------------------------------------------------------
    # Load
    # -------------------------------------------------------------------------

    def load(self, character_name: str) -> bool:
        """Load character.json + banter.json + scenario(s).json for given name."""
        char_dir  = os.path.join(self.dir, character_name)
        char_file = os.path.join(char_dir, "character.json")

        if not os.path.isfile(char_file):
            print(f"[CHAR] character.json not found: {char_file}")
            return False

        try:
            with open(char_file, "r", encoding="utf-8") as f:
                self._character = json.load(f)
        except Exception as e:
            print(f"[CHAR] Load error ({char_file}): {e}")
            return False

        # Support both scenarios.json and scenario.json
        scenario_file = os.path.join(char_dir, "scenarios.json")
        if not os.path.isfile(scenario_file):
            scenario_file = os.path.join(char_dir, "scenario.json")

        self._banter    = self._load_json(os.path.join(char_dir, "banter.json"),   [])
        self._scenarios = self._load_json(scenario_file, {})
        self._name      = character_name

        n_banter    = len(self._banter)
        n_scenarios = sum(
            len(v) if isinstance(v, list) else 1
            for v in self._scenarios.values()
        ) if self._scenarios else 0

        print(
            f"[CHAR] '{character_name}' loaded | "
            f"folder={self.dir} | "
            f"banter={n_banter} | scenarios={n_scenarios} | "
            f"anims={self.get_animations()}"
        )
        return True

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

    @property
    def active(self) -> Optional[str]:
        return self._name

    @property
    def character(self) -> Dict:
        if not self._character:
            raise RuntimeError("No character loaded — call load() first.")
        return self._character

    @property
    def banter(self) -> List:
        return self._banter

    @property
    def scenarios(self) -> Dict:
        return self._scenarios

    # -- File paths (for BanterManager) ---------------------------------------

    @property
    def banter_path(self) -> str:
        if not self._name:
            return "banter.json"
        return os.path.join(self.dir, self._name, "banter.json")

    @property
    def scenario_path(self) -> str:
        if not self._name:
            return "scenarios.json"
        for fname in ("scenarios.json", "scenario.json"):
            path = os.path.join(self.dir, self._name, fname)
            if os.path.isfile(path):
                return path
        return os.path.join(self.dir, self._name, "scenarios.json")

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def get_animations(self) -> List[str]:
        """Return allowed animation/expression names for this character."""
        return self._character.get(
            "animations", ["smile", "angry", "shy", "default"]
        )

    def get_default_expression(self) -> str:
        """First animation as safe fallback."""
        anims = self.get_animations()
        return anims[0] if anims else "default"

    # -------------------------------------------------------------------------
    # Static utils
    # -------------------------------------------------------------------------

    @staticmethod
    def _load_json(path: str, default):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                # Simple comment stripping: strip lines starting with // or #
                cleaned = "\n".join(
                    line for line in content.splitlines()
                    if not line.strip().startswith("//") and not line.strip().startswith("#")
                )
                return json.loads(cleaned)
            except Exception as e:
                print(f"[CHAR] Load {os.path.basename(path)}: {e}")
        return default
