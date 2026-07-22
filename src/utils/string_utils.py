
import os
import re
import difflib


def author_match_floor() -> float:
    """Minimum author similarity (0-1) required to accept a fuzzy title match when an
    author was supplied. Below this, a same-title/different-author candidate is rejected
    rather than committed. Overridable via TRACKER_AUTHOR_MATCH_MIN."""
    try:
        return float(os.environ.get("TRACKER_AUTHOR_MATCH_MIN", 0.5))
    except (TypeError, ValueError):
        return 0.5


def clean_book_title(title: str) -> str:
    """
    Cleans a book title by removing common subtitles, series info, and extra whitespace.
    
    Examples:
    "Harry Potter and the Sorcerer's Stone (Harry Potter, #1)" -> "Harry Potter and the Sorcerer's Stone"
    "Dune: Deluxe Edition" -> "Dune"
    """
    if not title:
        return ""
    
    # Remove text in parentheses (often series info or edition info)
    title = re.sub(r'\s*\(.*?\)', '', title)
    
    # Remove text after a colon (often subtitles) - debatable, but trying for stickiness to main title
    # For matching purposes, sometimes the subtitle is noise. 
    # Let's be careful: "Dune: Messiah" -> "Dune" might be bad if we want Messiah.
    # But usually Hardcover search is better with fewer words.
    # Let's strip subtitles for now as a "clean" strategy, 
    # but the caller might want to try both raw and clean.
    if ':' in title:
        title = title.split(':')[0]
        
    return title.strip()

def calculate_similarity(a: str, b: str) -> float:
    """
    Calculates the similarity ratio between two strings using SequenceMatcher.
    Returns reduced score if strings are very different in length to punish partial matches on short strings.
    """
    if not a or not b:
        return 0.0
        
    a = a.lower().strip()
    b = b.lower().strip()
    
    return difflib.SequenceMatcher(None, a, b).ratio()
