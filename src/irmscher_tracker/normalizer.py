import re


def normalize_part_number(raw: str) -> str:
    """Normalize a part number by stripping spaces, lowercasing, removing prefix 'i'.
    
    Examples:
        'i 34 01 009' -> '3401009'
        'i3401009' -> '3401009'
        '3401009' -> '3401009'
        'I 34 01 009' -> '3401009'
    """
    cleaned = raw.strip().lower()
    cleaned = re.sub(r'\s+', '', cleaned)
    if cleaned.startswith('i'):
        cleaned = cleaned[1:]
    return cleaned

def extract_part_numbers(text: str) -> list[str]:
    """Extract potential Irmscher part numbers from text.
    
    Matches patterns like:
    - i 34 01 009
    - i3401009  
    - 3401009 (7-digit numbers starting with 34)
    """
    patterns = [
        r'i\s*34\s*\d{2}\s*\d{3}',  # i 34 XX XXX with optional spaces
        r'\b34\d{5}\b',              # 34XXXXX 7-digit
    ]
    found = []
    text_lower = text.lower()
    for pattern in patterns:
        matches = re.findall(pattern, text_lower)
        for m in matches:
            found.append(normalize_part_number(m))
    return list(set(found))
