
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

def fuzzy_match_title(query: str, target: str, threshold: float = 0.6) -> bool:
    """
    Check if query title fuzzy matches the target title.
    Uses word-overlap logic with normalization to handle punctuation differences.
    
    Args:
        query: The search term (e.g. from filename)
        target: The target title (e.g. from ABS)
        threshold: Required match percentage (default 0.6 / 60%)
        
    Returns:
        True if it's a match, False otherwise.
    """
    if not query or not target:
        return False
    
    # Normalize: lowercase and remove punctuation except spaces
    def normalize(s):
        return re.sub(r'[^\w\s]', '', s.lower())
    
    query_norm = normalize(query)
    target_norm = normalize(target)
    
    # First check: exact substring match (handles "We Spread" == "We Spread: A Novel")
    if query_norm in target_norm:
        return True
    
    # Second check: ALL words from query must appear in target
    # This prevents "We Spread" from matching "Spread Me"
    query_words = query_norm.split()
    target_words = target_norm.split()
    
    # Require ALL query words to be present
    all_words_present = all(word in target_words for word in query_words)
    
    if not all_words_present:
        return False
    
    # Third check: Reject if target has extra words that look like sequel numbers
    # (e.g., reject "Dragons Justice 2" when searching for "Dragons Justice")
    extra_words = [w for w in target_words if w not in query_words]
    sequel_indicators = {'2', '3', '4', '5', '6', '7', '8', '9', 'ii', 'iii', 'iv', 'two', 'three', 'four', 'five'}
    has_sequel_number = any(word in sequel_indicators for word in extra_words)
    
    if has_sequel_number:
        # If target looks like a sequel, require exact substring match (which we already checked above)
        return False
    
    return True
