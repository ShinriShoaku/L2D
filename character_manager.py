#!/usr/bin/env python3
"""
character_manager.py — Multi-character support.

--- PATCH: integrasi character_memory.py (per-character bin) ---
Setiap kali load(name) dipanggil, modul ini otomatis:
  1. Load character.json + banter.json + scenario(s).json
  2. Load / auto-generate CharacterMemory di FOLDER KARAKTER itu sendiri:
     characters/<nama>/character_memory.bin
"""

import json
import os
from typing import Dict, List, Optional

import character_memory

_BASE = os.path.dirname(os.path.abspath(__file__))

def _find_character_dir() -> str:
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
    return os.path.join(_BASE, "characters")

class CharacterManager:
    def __init__(self, characters_dir: str = None):
        self.dir = characters_dir if characters_dir else _find_character_dir()
        os.makedirs(self.dir, exist_ok=True)
        self._name: Optional[str] = None
        self._character: Dict = {}
        self._banter: List = []
        self._scenarios: Dict = {}
        self._char_memory: Optional["character_memory.CharacterMemory"] = None
        self._char_dir: Optional[str] = None

    def list_characters(self) -> List[str]:
        if not os.path.isdir(self.dir):
            return []
        return sorted(
            d for d in os.listdir(self.dir)
            if os.path.isdir(os.path.join(self.dir, d))
            and os.path.isfile(os.path.join(self.dir, d, "character.json"))
        )

    def load(self, character_name: str) -> bool:
        char_dir = os.path.join(self.dir, character_name)
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

        scenario_file = os.path.join(char_dir, "scenarios.json")
        if not os.path.isfile(scenario_file):
            scenario_file = os.path.join(char_dir, "scenario.json")

        self._banter = self._load_json(os.path.join(char_dir, "banter.json"), [])
        self._scenarios = self._load_json(scenario_file, {})
        self._name = character_name
        self._char_dir = char_dir

        # PATCH: load / auto-generate character memory di FOLDER KARAKTER
        self._char_memory = character_memory.load(
            character_name,
            self._character,
            char_dir=char_dir,
        )

        n_banter = len(self._banter)
        n_scenarios = sum(
            len(v) if isinstance(v, list) else 1
            for v in self._scenarios.values()
        ) if self._scenarios else 0

        mem_path = os.path.join(char_dir, "character_memory.bin")
        has_mem = os.path.isfile(mem_path)

        print(
            f"[CHAR] '{character_name}' loaded | "
            f"folder={self.dir} | "
            f"banter={n_banter} | scenarios={n_scenarios} | "
            f"anims={self.get_animations()} | "
            f"memory_wants={len(self._char_memory.wants)} | "
            f"mem_file={'ada' if has_mem else 'baru dibuat'}"
        )
        return True

    

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

    @property
    def char_memory(self) -> "character_memory.CharacterMemory":
        if self._char_memory is None:
            raise RuntimeError("No character loaded — call load() first.")
        return self._char_memory

    @property
    def char_dir(self) -> Optional[str]:
        """Folder path karakter aktif (characters/<nama>/)."""
        return self._char_dir

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

    def get_animations(self) -> List[str]:
        return self._character.get(
            "animations", ["smile", "angry", "shy", "default"]
        )

    def get_default_expression(self) -> str:
        anims = self.get_animations()
        return anims[0] if anims else "default"

    @staticmethod
    def _load_json(path: str, default):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                    cleaned = "\n".join(
                        line for line in content.splitlines()
                        if not line.strip().startswith("//") and not line.strip().startswith("#")
                    )
                    return json.loads(cleaned)
            except Exception as e:
                print(f"[CHAR] Load {os.path.basename(path)}: {e}")
        return default