#!/usr/bin/env python3
# wav_detector.py - Deteksi suara dari file WAV untuk lipsync

import wave
import struct
import math
import random
import re
from typing import List, Dict

class WavVoiceDetector:
    WINDOW_MS = 10
    VOICE_THRESHOLD = 0.005

    @staticmethod
    def get_wav_duration_ms(file_path: str) -> int:
        try:
            with wave.open(file_path, 'rb') as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()
                return int((frames / rate) * 1000)
        except Exception as e:
            print(f"[WAV ERROR] {e}")
            return -1

    @staticmethod
    def analyze(file_path: str, target_string: str) -> Dict:
        if file_path == "offline":
            return {
                "mappedOutput": ".w..elco.met.my.......vo..ice.zurc.d..jxpy",
                "wavDurationMs": 4000
            }

        try:
            with wave.open(file_path, 'rb') as wav:
                params = wav.getparams()
                sample_rate = params.framerate
                num_channels = params.nchannels
                sample_width = params.sampwidth
                num_frames = params.nframes

                if num_channels != 1:
                    raise ValueError("Only mono WAV supported")

                audio_bytes = wav.readframes(num_frames)

            # Konversi byte ke array float (-1.0 .. 1.0)
            if sample_width == 2:
                fmt = f"<{num_frames}h"
                samples = struct.unpack(fmt, audio_bytes)
                audio_data = [s / 32768.0 for s in samples]
            else:
                raise ValueError("Only 16-bit PCM supported")

            window_size = int((sample_rate * WavVoiceDetector.WINDOW_MS) / 1000)
            if window_size == 0:
                window_size = 1

            num_windows = math.ceil(len(audio_data) / window_size)
            voice_array = []

            for i in range(num_windows):
                start = i * window_size
                end = min(start + window_size, len(audio_data))
                chunk = audio_data[start:end]
                if len(chunk) == 0:
                    voice_array.append(0)
                    continue
                energy = sum(s * s for s in chunk) / len(chunk)
                voice_array.append(1 if energy > WavVoiceDetector.VOICE_THRESHOLD else 0)

            mapped = WavVoiceDetector._map_voice_to_text(voice_array, target_string)
            duration = WavVoiceDetector.get_wav_duration_ms(file_path)

            return {
                "mappedOutput": mapped,
                "wavDurationMs": duration
            }

        except Exception as e:
            print(f"[WAV ANALYZE ERROR] {e}")
            return {
                "mappedOutput": "",
                "wavDurationMs": 0
            }

    @staticmethod
    def _map_voice_to_text(voice_array: List[int], text: str) -> str:
        # Hapus whitespace
        filtered = re.sub(r'\s+', '', text)
        # Ganti non-romaji dengan random a-z
        romaji = []
        for c in filtered:
            if c.isalpha() and c.isascii():
                romaji.append(c.lower())
            else:
                romaji.append(chr(random.randint(97, 122)))
        processed = ''.join(romaji)
        text_len = len(processed)

        segment_ms = 100
        windows_per_segment = segment_ms // WavVoiceDetector.WINDOW_MS
        if windows_per_segment == 0:
            windows_per_segment = 1

        result = []
        text_idx = 0

        for i in range(0, len(voice_array), windows_per_segment):
            segment_end = min(i + windows_per_segment, len(voice_array))
            voice_count = sum(voice_array[i:segment_end])

            if voice_count > (windows_per_segment / 2):
                if text_idx < text_len:
                    result.append(processed[text_idx])
                    text_idx += 1
                else:
                    result.append(chr(random.randint(97, 122)))
            else:
                result.append('.')

        return ''.join(result)