"""
Polisher Utility for Text Normalization and Rebuilding.
Handles cleanup of ebook text and reconstruction of fragmented audio sentences.
"""

import re
from typing import List, Dict, Tuple

class Polisher:
    """
    Polishes text for alignment and rebuilds fragmented sentences.
    """

    def __init__(self):
        # Roman numeral pattern (I to X, simplistic for chapter numbers)
        self.roman_pattern = re.compile(r'^(?=[MDCLXVI])M*(C[MD]|D?C{0,3})(X[CL]|L?X{0,3})(I[XV]|V?I{0,3})$', re.IGNORECASE)
        # Spelled out numbers mapping (0-100 covering most common cases)
        self.number_map = {
            'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
            'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
            'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
            'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
            'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40,
            'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80,
            'ninety': 90
        }

    def clean_punctuation(self, text: str) -> str:
        """
        Removes punctuation to standardize text for comparison.
        Keeps alphanumerics and spaces.
        """
        # Replace dashes/underscores with spaces to avoid merging words
        text = re.sub(r'[-_]', ' ', text)
        # Remove non-alphanumeric (except spaces)
        text = re.sub(r'[^\w\s]', '', text)
        return text

    def roman_to_int(self, text: str) -> str:
        """
        Converts Roman numerals to digits if the entire token is a Roman numeral.
        Useful for chapter headers like "Chapter IV" -> "Chapter 4".
        """
        token = text.strip().upper()
        if not token or not self.roman_pattern.match(token):
            return text

        # Simple conversion logic
        roman_values = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
        total = 0
        prev_value = 0
        
        for char in reversed(token):
            value = roman_values.get(char, 0)
            if value >= prev_value:
                total += value
            else:
                total -= value
            prev_value = value
            
        return str(total)

    def text_to_digits(self, text: str) -> str:
        """
        Converts spelled-out numbers to digits ("twenty five" -> "25").
        Handles 0-100 logic primarily for chapter numbers.
        """
        words = text.split()
        new_words = []
        i = 0
        while i < len(words):
            word = words[i].lower()
            if word in self.number_map:
                value = self.number_map[word]
                # Check for compound (twenty one)
                if i + 1 < len(words):
                    next_word = words[i+1].lower()
                    if next_word in self.number_map:
                        next_val = self.number_map[next_word]
                        # Only combined distinct magnitudes (20 + 1)
                        if value >= 20 and next_val < 10:
                            value += next_val
                            i += 1
                new_words.append(str(value))
            else:
                new_words.append(words[i])
            i += 1
            
        return " ".join(new_words)

    def collapse_whitespace(self, text: str) -> str:
        """Reduces multiple spaces to a single space and strips ends."""
        return re.sub(r'\s+', ' ', text).strip()

    def normalize(self, text: str) -> str:
        """
        Master normalization function.
        1. Collapse whitespace
        2. Convert Lowercase
        3. Convert Roman Numerals (in context?) - doing this word-wise might be risky for "I" (noun vs number).
           Adjusted: Only apply strict Roman conversion for "Chapter X" context usually, but for general match:
           Let's stick to standard alphanumeric polish.
        """
        if not text:
            return ""

        # 1. Basic Clean
        text = self.collapse_whitespace(text)
        
        # 2. Punctuation Removal (aggressive for matching)
        cleaned = self.clean_punctuation(text)
        
        # 3. Lowercase
        cleaned = cleaned.lower()
        
        # 4. Spelled numbers (optional, can be risky if "one" is used as noun)
        # For strict alignment, we might want to skip this or make it configurable. 
        # Using a safer approach: "Chapter One" contexts are handled by specific logic usually.
        # Let's apply it as it helps match "Chapter 1" to "Chapter One" in audio.
        cleaned = self.text_to_digits(cleaned)
        
        result = self.collapse_whitespace(cleaned)

        return result

    def rebuild_fragmented_sentences(self, segments: List[Dict], ebook_full_text: str) -> List[Dict]:
        """
        Rejoins broken sentences in the transcript (e.g., [start, end, "Mr."], [start, end, "Smith"]).
        
        Strategy:
        1. Look at adjacent segments.
        2. If combining them creates a valid sentence/phrase found in the ebook text, merge them.
        3. Prioritize longer matches.
        
        Args:
            segments: List of dicts {'start': float, 'end': float, 'text': str}
            ebook_full_text: The source truth text to validate against.
            
        Returns:
            New list of merged segments.
        """
        if not segments:
            return []

        merged_segments = []
        current_segment = segments[0]

        for next_segment in segments[1:]:
            # Check gap
            gap = next_segment['start'] - current_segment['end']
            
            # Heuristic: If gap is small (< 1.5s) and combined text looks like a continuation
            if gap < 2.0: 
                combined_text = (current_segment['text'] + " " + next_segment['text']).strip()
                normalized_combo = self.normalize(combined_text)
                
                # Simple check: Does the first segment end with specific punctuation?
                # If it ends with . or ? or !, likely a real sentence end.
                if not re.search(r'[.?!"]$', current_segment['text'].strip()):
                    # Likely fragmented. Merge.
                    current_segment = {
                        'start': current_segment['start'],
                        'end': next_segment['end'],
                        'text': current_segment['text'] + " " + next_segment['text']
                    }
                    continue

            # If not merged, push current and move to next
            merged_segments.append(current_segment)
            current_segment = next_segment

        merged_segments.append(current_segment)
        return merged_segments
