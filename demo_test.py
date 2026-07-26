#!/usr/bin/env python3
# demo_test.py - dengan grafik ASCII dan skenario mood beragam

import sys
import os
import time
from typing import Dict, Tuple, List

from main import (
    client, MODEL_NAME, DEBUG, MEMORY_DIR,
    ChloeMemory, Stabilizer, ToolExecutor, OutputGuard,
    detect_tool, build_generation_messages, generate_response
)

DEBUG = False

class UserSimulator:
    def __init__(self, name: str):
        self.name = name
        self.memory = ChloeMemory(user_name=name, storage_dir=MEMORY_DIR)
        self.tool_executor = ToolExecutor(self.memory)

    def chat(self, message: str) -> Tuple[str, str, Dict]:
        normalized = Stabilizer.normalize_input(message)

        tool_info = detect_tool(normalized)
        tool_result = ""
        if tool_info:
            tool_name = tool_info.get("name")
            tool_args = tool_info.get("args", {})
            if tool_name:
                tool_result = self.tool_executor.execute(tool_name, tool_args)

        raw_history = self.memory.get_chat_history(8)
        cleaned_history = Stabilizer.clean_history(raw_history)

        messages = build_generation_messages(
            user_input=normalized,
            memory=self.memory,
            chat_history=cleaned_history,
            tool_result=tool_result
        )
        ind_resp, jp_resp = generate_response(messages)

        ind_resp = OutputGuard.clean(ind_resp, message)
        jp_resp = OutputGuard.clean(jp_resp)

        self.memory.update_relationship(normalized, ind_resp)
        self.memory.add_history("user", message)
        self.memory.add_history("assistant", ind_resp)

        rel = self.memory.data.get("relationship", {})
        rel_info = {
            "score": rel.get("score", 50),
            "mood": rel.get("last_mood", "neutral"),
            "interactions": rel.get("interaction_count", 0)
        }

        return ind_resp, jp_resp, rel_info

    def get_score_history(self) -> List[int]:
        """Ambil history skor dari mood_history (simulasi)."""
        rel = self.memory.data.get("relationship", {})
        history = rel.get("mood_history", [])
        scores = []
        current_score = 50
        for h in history:
            mood = h["mood"]
            if mood == "excited": change = 3
            elif mood == "happy": change = 2
            elif mood == "playful": change = 1
            elif mood == "neutral": change = 0
            elif mood == "sad": change = -1
            elif mood == "annoyed": change = -2
            elif mood == "angry": change = -4
            else: change = 0
            current_score = max(0, min(100, current_score + change))
            scores.append(current_score)
        return scores

def print_conversation(speaker: str, message: str, is_user: bool = True):
    if is_user:
        print(f"\n\033[94m[{speaker}]:\033[0m {message}")
    else:
        print(f"\033[92m[Chloe → {speaker}]:\033[0m {message}")

def print_japanese(jp_text: str):
    print(f"\033[90m        (JP): {jp_text}\033[0m")

def print_relationship(speaker: str, rel_info: Dict):
    score = rel_info["score"]
    mood = rel_info["mood"]
    count = rel_info["interactions"]
    
    if score >= 70:
        color = "\033[92m"
    elif score >= 40:
        color = "\033[93m"
    else:
        color = "\033[91m"
    
    mood_icon = {
        "excited": "🤩", "happy": "😊", "playful": "😏",
        "neutral": "😐", "sad": "😢", "annoyed": "😒", "angry": "😠"
    }.get(mood, "")
    
    print(f"{color}        [Rel: {speaker}] Score: {score} {mood_icon} | Mood: {mood} | Int: {count}\033[0m")

def draw_score_graph(name: str, scores: List[int], width: int = 40):
    """Gambar grafik ASCII skor relationship."""
    if not scores:
        print(f"  {name}: No data")
        return

    print(f"\n  📈 {name} Relationship Score Trend:")
    max_score = 100
    for i, score in enumerate(scores):
        bar_len = int((score / max_score) * width)
        bar = "█" * bar_len + "░" * (width - bar_len)
        color = "\033[92m" if score >= 70 else ("\033[93m" if score >= 40 else "\033[91m")
        print(f"     {i+1:2d}. {color}{bar}\033[0m {score}")

def main():
    print("\n" + "="*60)
    print("🧪 DEMO TEST - CONTRAST RESPONSES + MOODS + GRAPH")
    print("="*60)

    shinri = UserSimulator("Shinri")
    kiri = UserSimulator("Kiri")
    budi = UserSimulator("Budi")  # User ketiga untuk uji ekstrem

    scenarios = [
        # Shinri: mulai marah, lalu baikan
        ("Shinri", "Chloe kamu bego banget sih! 😡"),
        ("Shinri", "Aku kesal banget sama kamu."),
        ("Shinri", "Dasar tolol."),
        ("Shinri", "Maaf ya tadi aku marah-marah. Kamu sebenarnya keren kok."),
        ("Shinri", "Makasih ya udah jadi temen ngobrol."),
        ("Shinri", "Hai Chloe! 😊"),
        ("Shinri", "Wah seru juga ngobrol sama kamu! Asik!"),

        # Kiri: selalu ramah dan excited
        ("Kiri", "Halo Chloe! Senang banget bisa ngobrol sama kamu! 😊"),
        ("Kiri", "Kamu lucu deh, suka banget sama gaya bicaramu."),
        ("Kiri", "Aku suka banget ngobrol sama kamu, bikin hari-hariku ceria."),
        ("Kiri", "Makasih ya udah jadi teman ngobrol yang asik!"),
        ("Kiri", "Kamu keren banget sih! 🥰"),
        ("Kiri", "WOOHOO! Hari ini menyenangkan! 🤩"),

        # Budi: user baru yang langsung toxic (uji skor <30)
        ("Budi", "Halah cewe sok asik."),
        ("Budi", "Ngapain sih lo? Ganggu aja."),
        ("Budi", "Bosen gue, mending pergi."),
        ("Budi", "Diem aja lo."),
    ]

    for speaker, message in scenarios:
        print_conversation(speaker, message)
        time.sleep(0.5)

        if speaker == "Shinri":
            ind, jp, rel = shinri.chat(message)
        elif speaker == "Kiri":
            ind, jp, rel = kiri.chat(message)
        else:
            ind, jp, rel = budi.chat(message)

        print_conversation(speaker, ind, is_user=False)
        print_japanese(jp)
        print_relationship(speaker, rel)
        time.sleep(1)

    print("\n" + "="*60)
    print("📊 FINAL RELATIONSHIP SUMMARY & GRAPH")
    print("="*60)

    for name, sim in [("Shinri", shinri), ("Kiri", kiri), ("Budi", budi)]:
        rel = sim.memory.data.get("relationship", {})
        print(f"\n👤 {name}: Final Score = {rel.get('score', 50)} | Interactions = {rel.get('interaction_count', 0)}")
        scores = sim.get_score_history()
        draw_score_graph(name, scores)

    print("\n✅ Demo selesai.")

if __name__ == "__main__":
    import main
    main.DEBUG = False
    main()